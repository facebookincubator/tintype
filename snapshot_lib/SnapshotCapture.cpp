// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#include "SnapshotCapture.h"

#include <Python.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <traceback.h>

#include <algorithm>
#include <chrono>
#include <deque>
#include <string>
#include <vector>

#include "compat.h"

#include "SnapshotWriter.h"

// Forward declarations for CPython internal functions used in free-threaded
// Python These are not in public headers but are exported by libpython
#if PY_VERSION_HEX >= 0x030D0000 && defined(Py_GIL_DISABLED)
extern "C" void _PyEval_StopTheWorld(PyInterpreterState* interp);
extern "C" void _PyEval_StartTheWorld(PyInterpreterState* interp);
#endif

namespace facebook::tintype::snapshot {

namespace {

/**
 * Extract local variables from a Python frame.
 * Calls PyFrame_GetLocals internally and appends local variable values to
 * processingQueue (for object graph traversal).
 * Returns a vector of (variable name, Python object ID) pairs.
 *
 * This function uses PyMapping_Keys/PyMapping_Values to support both:
 * - Python <3.13: locals is a dict
 * - Python >=3.13: locals is a PyFrameLocalsProxy_Type (PEP 667)
 */
std::vector<std::pair<std::string, uint64_t>> extractLocalVars(
    PyFrameObject* frame,
    ObjectQueue& processingQueue) {
  std::vector<std::pair<std::string, uint64_t>> localVars;

  if (frame == nullptr) {
    return localVars;
  }

  PyObject* locals = PyFrame_GetLocals(frame);
  if (locals == nullptr) {
    return localVars;
  }

  // Use PyMapping protocol to support both dict and PyFrameLocalsProxy_Type
  py::object locals_obj = py::reinterpret_steal<py::object>(locals);

  PyObject* keys = PyMapping_Keys(locals);
  if (keys == nullptr) {
    PyErr_Clear();
    SnapshotStatsCollector::getInstance().incrementErrors();
    return localVars;
  }
  py::object keys_obj = py::reinterpret_steal<py::object>(keys);

  PyObject* values = PyMapping_Values(locals_obj.ptr());
  if (values == nullptr) {
    PyErr_Clear();
    SnapshotStatsCollector::getInstance().incrementErrors();
    return localVars;
  }
  py::object values_obj = py::reinterpret_steal<py::object>(values);

  Py_ssize_t size = PyList_Size(keys);
  for (Py_ssize_t i = 0; i < size; ++i) {
    PyObject* key = PyList_GetItem(keys, i);
    PyObject* value = PyList_GetItem(values, i);

    if (key == nullptr || value == nullptr) {
      continue;
    }

    // Extract variable name as string
    if (!PyUnicode_Check(key)) {
      continue;
    }
    const char* keyStr = PyUnicode_AsUTF8(key);
    if (keyStr == nullptr) {
      PyErr_Clear();
      continue;
    }

    uint64_t valueId = reinterpret_cast<uint64_t>(value);
    localVars.emplace_back(keyStr, valueId);
    // Queue the value for object graph traversal.
    // py::reinterpret_borrow creates a py::object that borrows the reference,
    // but when pushed to processingQueue, the copy constructor INCREFs.
    // This ensures the object stays alive during serialization.
    // Note: In free-threaded Python (3.13t+), other threads may continue
    // executing and could mutate objects, but they won't be destroyed while
    // we hold this reference.
    processingQueue.emplace_back(py::reinterpret_borrow<py::object>(value), 0);
  }

  return localVars;
}

} // namespace

namespace py = pybind11;

// ============================================================================
// Helper Functions Implementation
// ============================================================================

uint64_t computeIdentityHash(
    uint64_t typePtr,
    uint64_t size,
    const uint64_t* childIds,
    size_t childCount) {
  // FNV-1a 64-bit hash
  constexpr uint64_t FNV_OFFSET_BASIS = 14695981039346656037ULL;
  constexpr uint64_t FNV_PRIME = 1099511628211ULL;

  uint64_t hash = FNV_OFFSET_BASIS;

  // Mix in type pointer
  hash ^= typePtr;
  hash *= FNV_PRIME;

  // Mix in size
  hash ^= size;
  hash *= FNV_PRIME;

  // Mix in child IDs (limit to first 32 for speed)
  size_t limit = std::min(childCount, static_cast<size_t>(32));
  for (size_t i = 0; i < limit; ++i) {
    hash ^= childIds[i];
    hash *= FNV_PRIME;
  }

  // If we have more than 32 children, also mix in some from the end
  // to catch appends to large collections
  if (childCount > 32) {
    size_t tailStart = childCount - 8;
    for (size_t i = tailStart; i < childCount; ++i) {
      hash ^= childIds[i];
      hash *= FNV_PRIME;
    }
  }

  return hash;
}

std::string getFullyQualifiedTypeName(PyTypeObject* type) {
  std::string typeName;

  // Get __qualname__ using C API (available in Python 3.11+)
  PyObject* qualname = PyType_GetQualName(type);
  if (qualname != nullptr) {
    const char* qualname_str = PyUnicode_AsUTF8(qualname);
    std::string qualName = qualname_str ? qualname_str : "";
    Py_DECREF(qualname);

    // Get module name using the most efficient available API
    std::string moduleName;
#if PY_VERSION_HEX >= 0x030D0000 // Python 3.13+
    // PyType_GetModuleName directly returns the module name string
    PyObject* module_name_obj = PyType_GetModuleName(type);
    if (module_name_obj != nullptr) {
      const char* module_str = PyUnicode_AsUTF8(module_name_obj);
      moduleName = module_str ? module_str : "";
      Py_DECREF(module_name_obj);
    } else {
      PyErr_Clear();
    }
#else
    // For Python < 3.13, try PyType_GetModule + PyModule_GetName first
    // PyType_GetModule works for heap types created with module state
    PyObject* module_obj = PyType_GetModule(type);
    if (module_obj != nullptr) {
      // PyModule_GetName returns a borrowed reference (const char*)
      const char* module_str = PyModule_GetName(module_obj);
      if (module_str != nullptr) {
        moduleName = module_str;
      } else {
        PyErr_Clear();
      }
      // Note: PyType_GetModule returns a borrowed reference, don't DECREF
    } else {
      PyErr_Clear();
      // Fallback to __module__ attribute for static types
      PyObject* module_attr = PyObject_GetAttrString(
          reinterpret_cast<PyObject*>(type), "__module__");
      if (module_attr != nullptr) {
        const char* module_str = PyUnicode_AsUTF8(module_attr);
        moduleName = module_str ? module_str : "";
        Py_DECREF(module_attr);
      } else {
        PyErr_Clear();
      }
    }
#endif

    if (moduleName == "builtins" || moduleName.empty()) {
      typeName = qualName;
    } else {
      typeName = moduleName + "." + qualName;
    }
  } else {
    PyErr_Clear();
    // Fallback to tp_name if PyType_GetQualName fails
    typeName = type->tp_name ? type->tp_name : "<unknown>";
  }

  return typeName;
}

void extractAttributes(
    py::handle obj,
    ObjectQueue& processingQueue,
    std::vector<std::pair<uint64_t, uint64_t>>& attrIds,
    uint32_t childDepth) {
  PyObject* objPtr = obj.ptr();

  // First, try to get instance __dict__ directly - this is where most
  // instance attributes are stored and we can iterate it without getattr
  {
    auto timer = SnapshotStatsCollector::getInstance().attrAccessTimer();
    PyObject* obj_dict = PyObject_GenericGetDict(objPtr, nullptr);
    if (obj_dict != nullptr) {
      // Iterate the dict directly - no getattr calls needed!
      PyObject* key;
      PyObject* value;
      Py_ssize_t pos = 0;
      while (PyDict_Next(obj_dict, &pos, &key, &value)) {
        // Skip non-string keys (shouldn't happen but be safe)
        if (!PyUnicode_Check(key)) {
          continue;
        }

        // Skip dunders
        Py_ssize_t key_len;
        const char* key_str = PyUnicode_AsUTF8AndSize(key, &key_len);
        if (key_str != nullptr && key_len >= 2 && key_str[0] == '_' &&
            key_str[1] == '_') {
          continue;
        }

        // Get IDs directly from the dict entries (no getattr needed!)
        uint64_t nameId = reinterpret_cast<uint64_t>(key);
        uint64_t valueId = reinterpret_cast<uint64_t>(value);
        attrIds.emplace_back(nameId, valueId);

        // Queue the attribute name and value for processing
        processingQueue.emplace_back(
            py::reinterpret_borrow<py::object>(key), childDepth);
        processingQueue.emplace_back(
            py::reinterpret_borrow<py::object>(value), childDepth);
      }
      Py_DECREF(obj_dict);
    } else {
      // No __dict__, clear the error
      PyErr_Clear();
    }
  }

  // Now handle __slots__ - iterate through the type's MRO to find all slots
  PyObject* mro = reinterpret_cast<PyObject*>(Py_TYPE(objPtr)->tp_mro);
  {
    auto timer = SnapshotStatsCollector::getInstance().slotsTimer();
    if (mro != nullptr && PyTuple_Check(mro)) {
      Py_ssize_t mro_len = PyTuple_Size(mro);
      for (Py_ssize_t i = 0; i < mro_len; ++i) {
        PyObject* base = PyTuple_GET_ITEM(mro, i);
        // Get __slots__ from this base class
        PyObject* slots = PyObject_GetAttrString(base, "__slots__");
        if (slots == nullptr) {
          PyErr_Clear();
          continue;
        }

        // __slots__ can be a string, tuple, or list
        py::object slots_obj = py::reinterpret_steal<py::object>(slots);

        // Handle if __slots__ is a single string
        if (PyUnicode_Check(slots)) {
          Py_ssize_t name_len;
          const char* name_str = PyUnicode_AsUTF8AndSize(slots, &name_len);
          if (name_str != nullptr && name_len >= 2 && name_str[0] == '_' &&
              name_str[1] == '_') {
            continue; // Skip dunders
          }
          // Get the slot value
          PyObject* slot_value = PyObject_GetAttr(objPtr, slots);
          if (slot_value != nullptr) {
            uint64_t nameId = reinterpret_cast<uint64_t>(slots);
            uint64_t valueId = reinterpret_cast<uint64_t>(slot_value);
            attrIds.emplace_back(nameId, valueId);

            processingQueue.emplace_back(
                py::reinterpret_borrow<py::object>(slots), childDepth);
            processingQueue.emplace_back(
                py::reinterpret_steal<py::object>(slot_value), childDepth);
          } else {
            PyErr_Clear();
          }
        } else {
          // Iterate over __slots__ (tuple or list)
          PyObject* iter = PyObject_GetIter(slots);
          if (iter != nullptr) {
            PyObject* slot_name;
            while ((slot_name = PyIter_Next(iter)) != nullptr) {
              if (PyUnicode_Check(slot_name)) {
                Py_ssize_t name_len;
                const char* name_str =
                    PyUnicode_AsUTF8AndSize(slot_name, &name_len);
                if (name_str != nullptr && name_len >= 2 &&
                    name_str[0] == '_' && name_str[1] == '_') {
                  Py_DECREF(slot_name);
                  continue; // Skip dunders
                }

                // Get the slot value using getattr (slots require this)
                PyObject* slot_value = PyObject_GetAttr(objPtr, slot_name);
                if (slot_value != nullptr) {
                  uint64_t nameId = reinterpret_cast<uint64_t>(slot_name);
                  uint64_t valueId = reinterpret_cast<uint64_t>(slot_value);
                  attrIds.emplace_back(nameId, valueId);

                  processingQueue.emplace_back(
                      py::reinterpret_steal<py::object>(slot_name), childDepth);
                  processingQueue.emplace_back(
                      py::reinterpret_steal<py::object>(slot_value),
                      childDepth);
                  continue; // Don't decref slot_name, it was stolen
                } else {
                  PyErr_Clear();
                }
              }
              Py_DECREF(slot_name);
            }
            Py_DECREF(iter);
          } else {
            PyErr_Clear();
          }
        }
      }
    }
  }

  // Now extract class members from the type hierarchy (MRO)
  // These are attributes defined on the class itself, not on instances
  // We need to track seen names to avoid duplicates from __dict__ and __slots__
  {
    auto timer = SnapshotStatsCollector::getInstance().classMembersTimer();

    // Build a set of already-seen attribute names to avoid duplicates
    FastSet<uint64_t> seenNameIds;
    for (const auto& [nameId, _] : attrIds) {
      seenNameIds.insert(nameId);
    }

    if (mro != nullptr && PyTuple_Check(mro)) {
      Py_ssize_t mro_len = PyTuple_Size(mro);
      for (Py_ssize_t i = 0; i < mro_len; ++i) {
        PyObject* base = PyTuple_GET_ITEM(mro, i);

        // Skip object and type base classes - they only have builtins
        if (base == reinterpret_cast<PyObject*>(&PyBaseObject_Type) ||
            base == reinterpret_cast<PyObject*>(&PyType_Type)) {
          continue;
        }

        // Get the class __dict__ (tp_dict)
        PyObject* class_dict = reinterpret_cast<PyTypeObject*>(base)->tp_dict;
        if (class_dict == nullptr || !PyDict_Check(class_dict)) {
          continue;
        }

        PyObject* key;
        PyObject* value;
        Py_ssize_t pos = 0;
        while (PyDict_Next(class_dict, &pos, &key, &value)) {
          // Skip non-string keys
          if (!PyUnicode_Check(key)) {
            continue;
          }

          // Skip dunders
          Py_ssize_t key_len;
          const char* key_str = PyUnicode_AsUTF8AndSize(key, &key_len);
          if (key_str != nullptr && key_len >= 2 && key_str[0] == '_' &&
              key_str[1] == '_') {
            continue;
          }

          // Skip if we've already seen this name
          if (seenNameIds.count(reinterpret_cast<uint64_t>(key)) > 0) {
            continue;
          }

          // Skip callable objects (methods, functions)
          if (PyCallable_Check(value)) {
            continue;
          }

          // Skip descriptors (property, classmethod, staticmethod, etc.)
          // These have __get__, __set__, or __delete__ methods
          PyTypeObject* value_type = Py_TYPE(value);
          if (value_type->tp_descr_get != nullptr ||
              value_type->tp_descr_set != nullptr) {
            continue;
          }

          // This is a class data member - get its value via getattr on instance
          // (in case it's shadowed or has special behavior)
          PyObject* attr_value = PyObject_GetAttr(objPtr, key);
          if (attr_value == nullptr) {
            PyErr_Clear();
            continue;
          }

          // Skip if the value is callable (could be a bound method)
          if (PyCallable_Check(attr_value)) {
            Py_DECREF(attr_value);
            continue;
          }

          uint64_t nameId = reinterpret_cast<uint64_t>(key);
          uint64_t valueId = reinterpret_cast<uint64_t>(attr_value);
          attrIds.emplace_back(nameId, valueId);
          seenNameIds.insert(nameId);

          processingQueue.emplace_back(
              py::reinterpret_borrow<py::object>(key), childDepth);
          processingQueue.emplace_back(
              py::reinterpret_steal<py::object>(attr_value), childDepth);
        }
      }
    }
  }
}

std::string dictToJson(const py::object& obj) {
  // Import Python's json module and use dumps()
  py::module_ json = py::module_::import("json");
  py::object dumps = json.attr("dumps");
  py::object result = dumps(obj);
  return result.cast<std::string>();
}

// ============================================================================
// SnapshotCapture Implementation
// ============================================================================

SnapshotCapture& SnapshotCapture::getInstance() {
  static SnapshotCapture instance;
  return instance;
}

void SnapshotCapture::initialize(
    bool collectStats,
    const std::vector<std::string>& frameFilePathFilters) {
  frameFilePathFilters_ = frameFilePathFilters;
  sysModule_ = py::module_::import("sys");
  threadingModule_ = py::module_::import("threading");
  SnapshotWriter::getInstance().initialize(collectStats);
}

bool SnapshotCapture::shouldFilterFrame(const std::string& filename) const {
  for (const auto& filter : frameFilePathFilters_) {
    if (filename.find(filter) != std::string::npos) {
      SnapshotStatsCollector::getInstance().incrementFramesFiltered();
      return true;
    }
  }
  return false;
}

FastMap<uint64_t, std::string> SnapshotCapture::buildThreadNameMap() {
  FastMap<uint64_t, std::string> threadNameMap;

  try {
    py::object enumerate = threadingModule_.attr("enumerate")();

    for (auto thread : enumerate) {
      py::object identAttr = thread.attr("ident");
      if (identAttr.is_none()) {
        continue;
      }
      uint64_t ident = identAttr.cast<uint64_t>();
      py::object nameAttr = thread.attr("name");
      if (!nameAttr.is_none()) {
        std::string name = nameAttr.cast<std::string>();
        threadNameMap[ident] = std::move(name);
      }
    }
  } catch (const py::error_already_set&) {
    PyErr_Clear();
  }

  return threadNameMap;
}

std::string SnapshotCapture::getCurrentThreadName() {
  try {
    py::object currentThread = threadingModule_.attr("current_thread")();
    py::object nameAttr = currentThread.attr("name");
    if (!nameAttr.is_none()) {
      return nameAttr.cast<std::string>();
    }
  } catch (const py::error_already_set&) {
    PyErr_Clear();
  }
  return "";
}

bool SnapshotCapture::isInitialized() const {
  return SnapshotWriter::getInstance().isInitialized();
}

void SnapshotCapture::requestCancel() {
  cancelRequested_.store(true, std::memory_order_release);
}

void SnapshotCapture::clearCancel() {
  cancelRequested_.store(false, std::memory_order_release);
}

bool SnapshotCapture::isCancelRequested() const {
  return cancelRequested_.load(std::memory_order_acquire);
}

void SnapshotCapture::startTimeoutTimer(double timeoutSeconds) {
  std::lock_guard<std::mutex> lock(timeoutMutex_);

  // If there's an existing timer thread that's still joinable, it means
  // a previous snapshot didn't clean up properly. This shouldn't happen
  // because snapshotInProgress_ should prevent concurrent snapshots.
  // But if it does, just skip starting a new timer - the existing one
  // will still fire eventually.
  if (timeoutThread_ && timeoutThread_->joinable()) {
    return;
  }

  timeoutShouldStop_ = false;
  timeoutThread_ = std::make_unique<std::thread>([this, timeoutSeconds]() {
    std::unique_lock<std::mutex> lock(timeoutMutex_);
    auto duration = std::chrono::duration<double>(timeoutSeconds);
    if (!timeoutCv_.wait_for(
            lock, duration, [this] { return timeoutShouldStop_; })) {
      requestCancel();
    }
  });
}

void SnapshotCapture::stopTimeoutTimer() {
  {
    std::lock_guard<std::mutex> lock(timeoutMutex_);
    if (!timeoutThread_) {
      return;
    }
    timeoutShouldStop_ = true;
  }
  timeoutCv_.notify_one();
  timeoutThread_->join();
  timeoutThread_.reset();
}

void SnapshotCapture::addReferencedFile(const std::string& filePath) {
  SnapshotWriter::getInstance().addReferencedFile(filePath);
}

void SnapshotCapture::finalize(
    const std::string& path,
    const std::string& metadataJson,
    int compressionLevel) {
  // Release ownership of the cached sys module without decrementing its
  // reference count. The sys module is immortal anyway, and this avoids
  // the destructor trying to call Py_DECREF during Python finalization
  // (when the GIL is no longer valid).
  sysModule_.release();
  // Release the cached threading module for the same reason.
  threadingModule_.release();
  SnapshotWriter::getInstance().finalize(path, metadataJson, compressionLevel);
}

py::dict SnapshotCapture::getStats() const {
  auto stats = SnapshotWriter::getInstance().getStats();

  py::dict result;
  result["initialize_time_ms"] = stats.initializeTimeNs / 1000000.0;
  result["finalize_time_ms"] = stats.finalizeTimeNs / 1000000.0;
  result["total_snapshot_time_ms"] = stats.totalSnapshotTimeNs / 1000000.0;
  result["snapshot_count"] = stats.snapshotCount;
  result["total_objects"] = stats.totalObjects();

  // Granular finalize breakdown
  py::dict finalize_breakdown;
  finalize_breakdown["file_table_time_ms"] =
      stats.finalizeFileTableTimeNs / 1000000.0;
  finalize_breakdown["environment_time_ms"] =
      stats.finalizeEnvironmentTimeNs / 1000000.0;
  finalize_breakdown["manifest_time_ms"] =
      stats.finalizeManifestTimeNs / 1000000.0;
  finalize_breakdown["metadata_time_ms"] =
      stats.finalizeMetadataTimeNs / 1000000.0;
  finalize_breakdown["msync_time_ms"] = stats.finalizeMsyncTimeNs / 1000000.0;
  finalize_breakdown["compression_time_ms"] =
      stats.finalizeCompressionTimeNs / 1000000.0;
  finalize_breakdown["output_file_time_ms"] =
      stats.finalizeOutputFileTimeNs / 1000000.0;
  finalize_breakdown["cleanup_time_ms"] =
      stats.finalizeCleanupTimeNs / 1000000.0;
  finalize_breakdown["uncompressed_data_size"] =
      stats.finalizeUncompressedDataSize;
  finalize_breakdown["compressed_data_size"] = stats.finalizeCompressedDataSize;
  finalize_breakdown["file_count"] = stats.finalizeFileCount;
  result["finalize_breakdown"] = finalize_breakdown;

  // Granular snapshot breakdown
  py::dict snapshot_breakdown;
  snapshot_breakdown["write_frame_record_time_ms"] =
      stats.writeFrameRecordTimeNs / 1000000.0;
  snapshot_breakdown["total_frame_count"] = stats.totalFrameCount;
  snapshot_breakdown["frames_filtered"] = stats.framesFiltered;
  snapshot_breakdown["total_objects_processed"] = stats.totalObjectsProcessed;
  snapshot_breakdown["snapshots_discarded"] = stats.snapshotsDiscarded;
  result["snapshot_breakdown"] = snapshot_breakdown;

  // Object queue breakdown
  py::dict object_queue_breakdown;
  object_queue_breakdown["object_lookup_time_ms"] =
      stats.objectLookupTimeNs / 1000000.0;
  object_queue_breakdown["object_processing_time_ms"] =
      stats.objectProcessingTimeNs / 1000000.0;
  object_queue_breakdown["repr_time_ms"] = stats.reprTimeNs / 1000000.0;
  object_queue_breakdown["slots_time_ms"] = stats.slotsTimeNs / 1000000.0;
  object_queue_breakdown["attr_access_time_ms"] =
      stats.attrAccessTimeNs / 1000000.0;
  object_queue_breakdown["class_members_time_ms"] =
      stats.classMembersTimeNs / 1000000.0;
  object_queue_breakdown["serialization_time_ms"] =
      stats.totalSerializationTimeNs() / 1000000.0;
  object_queue_breakdown["objects_skipped"] = stats.objectsSkipped;
  object_queue_breakdown["objects_cache_hit"] = stats.objectsCacheHit;
  object_queue_breakdown["string_bytes_cache_hit"] = stats.stringBytesCacheHit;
  result["object_queue_breakdown"] = object_queue_breakdown;

  // Errors (across all subsystems)
  result["errors"] = stats.errors;

  // File extension statistics
  py::dict file_extension;
  file_extension["time_ms"] = stats.fileExtensionTimeNs / 1000000.0;
  file_extension["count"] = stats.fileExtensionCount;
  file_extension["bytes"] = stats.fileExtensionBytes;
  result["file_extension"] = file_extension;

  // Per-type statistics
  py::dict object_stats;
  static const char* typeNames[] = {
      "Int64",
      "Float",
      "String",
      "Bytes",
      "List",
      "Tuple",
      "Dict",
      "Set",
      "IntBignum",
      "SerializedObject",
      "SerializedList",
      "SerializedSet",
      "SerializedTuple",
      "SerializedDict",
  };

  for (size_t i = 0; i < kNumObjectTypes; ++i) {
    const auto& typeStats = stats.objectStats[i];
    if (typeStats.count > 0) {
      py::dict typeDict;
      typeDict["count"] = typeStats.count;
      typeDict["total_time_ms"] = typeStats.totalTimeNs / 1000000.0;
      typeDict["avg_time_us"] = typeStats.averageTimeUs();
      typeDict["total_bytes"] = typeStats.totalBytes;
      typeDict["avg_bytes"] = typeStats.averageBytesPerObject();
      object_stats[typeNames[i]] = typeDict;
    }
  }
  result["object_stats"] = object_stats;

  return result;
}

void SnapshotCapture::resetStats() {
  SnapshotWriter::getInstance().resetStats();
}

bool SnapshotCapture::processFrameObjects(
    ObjectQueue& processingQueue,
    std::optional<uint32_t> maxObjectDepth,
    bool& objectDepthHit) {
  auto& statsCollector = SnapshotStatsCollector::getInstance();
  while (!processingQueue.empty()) {
    auto [obj, depth] = processingQueue.front();
    processingQueue.pop_front();

    processObject(obj, processingQueue, depth, maxObjectDepth, objectDepthHit);
    statsCollector.incrementObjectsProcessed();

    if (isCancelRequested()) {
      return false;
    }
  }
  return true;
}

void SnapshotCapture::writeTracebackFrames(
    PyObject* traceback,
    bool& wasCancelled,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    bool* objectDepthHit) {
  SnapshotWriter& writer = SnapshotWriter::getInstance();
  auto& statsCollector = SnapshotStatsCollector::getInstance();
  ObjectQueue processingQueue;
  wasCancelled = false;

  // Collect traceback frame metadata first, since tb_next walks
  // outermost→innermost but we want to process innermost-first.
  struct TbFrameMetadata {
    std::string filename;
    std::string funcname;
    std::string qualname;
    uint32_t lineno;
    PyFrameObject* frame; // borrowed reference (owned by traceback)
  };
  std::vector<TbFrameMetadata> collectedFrames;

  PyTracebackObject* tb = reinterpret_cast<PyTracebackObject*>(traceback);
  while (tb != nullptr) {
    PyFrameObject* frame = tb->tb_frame;
    PyCodeObject* code = PyFrame_GetCode(frame);

    int lineno = tb->tb_lineno;
    if (lineno < 0) {
      lineno = PyCode_Addr2Line(code, tb->tb_lasti);
    }

    PyObject* py_filename = code->co_filename;
    const char* filename_cstr = PyUnicode_AsUTF8(py_filename);
    std::string filename = filename_cstr ? filename_cstr : "";

    PyObject* py_funcname = code->co_name;
    const char* funcname_cstr = PyUnicode_AsUTF8(py_funcname);
    std::string funcname = funcname_cstr ? funcname_cstr : "";

#if PY_VERSION_HEX >= 0x030B0000
    PyObject* py_qualname = code->co_qualname;
    const char* qualname_cstr = PyUnicode_AsUTF8(py_qualname);
    std::string qualname = qualname_cstr ? qualname_cstr : funcname;
#else
    std::string qualname = funcname;
#endif

    Py_DECREF(code);

    collectedFrames.push_back(
        TbFrameMetadata{
            std::move(filename),
            std::move(funcname),
            std::move(qualname),
            static_cast<uint32_t>(lineno),
            frame});

    tb = tb->tb_next;
  }

  // Reverse to get innermost-first ordering
  std::reverse(collectedFrames.begin(), collectedFrames.end());

  // Process each frame: get locals, write record, process objects inline
  for (auto& fd : collectedFrames) {
    if (isCancelRequested()) {
      wasCancelled = true;
      break;
    }

    if (maxFrames.has_value() &&
        writer.getCurrentFrameCount() >= maxFrames.value()) {
      wasCancelled = true;
      break;
    }

    // Skip frames matching file path filters (not flagged as truncation)
    if (shouldFilterFrame(fd.filename)) {
      continue;
    }

    addReferencedFile(fd.filename);

    {
      auto localVars = extractLocalVars(fd.frame, processingQueue);

      size_t savedWriteOffset = writer.getWriteOffset();

      {
        auto writeTimer = statsCollector.writeFrameRecordTimer();
        writer.writeFrameRecord(
            fd.filename, fd.funcname, fd.qualname, fd.lineno, localVars);
      }

      bool localObjectDepthHit = false;
      bool completed = processFrameObjects(
          processingQueue, maxObjectDepth, localObjectDepthHit);
      if (localObjectDepthHit && objectDepthHit != nullptr) {
        *objectDepthHit = true;
      }

      if (!completed) {
        writer.rollbackWriteOffset(savedWriteOffset);
        wasCancelled = true;
        break;
      }

      statsCollector.incrementFrameCount();
    }
  }
}

bool SnapshotCapture::takeSnapshot(
    PyObject* traceback,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    std::optional<double> timeoutSeconds,
    uint32_t skipFrames) {
  // Claim the snapshot-in-progress flag. If another snapshot (takeSnapshot or
  // snapshotAllThreads) is already running, return false immediately.
  bool expected = false;
  if (!snapshotInProgress_.compare_exchange_strong(
          expected, true, std::memory_order_acq_rel)) {
    return false;
  }

  SnapshotWriter& writer = SnapshotWriter::getInstance();

  if (!writer.isInitialized()) {
    snapshotInProgress_.store(false, std::memory_order_release);
    throw std::runtime_error("snapshot module not initialized");
  }

  // Start timeout timer if requested
  if (timeoutSeconds.has_value()) {
    startTimeoutTimer(timeoutSeconds.value());
  }

  // Begin the snapshot record
  writer.beginSnapshot();

  // Begin the stacktrace record (using current thread ID and name)
  uint64_t threadId = static_cast<uint64_t>(PyThread_get_thread_ident());
  std::string threadName = getCurrentThreadName();
  writer.beginStacktrace(threadId, threadName);

  bool stacktraceTruncated = false;
  bool objectDepthHit = false;

  if (traceback != nullptr) {
    // Validate that the object is a traceback
    if (!PyTraceBack_Check(traceback)) {
      writer.endStacktrace();
      writer.endSnapshot();
      stopTimeoutTimer();
      clearCancel();
      snapshotInProgress_.store(false, std::memory_order_release);
      throw std::runtime_error("Expected a traceback object");
    }

    bool wasCancelled = false;
    writeTracebackFrames(
        traceback, wasCancelled, maxFrames, maxObjectDepth, &objectDepthHit);
    stacktraceTruncated = wasCancelled;
  } else {
    // Default behavior: walk the current thread's frame stack
    stacktraceTruncated = !writeCurrentThreadFrames(
        skipFrames, maxFrames, maxObjectDepth, &objectDepthHit);
  }

  // Handle stacktrace completion
  bool snapshotTruncated = false;
  if (writer.getCurrentFrameCount() == 0) {
    writer.discardCurrentStacktrace();
    snapshotTruncated = true;
  } else {
    writer.endStacktrace(stacktraceTruncated, objectDepthHit);
    if (stacktraceTruncated) {
      snapshotTruncated = true;
    }
  }

  // Handle snapshot completion
  bool snapshotWritten = false;
  if (writer.getCurrentStacktraceCount() == 0) {
    writer.discardCurrentSnapshot();
  } else {
    writer.endSnapshot(snapshotTruncated);
    snapshotWritten = true;
  }

  stopTimeoutTimer();
  clearCancel();
  snapshotInProgress_.store(false, std::memory_order_release);
  return snapshotWritten;
}

bool SnapshotCapture::takeSnapshotFromException(
    PyObject* exception,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    std::optional<double> timeoutSeconds) {
  bool expected = false;
  if (!snapshotInProgress_.compare_exchange_strong(
          expected, true, std::memory_order_acq_rel)) {
    return false;
  }

  SnapshotWriter& writer = SnapshotWriter::getInstance();

  if (!writer.isInitialized()) {
    snapshotInProgress_.store(false, std::memory_order_release);
    throw std::runtime_error("snapshot module not initialized");
  }

  if (!PyExceptionInstance_Check(exception)) {
    snapshotInProgress_.store(false, std::memory_order_release);
    throw std::runtime_error("Expected a BaseException object");
  }

  // Start timeout timer if requested
  if (timeoutSeconds.has_value()) {
    startTimeoutTimer(timeoutSeconds.value());
  }

  // Collect the exception chain into a vector.
  std::vector<PyObject*> exceptionChain;
  FastSet<PyObject*> seenExceptions;

  PyObject* current = exception;
  while (current != nullptr && seenExceptions.count(current) == 0) {
    exceptionChain.push_back(current);
    seenExceptions.insert(current);

    PyObject* cause = PyException_GetCause(current);
    if (cause != nullptr && cause != Py_None) {
      Py_DECREF(cause);
      current = cause;
      continue;
    }
    if (cause != nullptr) {
      Py_DECREF(cause);
    }

    PyObject* context = PyException_GetContext(current);
    if (context != nullptr && context != Py_None) {
      PyObject* suppress =
          PyObject_GetAttrString(current, "__suppress_context__");
      bool suppressed = false;
      if (suppress != nullptr) {
        suppressed = PyObject_IsTrue(suppress);
        Py_DECREF(suppress);
      } else {
        PyErr_Clear();
      }

      if (!suppressed) {
        Py_DECREF(context);
        current = context;
        continue;
      }
    }
    if (context != nullptr) {
      Py_DECREF(context);
    }

    break;
  }

  // Assign unique stacktrace IDs
  FastMap<PyObject*, uint64_t> excToStacktraceId;
  for (size_t i = 0; i < exceptionChain.size(); ++i) {
    excToStacktraceId[exceptionChain[i]] = static_cast<uint64_t>(i + 1);
  }

  // Begin the snapshot record
  writer.beginSnapshot();

  bool anyStacktraceOmitted = false;
  bool anyStacktraceTruncated = false;

  // Write a stacktrace for each exception in the chain
  for (size_t i = 0; i < exceptionChain.size(); ++i) {
    // Check cancel before starting the next exception's stacktrace
    if (isCancelRequested()) {
      anyStacktraceOmitted = true;
      break;
    }

    PyObject* exc = exceptionChain[i];
    uint64_t stacktraceId = excToStacktraceId[exc];

    // Serialize the exception object to the object heap
    bool excObjectDepthHit = false;
    py::handle excHandle(exc);
    ObjectQueue excQueue;
    processObject(excHandle, excQueue, 0, maxObjectDepth, excObjectDepthHit);
    SnapshotStatsCollector::getInstance().incrementObjectsProcessed();

    // Process exception's object graph inline
    processFrameObjects(excQueue, maxObjectDepth, excObjectDepthHit);

    // If cancelled during exception object processing, skip this stacktrace
    if (isCancelRequested()) {
      anyStacktraceOmitted = true;
      break;
    }

    uint64_t exceptionPythonId = reinterpret_cast<uint64_t>(exc);

    // Determine causeId
    uint64_t causeId = stacktraceId; // self-reference = none
    PyObject* cause = PyException_GetCause(exc);
    if (cause != nullptr && cause != Py_None) {
      auto it = excToStacktraceId.find(cause);
      if (it != excToStacktraceId.end()) {
        causeId = it->second;
      }
    }
    if (cause != nullptr) {
      Py_DECREF(cause);
    }

    // Determine contextId
    uint64_t contextId = stacktraceId; // self-reference = none
    PyObject* context = PyException_GetContext(exc);
    if (context != nullptr && context != Py_None) {
      auto it = excToStacktraceId.find(context);
      if (it != excToStacktraceId.end()) {
        contextId = it->second;
      }
    }
    if (context != nullptr) {
      Py_DECREF(context);
    }

    // Begin the stacktrace record
    writer.beginStacktrace(
        stacktraceId, "", exceptionPythonId, causeId, contextId);

    // Get the traceback and write its frames with inline object processing
    PyObject* tb = PyException_GetTraceback(exc);
    bool wasCancelled = false;
    if (tb != nullptr && PyTraceBack_Check(tb)) {
      writeTracebackFrames(
          tb, wasCancelled, maxFrames, maxObjectDepth, &excObjectDepthHit);
      Py_DECREF(tb);
    } else if (tb != nullptr) {
      Py_DECREF(tb);
    }

    // Handle stacktrace completion
    if (writer.getCurrentFrameCount() == 0) {
      writer.discardCurrentStacktrace();
      anyStacktraceOmitted = true;
    } else {
      writer.endStacktrace(wasCancelled, excObjectDepthHit);
      if (wasCancelled) {
        anyStacktraceTruncated = true;
      }
    }

    // If cancelled, stop processing further exceptions
    if (wasCancelled) {
      anyStacktraceOmitted = true;
      break;
    }
  }

  // Handle snapshot completion
  bool snapshotTruncated = anyStacktraceTruncated || anyStacktraceOmitted;
  bool snapshotWritten = false;
  if (writer.getCurrentStacktraceCount() == 0) {
    writer.discardCurrentSnapshot();
  } else {
    writer.endSnapshot(snapshotTruncated);
    snapshotWritten = true;
  }

  stopTimeoutTimer();
  clearCancel();
  snapshotInProgress_.store(false, std::memory_order_release);
  return snapshotWritten;
}

uint64_t SnapshotCapture::processObject(
    py::handle obj,
    ObjectQueue& processingQueue,
    uint32_t currentDepth,
    std::optional<uint32_t> maxObjectDepth,
    bool& objectDepthHit) {
  // Cache the writer reference to avoid repeated getInstance() calls
  SnapshotWriter& writer = SnapshotWriter::getInstance();
  auto& statsCollector = SnapshotStatsCollector::getInstance();

  // Get Python id directly from memory address (id() just returns the address)
  uint64_t pythonId = reinterpret_cast<uint64_t>(obj.ptr());

  // Check if we've already processed this object (single lock acquisition)
  {
    auto timer = statsCollector.objectLookupTimer();
    uint64_t existingOffset;
    if (writer.tryLookupObject(pythonId, existingOffset)) {
      statsCollector.incrementObjectsSkipped();
      return existingOffset;
    }
  }

  uint64_t offset = 0;

  // Time the entire object processing (excluding dedup lookup above)
  auto processingTimer = statsCollector.objectProcessingTimer();

  // Get raw pointer once for faster type checks
  PyObject* objPtr = obj.ptr();

  // Handle None - use magic offset instead of writing to heap
  if (objPtr == Py_None) {
    writer.registerObject(pythonId, SnapshotWriter::kNoneOffset);
    return SnapshotWriter::kNoneOffset;
  }

  // Handle bool (must check before int since bool is a subclass of int)
  // Use magic offsets instead of writing to heap
  if (PyBool_Check(objPtr)) {
    bool value = (objPtr == Py_True);
    uint64_t boolOffset =
        value ? SnapshotWriter::kTrueOffset : SnapshotWriter::kFalseOffset;
    writer.registerObject(pythonId, boolOffset);
    return boolOffset;
  }

  // Handle int (exact type only - subclasses fall through to SerializedObject)
  if (PyLong_CheckExact(objPtr)) {
    // Try to extract as int64 directly using PyLong_AsLongLongAndOverflow
    int overflow = 0;
    long long value = PyLong_AsLongLongAndOverflow(objPtr, &overflow);

    if (overflow == 0 && !PyErr_Occurred()) {
      // Value fits in int64 - write directly without string conversion
      offset = writer.writeInt64(static_cast<int64_t>(value));
      writer.registerObject(pythonId, offset);
      return offset;
    }

    // Value overflows int64 - fall back to string conversion
    PyErr_Clear();
    PyObject* str_obj = PyObject_Str(objPtr);
    if (str_obj == nullptr) {
      PyErr_Clear();
      statsCollector.incrementErrors();
      return 0;
    }
    const char* str_ptr = PyUnicode_AsUTF8(str_obj);
    std::string valueStr = str_ptr ? str_ptr : "";
    Py_DECREF(str_obj);
    offset = writer.writeInt(valueStr);
    writer.registerObject(pythonId, offset);
    return offset;
  }

  // Handle float (exact type only - subclasses fall through to
  // SerializedObject)
  if (PyFloat_CheckExact(objPtr)) {
    double value = PyFloat_AS_DOUBLE(objPtr);
    offset = writer.writeFloat(value);
    writer.registerObject(pythonId, offset);
    return offset;
  }

  // Handle str (exact type only - subclasses fall through to SerializedObject)
  if (PyUnicode_CheckExact(objPtr)) {
    Py_ssize_t size;
    const char* str = PyUnicode_AsUTF8AndSize(objPtr, &size);
    if (str == nullptr) {
      PyErr_Clear();
      statsCollector.incrementErrors();
      return 0;
    }

    // Use PyObject_Hash for cross-snapshot dedup. Python caches the hash
    // on the str object itself, so subsequent calls are O(1). Mix in the
    // type pointer to distinguish str from bytes at the same address.
    constexpr uint64_t FNV_PRIME = 1099511628211ULL;
    Py_hash_t pyHash = PyObject_Hash(objPtr);
    if (pyHash == -1 && PyErr_Occurred()) {
      PyErr_Clear();
      // Fall through to non-cached write
      std::string value(str, static_cast<size_t>(size));
      offset = writer.writeString(value);
      writer.registerObject(pythonId, offset);
      return offset;
    }
    uint64_t identityHash = static_cast<uint64_t>(pyHash) ^
        (reinterpret_cast<uint64_t>(Py_TYPE(objPtr)) * FNV_PRIME);

    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      statsCollector.incrementStringBytesCacheHit();
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    std::string value(str, static_cast<size_t>(size));
    offset = writer.writeString(value);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle bytes (exact type only - subclasses fall through to
  // SerializedObject)
  if (PyBytes_CheckExact(objPtr)) {
    char* buffer;
    Py_ssize_t length;
    if (PyBytes_AsStringAndSize(objPtr, &buffer, &length) == -1) {
      PyErr_Clear();
      statsCollector.incrementErrors();
      return 0;
    }

    // Use PyObject_Hash for cross-snapshot dedup (same pattern as str above)
    constexpr uint64_t FNV_PRIME_BYTES = 1099511628211ULL;
    Py_hash_t pyHash = PyObject_Hash(objPtr);
    if (pyHash == -1 && PyErr_Occurred()) {
      PyErr_Clear();
      std::string value(buffer, static_cast<size_t>(length));
      offset = writer.writeBytes(value);
      writer.registerObject(pythonId, offset);
      return offset;
    }
    uint64_t identityHash = static_cast<uint64_t>(pyHash) ^
        (reinterpret_cast<uint64_t>(Py_TYPE(objPtr)) * FNV_PRIME_BYTES);

    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      statsCollector.incrementStringBytesCacheHit();
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    std::string value(buffer, static_cast<size_t>(length));
    offset = writer.writeBytes(value);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Depth limit check: if we've reached maxObjectDepth, serialize as repr
  // instead of recursing into children. Primitives (None, bool, int, float,
  // str, bytes) are already handled above as leaf types.
  if (maxObjectDepth.has_value() && currentDepth >= maxObjectDepth.value()) {
    objectDepthHit = true;
    PyTypeObject* type = Py_TYPE(objPtr);
    std::string typeName = getFullyQualifiedTypeName(type);
    std::string repr;
    {
      auto reprTimer = statsCollector.reprTimer();
      PyObject* repr_obj = PyObject_Repr(objPtr);
      if (repr_obj != nullptr) {
        const char* repr_str = PyUnicode_AsUTF8(repr_obj);
        repr = repr_str ? repr_str : "";
        Py_DECREF(repr_obj);
      } else {
        PyErr_Clear();
        repr = "<repr failed>";
      }
    }
    std::vector<std::pair<uint64_t, uint64_t>> emptyAttrs;
    offset = writer.writeSerializedObject(typeName, repr, emptyAttrs);
    writer.registerObject(pythonId, offset);
    return offset;
  }

  // Handle list (exact type - subclasses handled below as SerializedList)
  if (PyList_CheckExact(objPtr)) {
    Py_ssize_t size = PyList_Size(objPtr);
    std::vector<uint64_t> elementIds;
    elementIds.reserve(static_cast<size_t>(size));
    for (Py_ssize_t i = 0; i < size; ++i) {
      PyObject* item = PyList_GET_ITEM(objPtr, i);
      elementIds.push_back(reinterpret_cast<uint64_t>(item));
      // Queue item for processing
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(item), currentDepth + 1);
    }

    // Compute identity hash
    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(Py_TYPE(objPtr)),
        static_cast<uint64_t>(size),
        elementIds.data(),
        elementIds.size());

    // Check global cache for unchanged object
    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeList(elementIds);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle tuple (exact type - subclasses handled below as SerializedTuple)
  if (PyTuple_CheckExact(objPtr)) {
    Py_ssize_t size = PyTuple_Size(objPtr);
    std::vector<uint64_t> elementIds;
    elementIds.reserve(static_cast<size_t>(size));
    for (Py_ssize_t i = 0; i < size; ++i) {
      PyObject* item = PyTuple_GET_ITEM(objPtr, i);
      elementIds.push_back(reinterpret_cast<uint64_t>(item));
      // Queue item for processing
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(item), currentDepth + 1);
    }

    // Compute identity hash
    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(Py_TYPE(objPtr)),
        static_cast<uint64_t>(size),
        elementIds.data(),
        elementIds.size());

    // Check global cache for unchanged object
    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeTuple(elementIds);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle dict (exact type - subclasses handled below as SerializedDict)
  if (PyDict_CheckExact(objPtr)) {
    std::vector<std::pair<uint64_t, uint64_t>> items;
    std::vector<uint64_t> childIds; // For identity hash

    PyObject* key;
    PyObject* value;
    Py_ssize_t pos = 0;
    while (PyDict_Next(objPtr, &pos, &key, &value)) {
      uint64_t keyId = reinterpret_cast<uint64_t>(key);
      uint64_t valueId = reinterpret_cast<uint64_t>(value);
      items.emplace_back(keyId, valueId);
      childIds.push_back(keyId);
      childIds.push_back(valueId);
      // Queue key and value for processing
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(key), currentDepth + 1);
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(value), currentDepth + 1);
    }

    // Compute identity hash
    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(Py_TYPE(objPtr)),
        static_cast<uint64_t>(items.size()),
        childIds.data(),
        childIds.size());

