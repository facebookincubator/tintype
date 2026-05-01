// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

#include "compat.h"

#include "SnapshotStats.h"
#include "SnapshotTypes.h"

namespace facebook::tintype::snapshot {

// Forward declaration
class SnapshotReader;

// ============================================================================
// File Format Structures
// ============================================================================

// All on-disk structs are packed to eliminate compiler-inserted padding.
// This ensures the binary format is portable across platforms with different
// alignment requirements (e.g., files written on Linux can be read on macOS).
// Packing means some fields may be misaligned within the struct; this is fine
// on our target platforms (x86-64, ARM64) which handle unaligned access
// natively. The reader uses memcpy to load structs from the mmap'd buffer,
// which avoids strict-aliasing violations from casting raw pointers to struct
// pointers.
#pragma pack(push, 1)

/**
 * File header - written at the start of the file.
 * All positions (Pos) are absolute byte offsets from the start of the
 * uncompressed data. All offsets (Offset) are relative to a section start
 * (e.g., object heap offsets are relative to the object heap start).
 */
struct FileHeader {
  uint32_t magic; // Magic number (kMagicNumber)
  uint32_t version; // Format version (kFormatVersion)
  uint64_t lastSnapshotPos; // Absolute position of the last snapshot header
  uint32_t snapshotCount; // Number of snapshot records in the file
  uint64_t objectHeapPos; // Absolute position of the object heap start
  uint64_t fileTablePos; // Absolute position of the file table start
  uint32_t fileTableCount; // Number of entries in the file table
  uint64_t envPos; // Absolute position of the environment section
  uint64_t envSize; // Size of environment data in bytes (0 if none)
  uint64_t manifestPos; // Absolute position of the manifest section
  uint64_t manifestSize; // Size of manifest JSON in bytes
  uint64_t metadataPos; // Absolute position of the metadata section
  uint64_t metadataSize; // Size of metadata in bytes
  uint64_t statsPos; // Absolute position of the statistics section (0 if none)
  uint32_t statsCount; // Number of statistics entries (0 if none)
};

/**
 * Snapshot record header - written at the start of each snapshot.
 * The snapshot record layout is:
 *   SnapshotRecordHeader
 *   StacktraceRecordHeader[stacktraceCount] (each followed by its frame
 * records) ObjectMapRecord[objectMapCount] (at objectMapPos)
 */
struct SnapshotRecordHeader {
  uint64_t timestamp; // Unix timestamp in microseconds
  uint64_t
      prevSnapshotPos; // Absolute position of previous snapshot (0 if first)
  uint32_t stacktraceCount; // Number of stacktrace records following
  uint64_t objectMapPos; // Absolute position of object map table
  uint32_t objectMapCount; // Number of entries in the object map table
  uint8_t flags; // Flags (kSnapshotTruncated = 0x01)
};

/**
 * Stacktrace record header - one per stacktrace in a snapshot.
 * A stacktrace can represent either a thread's call stack or an exception's
 * traceback. Each stacktrace contains a sequence of frame records.
 *
 * For thread stacktraces:
 *   - id is the thread ID (0 for main thread)
 *   - exceptionPythonId is 0 (no exception)
 *   - causeId and contextId equal id (self-reference means none)
 *
 * For exception stacktraces:
 *   - id is a unique identifier for this exception stacktrace
 *   - exceptionPythonId is the Python id() of the exception object
 *   - causeId references the stacktrace for __cause__ (equals id if none)
 *   - contextId references the stacktrace for __context__ (equals id if none)
 */
struct StacktraceRecordHeader {
  uint64_t
      id; // Stacktrace ID (thread ID for threads, unique ID for exceptions)
  uint32_t frameCount; // Number of frame records following for this stacktrace
  uint64_t exceptionPythonId; // Python id() of exception object (0 if thread)
  uint64_t causeId; // ID of stacktrace for __cause__ (equals id if none)
  uint64_t contextId; // ID of stacktrace for __context__ (equals id if none)
  uint8_t flags; // Flags (kStacktraceTruncated = 0x01)
  uint32_t threadNameLength; // Length of thread name string following header
};

/**
 * Object map record - maps a Python object id to its location in the heap.
 * The object map table appears at the end of each snapshot record.
 * The objectHeapOffset is relative to the object heap start
 * (FileHeader.objectHeapPos).
 */
struct ObjectMapRecord {
  uint64_t pythonId; // Python id() of the object
  uint64_t objectHeapOffset; // Offset relative to object heap start
};

/**
 * Frame record - one per frame in the traceback.
 * The file path is stored directly as a length-prefixed string,
 * so snapshots can be read before the file table is finalized.
 */
struct FrameRecordHeader {
  uint32_t filePathLength; // Length of file path string
  // Followed by: file path bytes (filePathLength)
  uint32_t coNameLength; // Length of co_name string
  // Followed by: co_name bytes (coNameLength)
  uint32_t coQualNameLength; // Length of co_qualname string
  // Followed by: co_qualname bytes (coQualNameLength)
  uint32_t lineNumber; // Line number in source file
  uint32_t localVarCount; // Number of local variable records
  // Followed by: LocalVariableRecord[localVarCount]
};

/**
 * Local variable record - one per local variable in a frame.
 * The objectHeapOffset can be looked up in the snapshot's object map
 * using the pythonId.
 */
struct LocalVariableRecord {
  uint64_t pythonId; // Python id() of the object
  uint32_t nameLength; // Length of variable name
  // Followed by: variable name bytes (nameLength)
};

/**
 * Object heap record header - common header for all heap objects.
 */
struct ObjectHeapRecordHeader {
  ObjectType type; // Type of the object
  // Followed by: type-specific data
};

/**
 * File table record - one per source file referenced.
 */
struct FileTableRecordHeader {
  uint32_t pathLength; // Length of file path
  // Followed by: file path bytes (pathLength)
  uint32_t contentLength; // Length of file contents
  // Followed by: file content bytes (contentLength)
};

#pragma pack(pop)

// ============================================================================
// SnapshotWriter Class
// ============================================================================

/**
 * Singleton class for writing snapshot files.
 * Handles binary file format, mmap, compression, and serialization.
 */
class SnapshotWriter {
 public:
  // Initial size of the snapshot file (16 MB, extends via mremap on demand)
  static constexpr size_t kInitialFileSize = 16 * 1024 * 1024;

