// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <Python.h>
#include <traceback.h>

#include <memory>
#include <optional>
#include <string>
#include <variant>
#include <vector>

#include "snapshot_lib/SnapshotCapture.h"
#include "snapshot_lib/SnapshotReader.h"

namespace py = pybind11;

namespace facebook::tintype {

namespace {

// Offset of f_back within CPython's PyFrameObject, determined at runtime.
// Works across standard and free-threaded Python builds which have different
// PyObject_HEAD sizes.
Py_ssize_t gFrameFBackOffset = -1;

// Determine the offset by inspecting a frame with a known f_back value.
void detectFBackOffset(PyFrameObject* frame) {
  if (gFrameFBackOffset >= 0) {
    return;
  }

  PyFrameObject* back = PyFrame_GetBack(frame);
  if (!back) {
    return;
  }

  auto frameAddr = reinterpret_cast<uintptr_t>(frame);
  auto backAddr = reinterpret_cast<uintptr_t>(back);

  for (Py_ssize_t off = 0; off < 64;
       off += static_cast<Py_ssize_t>(sizeof(void*))) {
    auto* ptr = reinterpret_cast<uintptr_t*>(frameAddr + off);
    if (*ptr == backAddr) {
      gFrameFBackOffset = off;
      break;
    }
  }

  Py_DECREF(back);
}

void setFrameFBack(PyFrameObject* frame, PyFrameObject* newBack) {
  if (gFrameFBackOffset < 0) {
    return;
  }

  // Read old f_back via public API (returns new reference).
  PyFrameObject* oldBack = PyFrame_GetBack(frame);

  // Write new f_back pointer directly.
  auto** fBackPtr = reinterpret_cast<PyFrameObject**>(
      reinterpret_cast<uintptr_t>(frame) + gFrameFBackOffset);
  Py_XINCREF(newBack);
  *fBackPtr = newBack;

  // Release the struct's old reference + PyFrame_GetBack's reference.
  Py_XDECREF(oldBack);
  Py_XDECREF(oldBack);
}

// Wire a Snapshot's back-references so that get_locals() and get_traceback()
// work. Sets _reader on the snapshot, _snapshot on each stacktrace, and
// _stacktrace on each frame. Caches the wired collections as _stacktraces
// and _frames.
void wireSnapshot(py::object snapPy, const py::object& readerObj) {
  snapPy.attr("_reader") = readerObj;
  py::dict stacktraces = snapPy.attr("stacktraces");
  for (auto item : stacktraces) {
    py::object stPy = py::reinterpret_borrow<py::object>(item.second);
    stPy.attr("_snapshot") = snapPy;
    py::list frames = stPy.attr("frames");
    for (auto framePy : frames) {
      py::reinterpret_borrow<py::object>(framePy).attr("_stacktrace") = stPy;
    }
    stPy.attr("_frames") = frames;
  }
  snapPy.attr("_stacktraces") = stacktraces;
}

// Link two chronologically adjacent snapshots so that get_prev_snapshot()
// and get_next_snapshot() are O(1) cache hits. Only sets each attribute
// if it hasn't been cached already.
void linkAdjacentSnapshots(py::object prev, py::object next) {
  if (!py::hasattr(prev, "_next_snapshot")) {
    prev.attr("_next_snapshot") = next;
  }
  if (!py::hasattr(next, "_prev_snapshot")) {
    next.attr("_prev_snapshot") = prev;
  }
}

} // namespace

// ============================================================================
// SerializedObject types - defined using CPython C API
// ============================================================================

// --- SerializedObject (no builtin base) ---

struct SerializedObjectData {
  PyObject_HEAD;
  PyObject* serializedRepr; // str
};

static int
SerializedObject_init(PyObject* self, PyObject* args, PyObject* /*kwds*/) {
  PyObject* serializedRepr = nullptr;
  if (!PyArg_ParseTuple(args, "U", &serializedRepr) || !serializedRepr) {
    return -1;
  }
  auto* data = reinterpret_cast<SerializedObjectData*>(self);
  Py_XDECREF(data->serializedRepr);
  Py_INCREF(serializedRepr);
  data->serializedRepr = serializedRepr;
  return 0;
}

static PyObject* SerializedObject_repr(PyObject* self) {
  auto* data = reinterpret_cast<SerializedObjectData*>(self);
  if (data->serializedRepr) {
    Py_INCREF(data->serializedRepr);
    return data->serializedRepr;
  }
  return PyUnicode_FromString("");
}

static int
SerializedObject_traverse(PyObject* self, visitproc visit, void* arg) {
  auto* data = reinterpret_cast<SerializedObjectData*>(self);
  Py_VISIT(data->serializedRepr);
  return 0;
}

static int SerializedObject_clear(PyObject* self) {
  auto* data = reinterpret_cast<SerializedObjectData*>(self);
  Py_CLEAR(data->serializedRepr);
  return 0;
}

static void SerializedObject_dealloc(PyObject* self) {
  PyObject_GC_UnTrack(self);
  SerializedObject_clear(self);
  Py_TYPE(self)->tp_free(self);
}

// __dict__ descriptor for types using Py_TPFLAGS_MANAGED_DICT
static PyObject* ManagedDict_get(PyObject* self, void* /*closure*/) {
  return PyObject_GenericGetDict(self, nullptr);
}

static int ManagedDict_set(PyObject* self, PyObject* value, void* /*closure*/) {
  if (value == nullptr) {
    PyErr_SetString(PyExc_TypeError, "cannot delete __dict__");
    return -1;
  }
  if (!PyDict_Check(value)) {
    PyErr_SetString(PyExc_TypeError, "__dict__ must be a dictionary");
    return -1;
  }
  // Use GenericSetDict for managed dict types
  return PyObject_GenericSetDict(self, value, nullptr);
}

// clang-format off
static PyGetSetDef SerializedObject_getset[] = {
    {"__dict__", ManagedDict_get, ManagedDict_set, nullptr, nullptr},
    {nullptr, nullptr, nullptr, nullptr, nullptr},
};

static PyType_Slot SerializedObject_slots[] = {
    {Py_tp_init, reinterpret_cast<void*>(SerializedObject_init)},
    {Py_tp_repr, reinterpret_cast<void*>(SerializedObject_repr)},
    {Py_tp_dealloc, reinterpret_cast<void*>(SerializedObject_dealloc)},
    {Py_tp_traverse, reinterpret_cast<void*>(SerializedObject_traverse)},
    {Py_tp_clear, reinterpret_cast<void*>(SerializedObject_clear)},
    {Py_tp_getset, reinterpret_cast<void*>(SerializedObject_getset)},
    {0, nullptr},
};

static PyType_Spec SerializedObject_spec = {
    "tintype._snapshot.SerializedObject",
    sizeof(SerializedObjectData),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_MANAGED_DICT,
    SerializedObject_slots,
};
// clang-format on

// --- SerializedListObject(list) ---
// Subclasses Python list, stores __serialized_repr__ in the instance dict.

static int
SerializedListObject_init(PyObject* self, PyObject* args, PyObject* /*kwds*/) {
  PyObject* listObj = nullptr;
  PyObject* serializedRepr = nullptr;
  if (!PyArg_ParseTuple(args, "OU", &listObj, &serializedRepr) || !listObj ||
      !serializedRepr) {
    return -1;
  }

  // Initialize the list base with the list contents
  PyObject* listArgs = PyTuple_Pack(1, listObj);
  if (!listArgs) {
    return -1;
  }
  int rc = PyList_Type.tp_init(self, listArgs, nullptr);
  Py_DECREF(listArgs);
  if (rc < 0) {
    return -1;
  }

  // Store __serialized_repr__ as an attribute
  if (PyObject_SetAttrString(self, "__serialized_repr__", serializedRepr) < 0) {
    return -1;
  }
  return 0;
}

static PyObject* SerializedListObject_repr(PyObject* self) {
  PyObject* repr = PyObject_GetAttrString(self, "__serialized_repr__");
  if (repr) {
    return repr;
  }
  PyErr_Clear();
  return PyUnicode_FromString("");
}

// Identity-based hash for serialized collection types.
// list and dict set __hash__ = None (unhashable), but these are frozen snapshot
// objects that may appear as dict keys in the object graph, so we restore
// identity-based hashing.
static Py_hash_t SerializedCollection_hash(PyObject* self) {
  return _Py_HashPointer(self);
}

// clang-format off
static PyGetSetDef SerializedListObject_getset[] = {
    {"__dict__", ManagedDict_get, ManagedDict_set, nullptr, nullptr},
    {nullptr, nullptr, nullptr, nullptr, nullptr},
};

static PyType_Slot SerializedListObject_slots[] = {
    {Py_tp_init, reinterpret_cast<void*>(SerializedListObject_init)},
    {Py_tp_repr, reinterpret_cast<void*>(SerializedListObject_repr)},
    {Py_tp_hash, reinterpret_cast<void*>(SerializedCollection_hash)},
    {Py_tp_getset, reinterpret_cast<void*>(SerializedListObject_getset)},
    {0, nullptr},
};

static PyType_Spec SerializedListObject_spec = {
    "tintype._snapshot.SerializedListObject",
    0, // no extra instance storage beyond list
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_MANAGED_DICT,
    SerializedListObject_slots,
};
// clang-format on

// --- SerializedDictObject(dict) ---
// Subclasses Python dict, stores __serialized_repr__ in the instance dict.

static int
SerializedDictObject_init(PyObject* self, PyObject* args, PyObject* /*kwds*/) {
  PyObject* dictObj = nullptr;
  PyObject* serializedRepr = nullptr;
  if (!PyArg_ParseTuple(args, "OU", &dictObj, &serializedRepr) || !dictObj ||
      !serializedRepr) {
    return -1;
  }

  // Initialize the dict base with the dict contents
  PyObject* dictArgs = PyTuple_Pack(1, dictObj);
  if (!dictArgs) {
    return -1;
  }
  int rc = PyDict_Type.tp_init(self, dictArgs, nullptr);
  Py_DECREF(dictArgs);
  if (rc < 0) {
    return -1;
  }

  // Store __serialized_repr__ as an attribute
  if (PyObject_SetAttrString(self, "__serialized_repr__", serializedRepr) < 0) {
    return -1;
  }
  return 0;
}

static PyObject* SerializedDictObject_repr(PyObject* self) {
  PyObject* repr = PyObject_GetAttrString(self, "__serialized_repr__");
  if (repr) {
    return repr;
  }
  PyErr_Clear();
  return PyUnicode_FromString("");
}

// clang-format off
static PyGetSetDef SerializedDictObject_getset[] = {
    {"__dict__", ManagedDict_get, ManagedDict_set, nullptr, nullptr},
    {nullptr, nullptr, nullptr, nullptr, nullptr},
};

static PyType_Slot SerializedDictObject_slots[] = {
    {Py_tp_init, reinterpret_cast<void*>(SerializedDictObject_init)},
    {Py_tp_repr, reinterpret_cast<void*>(SerializedDictObject_repr)},
    {Py_tp_hash, reinterpret_cast<void*>(SerializedCollection_hash)},
    {Py_tp_getset, reinterpret_cast<void*>(SerializedDictObject_getset)},
    {0, nullptr},
};

static PyType_Spec SerializedDictObject_spec = {
    "tintype._snapshot.SerializedDictObject",
    0, // no extra instance storage beyond dict
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_MANAGED_DICT,
    SerializedDictObject_slots,
};
// clang-format on

// ============================================================================
// Python API Functions - Thin wrappers around SnapshotCapture
// ============================================================================

// Module-level reference to the borrowed reader returned by initialize().
// This allows initialize() to return the same reader on subsequent calls
// and take_snapshot() to auto-initialize when needed.
py::object borrowedReaderObj;

py::object initialize(
    bool collectStats,
    const std::optional<std::vector<std::string>>& frameFilePathFilters) {
  // If already initialized, return the existing reader
  if (snapshot::SnapshotCapture::getInstance().isInitialized()) {
    return borrowedReaderObj;
  }

  snapshot::SnapshotCapture::getInstance().initialize(
      collectStats, frameFilePathFilters.value_or(std::vector<std::string>{}));

  auto& writer = snapshot::SnapshotWriter::getInstance();
  auto reader = std::make_unique<snapshot::SnapshotReader>(
      writer.getSnapshotFilePtr(),
      writer.getSnapshotFileSize(),
      writer.getTempFilePath());
  writer.setBorrowedReader(reader.get());
  borrowedReaderObj = py::cast(std::move(reader));
  return borrowedReaderObj;
}

void finalize(
    const std::string& path,
    std::optional<py::dict> metadata,
    std::optional<int> compressionLevel) {
  std::string metadataJson = "{}";
  if (metadata.has_value()) {
    metadataJson = snapshot::dictToJson(metadata.value());
  }
  // None means no compression (level 0), default uses kCompressionLevel (3)
  int level = compressionLevel.value_or(0);
  snapshot::SnapshotCapture::getInstance().finalize(path, metadataJson, level);
  borrowedReaderObj = py::object();
}

py::dict get_stats() {
  return snapshot::SnapshotCapture::getInstance().getStats();
}

void reset_stats() {
  snapshot::SnapshotCapture::getInstance().resetStats();
}

// ============================================================================
// Python Module Definition
// ============================================================================

PYBIND11_MODULE(_snapshot, m) {
  m.doc() = "C++ Python extension for taking traceback snapshots";

  // Define SerializedObject types using the C API
  PyObject* SerializedObjectType = PyType_FromSpec(&SerializedObject_spec);
  if (!SerializedObjectType) {
    throw py::error_already_set();
  }
  m.attr("SerializedObject") =
      py::reinterpret_steal<py::object>(SerializedObjectType);

  // Create SerializedListObject with list as base type
  PyObject* listBases = PyTuple_Pack(1, &PyList_Type);
  if (!listBases) {
    throw py::error_already_set();
  }
  PyObject* SerializedListObjectType =
      PyType_FromSpecWithBases(&SerializedListObject_spec, listBases);
  Py_DECREF(listBases);
  if (!SerializedListObjectType) {
    throw py::error_already_set();
  }
  m.attr("SerializedListObject") =
      py::reinterpret_steal<py::object>(SerializedListObjectType);

  // Create SerializedDictObject with dict as base type
  PyObject* dictBases = PyTuple_Pack(1, &PyDict_Type);
  if (!dictBases) {
    throw py::error_already_set();
  }
  PyObject* SerializedDictObjectType =
      PyType_FromSpecWithBases(&SerializedDictObject_spec, dictBases);
  Py_DECREF(dictBases);
  if (!SerializedDictObjectType) {
    throw py::error_already_set();
  }
  m.attr("SerializedDictObject") =
      py::reinterpret_steal<py::object>(SerializedDictObjectType);

  m.def(
      "initialize",
      &initialize,
      py::arg("collect_stats") = false,
      py::arg("frame_file_path_filters") = py::none(),
      "Initialize the snapshot module with optional stats collection flag. "
      "Returns a SnapshotReader that shares memory with the writer, "
      "allowing snapshots to be read before finalize() is called. "
      "If already initialized, returns the existing reader. "
      "frame_file_path_filters is an optional list of substrings; frames "
      "whose file path contains any of them will be silently skipped.");

  m.def(
      "take_snapshot",
      [](py::object obj,
         std::optional<uint32_t> maxFrames,
         std::optional<uint32_t> maxObjectDepth,
         std::optional<double> timeout,
         uint32_t skipFrames) -> py::object {
        if (!snapshot::SnapshotCapture::getInstance().isInitialized()) {
          initialize(false, std::nullopt);
        }

        bool written = false;
        if (obj.is_none()) {
          written = snapshot::SnapshotCapture::getInstance().takeSnapshot(
              nullptr, maxFrames, maxObjectDepth, timeout, skipFrames);
        } else if (PyExceptionInstance_Check(obj.ptr())) {
          written = snapshot::SnapshotCapture::getInstance()
                        .takeSnapshotFromException(
                            obj.ptr(), maxFrames, maxObjectDepth, timeout);
        } else {
          // Assume traceback (validated in C++)
          written = snapshot::SnapshotCapture::getInstance().takeSnapshot(
              obj.ptr(), maxFrames, maxObjectDepth, timeout);
        }

        if (!written) {
          return py::none();
        }

        // Read the snapshot that was just taken from the borrowed reader
        auto& reader = borrowedReaderObj.cast<snapshot::SnapshotReader&>();
        auto result = reader.getLatestSnapshot();
        if (!result) {
          return py::none();
        }
        py::object snapPy = py::cast(std::move(*result));
        wireSnapshot(snapPy, borrowedReaderObj);
        return snapPy;
      },
      py::arg("traceback_or_exception") = py::none(),
      py::arg("max_frames") = py::none(),
      py::arg("max_object_depth") = py::none(),
      py::arg("timeout") = py::none(),
      py::arg("skip_frames") = 0,
      "Take a snapshot and return the Snapshot object. Pass None (default) "
      "to capture the current call stack, a traceback object to capture its "
      "frames, or an exception to capture its traceback and the full "
      "__cause__/__context__ chain. Auto-initializes if not already "
      "initialized. max_frames limits the number of frames captured per "
      "stacktrace. max_object_depth limits the depth of object serialization. "
      "timeout sets a time limit in seconds for the entire "
      "operation. skip_frames skips N frames from the top of the call stack "
      "(only applies when capturing the current stack).");

  m.def(
      "finalize",
      &finalize,
      py::arg("path") = "",
      py::arg("metadata") = py::none(),
      py::arg("compression_level") = 3,
      "Finalize the snapshot module. If path is provided, compresses and "
      "writes the snapshot data to that file. If path is empty, discards "
      "the snapshot data. If metadata dict is provided, it is stored as "
      "JSON in the snapshot file. compression_level controls zstd level "
      "(1-22), or pass None to write uncompressed.");

  m.def(
      "get_stats",
      &get_stats,
      "Get statistics about the snapshot module as a dictionary");

  m.def("reset_stats", &reset_stats, "Reset statistics to zero");

  m.def(
      "cancel_snapshot",
      []() { snapshot::SnapshotCapture::getInstance().requestCancel(); },
      "Request cancellation of the current snapshot operation. "
      "Thread-safe: can be called from any thread.");

  m.def(
      "snapshot_all_threads",
      [](double timeout,
         std::optional<uint32_t> maxFrames,
         std::optional<uint32_t> maxObjectDepth) -> py::object {
#if defined(Py_GIL_DISABLED)
        // snapshot_all_threads is not supported in free-threaded Python
        // due to potential deadlocks when capturing frames from other threads
        throw std::runtime_error(
            "snapshot_all_threads() is not supported in free-threaded Python. "
            "Use take_snapshot() to capture the current thread instead.");
#else
        if (!snapshot::SnapshotCapture::getInstance().isInitialized()) {
          initialize(false, std::nullopt);
        }

        bool written =
            snapshot::SnapshotCapture::getInstance().snapshotAllThreads(
                maxFrames, maxObjectDepth, timeout);

        if (!written) {
          return py::none();
        }

        // Read the snapshot that was just taken from the borrowed reader
        auto& reader = borrowedReaderObj.cast<snapshot::SnapshotReader&>();
        auto result = reader.getLatestSnapshot();
        if (!result) {
          return py::none();
        }
        py::object snapPy = py::cast(std::move(*result));
        wireSnapshot(snapPy, borrowedReaderObj);
        return snapPy;
#endif
      },
      py::arg("timeout") = 1.0,
      py::arg("max_frames") = py::none(),
      py::arg("max_object_depth") = py::none(),
      "Take a snapshot of all Python threads and return the Snapshot object. "
      "Uses sys._current_frames() while holding the GIL to capture all thread "
      "stacks atomically. Returns None if a snapshot is already in progress. "
      "max_frames limits the number of frames captured per stacktrace. "
      "timeout sets a time limit in seconds for serialization (default 1.0).");

  m.def(
      "take_snapshot_from_frame",
      [](py::object frameObj,
         uint64_t threadId,
         std::optional<uint32_t> maxFrames,
         std::optional<uint32_t> maxObjectDepth,
         std::optional<double> timeout) -> py::object {
        auto& capture = snapshot::SnapshotCapture::getInstance();
        if (!capture.isInitialized()) {
          initialize(false, std::nullopt);
        }
        auto* frame = reinterpret_cast<PyFrameObject*>(frameObj.ptr());
        bool written = capture.takeSnapshotFromFrame(
            frame, threadId, maxFrames, maxObjectDepth, timeout);
        if (!written) {
          return py::none();
        }
        auto& reader = borrowedReaderObj.cast<snapshot::SnapshotReader&>();
        auto result = reader.getLatestSnapshot();
        if (!result) {
          return py::none();
        }
        py::object snapPy = py::cast(std::move(*result));
        wireSnapshot(snapPy, borrowedReaderObj);
        return snapPy;
      },
      py::arg("frame"),
      py::arg("thread_id"),
      py::arg("max_frames") = py::none(),
      py::arg("max_object_depth") = py::none(),
      py::arg("timeout") = py::none(),
      "Take a snapshot of a single thread given its frame object. "
      "Used for sampling fallback when the target thread is in native code.");

  m.def(
      "disable_sampling",
      []() {
        // Release the GIL before joining the sampling thread, because
        // the sampling thread needs the GIL to complete its current tick
        // (e.g., PyEval_RestoreThread in sampleSingleThread, or
        // PyGILState_Release in samplingLoop). Without this, the main
        // thread holds the GIL and waits on join(), while the sampling
        // thread waits for the GIL — deadlock.
        py::gil_scoped_release release;
        snapshot::SnapshotCapture::getInstance().disableSampling();
      },
      "Stop periodic sampling.");

  // SamplingMode enum for enable_sampling
  py::enum_<snapshot::SnapshotCapture::SamplingMode>(m, "SamplingMode")
      .value(
          "SINGLE_THREAD",
          snapshot::SnapshotCapture::SamplingMode::SINGLE_THREAD)
      .value(
          "ALL_THREADS", snapshot::SnapshotCapture::SamplingMode::ALL_THREADS);

  m.def(
      "enable_sampling",
      [](double interval,
         snapshot::SnapshotCapture::SamplingMode mode,
         std::optional<uint32_t> maxFrames,
         std::optional<uint32_t> maxObjectDepth,
         double timeout) {
#if defined(Py_GIL_DISABLED)
        // Sampling is not supported in free-threaded Python because it
        // requires capturing frames from other threads, which can deadlock
        (void)interval;
        (void)mode;
        (void)maxFrames;
        (void)maxObjectDepth;
        (void)timeout;
        throw std::runtime_error(
            "Sampling is not supported in free-threaded Python. "
            "Capturing frames from other threads can cause deadlocks.");
#else
        auto& capture = snapshot::SnapshotCapture::getInstance();
        if (!capture.isInitialized()) {
          initialize(false, std::nullopt);
        }
        // Get the calling thread's ID - this is the target for SINGLE_THREAD
        // mode
        uint64_t targetThreadId =
            static_cast<uint64_t>(PyThread_get_thread_ident());
        capture.enableSampling(
            interval, mode, targetThreadId, maxFrames, maxObjectDepth, timeout);
#endif
      },
      py::arg("interval"),
      py::arg("mode") = snapshot::SnapshotCapture::SamplingMode::ALL_THREADS,
      py::arg("max_frames") = py::none(),
      py::arg("max_object_depth") = py::none(),
      py::arg("timeout") = 1.0,
      "Start periodic sampling with a C++ timer thread.\n\n"
      "Args:\n"
      "    interval: Seconds between samples.\n"
      "    mode: SamplingMode.SINGLE_THREAD samples only the calling thread;\n"
      "          SamplingMode.ALL_THREADS samples all Python threads.\n"
      "    max_frames: Max frames per stacktrace (None = no limit).\n"
      "    max_object_depth: Max depth for object serialization (None = no limit).\n"
      "    timeout: Timeout per sample in seconds.\n\n"
      "Raises:\n"
      "    RuntimeError: If sampling is already active.");

  // Expose SnapshotReader class and types
  py::class_<snapshot::LocalVariable>(m, "LocalVariable")
      .def_readonly("name", &snapshot::LocalVariable::name)
      .def_readonly("python_id", &snapshot::LocalVariable::pythonId);

  py::class_<snapshot::Frame>(m, "Frame", py::dynamic_attr())
      .def_readonly("file_path", &snapshot::Frame::filePath)
      .def_readonly("original_file_path", &snapshot::Frame::originalFilePath)
      .def_readonly("function_name", &snapshot::Frame::functionName)
      .def_readonly("function_qualname", &snapshot::Frame::functionQualName)
      .def_readonly("line_number", &snapshot::Frame::lineNumber)
      .def_readonly("_local_variables", &snapshot::Frame::localVariables)
      .def(
          "get_locals",
          [](const py::object& self) -> py::dict {
            auto& frame = self.cast<snapshot::Frame&>();
            py::object stacktracePy = self.attr("_stacktrace");
            py::object snapshotPy = stacktracePy.attr("_snapshot");
            auto& reader =
                snapshotPy.attr("_reader").cast<snapshot::SnapshotReader&>();
            auto& objectMap = snapshotPy.cast<snapshot::Snapshot&>().objectMap;
            py::dict result;
            for (const auto& var : frame.localVariables) {
              result[py::str(var.name)] =
                  reader.getPythonObject(var.pythonId, objectMap);
            }
            return result;
          },
          "Get a dictionary mapping local variable names to their Python "
          "objects.");

  py::class_<snapshot::Stacktrace>(m, "Stacktrace", py::dynamic_attr())
      .def_readonly("id", &snapshot::Stacktrace::id)
      .def_readonly("truncated", &snapshot::Stacktrace::truncated)
      .def_readonly(
          "object_depth_truncated", &snapshot::Stacktrace::objectDepthTruncated)
      .def_readonly("thread_name", &snapshot::Stacktrace::threadName)
      .def_property_readonly(
          "frames",
          [](const py::object& self) -> py::object {
            // Return cached wired frames if available
            if (py::hasattr(self, "_frames")) {
              return self.attr("_frames");
            }
            // Fallback to C++ frames (no wiring)
            auto& st = self.cast<snapshot::Stacktrace&>();
            return py::cast(st.frames);
          })
      .def_readonly("exception_object", &snapshot::Stacktrace::exceptionObject)
      .def_property_readonly(
          "cause_id",
          [](const py::object& self) -> py::object {
            auto& st = self.cast<snapshot::Stacktrace&>();
            if (st.causeId == st.id) {
              return py::none();
            }
            return py::int_(st.causeId);
          })
      .def_property_readonly(
          "context_id",
          [](const py::object& self) -> py::object {
            auto& st = self.cast<snapshot::Stacktrace&>();
            if (st.contextId == st.id) {
              return py::none();
            }
            return py::int_(st.contextId);
          })
      .def(
          "get_cause",
          [](const py::object& self) -> py::object {
            if (py::hasattr(self, "_cause")) {
              return self.attr("_cause");
            }
            auto& st = self.cast<snapshot::Stacktrace&>();
            if (st.causeId == st.id) {
              self.attr("_cause") = py::none();
              return py::none();
            }
            py::object snapshotPy = self.attr("_snapshot");
            py::dict stacktraces = snapshotPy.attr("stacktraces");
            py::object key = py::int_(st.causeId);
            if (stacktraces.contains(key)) {
              py::object cause = stacktraces[key];
              self.attr("_cause") = cause;
              return cause;
            }
            self.attr("_cause") = py::none();
            return py::none();
          },
          "Get the __cause__ stacktrace, or None if there is no cause.")
      .def(
          "get_context",
          [](const py::object& self) -> py::object {
            if (py::hasattr(self, "_context")) {
              return self.attr("_context");
            }
            auto& st = self.cast<snapshot::Stacktrace&>();
            if (st.contextId == st.id) {
              self.attr("_context") = py::none();
              return py::none();
            }
            py::object snapshotPy = self.attr("_snapshot");
            py::dict stacktraces = snapshotPy.attr("stacktraces");
            py::object key = py::int_(st.contextId);
            if (stacktraces.contains(key)) {
              py::object context = stacktraces[key];
              self.attr("_context") = context;
              return context;
            }
            self.attr("_context") = py::none();
            return py::none();
          },
          "Get the __context__ stacktrace, or None if there is no context.")
      .def(
          "get_traceback",
          [](const py::object& self) -> py::object {
            auto& stacktrace = self.cast<snapshot::Stacktrace&>();
            if (stacktrace.frames.empty()) {
              return py::none();
            }
            py::module_ snapshot_utils =
                py::module_::import("tintype._traceback");
            py::object gen_fn =
                snapshot_utils.attr("_generate_traceback_from_stacktrace");
            py::object result = gen_fn(self);
            if (result.is_none()) {
              return result;
            }

            // Fix up f_back chain: each frame's f_back should point to the
            // previous frame in the traceback chain (outermost to innermost).
            // Use the first frame (which has a non-null f_back from exec)
            // to detect the struct offset.
            auto* tb = reinterpret_cast<PyTracebackObject*>(result.ptr());
            if (tb != nullptr) {
              detectFBackOffset(tb->tb_frame);
            }
            PyFrameObject* prevFrame = nullptr;
            while (tb != nullptr) {
              setFrameFBack(tb->tb_frame, prevFrame);
              prevFrame = tb->tb_frame;
              tb = tb->tb_next;
            }

            return result;
          },
          "Generate a Python traceback from this stacktrace's frames.");

  py::class_<snapshot::Snapshot>(m, "Snapshot", py::dynamic_attr())
      .def_readonly("timestamp", &snapshot::Snapshot::timestamp)
      .def_readonly("truncated", &snapshot::Snapshot::truncated)
      .def(
          "get_prev_snapshot",
          [](const py::object& self) -> py::object {
            if (py::hasattr(self, "_prev_snapshot")) {
              return self.attr("_prev_snapshot");
            }
            auto& snap = self.cast<snapshot::Snapshot&>();
            if (snap.prevSnapshotPos == 0) {
              self.attr("_prev_snapshot") = py::none();
              return py::none();
            }
            py::object readerObj = self.attr("_reader");
            auto& reader = readerObj.cast<snapshot::SnapshotReader&>();
            auto prevSnap = reader.getSnapshotAtPosition(snap.prevSnapshotPos);
            py::object prevSnapPy = py::cast(std::move(prevSnap));
            wireSnapshot(prevSnapPy, readerObj);
            linkAdjacentSnapshots(prevSnapPy, self);
            return prevSnapPy;
          },
          "Get the previous snapshot, or None if this is the first snapshot.")
      .def(
          "get_next_snapshot",
          [](const py::object& self) -> py::object {
            if (py::hasattr(self, "_next_snapshot")) {
              return self.attr("_next_snapshot");
            }
            auto& snap = self.cast<snapshot::Snapshot&>();
            py::object readerObj = self.attr("_reader");
            auto& reader = readerObj.cast<snapshot::SnapshotReader&>();

            auto header = reader.getHeader();
            if (header.lastSnapshotPos == 0 ||
                snap.position == header.lastSnapshotPos) {
              // Self is the latest (or no snapshots) — no next.
              // Don't cache None: new snapshots may be appended later
              // (borrowed readers).
              return py::none();
            }

            // Walk backwards from the latest snapshot. As we go, link
            // adjacent pairs so both get_prev/get_next are O(1) cache hits.
            auto latestSnap =
                reader.getSnapshotAtPosition(header.lastSnapshotPos);
            py::object prevPy = py::cast(std::move(latestSnap));
            wireSnapshot(prevPy, readerObj);
            // Don't cache _next_snapshot=None on latest: it may gain
            // a successor if new snapshots are appended.

            while (true) {
              auto& cur = prevPy.cast<snapshot::Snapshot&>();
              if (cur.prevSnapshotPos == 0) {
                // Reached the beginning without finding self
                break;
              }
              if (cur.prevSnapshotPos == snap.position) {
                // cur's predecessor is self — cur is the next snapshot
                linkAdjacentSnapshots(self, prevPy);
                return prevPy;
              }

              auto intermediate =
                  reader.getSnapshotAtPosition(cur.prevSnapshotPos);
              py::object intermediatePy = py::cast(std::move(intermediate));
              wireSnapshot(intermediatePy, readerObj);
              linkAdjacentSnapshots(intermediatePy, prevPy);
              prevPy = intermediatePy;
            }

            // Self not found in the chain (shouldn't happen for valid
            // snapshots)
            return py::none();
          },
          "Get the next snapshot, or None if this is the most recent "
          "snapshot.")
      .def_property_readonly(
          "stacktraces",
          [](const py::object& self) -> py::object {
            // Return cached wired stacktraces if available
            if (py::hasattr(self, "_stacktraces")) {
              return self.attr("_stacktraces");
            }
            // Fallback to C++ stacktraces (no wiring)
            auto& snap = self.cast<snapshot::Snapshot&>();
            return py::cast(snap.stacktraces);
          })
      .def(
          "frames",
          [](const py::object& self) -> py::object {
            auto& snap = self.cast<snapshot::Snapshot&>();
            if (snap.stacktraces.empty()) {
              return py::list();
            }
            // Return frames from the first stacktrace (which has wiring)
            py::object stacktraces = self.attr("stacktraces");
            py::dict stDict = stacktraces.cast<py::dict>();
            auto it = stDict.begin();
            py::object firstSt =
                py::reinterpret_borrow<py::object>((*it).second);
            return firstSt.attr("frames");
          })
      .def_readonly("object_map", &snapshot::Snapshot::objectMap)
      .def(
          "get_python_object",
          [](py::object self, uint64_t pythonId) -> py::object {
            auto& snap = self.cast<snapshot::Snapshot&>();
            auto& reader =
                self.attr("_reader").cast<snapshot::SnapshotReader&>();
            return reader.getPythonObject(pythonId, snap.objectMap);
          },
          py::arg("python_id"),
          "Get a Python object from the heap by pythonId. Uses this "
          "snapshot's object map to resolve the ID.");

  py::class_<snapshot::SourceFile>(m, "SourceFile")
      .def_readonly("path", &snapshot::SourceFile::path)
      .def_readonly("content", &snapshot::SourceFile::content);

  py::class_<snapshot::SnapshotReader>(m, "SnapshotReader")
      .def(py::init<const std::string&>(), py::arg("path"))
      .def("get_last_error", &snapshot::SnapshotReader::getLastError)
      .def(
          "snapshot_count",
          [](const snapshot::SnapshotReader& reader) -> uint32_t {
            return reader.getHeader().snapshotCount;
          },
          "Get the number of snapshot records in the file.")
      .def("get_metadata", &snapshot::SnapshotReader::getMetadata)
      .def("get_manifest", &snapshot::SnapshotReader::getManifest)
      .def("get_environment", &snapshot::SnapshotReader::getEnvironment)
      .def(
          "get_latest_snapshot",
          [](const py::object& self) -> py::object {
            auto& reader = self.cast<snapshot::SnapshotReader&>();
            auto result = reader.getLatestSnapshot();
            if (!result) {
              return py::none();
            }
            py::object snapPy = py::cast(std::move(*result));
            wireSnapshot(snapPy, self);
            return snapPy;
          })
      .def(
          "get_snapshot_at_index",
          [](const py::object& self, size_t index) -> py::object {
            auto& reader = self.cast<snapshot::SnapshotReader&>();
            auto result = reader.getSnapshotAtIndex(index);
            if (!result) {
              return py::none();
            }
            py::object snapPy = py::cast(std::move(*result));
            wireSnapshot(snapPy, self);
            return snapPy;
          },
          py::arg("index"))
      .def(
          "get_all_snapshots",
          [](const py::object& self) -> py::list {
            auto& reader = self.cast<snapshot::SnapshotReader&>();
            auto snapshots = reader.getAllSnapshots();
            py::list result;
            py::object prevSnapPy;
            for (auto& snap : snapshots) {
              py::object snapPy = py::cast(std::move(snap));
              wireSnapshot(snapPy, self);
              if (prevSnapPy) {
                linkAdjacentSnapshots(prevSnapPy, snapPy);
              } else {
                snapPy.attr("_prev_snapshot") = py::none();
              }
              result.append(snapPy);
              prevSnapPy = snapPy;
            }
            // Don't cache _next_snapshot=None on the last snapshot:
            // new snapshots may be appended later (borrowed readers).
            return result;
          })
      .def("get_all_source_files", &snapshot::SnapshotReader::getAllSourceFiles)
      .def(
          "get_extracted_files_dir",
          &snapshot::SnapshotReader::getExtractedFilesDir,
          "Get the path to the temporary directory containing extracted source "
          "files.")
      .def(
          "get_working_file_path",
          [](const snapshot::SnapshotReader& reader) -> py::object {
            const auto& path = reader.getWorkingFilePath();
            if (path.empty()) {
              return py::none();
            }
            return py::cast(path);
          },
          "Get the path to the file that is mmapped by this reader. "
          "For file-based readers, this is the temporary decompressed file. "
          "For borrowed-memory readers, this is the writer's backing file. "
          "Returns None if no path is available.")
      .def(
          "_read_raw_object",
          [](const snapshot::SnapshotReader& reader,
             uint64_t offset) -> py::object {
            // Check for magic offsets first
            if (snapshot::SnapshotReader::isMagicOffset(offset)) {
              auto magicType =
                  snapshot::SnapshotReader::getMagicOffsetType(offset);
              if (magicType == "None") {
                return py::none();
              } else if (magicType == "True") {
                return py::bool_(true);
              } else if (magicType == "False") {
                return py::bool_(false);
              }
            }

            auto obj = reader.readObject(offset);
            if (!obj) {
              return py::none();
            }

            py::dict result;
            result["type"] = static_cast<int>(obj->type);
            result["type_name"] = obj->typeName;
            result["repr"] = obj->repr;

            // Convert value based on type
            std::visit(
                [&result](auto&& arg) {
                  using T = std::decay_t<decltype(arg)>;
                  if constexpr (std::is_same_v<T, std::nullptr_t>) {
                    result["value"] = py::none();
                  } else if constexpr (std::is_same_v<T, bool>) {
                    result["value"] = arg;
                  } else if constexpr (std::is_same_v<T, int64_t>) {
                    result["value"] = arg;
                  } else if constexpr (std::is_same_v<T, double>) {
                    result["value"] = arg;
                  } else if constexpr (std::is_same_v<T, std::string>) {
                    result["value"] = arg;
                  } else if constexpr (std::
                                           is_same_v<T, std::vector<uint8_t>>) {
                    result["value"] = py::bytes(
                        reinterpret_cast<const char*>(arg.data()), arg.size());
                  } else if constexpr (std::is_same_v<
                                           T,
                                           std::vector<uint64_t>>) {
                    result["value"] = arg;
                  } else if constexpr (
                      std::is_same_v<
                          T,
                          std::vector<std::pair<uint64_t, uint64_t>>>) {
                    py::list pairs;
                    for (const auto& [k, v] : arg) {
                      pairs.append(py::make_tuple(k, v));
                    }
                    result["value"] = pairs;
                  } else if constexpr (std::is_same_v<
                                           T,
                                           std::shared_ptr<
                                               snapshot::DeserializedObject>>) {
                    result["value"] = py::none();
                  }
                },
                obj->value);

            // Add attributes if present
            if (!obj->attributes.empty()) {
              py::list attrs;
              for (const auto& [nameId, valueId] : obj->attributes) {
                attrs.append(py::make_tuple(nameId, valueId));
              }
              result["attributes"] = attrs;
            }

            return result;
          },
          py::arg("offset"))
      .def_static(
          "is_magic_offset",
          &snapshot::SnapshotReader::isMagicOffset,
          py::arg("offset"))
      .def_static(
          "get_magic_offset_type",
          &snapshot::SnapshotReader::getMagicOffsetType,
          py::arg("offset"))
      .def(
          "get_python_object",
          &snapshot::SnapshotReader::getPythonObject,
          py::arg("python_id"),
          py::arg("object_map"),
          "Get a Python object from the heap by pythonId. Uses the object map "
          "to resolve pythonId to heap offset. Returns primitives directly and "
          "creates SerializedObject/SerializedDict/SerializedList for complex "
          "types.")
      .def(
          "get_stats",
          &snapshot::SnapshotReader::getStats,
          "Get statistics as a dictionary. For file-based readers, reads stats "
          "from the file's statistics section. For borrowed-memory readers "
          "(returned by initialize()), returns live stats.");
}

} // namespace facebook::tintype