    // Check global cache for unchanged object
    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeDict(items);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle set or frozenset (exact types only - subclasses handled below as
  // SerializedSet). Note: PySet_CheckExact doesn't exist, so we compare
  // types directly.
  if (Py_TYPE(objPtr) == &PySet_Type || Py_TYPE(objPtr) == &PyFrozenSet_Type) {
    std::vector<uint64_t> elementIds;
    PyObject* iter = PyObject_GetIter(objPtr);
    if (iter != nullptr) {
      PyObject* item;
      while ((item = PyIter_Next(iter)) != nullptr) {
        elementIds.push_back(reinterpret_cast<uint64_t>(item));
        // Queue item for processing (steals reference)
        processingQueue.emplace_back(
            py::reinterpret_steal<py::object>(item), currentDepth + 1);
      }
      Py_DECREF(iter);
    } else {
      PyErr_Clear();
    }

    // Compute identity hash
    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(Py_TYPE(objPtr)),
        static_cast<uint64_t>(elementIds.size()),
        elementIds.data(),
        elementIds.size());

    // Check global cache for unchanged object
    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeSet(elementIds);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle list subclasses (not exact list, which was handled above)
  if (PyList_Check(objPtr)) {
    // Extract list elements
    Py_ssize_t size = PyList_Size(objPtr);
    std::vector<uint64_t> elementIds;
    elementIds.reserve(static_cast<size_t>(size));
    for (Py_ssize_t i = 0; i < size; ++i) {
      PyObject* item = PyList_GET_ITEM(objPtr, i);
      elementIds.push_back(reinterpret_cast<uint64_t>(item));
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(item), currentDepth + 1);
    }

    // Get type name and repr
    PyTypeObject* type = Py_TYPE(objPtr);
    std::string typeName = getFullyQualifiedTypeName(type);

    std::string repr;
    {
      auto reprTimer = statsCollector.reprTimer();
      PyObject* repr_obj = PyObject_Repr(objPtr);
      if (repr_obj != nullptr) {
        const char* repr_str = PyUnicode_AsUTF8(repr_obj);
        repr = repr_str ? repr_str : "";
        Py_DECREF(repr_obj);
      } else {
        PyErr_Clear();
      }
    }

    // Extract attributes
    std::vector<std::pair<uint64_t, uint64_t>> attrIds;
    extractAttributes(obj, processingQueue, attrIds, currentDepth + 1);

    // Compute identity hash from elements + attributes
    std::vector<uint64_t> allChildIds;
    allChildIds.reserve(elementIds.size() + attrIds.size() * 2);
    allChildIds.insert(allChildIds.end(), elementIds.begin(), elementIds.end());
    for (const auto& [nameId, valueId] : attrIds) {
      allChildIds.push_back(nameId);
      allChildIds.push_back(valueId);
    }

    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(type),
        static_cast<uint64_t>(elementIds.size()),
        allChildIds.data(),
        allChildIds.size());

    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeSerializedList(elementIds, typeName, repr, attrIds);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle tuple subclasses (not exact tuple, which was handled above)
  if (PyTuple_Check(objPtr)) {
    // Extract tuple elements
    Py_ssize_t size = PyTuple_Size(objPtr);
    std::vector<uint64_t> elementIds;
    elementIds.reserve(static_cast<size_t>(size));
    for (Py_ssize_t i = 0; i < size; ++i) {
      PyObject* item = PyTuple_GET_ITEM(objPtr, i);
      elementIds.push_back(reinterpret_cast<uint64_t>(item));
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(item), currentDepth + 1);
    }