  // Maximum size of the snapshot file (16 GB)
  static constexpr size_t kMaxFileSize = 16ULL * 1024 * 1024 * 1024;

  // Magic number to identify the file format: "PYTB" (Python TraceBack)
  static constexpr uint32_t kMagicNumber = 0x50595442; // "PYTB" in ASCII

  // File format version
  static constexpr uint32_t kFormatVersion = 1;

  // Streaming compression buffer size (256 KB)
  static constexpr size_t kStreamingBufferSize = 256 * 1024;

  // Compression level (zstd range 1-22, 0 means no compression)
  static constexpr int kCompressionLevel = 3;

  // Block size for offset alignment (4 KB)
  static constexpr size_t kBlockSize = 4096;

  // Initial allocation for snapshot records section (1 MB, grows via
  // ensureSnapshotRecordSpaceUnlocked which relocates the object heap)
  static constexpr size_t kInitialSnapshotRecordsSize = 1024 * 1024;

  // Magic offset values for singleton objects (not written to heap)
  // These use high values at the end of uint64 range, which cannot be valid
  // heap offsets (the heap would need to be petabytes in size)
  static constexpr uint64_t kNoneOffset = UINT64_MAX - 2;
  static constexpr uint64_t kTrueOffset = UINT64_MAX - 1;
  static constexpr uint64_t kFalseOffset = UINT64_MAX;

  // Truncation flag bits for record headers
  static constexpr uint8_t kSnapshotTruncated = 0x01;
  static constexpr uint8_t kStacktraceTruncated = 0x01;
  static constexpr uint8_t kObjectDepthTruncated = 0x02;

  /**
   * Check if an offset is a magic offset (None, True, or False singleton).
   * @param offset The offset to check.
   * @return true if the offset is a magic offset, false otherwise.
   */
  static constexpr bool isMagicOffset(uint64_t offset) {
    return offset >= kNoneOffset;
  }

  /**
   * Get the singleton instance.
   */
  static SnapshotWriter& getInstance();

  // Delete copy and move operations for singleton
  SnapshotWriter(const SnapshotWriter&) = delete;
  SnapshotWriter& operator=(const SnapshotWriter&) = delete;
  SnapshotWriter(SnapshotWriter&&) = delete;
  SnapshotWriter& operator=(SnapshotWriter&&) = delete;

