// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#pragma once

#include <pybind11/pybind11.h>

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <variant>
#include <vector>

#include "SnapshotStats.h"
#include "SnapshotWriter.h"

namespace py = pybind11;

namespace facebook::tintype::snapshot {

/**
 * Represents a local variable in a frame.
 */
struct LocalVariable {
  std::string name;
  uint64_t pythonId;
};

/**
 * Represents a stack frame in a snapshot.
 */
struct Frame {
  std::string filePath; // Full path to extracted file in temp directory
  std::string
      originalFilePath; // Original file path as recorded at capture time
  std::string functionName;
  std::string functionQualName;
  uint32_t lineNumber;
  std::vector<LocalVariable> localVariables;
};

/**
 * Represents a stacktrace in a snapshot.
 * A stacktrace can represent either a thread's call stack or an exception's
 * traceback.
 */
struct Stacktrace {
  uint64_t
      id; // Stacktrace ID (thread ID for threads, unique ID for exceptions)
  std::vector<Frame> frames;
  py::object exceptionObject; // Deserialized exception Python object, or None
                              // if this is a thread stacktrace
  uint64_t causeId; // ID of stacktrace for __cause__ (equals id if none)
  uint64_t contextId; // ID of stacktrace for __context__ (equals id if none)
  bool truncated = false; // True if this stacktrace was truncated
  bool objectDepthTruncated = false; // True if object depth limit was reached
  std::string threadName; // Name of the thread (from threading module), empty
                          // if unknown or for exception stacktraces
};

/**
 * Represents a snapshot record.
 */
struct Snapshot {
  uint64_t timestamp;
  uint64_t
      prevSnapshotPos; // Absolute position of previous snapshot (0 if first)
  uint64_t position = 0; // Absolute position of this snapshot in the file
  std::map<uint64_t, Stacktrace>
      stacktraces; // stacktrace ID -> Stacktrace, ordered by ID
  std::unordered_map<uint64_t, uint64_t>
      objectMap; // pythonId -> heap offset (relative to heap start)
  bool truncated = false; // True if this snapshot was truncated

  // Convenience accessor for single-stacktrace snapshots
  // Returns frames from the first stacktrace, or empty vector if no stacktraces
  const std::vector<Frame>& frames() const {
    static const std::vector<Frame> empty;
    return stacktraces.empty() ? empty : stacktraces.begin()->second.frames;
  }
};

/**
 * Represents a source file entry.
 */
struct SourceFile {
  std::string path;
  std::string content;
};

/**
 * Represents the value of a deserialized object.
 */
struct DeserializedObject;

using ObjectValue = std::variant<
    std::nullptr_t, // None
    bool, // Bool
    int64_t, // Int64
    std::string, // IntBignum as string, String
    double, // Float
    std::vector<uint8_t>, // Bytes
    std::vector<uint64_t>, // List/Tuple/Set element IDs
    std::vector<std::pair<uint64_t, uint64_t>>, // Dict key-value IDs
    std::shared_ptr<DeserializedObject> // SerializedObject and subclasses
    >;

/**
 * Represents a deserialized object from the heap.
 */
struct DeserializedObject {
  ObjectType type;
  ObjectValue value;
  std::string typeName; // For SerializedObject types
  std::string repr; // For SerializedObject types
  std::vector<std::pair<uint64_t, uint64_t>>
      attributes; // For SerializedObject types
};

/**
 * Class for reading snapshot files.
 */
class SnapshotReader {
 public:
  /**
   * Construct a SnapshotReader and open the given snapshot file.
   * Decompresses the file and mmaps it into memory.
   * @param path Path to the compressed snapshot file.
   * @throws std::runtime_error if the file cannot be opened or is invalid.
   */
  explicit SnapshotReader(const std::string& path);

  /**
   * Construct a SnapshotReader that borrows memory from an existing buffer.
   * The caller (typically SnapshotWriter) retains ownership of the memory.
   * The reader will NOT munmap or free the data on destruction.
   * @param data Pointer to the memory buffer (must remain valid for the
   *        lifetime of this reader).
   * @param dataSize Size of the memory buffer in bytes.
   * @param filePath Optional path to the underlying file backing the buffer.
   * @throws std::runtime_error if the data is invalid.
   */
  SnapshotReader(void* data, size_t dataSize, const std::string& filePath = "");

  ~SnapshotReader();

  // Delete copy operations
  SnapshotReader(const SnapshotReader&) = delete;
  SnapshotReader& operator=(const SnapshotReader&) = delete;

  // Allow move operations
  SnapshotReader(SnapshotReader&& other) noexcept;
  SnapshotReader& operator=(SnapshotReader&& other) noexcept;

  /**
   * Get the last error message.
   * Returns empty string if no error occurred.
   */
  const std::string& getLastError() const;

  /**
   * Get the file header (read directly from the mmap'd buffer).
   */
  FileHeader getHeader() const;

  /**
   * Get the metadata JSON string.
   */
  std::string getMetadata() const;

  /**
   * Get the manifest JSON string (from __manifest__.json).
   * Returns empty string if no manifest was captured.
   */
  std::string getManifest() const;

  /**
   * Get the environment variables as a map of key-value pairs.
   * Returns an empty map if no environment was captured.
   */
  std::unordered_map<std::string, std::string> getEnvironment() const;

  /**
   * Get the most recent snapshot.
   */
  std::optional<Snapshot> getLatestSnapshot();

  /**
   * Get a snapshot by its chronological index (0 = first/oldest snapshot).
   * Returns nullopt if the index is out of range.
   */
  std::optional<Snapshot> getSnapshotAtIndex(size_t index);

  /**
   * Get all snapshots in chronological order (oldest first).
   */
  std::vector<Snapshot> getAllSnapshots();