    // Get type name and repr
    PyTypeObject* type = Py_TYPE(objPtr);
    std::string typeName = getFullyQualifiedTypeName(type);

    std::string repr;
    {
      auto reprTimer = statsCollector.reprTimer();
      PyObject* repr_obj = PyObject_Repr(objPtr);
      if (repr_obj != nullptr) {
        const char* repr_str = PyUnicode_AsUTF8(repr_obj);
        repr = repr_str ? repr_str : "";
        Py_DECREF(repr_obj);
      } else {
        PyErr_Clear();
      }
    }

    // Extract attributes
    std::vector<std::pair<uint64_t, uint64_t>> attrIds;
    extractAttributes(obj, processingQueue, attrIds, currentDepth + 1);

    // Compute identity hash from elements + attributes
    std::vector<uint64_t> allChildIds;
    allChildIds.reserve(elementIds.size() + attrIds.size() * 2);
    allChildIds.insert(allChildIds.end(), elementIds.begin(), elementIds.end());
    for (const auto& [nameId, valueId] : attrIds) {
      allChildIds.push_back(nameId);
      allChildIds.push_back(valueId);
    }

    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(type),
        static_cast<uint64_t>(elementIds.size()),
        allChildIds.data(),
        allChildIds.size());

    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeSerializedTuple(elementIds, typeName, repr, attrIds);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle dict subclasses (not exact dict, which was handled above)
  if (PyDict_Check(objPtr)) {
    // Extract dict items
    std::vector<std::pair<uint64_t, uint64_t>> items;
    std::vector<uint64_t> childIds;

    PyObject* key;
    PyObject* value;
    Py_ssize_t pos = 0;
    while (PyDict_Next(objPtr, &pos, &key, &value)) {
      uint64_t keyId = reinterpret_cast<uint64_t>(key);
      uint64_t valueId = reinterpret_cast<uint64_t>(value);
      items.emplace_back(keyId, valueId);
      childIds.push_back(keyId);
      childIds.push_back(valueId);
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(key), currentDepth + 1);
      processingQueue.emplace_back(
          py::reinterpret_borrow<py::object>(value), currentDepth + 1);
    }

    // Get type name and repr
    PyTypeObject* type = Py_TYPE(objPtr);
    std::string typeName = getFullyQualifiedTypeName(type);

    std::string repr;
    {
      auto reprTimer = statsCollector.reprTimer();
      PyObject* repr_obj = PyObject_Repr(objPtr);
      if (repr_obj != nullptr) {
        const char* repr_str = PyUnicode_AsUTF8(repr_obj);
        repr = repr_str ? repr_str : "";
        Py_DECREF(repr_obj);
      } else {
        PyErr_Clear();
      }
    }

    // Extract attributes
    std::vector<std::pair<uint64_t, uint64_t>> attrIds;
    extractAttributes(obj, processingQueue, attrIds, currentDepth + 1);

    // Compute identity hash from dict items + attributes
    std::vector<uint64_t> allChildIds;
    allChildIds.reserve(childIds.size() + attrIds.size() * 2);
    allChildIds.insert(allChildIds.end(), childIds.begin(), childIds.end());
    for (const auto& [nameId, valueId] : attrIds) {
      allChildIds.push_back(nameId);
      allChildIds.push_back(valueId);
    }

    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(type),
        static_cast<uint64_t>(items.size()),
        allChildIds.data(),
        allChildIds.size());

    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeSerializedDict(items, typeName, repr, attrIds);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Handle set/frozenset subclasses (not exact types, which were handled above)
  if (PySet_Check(objPtr) || PyFrozenSet_Check(objPtr)) {
    // Extract set elements via iteration
    std::vector<uint64_t> elementIds;
    PyObject* iter = PyObject_GetIter(objPtr);
    if (iter != nullptr) {
      PyObject* item;
      while ((item = PyIter_Next(iter)) != nullptr) {
        elementIds.push_back(reinterpret_cast<uint64_t>(item));
        processingQueue.emplace_back(
            py::reinterpret_steal<py::object>(item), currentDepth + 1);
      }
      Py_DECREF(iter);
    } else {
      PyErr_Clear();
    }

    // Get type name and repr
    PyTypeObject* type = Py_TYPE(objPtr);
    std::string typeName = getFullyQualifiedTypeName(type);

    std::string repr;
    {
      auto reprTimer = statsCollector.reprTimer();
      PyObject* repr_obj = PyObject_Repr(objPtr);
      if (repr_obj != nullptr) {
        const char* repr_str = PyUnicode_AsUTF8(repr_obj);
        repr = repr_str ? repr_str : "";
        Py_DECREF(repr_obj);
      } else {
        PyErr_Clear();
      }
    }

    // Extract attributes
    std::vector<std::pair<uint64_t, uint64_t>> attrIds;
    extractAttributes(obj, processingQueue, attrIds, currentDepth + 1);

    // Compute identity hash from elements + attributes
    std::vector<uint64_t> allChildIds;
    allChildIds.reserve(elementIds.size() + attrIds.size() * 2);
    allChildIds.insert(allChildIds.end(), elementIds.begin(), elementIds.end());
    for (const auto& [nameId, valueId] : attrIds) {
      allChildIds.push_back(nameId);
      allChildIds.push_back(valueId);
    }

    uint64_t identityHash = computeIdentityHash(
        reinterpret_cast<uint64_t>(type),
        static_cast<uint64_t>(elementIds.size()),
        allChildIds.data(),
        allChildIds.size());

    uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
    if (cachedOffset != 0) {
      writer.registerObject(pythonId, cachedOffset);
      return cachedOffset;
    }

    offset = writer.writeSerializedSet(elementIds, typeName, repr, attrIds);
    writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
    return offset;
  }

  // Write certain object types as their repr string instead of serializing
  // their attributes. These types have large attribute graphs (e.g. functions
  // have __globals__ pointing to the entire module namespace) that are not
  // useful for debugging. The repr string is sufficient.
  if (PyModule_Check(objPtr) || PyType_Check(objPtr) ||
      PyFunction_Check(objPtr) || PyCFunction_Check(objPtr) ||
      PyMethod_Check(objPtr) || PyCode_Check(objPtr) || PyGen_Check(objPtr) ||
      PyCoro_CheckExact(objPtr) || PyAsyncGen_CheckExact(objPtr) ||
      PyCell_Check(objPtr)) {
    std::string repr;
    {
      auto reprTimer = statsCollector.reprTimer();
      PyObject* repr_obj = PyObject_Repr(objPtr);
      if (repr_obj != nullptr) {
        const char* repr_str = PyUnicode_AsUTF8(repr_obj);
        repr = repr_str ? repr_str : "";
        Py_DECREF(repr_obj);
      } else {
        PyErr_Clear();
        repr = "<repr failed>";
      }
    }
    offset = writer.writeString(repr);
    writer.registerObject(pythonId, offset);
    return offset;
  }

  // Fallback: SerializedObject for all other types
  // Get type name using C API
  PyTypeObject* type = Py_TYPE(objPtr);
  std::string typeName = getFullyQualifiedTypeName(type);

  // Get repr (with error handling)
  std::string repr;
  {
    auto reprTimer = statsCollector.reprTimer();
    PyObject* repr_obj = PyObject_Repr(objPtr);
    if (repr_obj != nullptr) {
      const char* repr_str = PyUnicode_AsUTF8(repr_obj);
      repr = repr_str ? repr_str : "";
      Py_DECREF(repr_obj);
    } else {
      PyErr_Clear();
      repr = "<repr failed>";
    }
  }

  // Extract attributes using our fast extraction method
  std::vector<std::pair<uint64_t, uint64_t>> attrIds;
  extractAttributes(obj, processingQueue, attrIds, currentDepth + 1);

  // Compute identity hash from children
  std::vector<uint64_t> flatAttrIds;
  flatAttrIds.reserve(attrIds.size() * 2);
  for (const auto& [nameId, valueId] : attrIds) {
    flatAttrIds.push_back(nameId);
    flatAttrIds.push_back(valueId);
  }

  uint64_t identityHash = computeIdentityHash(
      reinterpret_cast<uint64_t>(type),
      attrIds.size(),
      flatAttrIds.data(),
      flatAttrIds.size());

  // Check global cache for unchanged object
  uint64_t cachedOffset = writer.lookupObjectWithHash(pythonId, identityHash);
  if (cachedOffset != 0) {
    // Object unchanged - reuse cached offset
    // Note: children were already queued during attribute extraction above
    writer.registerObject(pythonId, cachedOffset);
    return cachedOffset;
  }

  offset = writer.writeSerializedObject(typeName, repr, attrIds);
  writer.registerObjectWithBothCaches(pythonId, offset, identityHash);
  return offset;
}