  /**
   * Initialize the snapshotter.
   * Creates and mmaps a temporary file for writing snapshot data.
   * @param collectStats If true, collect timing and performance statistics.
   *        Default is false for maximum performance.
   */
  void initialize(bool collectStats = false);

  /**
   * Finalize and clean up the snapshotter.
   * If path is non-empty, writes the file table and metadata, compresses the
   * snapshot data using zstd, and writes to the specified path. If path is
   * empty, discards the data.
   * In both cases, unmaps and closes the temporary file.
   * If stats collection was enabled, statistics are embedded in the output.
   * @param path Optional output file path for the compressed snapshot.
   * @param metadataJson Optional JSON string containing metadata to store.
   * @param compressionLevel Zstd compression level (1-22), or 0 to write
   *        uncompressed. Values outside 0-22 will throw.
   */
  void finalize(
      const std::string& path = "",
      const std::string& metadataJson = "{}",
      int compressionLevel = kCompressionLevel);

  /**
   * Set the current write offset in the snapshot file.
   * @param offset The offset in bytes.
   */
  void setWriteOffset(size_t offset);

  /**
   * Get the current write offset in the snapshot file.
   * @return The current write offset in bytes.
   */
  size_t getWriteOffset() const;

  /**
   * Check if the snapshotter is initialized.
   * @return true if initialized, false otherwise.
   */
  bool isInitialized() const;

  /**
   * Get the path to the uncompressed temporary file being written to.
   * This file can be opened by SnapshotReader before finalize() is called.
   * @return The temporary file path, or empty string if not initialized.
   */
  const std::string& getTempFilePath() const;

  /**
   * Get pointer to the mmap'd snapshot file.
   * @return Pointer to the snapshot file memory, or nullptr if not initialized.
   */
  void* getSnapshotFilePtr() const;

  /**
   * Get the size of the snapshot file.
   * @return Size of the snapshot file in bytes.
   */
  size_t getSnapshotFileSize() const;