  /**
   * Get a snapshot at a specific byte position in the file.
   * Used by the Python bindings for Snapshot.get_prev_snapshot().
   * @param position Absolute byte offset of the snapshot record.
   */
  Snapshot getSnapshotAtPosition(uint64_t position);

  /**
   * Get all source files.
   */
  std::vector<SourceFile> getAllSourceFiles() const;

  /**
   * Read an object from the heap by offset.
   * The offset is relative to the object heap start (FileHeader.objectHeapPos).
   * Returns nullopt for magic offsets (None, True, False).
   */
  std::optional<DeserializedObject> readObject(uint64_t offset) const;

  /**
   * Get a Python object from the heap by pythonId.
   * Uses the provided object map to resolve pythonId to heap offset.
   * Maintains an internal cache to handle cycles and avoid recreating objects.
   * The cache persists for the lifetime of the reader since the object heap
   * is immutable.
   *
   * For primitive types (int, float, str, bytes, bool, None), returns the
   * corresponding Python primitive.
   *
   * For complex types:
   * - SerializedObject -> snapshot.SerializedObject
   * - SerializedDict -> snapshot.SerializedDictObject
   * - SerializedList/SerializedSet/SerializedTuple ->
   *   snapshot.SerializedListObject
   * - List/Tuple/Set -> Python list/tuple/set with resolved elements
   * - Dict -> Python dict with resolved key-value pairs
   *
   * @param pythonId The Python object ID to look up.
   * @param objectMap Map from pythonId to heap offset for resolving references.
   * @return The Python object, or None if the pythonId is not found.
   */
  py::object getPythonObject(
      uint64_t pythonId,
      const std::unordered_map<uint64_t, uint64_t>& objectMap);

  /**
   * Update the memory pointer and size for a borrowed-memory reader.
   * Called by SnapshotWriter after mremap when the file is extended,
   * or with (nullptr, 0) to invalidate the reader on finalize.
   * @param data New pointer to the memory buffer.
   * @param dataSize New size of the memory buffer.
   */
  void updateMemory(void* data, size_t dataSize);

  /**
   * Get the path to the temporary directory containing extracted source files.
   * Returns empty string if no files were extracted or file is not open.
   */
  const std::string& getExtractedFilesDir() const;

  /**
   * Get the path to the file that is mmapped by this reader.
   * For file-based readers, this is the temporary decompressed file.
   * For borrowed-memory readers, this is the file path provided at
   * construction (typically the writer's temp file path).
   * Returns empty string if no path is available.
   */
  const std::string& getWorkingFilePath() const;

  /**
   * Check if an offset is a magic offset (None, True, False).
   * Magic offsets are at the high end of uint64 range (>= UINT64_MAX - 2).
   */
  static bool isMagicOffset(uint64_t offset);

  /**
   * Get the type of magic offset (None, True, or False).
   * Returns nullopt if not a magic offset.
   */
  static std::optional<std::string> getMagicOffsetType(uint64_t offset);

  /**
   * Get statistics from the snapshot file or the stats collector singleton.
   * For file-based readers, reads stats from the statistics section of the
   * file. For borrowed-memory readers, returns stats from the
   * SnapshotStatsCollector singleton.
   * @return Map of stat name to value (all values in nanoseconds or counts).
   */
  std::map<std::string, uint64_t> getStats() const;

 private:
  // Open a snapshot file for reading (called by constructor)
  bool open(const std::string& path);

  // Initialize from an existing memory buffer (called by borrowed-memory
  // constructor)
  bool initFromMemory(void* data, size_t dataSize);

  // Close the snapshot file and release resources (called by destructor)
  void close();

  // Check if a file is currently open
  bool isOpen() const;

  // Read a snapshot at a given offset
  Snapshot readSnapshotAt(uint64_t offset);

  // Read a frame at a given offset, returns the frame and bytes consumed
  std::pair<Frame, size_t> readFrameAt(size_t offset) const;

  // Read a source file at a given offset, returns the file and bytes consumed
  std::pair<SourceFile, size_t> readSourceFileAt(size_t offset) const;

  // Extract source files from file table to a temporary directory
  bool extractSourceFiles();

  // Recursively remove a directory and its contents
  static void removeDirectory(const std::string& path);

  // Helper to read raw data from the mmap'd buffer
  template <typename T>
  T readValue(size_t offset) const;

  // Helper to read a string from the mmap'd buffer
  std::string readString(size_t offset, size_t length) const;

  // Re-mmap the file if it has grown (file-based uncompressed readers only).
  // Called by readValue/readString when an offset is out of bounds.
  bool remapIfGrown() const;

  // Pointer to mmap'd data
  void* data_ = nullptr;

  // Size of mmap'd data
  size_t dataSize_ = 0;

  // File descriptor for the underlying file (kept open for uncompressed
  // file-based readers so we can re-mmap when the file grows)
  int fileFd_ = -1;

  // File descriptor for the temporary file
  int tempFd_ = -1;

  // Path to the temporary file
  std::string tempFilePath_;

  // Path to the temporary directory containing extracted source files
  std::string extractedFilesDir_;

  // Maps original file path to extracted file path (for files extracted to temp
  // dir)
  std::unordered_map<std::string, std::string> extractedFilePathMap_;

  // Whether file is open
  bool isOpen_ = false;

  // Whether this reader owns the mmap'd data (false when borrowing from writer)
  bool ownsData_ = true;

  // Last error message
  std::string lastError_;

  // Object cache: maps heap offset to Python object
  // Used to handle cycles and avoid recreating objects
  std::unordered_map<uint64_t, py::object> objectCache_;
};

} // namespace facebook::tintype::snapshot