bool SnapshotCapture::writeFramesFromFrame(
    PyFrameObject* startFrame,
    uint32_t skipFrames,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    bool* objectDepthHit) {
  SnapshotWriter& writer = SnapshotWriter::getInstance();
  ObjectQueue processingQueue;

  if (startFrame == nullptr) {
    return false;
  }

  // Take ownership of the frame (borrowed ref from caller)
  Py_INCREF(startFrame);
  PyFrameObject* frame = startFrame;

  // Skip the requested number of frames from the top of the stack
  for (uint32_t i = 0; i < skipFrames && frame != nullptr; ++i) {
    PyFrameObject* next = PyFrame_GetBack(frame);
    Py_DECREF(frame);
    frame = next;
  }

  auto& statsCollector = SnapshotStatsCollector::getInstance();
  bool truncated = false;

  while (frame != nullptr) {
    // Check cancel before starting the frame
    if (isCancelRequested()) {
      Py_DECREF(frame);
      truncated = true;
      break;
    }

    // Check max_frames limit
    if (maxFrames.has_value() &&
        writer.getCurrentFrameCount() >= maxFrames.value()) {
      Py_DECREF(frame);
      truncated = true;
      break;
    }

    {
      PyCodeObject* code = PyFrame_GetCode(frame);

      PyObject* py_filename = code->co_filename;
      const char* filename_cstr = PyUnicode_AsUTF8(py_filename);
      std::string filename = filename_cstr ? filename_cstr : "";

      PyObject* py_funcname = code->co_name;
      const char* funcname_cstr = PyUnicode_AsUTF8(py_funcname);
      std::string funcname = funcname_cstr ? funcname_cstr : "";

#if PY_VERSION_HEX >= 0x030B0000
      PyObject* py_qualname = code->co_qualname;
      const char* qualname_cstr = PyUnicode_AsUTF8(py_qualname);
      std::string qualname = qualname_cstr ? qualname_cstr : funcname;
#else
      std::string qualname = funcname;
#endif

      int lineno = PyFrame_GetLineNumber(frame);

      Py_DECREF(code);

      // Skip frames matching file path filters (not flagged as truncation)
      if (shouldFilterFrame(filename)) {
        PyFrameObject* prev_frame = PyFrame_GetBack(frame);
        Py_DECREF(frame);
        frame = prev_frame;
        continue;
      }

      addReferencedFile(filename);

      {
        auto localVars = extractLocalVars(frame, processingQueue);

        // Save writeOffset before writing frame record
        size_t savedWriteOffset = writer.getWriteOffset();

        {
          auto writeTimer = statsCollector.writeFrameRecordTimer();
          writer.writeFrameRecord(
              filename,
              funcname,
              qualname,
              static_cast<uint32_t>(lineno),
              localVars);
          statsCollector.incrementFrameCount();
        }

        // Process this frame's objects inline
        bool localObjectDepthHit = false;
        bool completed = processFrameObjects(
            processingQueue, maxObjectDepth, localObjectDepthHit);
        if (localObjectDepthHit && objectDepthHit != nullptr) {
          *objectDepthHit = true;
        }

        if (!completed) {
          writer.rollbackWriteOffset(savedWriteOffset);
          Py_DECREF(frame);
          truncated = true;
          break;
        }
      }
    }

    // Get the next frame (back/outer frame)
    PyFrameObject* prev_frame = PyFrame_GetBack(frame);
    Py_DECREF(frame);
    frame = prev_frame;
  }

  // Return true if we wrote frames without truncation
  return !truncated;
}