  /**
   * Write a string object to the object heap.
   * Format: ObjectHeapRecordHeader + uint32_t length + string bytes
   * @param value The string value.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeString(const std::string& value);

  /**
   * Write a bytes object to the object heap.
   * Format: ObjectHeapRecordHeader + uint32_t length + byte data
   * @param value The bytes value.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeBytes(const std::string& value);

  /**
   * Write an integer object to the object heap.
   * Uses one of two ObjectType values based on the integer size:
   *   Int64: int64_t (8 bytes) - fits in signed 64-bit
   *   IntBignum: uint32_t length + decimal string bytes - arbitrary precision
   * @param value The integer as a decimal string.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeInt(const std::string& value);

  /**
   * Write a 64-bit integer object directly to the object heap.
   * This is more efficient than writeInt() for integers
   * that are known to fit in int64_t.
   * @param value The integer value.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeInt64(int64_t value);

  /**
   * Write a float object to the object heap.
   * Format: ObjectHeapRecordHeader + 8-byte IEEE 754 double
   * @param value The float value as a double.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeFloat(double value);

  /**
   * Write a list object to the object heap.
   * Format: ObjectHeapRecordHeader + uint32_t count + uint64_t[count] element
   * IDs
   * @param elementIds The Python id() of each element in the list.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeList(const std::vector<uint64_t>& elementIds);

  /**
   * Write a tuple object to the object heap.
   * Format: ObjectHeapRecordHeader + uint32_t count + uint64_t[count] element
   * IDs
   * @param elementIds The Python id() of each element in the tuple.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeTuple(const std::vector<uint64_t>& elementIds);

  /**
   * Write a set or frozenset object to the object heap.
   * Format: ObjectHeapRecordHeader + uint32_t count + uint64_t[count] element
   * IDs
   * @param elementIds The Python id() of each element in the set.
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeSet(const std::vector<uint64_t>& elementIds);

  /**
   * Write a dict object to the object heap.
   * Format: ObjectHeapRecordHeader + uint32_t count +
   *         (uint64_t keyId, uint64_t valueId)[count]
   * @param keyValueIds Pairs of (key Python id, value Python id).
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeDict(
      const std::vector<std::pair<uint64_t, uint64_t>>& keyValueIds);

  /**
   * Write a serialized object to the object heap.
   * This is a catch-all for objects that don't have a specific type handler.
   * Format: ObjectHeapRecordHeader + uint32_t typeNameLength + typeName bytes +
   *         uint32_t reprLength + repr bytes +
   *         uint32_t attrCount + (uint64_t nameId, uint64_t valueId)[attrCount]
   * @param typeName The fully qualified type name (e.g., "module.ClassName").
   * @param repr The repr() string representation of the object.
   * @param attrIds Pairs of (attribute name Python id, attribute value Python
   * id).
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeSerializedObject(
      const std::string& typeName,
      const std::string& repr,
      const std::vector<std::pair<uint64_t, uint64_t>>& attrIds);

  /**
   * Write a serialized list object to the object heap.
   * For subclasses of list that need both list data and object metadata.
   * Format: ObjectHeapRecordHeader +
   *         uint32_t elementCount + uint64_t[elementCount] element IDs +
   *         uint32_t typeNameLength + typeName bytes +
   *         uint32_t reprLength + repr bytes +
   *         uint32_t attrCount + (uint64_t nameId, uint64_t valueId)[attrCount]
   * @param elementIds The Python id() of each element in the list.
   * @param typeName The fully qualified type name (e.g., "module.MyList").
   * @param repr The repr() string representation of the object.
   * @param attrIds Pairs of (attribute name Python id, attribute value Python
   * id).
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeSerializedList(
      const std::vector<uint64_t>& elementIds,
      const std::string& typeName,
      const std::string& repr,
      const std::vector<std::pair<uint64_t, uint64_t>>& attrIds);

  /**
   * Write a serialized set object to the object heap.
   * For subclasses of set/frozenset that need both set data and object
   * metadata. Format: ObjectHeapRecordHeader + uint32_t elementCount +
   * uint64_t[elementCount] element IDs + uint32_t typeNameLength + typeName
   * bytes + uint32_t reprLength + repr bytes + uint32_t attrCount + (uint64_t
   * nameId, uint64_t valueId)[attrCount]
   * @param elementIds The Python id() of each element in the set.
   * @param typeName The fully qualified type name (e.g., "module.MySet").
   * @param repr The repr() string representation of the object.
   * @param attrIds Pairs of (attribute name Python id, attribute value Python
   * id).
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeSerializedSet(
      const std::vector<uint64_t>& elementIds,
      const std::string& typeName,
      const std::string& repr,
      const std::vector<std::pair<uint64_t, uint64_t>>& attrIds);

  /**
   * Write a serialized tuple object to the object heap.
   * For subclasses of tuple that need both tuple data and object metadata.
   * Format: ObjectHeapRecordHeader +
   *         uint32_t elementCount + uint64_t[elementCount] element IDs +
   *         uint32_t typeNameLength + typeName bytes +
   *         uint32_t reprLength + repr bytes +
   *         uint32_t attrCount + (uint64_t nameId, uint64_t valueId)[attrCount]
   * @param elementIds The Python id() of each element in the tuple.
   * @param typeName The fully qualified type name (e.g., "module.MyTuple").
   * @param repr The repr() string representation of the object.
   * @param attrIds Pairs of (attribute name Python id, attribute value Python
   * id).
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeSerializedTuple(
      const std::vector<uint64_t>& elementIds,
      const std::string& typeName,
      const std::string& repr,
      const std::vector<std::pair<uint64_t, uint64_t>>& attrIds);

  /**
   * Write a serialized dict object to the object heap.
   * For subclasses of dict that need both dict data and object metadata.
   * Format: ObjectHeapRecordHeader +
   *         uint32_t entryCount + (uint64_t keyId, uint64_t
   * valueId)[entryCount] + uint32_t typeNameLength + typeName bytes + uint32_t
   * reprLength + repr bytes + uint32_t attrCount + (uint64_t nameId, uint64_t
   * valueId)[attrCount]
   * @param keyValueIds Pairs of (key Python id, value Python id).
   * @param typeName The fully qualified type name (e.g., "module.MyDict").
   * @param repr The repr() string representation of the object.
   * @param attrIds Pairs of (attribute name Python id, attribute value Python
   * id).
   * @return The offset in the object heap where the object was written.
   */
  uint64_t writeSerializedDict(
      const std::vector<std::pair<uint64_t, uint64_t>>& keyValueIds,
      const std::string& typeName,
      const std::string& repr,
      const std::vector<std::pair<uint64_t, uint64_t>>& attrIds);