bool SnapshotCapture::writeCurrentThreadFrames(
    uint32_t skipFrames,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    bool* objectDepthHit) {
  // PyFrame_GetBack() walks innermost→outermost, which is the order we want.
  PyThreadState* tstate = PyThreadState_Get();
  if (tstate == nullptr) {
    return false;
  }

  PyFrameObject* frame = PyThreadState_GetFrame(tstate);
  if (frame == nullptr) {
    return false;
  }

  // writeFramesFromFrame takes a borrowed ref and INCREFs internally,
  // so we need to DECREF the new ref from PyThreadState_GetFrame.
  bool result = writeFramesFromFrame(
      frame, skipFrames, maxFrames, maxObjectDepth, objectDepthHit);
  Py_DECREF(frame);
  return result;
}

void SnapshotCapture::captureAllThreads(
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth) {
  // Build thread name map before stopping the world (or before iteration)
  // This must be done while threads can still execute Python code
  auto threadNameMap = buildThreadNameMap();

#if PY_VERSION_HEX >= 0x030D0000 && defined(Py_GIL_DISABLED)
  // In free-threaded Python, sys._current_frames() can deadlock.
  // Use PyThreadState iteration with stop-the-world instead.
  SnapshotWriter& writer = SnapshotWriter::getInstance();
  PyInterpreterState* interp = PyInterpreterState_Get();

  // Disable garbage collection to prevent GC from running while
  // we have the world stopped (GC also needs to stop the world)
  py::module_ gc = py::module_::import("gc");
  bool gcWasEnabled = gc.attr("isenabled")().cast<bool>();
  gc.attr("disable")();

  _PyEval_StopTheWorld(interp);

  try {
    PyThreadState* tstate = PyInterpreterState_ThreadHead(interp);

    while (tstate != nullptr) {
      if (isCancelRequested()) {
        break;
      }

      uint64_t threadId = PyThreadState_GetID(tstate);

      PyFrameObject* frame = PyThreadState_GetFrame(tstate);
      if (frame == nullptr) {
        tstate = PyThreadState_Next(tstate);
        continue;
      }

      // Look up thread name from pre-built map
      std::string threadName;
      auto it = threadNameMap.find(threadId);
      if (it != threadNameMap.end()) {
        threadName = it->second;
      }

      writer.beginStacktrace(threadId, threadName);
      bool objectDepthHit = false;
      bool framesComplete = writeFramesFromFrame(
          frame, 0, maxFrames, maxObjectDepth, &objectDepthHit);

      // PyThreadState_GetFrame returns a new reference
      Py_DECREF(frame);

      if (writer.getCurrentFrameCount() == 0) {
        writer.discardCurrentStacktrace();
      } else {
        writer.endStacktrace(!framesComplete, objectDepthHit);
      }

      tstate = PyThreadState_Next(tstate);
    }
  } catch (...) {
    _PyEval_StartTheWorld(interp);
    if (gcWasEnabled) {
      gc.attr("enable")();
    }
    throw;
  }

  _PyEval_StartTheWorld(interp);

  if (gcWasEnabled) {
    gc.attr("enable")();
  }

#else
  // Standard Python: use sys._current_frames()
  py::dict currentFrames = sysModule_.attr("_current_frames")();

  SnapshotWriter& writer = SnapshotWriter::getInstance();

  for (auto item : currentFrames) {
    if (isCancelRequested()) {
      break;
    }

    uint64_t threadId = item.first.cast<uint64_t>();

    py::object frameObj = py::reinterpret_borrow<py::object>(item.second);
    PyFrameObject* frame = reinterpret_cast<PyFrameObject*>(frameObj.ptr());
    if (frame == nullptr ||
        !PyFrame_Check(reinterpret_cast<PyObject*>(frame))) {
      continue;
    }

    // Look up thread name from pre-built map
    std::string threadName;
    auto it = threadNameMap.find(threadId);
    if (it != threadNameMap.end()) {
      threadName = it->second;
    }

    writer.beginStacktrace(threadId, threadName);

    // writeFramesFromFrame takes a borrowed ref and INCREFs internally.
    // Returns false when truncated (by maxFrames or cancel). Only discard
    // the stacktrace when zero frames were actually written.
    bool objectDepthHit = false;
    bool framesComplete = writeFramesFromFrame(
        frame, 0, maxFrames, maxObjectDepth, &objectDepthHit);

    if (writer.getCurrentFrameCount() == 0) {
      writer.discardCurrentStacktrace();
    } else {
      writer.endStacktrace(!framesComplete, objectDepthHit);
    }
  }
#endif
}

bool SnapshotCapture::snapshotAllThreads(
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    double timeoutSeconds,
    bool alreadyHoldsLock) {
  // Claim the snapshot-in-progress flag (shared with takeSnapshot)
  // unless caller already holds it
  if (!alreadyHoldsLock) {
    bool expected = false;
    if (!snapshotInProgress_.compare_exchange_strong(
            expected, true, std::memory_order_acq_rel)) {
      return false;
    }
  }

  try {
    SnapshotWriter& writer = SnapshotWriter::getInstance();

    if (!writer.isInitialized()) {
      if (!alreadyHoldsLock) {
        snapshotInProgress_.store(false, std::memory_order_release);
      }
      throw std::runtime_error("snapshot module not initialized");
    }

    clearCancel();
    startTimeoutTimer(timeoutSeconds);

    writer.beginSnapshot();

    captureAllThreads(maxFrames, maxObjectDepth);

    // End the snapshot record
    bool snapshotTruncated = isCancelRequested();
    bool snapshotWritten = false;
    if (writer.getCurrentStacktraceCount() == 0) {
      writer.discardCurrentSnapshot();
    } else {
      writer.endSnapshot(snapshotTruncated);
      snapshotWritten = true;
    }

    stopTimeoutTimer();
    clearCancel();
    if (!alreadyHoldsLock) {
      snapshotInProgress_.store(false, std::memory_order_release);
    }

    return snapshotWritten;

  } catch (...) {
    // Clean up: discard any in-progress snapshot, reset all flags
    stopTimeoutTimer();
    clearCancel();
    SnapshotWriter& writer = SnapshotWriter::getInstance();
    if (writer.isInitialized()) {
      writer.discardCurrentSnapshot();
    }
    if (!alreadyHoldsLock) {
      snapshotInProgress_.store(false, std::memory_order_release);
    }

    throw;
  }
}