  /**
   * Begin a new snapshot record.
   * Must be called before writing frame records.
   * @return The offset where the snapshot record header is written.
   */
  uint64_t beginSnapshot();

  /**
   * Begin a new stacktrace record within the current snapshot.
   * Must be called between beginSnapshot() and endSnapshot().
   *
   * For thread stacktraces, call with just the id (thread ID) and optionally
   * the thread name. For exception stacktraces, provide the exception object
   * offset and optionally the cause/context IDs.
   *
   * @param id The stacktrace ID (thread ID for threads, unique ID for
   * exceptions).
   * @param threadName Name of the thread from Python's threading module. Empty
   * string if unknown or for exception stacktraces.
   * @param exceptionPythonId Python id() of the exception object (0 if thread).
   * @param causeId ID of stacktrace for __cause__. Use the same value as `id`
   * to indicate no cause (self-reference means none).
   * @param contextId ID of stacktrace for __context__. Use the same value as
   * `id` to indicate no context (self-reference means none).
   */
  void beginStacktrace(
      uint64_t id,
      const std::string& threadName = "",
      uint64_t exceptionPythonId = 0,
      uint64_t causeId = UINT64_MAX,
      uint64_t contextId = UINT64_MAX);

  /**
   * End the current stacktrace record.
   * Updates the stacktrace header with the frame count.
   * Must be called after all frame records for this stacktrace are written.
   * @param truncated If true, sets the kStacktraceTruncated flag.
   * @param objectDepthTruncated If true, sets the kObjectDepthTruncated flag.
   */
  void endStacktrace(bool truncated = false, bool objectDepthTruncated = false);

  /**
   * Discard the current stacktrace record.
   * Rolls back writeOffset_ to the stacktrace header position, resets
   * frame count, and does NOT increment stacktrace count. Heap is untouched.
   */
  void discardCurrentStacktrace();

  /**
   * Write a frame record to the current stacktrace.
   * Must be called between beginStacktrace() and endStacktrace().
   * @param filePath The source file path (used to look up file table offset).
   * @param functionName The function name (co_name).
   * @param functionQualName The qualified function name (co_qualname).
   * @param lineNumber The line number in the source file.
   * @param localVars Vector of (variable name, python id) pairs for local
   * variables.
   */
  void writeFrameRecord(
      const std::string& filePath,
      const std::string& functionName,
      const std::string& functionQualName,
      uint32_t lineNumber,
      const std::vector<std::pair<std::string, uint64_t>>& localVars);

  /**
   * End the current snapshot record.
   * Writes the object map table and updates the snapshot header.
   * Must be called after all frame records are written.
   * @param truncated If true, sets the kSnapshotTruncated flag.
   */
  void endSnapshot(bool truncated = false);

  /**
   * Discard the current snapshot record.
   * Rolls back writeOffset_ to the snapshot header position, clears
   * objectIdToOffset_, resets all snapshot state flags. Does NOT update
   * lastSnapshotPos_. Heap is untouched.
   */
  void discardCurrentSnapshot();

  /**
   * Roll back the write offset to a previously saved position.
   * Also decrements the frame count to account for the rolled-back frame.
   * @param savedOffset The offset to restore (from a prior getWriteOffset()).
   */
  void rollbackWriteOffset(size_t savedOffset);

  /**
   * Get the current frame count for the in-progress stacktrace.
   */
  uint32_t getCurrentFrameCount() const;

  /**
   * Get the current stacktrace count for the in-progress snapshot.
   */
  uint32_t getCurrentStacktraceCount() const;

  /**
   * Get the current object heap write offset.
   * @return The current write position in the object heap.
   */
  size_t getObjectHeapOffset() const;

  /**
   * Look up an object by its Python id.
   * @param pythonId The Python id() of the object.
   * @return The offset in the object heap if found, or 0 if not found.
   */
  uint64_t lookupObject(uint64_t pythonId) const;

  /**
   * Check if an object with the given Python id has been written.
   * @param pythonId The Python id() of the object.
   * @return true if the object has been written, false otherwise.
   */
  bool hasObject(uint64_t pythonId) const;

  /**
   * Look up an object by its Python id, returning both existence and offset.
   * This is faster than calling hasObject() + lookupObject() separately.
   * @param pythonId The Python id() of the object.
   * @param outOffset Output parameter for the offset if found.
   * @return true if found, false otherwise.
   */
  bool tryLookupObject(uint64_t pythonId, uint64_t& outOffset) const;

  /**
   * Register an object mapping from Python id to object heap offset.
   * This is called automatically by the write* methods.
   * @param pythonId The Python id() of the object.
   * @param offset The offset in the object heap.
   */
  void registerObject(uint64_t pythonId, uint64_t offset);

  /**
   * Check if an object with the given Python id and identity hash exists
   * in the global cache (cross-snapshot deduplication).
   * @param pythonId The Python id() of the object.
   * @param identityHash Hash of the object's structure (children IDs + size).
   * @return The heap offset if found and hash matches, or 0 if not
   * found/changed.
   */
  uint64_t lookupObjectWithHash(uint64_t pythonId, uint64_t identityHash) const;

  /**
   * Register an object with its identity hash for cross-snapshot deduplication.
   * @param pythonId The Python id() of the object.
   * @param offset The offset in the object heap.
   * @param identityHash Hash of the object's structure for change detection.
   */
  void registerObjectWithHash(
      uint64_t pythonId,
      uint64_t offset,
      uint64_t identityHash);

  /**
   * Register an object in both the current-snapshot cache and the global
   * cross-snapshot cache. This is faster than calling registerObject() and
   * registerObjectWithHash() separately because it uses a single lock.
   * @param pythonId The Python id() of the object.
   * @param offset The offset in the object heap.
   * @param identityHash Hash of the object's structure for change detection.
   */
  void registerObjectWithBothCaches(
      uint64_t pythonId,
      uint64_t offset,
      uint64_t identityHash);

  /**
   * Add a file path to the set of referenced files.
   * These files will be included in the file table during finalize.
   * @param filePath The absolute path to the source file.
   */
  void addReferencedFile(const std::string& filePath);

  /**
   * Get the current statistics.
   * @return A copy of the current statistics.
   */
  SnapshotStats getStats() const;

  /**
   * Reset statistics to zero.
   * Called automatically by initialize().
   */
  void resetStats();

  /**
   * Set a borrowed reader that shares this writer's memory.
   * The writer will update the reader's memory pointer and size
   * whenever the file is extended via mremap.
   * @param reader Pointer to the SnapshotReader, or nullptr to clear.
   */
  void setBorrowedReader(SnapshotReader* reader);

 private:
  SnapshotWriter() = default;
  ~SnapshotWriter() = default;

  // Helper to align an offset to the next block boundary
  static size_t alignToBlock(size_t offset);

  // Write data to the mmap'd file at writeOffset_ and advance the offset
  void writeData(const void* data, size_t size);

  // Write the initial file header with placeholder offsets
  void writeInitialHeader();

  // Helper to check initialization and space availability (must hold mutex)
  // Will extend the file if needed.
  void ensureHeapSpaceUnlocked(size_t requiredSpace);

  // Helper to ensure snapshot records section has enough space (must hold
  // mutex) If writeOffset_ + bytesNeeded would overlap the object heap, extends
  // the file and relocates the object heap forward.
  void ensureSnapshotRecordSpaceUnlocked(size_t bytesNeeded);

  // Helper to extend the file size using mremap (must hold mutex)
  // Returns true on success, false on failure.
  bool extendFileUnlocked(size_t newSize);

  // Helper to extend the file to accommodate at least requiredSize bytes.
  // Doubles currentFileSize_ repeatedly until it fits, capped at kMaxFileSize.
  // Returns true on success, false if the required size exceeds kMaxFileSize.
  // Must hold mutex.
  bool extendToFitUnlocked(size_t requiredSize);

  // Helper to write object header and return starting offset (must hold mutex)
  uint64_t writeObjectHeaderUnlocked(ObjectType type);

  // Helper to write raw bytes to object heap (must hold mutex)
  void writeToHeapUnlocked(const void* data, size_t size);

  // Helper to write a uint32_t to object heap (must hold mutex)
  void writeUint32ToHeapUnlocked(uint32_t value);

  // Helper to write a string (length + bytes) to object heap (must hold mutex)
  void writeStringToHeapUnlocked(const std::string& str);

  // Helper to write element IDs to object heap (must hold mutex)
  void writeElementIdsToHeapUnlocked(const std::vector<uint64_t>& elementIds);