bool SnapshotCapture::takeSnapshotFromFrame(
    PyFrameObject* frame,
    uint64_t threadId,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    std::optional<double> timeoutSeconds,
    bool alreadyHoldsLock) {
  // Claim snapshot-in-progress flag
  bool expected = false;
  if (!alreadyHoldsLock &&
      !snapshotInProgress_.compare_exchange_strong(
          expected, true, std::memory_order_acq_rel)) {
    return false;
  }

  // Safety net: if any operation throws after claiming snapshotInProgress_,
  // clean up so the module doesn't get stuck.
  try {
    SnapshotWriter& writer = SnapshotWriter::getInstance();

    if (!writer.isInitialized()) {
      if (!alreadyHoldsLock) {
        snapshotInProgress_.store(false, std::memory_order_release);
      }
      throw std::runtime_error("snapshot module not initialized");
    }

    if (timeoutSeconds.has_value()) {
      startTimeoutTimer(timeoutSeconds.value());
    }

    writer.beginSnapshot();

    // Look up thread name - for single thread capture, we need to build the map
    // since the thread ID might not be the current thread
    auto threadNameMap = buildThreadNameMap();
    std::string threadName;
    auto it = threadNameMap.find(threadId);
    if (it != threadNameMap.end()) {
      threadName = it->second;
    }

    writer.beginStacktrace(threadId, threadName);

    // writeFramesFromFrame returns false when truncated (by maxFrames or
    // cancel). We should only discard the stacktrace when zero frames were
    // actually written.
    bool objectDepthHit = false;
    bool framesComplete = writeFramesFromFrame(
        frame, 0, maxFrames, maxObjectDepth, &objectDepthHit);

    if (writer.getCurrentFrameCount() == 0) {
      writer.discardCurrentStacktrace();
    } else {
      writer.endStacktrace(!framesComplete, objectDepthHit);
    }

    bool snapshotWritten = false;
    if (writer.getCurrentStacktraceCount() == 0) {
      writer.discardCurrentSnapshot();
    } else {
      writer.endSnapshot(!framesComplete);
      snapshotWritten = true;
    }

    if (timeoutSeconds.has_value()) {
      stopTimeoutTimer();
    }
    clearCancel();
    if (!alreadyHoldsLock) {
      snapshotInProgress_.store(false, std::memory_order_release);
    }
    return snapshotWritten;

  } catch (...) {
    stopTimeoutTimer();
    clearCancel();
    SnapshotWriter& writer = SnapshotWriter::getInstance();
    if (writer.isInitialized()) {
      writer.discardCurrentSnapshot();
    }
    if (!alreadyHoldsLock) {
      snapshotInProgress_.store(false, std::memory_order_release);
    }
    throw;
  }
}