  // Helper to write key-value pairs to object heap (must hold mutex)
  void writeKeyValuePairsToHeapUnlocked(
      const std::vector<std::pair<uint64_t, uint64_t>>& pairs);

  // Shared implementation for writeList/writeTuple/writeSet (must hold mutex)
  uint64_t writeSequenceUnlocked(
      ObjectType type,
      const std::vector<uint64_t>& elementIds);

  // Shared implementation for writeSerializedList/Set/Tuple (must hold mutex)
  uint64_t writeSerializedSequenceUnlocked(
      ObjectType type,
      const std::vector<uint64_t>& elementIds,
      const std::string& typeName,
      const std::string& repr,
      const std::vector<std::pair<uint64_t, uint64_t>>& attrIds);

  // Write a data section to the mmap'd file at the given position,
  // extending the file if needed. Returns the number of bytes written
  // (0 if the data didn't fit). Must hold mutex.
  size_t writeSectionUnlocked(size_t writePos, const void* data, size_t size);

  // Write file table records after the object heap. Must hold mutex.
  // Returns (fileTableStart, fileTableEnd, filesWritten).
  struct FileTableResult {
    size_t start;
    size_t end;
    uint32_t filesWritten;
  };
  FileTableResult writeFileTableUnlocked();

  // Write environment, manifest, and metadata sections sequentially.
  // Must hold mutex. Returns the position past the last written byte.
  struct SectionPositions {
    size_t envStart;
    uint64_t envSize;
    size_t manifestStart;
    uint64_t manifestSize;
    size_t metadataStart;
    uint64_t metadataSize;
    size_t end; // Position past the last byte written
  };
  SectionPositions writeTrailingSectionsUnlocked(
      size_t startPos,
      const std::string& metadataJson);

  // Write the output file (compressed or raw copy). Must hold mutex.
  // snapshotRecordsEnd is the byte offset past the last snapshot record;
  // the gap [snapshotRecordsEnd, objectHeapStart_) is zero-filled and
  // will be skipped in the output to reduce compressed size.
  void writeOutputFileUnlocked(
      const std::string& path,
      size_t totalDataSize,
      size_t snapshotRecordsEnd,
      int compressionLevel);

  // Invalidate borrowed reader, unmap, close temp file, reset state.
  // Must hold mutex.
  void cleanupUnlocked();

  // Update the file header with final offsets
  void updateHeader();

  mutable std::mutex mutex_;
  std::string tempFilePath_;
  bool initialized_ = false;
  int snapshotFileFd_ = -1;
  void* snapshotFilePtr_ = nullptr;
  size_t currentFileSize_ = 0; // Current size of the mmap'd file

  // File layout tracking
  size_t writeOffset_ = 0; // Current write position
  size_t snapshotRecordsStart_ = 0; // Start of snapshot records section
  size_t objectHeapStart_ = 0; // Start of object heap section
  size_t objectHeapOffset_ =
      0; // Current write position in object heap (relative to heap start)
  size_t lastSnapshotPos_ = 0; // Absolute position of the last snapshot record
  uint32_t snapshotCount_ = 0; // Number of completed snapshot records

  // Set of referenced file paths (for file table during finalize)
  FastSet<std::string> referencedFiles_;
  // Object ID to heap offset mapping for deduplication within a snapshot
  FastMap<uint64_t, uint64_t> objectIdToOffset_;
  // Cross-snapshot deduplication: maps pythonId to (heapOffset, identityHash)
  // The identity hash captures the object's structure (type + children IDs +
  // size) so we can detect if a mutable object has changed between snapshots
  FastMap<uint64_t, std::pair<uint64_t, uint64_t>> globalObjectCache_;

  // Current snapshot state
  size_t currentSnapshotPos_ =
      0; // Absolute position of current snapshot header
  uint32_t currentStacktraceCount_ =
      0; // Number of stacktraces in current snapshot
  bool snapshotInProgress_ = false; // True between beginSnapshot/endSnapshot

  // Current stacktrace state
  size_t currentStacktracePos_ =
      0; // Absolute position of current stacktrace header
  uint32_t currentFrameCount_ = 0; // Number of frames in current stacktrace
  bool stacktraceInProgress_ =
      false; // True between beginStacktrace/endStacktrace

  // Borrowed reader that shares this writer's memory
  // Updated after mremap and cleared on finalize
  SnapshotReader* borrowedReader_ = nullptr;
};

} // namespace facebook::tintype::snapshot