bool SnapshotCapture::isSamplingActive() const {
  return samplingActive_;
}

void SnapshotCapture::enableSampling(
    double interval,
    SamplingMode mode,
    uint64_t targetThreadId,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    double timeout) {
  std::lock_guard<std::mutex> lock(samplingMutex_);
  if (samplingActive_) {
    throw std::runtime_error("sampling is already active");
  }

  samplingActive_ = true;
  samplingShouldStop_ = false;

  samplingThread_ = std::make_unique<std::thread>(
      &SnapshotCapture::samplingLoop,
      this,
      interval,
      mode,
      targetThreadId,
      maxFrames,
      maxObjectDepth,
      timeout);
}

void SnapshotCapture::disableSampling() {
  {
    std::lock_guard<std::mutex> lock(samplingMutex_);
    if (!samplingActive_) {
      throw std::runtime_error("sampling is not active");
    }
    samplingShouldStop_ = true;
  }
  samplingCv_.notify_one();

  if (samplingThread_ && samplingThread_->joinable()) {
    samplingThread_->join();
  }
  samplingThread_.reset();

  std::lock_guard<std::mutex> lock(samplingMutex_);
  samplingActive_ = false;
}

void SnapshotCapture::samplingLoop(
    double interval,
    SamplingMode mode,
    uint64_t targetThreadId,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    double timeout) {
  while (true) {
    // Sleep for the interval, interruptible by disableSampling()
    {
      std::unique_lock<std::mutex> lock(samplingMutex_);
      if (samplingCv_.wait_for(
              lock, std::chrono::duration<double>(interval), [this] {
                return samplingShouldStop_;
              })) {
        break; // stopped
      }
    }

    // Check stop flag again before doing any work — disableSampling() may
    // have been called while we were between releasing samplingMutex_ and
    // here.
    if (samplingShouldStop_) {
      break;
    }

    // Try to claim snapshotInProgress_ immediately after waking.
    // This prevents the sampling thread from blocking on PyGILState_Ensure()
    // while another thread holds the GIL and is waiting on
    // snapshotInProgress_.
    bool expected = false;
    if (!snapshotInProgress_.compare_exchange_strong(
            expected, true, std::memory_order_acq_rel)) {
      // Another snapshot is in progress, skip this sample
      continue;
    }

    // Check stop flag again after claiming lock — if we need to stop, release
    // the lock immediately and exit without trying to acquire the GIL.
    if (samplingShouldStop_) {
      snapshotInProgress_.store(false, std::memory_order_release);
      break;
    }

    // Acquire the GIL to do Python work
    PyGILState_STATE gstate = PyGILState_Ensure();

    try {
      if (mode == SamplingMode::ALL_THREADS) {
        // We already hold snapshotInProgress_, so tell snapshotAllThreads not
        // to try to acquire it again — this avoids a deadlock where another
        // thread could grab the lock between our release and re-acquire.
        snapshotAllThreads(
            maxFrames, maxObjectDepth, timeout, /*alreadyHoldsLock=*/true);
      } else {
        sampleSingleThread(
            targetThreadId,
            maxFrames,
            maxObjectDepth,
            timeout,
            /*alreadyHoldsLock=*/true);
      }
    } catch (...) {
      // Swallow — don't let exceptions kill the sampling thread.
      // Individual functions handle their own state cleanup.
    }

    // Release the lock after we're done with this sample
    snapshotInProgress_.store(false, std::memory_order_release);

    PyGILState_Release(gstate);
  }
}

void SnapshotCapture::sampleSingleThread(
    uint64_t targetThreadId,
    std::optional<uint32_t> maxFrames,
    std::optional<uint32_t> maxObjectDepth,
    double timeout,
    bool alreadyHoldsLock) {
  // Directly capture the target thread using sys._current_frames().
  // The GIL is held by samplingLoop() via PyGILState_Ensure().
  // Note: In free-threaded Python (3.13t+), other threads continue executing
  // even with GIL held - we hold references to prevent object destruction,
  // but object state may be mutated during serialization.

  if (samplingShouldStop_) {
    return;
  }

  py::dict currentFrames = sysModule_.attr("_current_frames")();

  py::object key = py::cast(targetThreadId);
  if (currentFrames.contains(key)) {
    py::object frameObj = currentFrames[key];
    auto* frame = reinterpret_cast<PyFrameObject*>(frameObj.ptr());
    if (frame != nullptr && PyFrame_Check(reinterpret_cast<PyObject*>(frame))) {
      takeSnapshotFromFrame(
          frame,
          targetThreadId,
          maxFrames,
          maxObjectDepth,
          timeout,
          alreadyHoldsLock);
    }
  }
  // If target thread not found (e.g., exited), silently skip this sample
}

} // namespace facebook::tintype::snapshot
