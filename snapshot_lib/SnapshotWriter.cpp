// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#include "SnapshotWriter.h"

#include <Python.h>
#include <pybind11/pybind11.h>

#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <zstd.h>

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <vector>
#include "compat.h"

extern char** environ;

#include "SnapshotReader.h"

namespace py = pybind11;

namespace facebook::tintype::snapshot {

SnapshotWriter& SnapshotWriter::getInstance() {
  static SnapshotWriter instance;
  return instance;
}

size_t SnapshotWriter::alignToBlock(size_t offset) {
  return (offset + kBlockSize - 1) & ~(kBlockSize - 1);
}

void SnapshotWriter::writeData(const void* data, size_t size) {
  if (writeOffset_ + size > currentFileSize_) {
    throw std::runtime_error("Snapshot file size exceeded");
  }
  std::memcpy(static_cast<char*>(snapshotFilePtr_) + writeOffset_, data, size);
  writeOffset_ += size;
}

void SnapshotWriter::ensureHeapSpaceUnlocked(size_t requiredSpace) {
  if (!initialized_) {
    throw std::runtime_error("SnapshotWriter not initialized");
  }

  size_t absoluteHeapPos = objectHeapStart_ + objectHeapOffset_;
  size_t requiredSize = absoluteHeapPos + requiredSpace;

  if (requiredSize > currentFileSize_) {
    if (!extendToFitUnlocked(requiredSize)) {
      throw std::runtime_error("Object heap full - maximum file size reached");
    }
  }
}

void SnapshotWriter::ensureSnapshotRecordSpaceUnlocked(size_t bytesNeeded) {
  if (writeOffset_ + bytesNeeded <= objectHeapStart_) {
    return; // Enough space
  }

  // Calculate new heap start: double the snapshot records allocation
  size_t currentSnapshotRecordsSize = objectHeapStart_ - snapshotRecordsStart_;
  size_t newSnapshotRecordsSize = currentSnapshotRecordsSize;
  while (snapshotRecordsStart_ + newSnapshotRecordsSize <
         writeOffset_ + bytesNeeded) {
    newSnapshotRecordsSize *= 2;
  }
  size_t newObjectHeapStart =
      alignToBlock(snapshotRecordsStart_ + newSnapshotRecordsSize);

  size_t heapUsed = objectHeapOffset_;

  // Ensure the file is large enough for the relocated heap
  size_t requiredSize = newObjectHeapStart + heapUsed;
  if (requiredSize > currentFileSize_) {
    if (!extendToFitUnlocked(requiredSize)) {
      throw std::runtime_error(
          "Cannot relocate object heap - maximum file size reached");
    }
  }

  // Move the object heap data forward
  if (heapUsed > 0) {
    std::memmove(
        static_cast<char*>(snapshotFilePtr_) + newObjectHeapStart,
        static_cast<char*>(snapshotFilePtr_) + objectHeapStart_,
        heapUsed);
  }

  objectHeapStart_ = newObjectHeapStart;

  // Update the file header so that any borrowed reader sees the new heap
  // position immediately, rather than waiting for endSnapshot().
  updateHeader();
}

bool SnapshotWriter::extendFileUnlocked(size_t newSize) {
  if (newSize <= currentFileSize_) {
    return true;
  }

  size_t extensionBytes = newSize - currentFileSize_;
  auto timer = SnapshotStatsCollector::getInstance().fileExtensionTimer();

  // Extend the file using ftruncate
  if (ftruncate(snapshotFileFd_, static_cast<off_t>(newSize)) != 0) {
    return false;
  }

  // Remap to the new size. On Linux, mremap can extend in place; on other
  // platforms (e.g., macOS), we fall back to munmap + mmap.
#ifdef __linux__
  void* newPtr =
      mremap(snapshotFilePtr_, currentFileSize_, newSize, MREMAP_MAYMOVE);
#else
  munmap(snapshotFilePtr_, currentFileSize_);
  void* newPtr = mmap(
      nullptr, newSize, PROT_READ | PROT_WRITE, MAP_SHARED, snapshotFileFd_, 0);
#endif
  // NOLINTNEXTLINE(performance-no-int-to-ptr)
  if (newPtr == MAP_FAILED) {
#ifdef __linux__
    // Try to restore original size
    ftruncate(snapshotFileFd_, static_cast<off_t>(currentFileSize_));
#else
    // Re-establish the old mapping at the original size
    snapshotFilePtr_ = mmap(
        nullptr,
        currentFileSize_,
        PROT_READ | PROT_WRITE,
        MAP_SHARED,
        snapshotFileFd_,
        0);
    ftruncate(snapshotFileFd_, static_cast<off_t>(currentFileSize_));
#endif
    return false;
  }

  snapshotFilePtr_ = newPtr;
  currentFileSize_ = newSize;

  // Update the borrowed reader's memory pointer if one is set
  if (borrowedReader_ != nullptr) {
    borrowedReader_->updateMemory(snapshotFilePtr_, currentFileSize_);
  }

  // Update file extension stats (count and bytes only on success)
  auto& statsCollector = SnapshotStatsCollector::getInstance();
  statsCollector.incrementFileExtensionCount();
  statsCollector.addFileExtensionBytes(extensionBytes);

  return true;
}

bool SnapshotWriter::extendToFitUnlocked(size_t requiredSize) {
  if (requiredSize <= currentFileSize_) {
    return true;
  }
  size_t newSize = currentFileSize_;
  while (newSize < requiredSize) {
    if (newSize >= kMaxFileSize) {
      return false;
    }
    newSize *= 2;
  }
  if (newSize > kMaxFileSize) {
    newSize = kMaxFileSize;
  }
  if (requiredSize > newSize) {
    return false;
  }
  return extendFileUnlocked(newSize);
}

uint64_t SnapshotWriter::writeObjectHeaderUnlocked(ObjectType type) {
  // objectOffset is relative to the heap start
  uint64_t objectOffset = objectHeapOffset_;

  ObjectHeapRecordHeader header{};
  header.type = type;

  // Write at absolute position = objectHeapStart_ + objectHeapOffset_
  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + objectHeapStart_ +
          objectHeapOffset_,
      &header,
      sizeof(header));
  objectHeapOffset_ += sizeof(header);

  return objectOffset;
}

void SnapshotWriter::writeToHeapUnlocked(const void* data, size_t size) {
  // Write at absolute position = objectHeapStart_ + objectHeapOffset_
  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + objectHeapStart_ +
          objectHeapOffset_,
      data,
      size);
  objectHeapOffset_ += size;
}

void SnapshotWriter::writeUint32ToHeapUnlocked(uint32_t value) {
  writeToHeapUnlocked(&value, sizeof(value));
}

void SnapshotWriter::writeStringToHeapUnlocked(const std::string& str) {
  uint32_t length = static_cast<uint32_t>(str.size());
  writeUint32ToHeapUnlocked(length);
  if (!str.empty()) {
    writeToHeapUnlocked(str.data(), str.size());
  }
}

void SnapshotWriter::writeElementIdsToHeapUnlocked(
    const std::vector<uint64_t>& elementIds) {
  writeUint32ToHeapUnlocked(static_cast<uint32_t>(elementIds.size()));
  for (uint64_t elementId : elementIds) {
    writeToHeapUnlocked(&elementId, sizeof(elementId));
  }
}

void SnapshotWriter::writeKeyValuePairsToHeapUnlocked(
    const std::vector<std::pair<uint64_t, uint64_t>>& pairs) {
  writeUint32ToHeapUnlocked(static_cast<uint32_t>(pairs.size()));
  for (const auto& [keyId, valueId] : pairs) {
    writeToHeapUnlocked(&keyId, sizeof(keyId));
    writeToHeapUnlocked(&valueId, sizeof(valueId));
  }
}

uint64_t SnapshotWriter::writeSequenceUnlocked(
    ObjectType type,
    const std::vector<uint64_t>& elementIds) {
  size_t requiredSpace = sizeof(ObjectHeapRecordHeader) + sizeof(uint32_t) +
      (elementIds.size() * sizeof(uint64_t));
  ensureHeapSpaceUnlocked(requiredSpace);

  uint64_t objectOffset = writeObjectHeaderUnlocked(type);
  writeElementIdsToHeapUnlocked(elementIds);
  return objectOffset;
}

uint64_t SnapshotWriter::writeSerializedSequenceUnlocked(
    ObjectType type,
    const std::vector<uint64_t>& elementIds,
    const std::string& typeName,
    const std::string& repr,
    const std::vector<std::pair<uint64_t, uint64_t>>& attrIds) {
  size_t requiredSpace = sizeof(ObjectHeapRecordHeader) + sizeof(uint32_t) +
      (elementIds.size() * sizeof(uint64_t)) + sizeof(uint32_t) +
      typeName.size() + sizeof(uint32_t) + repr.size() + sizeof(uint32_t) +
      (attrIds.size() * 2 * sizeof(uint64_t));
  ensureHeapSpaceUnlocked(requiredSpace);

  uint64_t objectOffset = writeObjectHeaderUnlocked(type);
  writeElementIdsToHeapUnlocked(elementIds);
  writeStringToHeapUnlocked(typeName);
  writeStringToHeapUnlocked(repr);
  writeKeyValuePairsToHeapUnlocked(attrIds);
  return objectOffset;
}

void SnapshotWriter::writeInitialHeader() {
  FileHeader header{};
  header.magic = kMagicNumber;
  header.version = kFormatVersion;
  header.lastSnapshotPos = 0;
  header.snapshotCount = 0;
  header.objectHeapPos = 0;
  header.fileTablePos = 0;
  header.fileTableCount = 0;
  header.envPos = 0;
  header.envSize = 0;
  header.manifestPos = 0;
  header.manifestSize = 0;
  header.metadataPos = 0;
  header.metadataSize = 0;
  header.statsPos = 0;
  header.statsCount = 0;

  writeData(&header, sizeof(header));

  // Align to block boundary after header
  size_t paddingNeeded = alignToBlock(writeOffset_) - writeOffset_;
  if (paddingNeeded > 0) {
    std::vector<char> padding(paddingNeeded, 0);
    writeData(padding.data(), paddingNeeded);
  }
}

void SnapshotWriter::updateHeader() {
  FileHeader* header = static_cast<FileHeader*>(snapshotFilePtr_);
  header->lastSnapshotPos = lastSnapshotPos_;
  header->snapshotCount = snapshotCount_;
  header->objectHeapPos = objectHeapStart_;
  // fileTablePos, metadataPos, metadataSize will be set during finalize
}

void SnapshotWriter::initialize(bool collectStats) {
  std::lock_guard<std::mutex> lock(mutex_);

  if (initialized_) {
    throw std::runtime_error("SnapshotWriter already initialized");
  }

  // Set up stats collection
  auto& statsCollector = SnapshotStatsCollector::getInstance();
  if (collectStats) {
    statsCollector.beginCollection();
  } else {
    statsCollector.endCollection();
  }

  auto timer = statsCollector.initializeTimer();

  writeOffset_ = 0;
  lastSnapshotPos_ = 0;
  snapshotCount_ = 0;
  objectHeapOffset_ = 0;
  referencedFiles_.clear();

  // Create a temporary file for the mmap'd snapshot data
  char tempTemplate[] = "/tmp/snapshot_XXXXXX";
  snapshotFileFd_ = mkstemp(tempTemplate);
  if (snapshotFileFd_ < 0) {
    throw std::runtime_error(
        "Failed to create temp file: " + errnoString(errno));
  }
  tempFilePath_ = tempTemplate;

  // Extend file to kInitialFileSize
  if (ftruncate(snapshotFileFd_, kInitialFileSize) != 0) {
    close(snapshotFileFd_);
    snapshotFileFd_ = -1;
    throw std::runtime_error("Failed to resize file: " + errnoString(errno));
  }

  // mmap the file
  snapshotFilePtr_ = mmap(
      nullptr,
      kInitialFileSize,
      PROT_READ | PROT_WRITE,
      MAP_SHARED,
      snapshotFileFd_,
      0);
  // NOLINTNEXTLINE(performance-no-int-to-ptr)
  if (snapshotFilePtr_ == MAP_FAILED) {
    close(snapshotFileFd_);
    snapshotFileFd_ = -1;
    snapshotFilePtr_ = nullptr;
    throw std::runtime_error("Failed to mmap file: " + errnoString(errno));
  }

  // Track the current file size
  currentFileSize_ = kInitialFileSize;

  // Write the initial file header
  writeInitialHeader();

  // Mark start of snapshot records section (right after header block)
  snapshotRecordsStart_ = alignToBlock(sizeof(FileHeader));

  // Calculate where object heap will start (after initial 100MB for snapshots)
  objectHeapStart_ = snapshotRecordsStart_ + kInitialSnapshotRecordsSize;
  objectHeapStart_ = alignToBlock(objectHeapStart_);
  // objectHeapOffset_ tracks relative position within the heap, starting at 0
  objectHeapOffset_ = 0;

  initialized_ = true;
}

// Helper function to read file contents from the filesystem
std::string readFileFromFilesystem(const std::string& filePath) {
  std::ifstream file(filePath, std::ios::binary);
  if (!file.is_open()) {
    return "";
  }
  std::stringstream buffer;
  buffer << file.rdbuf();
  return buffer.str();
}

// Helper function to read file contents from a zip file (PAR archive)
std::string readFileFromZip(
    const std::string& zipPath,
    const std::string& filePath) {
  try {
    py::module_ zipfile = py::module_::import("zipfile");
    py::object ZipFile = zipfile.attr("ZipFile");
    py::object zipObj = ZipFile(zipPath, "r");

    // Try to read the file from the zip
    py::object readFunc = zipObj.attr("read");
    py::bytes data = readFunc(filePath);
    zipObj.attr("close")();

    return data.cast<std::string>();
  } catch (...) {
    return "";
  }
}

// Helper function to read file contents with fallback logic
std::string readFileContents(const std::string& filePath) {
  // First, try linecache
  try {
    py::module_ linecache = py::module_::import("linecache");
    py::object lines = linecache.attr("getlines")(filePath);
    py::object result = py::str("").attr("join")(lines);
    std::string contents = result.cast<std::string>();
    if (!contents.empty()) {
      return contents;
    }
  } catch (...) {
    // linecache failed, try fallback methods
  }

  // Check if this is a built-in or special file (e.g., "<stdin>", "<string>")
  // These cannot be read from the filesystem
  if (!filePath.empty() && filePath[0] == '<') {
    return "";
  }

  // Check if the path is absolute
  bool isAbsolute = !filePath.empty() && filePath[0] == '/';

  if (isAbsolute) {
    // Try to read the file directly from the filesystem
    std::string contents = readFileFromFilesystem(filePath);
    if (!contents.empty()) {
      return contents;
    }
  } else {
    // Relative path - check if we're running from a PAR file
    const char* parFilename = std::getenv("FB_PAR_FILENAME");
    if (parFilename != nullptr && parFilename[0] != '\0') {
      std::string parPath(parFilename);

      // Check if the PAR file is a zip file (ends with .par or is a zip)
      // PAR files are typically zip archives
      std::string contents = readFileFromZip(parPath, filePath);
      if (!contents.empty()) {
        return contents;
      }
    }
  }

  return "";
}

// Helper to check if a path contains a buck-out link-tree pattern
bool isBuckOutLinkTree(const std::string& path) {
  // Matches the regex: buck-out[\\/].*#.*link-tree[^\\/]*$
  auto buckOutPos = path.find("buck-out");
  if (buckOutPos == std::string::npos) {
    return false;
  }
  // Must have a separator after "buck-out"
  size_t afterBuckOut = buckOutPos + 8; // strlen("buck-out")
  if (afterBuckOut >= path.size() ||
      (path[afterBuckOut] != '/' && path[afterBuckOut] != '\\')) {
    return false;
  }
  // Must have "#" somewhere after that
  auto hashPos = path.find('#', afterBuckOut);
  if (hashPos == std::string::npos) {
    return false;
  }
  // Must have "link-tree" after the "#"
  auto linkTreePos = path.find("link-tree", hashPos);
  if (linkTreePos == std::string::npos) {
    return false;
  }
  // No path separator after "link-tree"
  size_t afterLinkTree = linkTreePos + 9; // strlen("link-tree")
  for (size_t i = afterLinkTree; i < path.size(); ++i) {
    if (path[i] == '/' || path[i] == '\\') {
      return false;
    }
  }
  return true;
}

// Helper to check if a path ends with "#link-tree" (with optional trailing
// chars)
bool endsWithLinkTree(const std::string& path) {
  // Matches the regex: #link-tree$
  const std::string suffix = "#link-tree";
  if (path.size() < suffix.size()) {
    return false;
  }
  return path.compare(path.size() - suffix.size(), suffix.size(), suffix) == 0;
}

// Get the runtime files path, equivalent to
// serialization.py::_get_runtime_path()
std::string getRuntimePath() {
  const char* runtimeFiles = std::getenv("FB_PAR_RUNTIME_FILES");

  // Search sys.path for link-tree entries
  PyObject* sysPath = PySys_GetObject("path"); // borrowed ref
  if (sysPath != nullptr && PyList_Check(sysPath)) {
    Py_ssize_t len = PyList_Size(sysPath);

    // First pass: look for buck-out link-tree (higher priority)
    std::string firstCandidate;
    for (Py_ssize_t i = 0; i < len; ++i) {
      PyObject* item = PyList_GET_ITEM(sysPath, i); // borrowed ref
      const char* pathStr = PyUnicode_AsUTF8(item);
      if (pathStr == nullptr) {
        PyErr_Clear();
        continue;
      }
      std::string path(pathStr);
      if (isBuckOutLinkTree(path) && access(pathStr, R_OK) == 0) {
        if (runtimeFiles != nullptr && path == runtimeFiles) {
          return std::string(runtimeFiles);
        }
        if (firstCandidate.empty()) {
          firstCandidate = path;
        }
      }
    }

    // Second pass: any #link-tree (fallback)
    if (firstCandidate.empty()) {
      for (Py_ssize_t i = 0; i < len; ++i) {
        PyObject* item = PyList_GET_ITEM(sysPath, i);
        const char* pathStr = PyUnicode_AsUTF8(item);
        if (pathStr == nullptr) {
          PyErr_Clear();
          continue;
        }
        std::string path(pathStr);
        if (endsWithLinkTree(path) && access(pathStr, R_OK) == 0) {
          if (runtimeFiles != nullptr && path == runtimeFiles) {
            return std::string(runtimeFiles);
          }
          if (firstCandidate.empty()) {
            firstCandidate = path;
          }
        }
      }
    }

    if (!firstCandidate.empty()) {
      return firstCandidate;
    }
  }

  // Fall back to environment variables
  for (const char* envVar :
       {"FB_PAR_RUNTIME_FILES", "FB_PAR_RUNTIME_FILES_UNPACKED"}) {
    const char* val = std::getenv(envVar);
    if (val != nullptr && val[0] != '\0' && access(val, R_OK) == 0) {
      return std::string(val);
    }
  }

  return "";
}

// Read __manifest__.json from the runtime path if available
std::string readManifestJson() {
  std::string runtimePath = getRuntimePath();
  if (runtimePath.empty()) {
    return "";
  }

  std::string manifestPath = runtimePath + "/__manifest__.json";
  if (access(manifestPath.c_str(), R_OK) != 0) {
    return "";
  }

  return readFileFromFilesystem(manifestPath);
}

// Capture current process environment variables as a sequence of
// null-terminated KEY=VALUE strings. The total size of the returned
// string (including all null terminators) is used as envSize in the header.
std::string captureEnvironment() {
  std::string result;
  for (char** env = environ; *env != nullptr; ++env) {
    size_t len = std::strlen(*env);
    result.append(*env, len + 1); // include the null terminator
  }
  return result;
}

size_t SnapshotWriter::writeSectionUnlocked(
    size_t writePos,
    const void* data,
    size_t size) {
  if (size == 0) {
    return 0;
  }
  if (writePos + size > currentFileSize_) {
    if (!extendToFitUnlocked(writePos + size)) {
      return 0;
    }
  }
  std::memcpy(static_cast<char*>(snapshotFilePtr_) + writePos, data, size);
  return size;
}

SnapshotWriter::FileTableResult SnapshotWriter::writeFileTableUnlocked() {
  auto timer = SnapshotStatsCollector::getInstance().finalizeFileTableTimer();

  size_t absoluteHeapEnd = objectHeapStart_ + objectHeapOffset_;
  size_t fileTableStart = absoluteHeapEnd;
  size_t fileTableWritePos = fileTableStart;
  uint32_t filesWritten = 0;

  for (const auto& filePath : referencedFiles_) {
    std::string contents = readFileContents(filePath);

    FileTableRecordHeader recordHeader{};
    recordHeader.pathLength = static_cast<uint32_t>(filePath.size());
    recordHeader.contentLength = static_cast<uint32_t>(contents.size());

    size_t recordSize =
        sizeof(recordHeader) + filePath.size() + contents.size();
    if (fileTableWritePos + recordSize > currentFileSize_) {
      if (!extendToFitUnlocked(fileTableWritePos + recordSize)) {
        SnapshotStatsCollector::getInstance().incrementErrors();
        break;
      }
    }

    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + fileTableWritePos,
        &recordHeader,
        sizeof(recordHeader));
    fileTableWritePos += sizeof(recordHeader);

    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + fileTableWritePos,
        filePath.data(),
        filePath.size());
    fileTableWritePos += filePath.size();

    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + fileTableWritePos,
        contents.data(),
        contents.size());
    fileTableWritePos += contents.size();

    filesWritten++;
  }

  SnapshotStatsCollector::getInstance().setFinalizeFileCount(filesWritten);

  return {fileTableStart, fileTableWritePos, filesWritten};
}

SnapshotWriter::SectionPositions SnapshotWriter::writeTrailingSectionsUnlocked(
    size_t startPos,
    const std::string& metadataJson) {
  SectionPositions pos{};

  // --- Environment ---
  {
    auto timer =
        SnapshotStatsCollector::getInstance().finalizeEnvironmentTimer();
    std::string envData = captureEnvironment();
    pos.envStart = startPos;
    pos.envSize = envData.size();
    size_t written =
        writeSectionUnlocked(startPos, envData.data(), envData.size());
    if (written == 0 && !envData.empty()) {
      pos.envSize = 0;
      SnapshotStatsCollector::getInstance().incrementErrors();
    }
    pos.manifestStart = startPos + written;
  }

  // --- Manifest ---
  {
    auto timer = SnapshotStatsCollector::getInstance().finalizeManifestTimer();
    std::string manifestJson = readManifestJson();
    pos.manifestSize = manifestJson.size();
    size_t written = writeSectionUnlocked(
        pos.manifestStart, manifestJson.data(), manifestJson.size());
    if (written == 0 && !manifestJson.empty()) {
      pos.manifestSize = 0;
      SnapshotStatsCollector::getInstance().incrementErrors();
    }
    pos.metadataStart = pos.manifestStart + written;
  }

  // --- Metadata ---
  {
    auto timer = SnapshotStatsCollector::getInstance().finalizeMetadataTimer();
    pos.metadataSize = metadataJson.size();
    size_t written = writeSectionUnlocked(
        pos.metadataStart, metadataJson.data(), metadataJson.size());
    if (written == 0 && !metadataJson.empty()) {
      pos.metadataSize = 0;
      SnapshotStatsCollector::getInstance().incrementErrors();
    }
    pos.end = pos.metadataStart + written;
  }

  return pos;
}

void SnapshotWriter::writeOutputFileUnlocked(
    const std::string& path,
    size_t totalDataSize,
    size_t snapshotRecordsEnd,
    int compressionLevel) {
  // NOLINTNEXTLINE(performance-no-int-to-ptr)
  if (snapshotFilePtr_ == nullptr || snapshotFilePtr_ == MAP_FAILED ||
      totalDataSize == 0) {
    return;
  }

  // Defensive validation: ensure section boundaries don't exceed total size.
  // These are size_t (unsigned), so a caller bug could cause underflow to
  // massive values, leading to out-of-bounds reads.
  if (snapshotRecordsEnd > totalDataSize || objectHeapStart_ > totalDataSize) {
    return;
  }

  // Calculate gap size between end of snapshot records and object heap.
  // This zero-filled region is skipped in the output file.
  // When snapshotRecordsEnd >= objectHeapStart_ (no gap or overlap), gapSize
  // stays 0 and we fall through to the single-buffer path below.
  size_t gapSize = 0;
  if (snapshotRecordsEnd < objectHeapStart_) {
    gapSize = objectHeapStart_ - snapshotRecordsEnd;
  }
  size_t compactDataSize = totalDataSize - gapSize;

  // msync the working file before reading its contents for output.
  // This ensures the copy below reflects the latest data.
  {
    auto timer = SnapshotStatsCollector::getInstance().finalizeMsyncTimer();
    msync(snapshotFilePtr_, totalDataSize, MS_SYNC);
  }

  // Create a stack-local copy of the header with adjusted positions to
  // reflect the compacted layout. The mmap'd file is never modified, keeping
  // it valid for out-of-process readers and failure safety.
  FileHeader adjustedHeader;
  std::memcpy(&adjustedHeader, snapshotFilePtr_, sizeof(FileHeader));
  if (gapSize > 0) {
    adjustedHeader.objectHeapPos -= gapSize;
    adjustedHeader.fileTablePos -= gapSize;
    adjustedHeader.envPos -= gapSize;
    adjustedHeader.manifestPos -= gapSize;
    adjustedHeader.metadataPos -= gapSize;
    if (adjustedHeader.statsPos > 0) {
      adjustedHeader.statsPos -= gapSize;
    }
    // lastSnapshotPos is not adjusted because it points into the snapshot
    // records section (segment 1), which is before the gap and therefore
    // unaffected by the compaction.
  }

  const char* srcData = static_cast<const char*>(snapshotFilePtr_);

  // --- Output file ---
  {
    auto timer =
        SnapshotStatsCollector::getInstance().finalizeOutputFileTimer();

    if (compressionLevel == 0) {
      // No compression — write raw data directly
      int outputFd = open(path.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0644);
      if (outputFd >= 0) {
        if (ftruncate(outputFd, static_cast<off_t>(compactDataSize)) == 0) {
          void* outputPtr = mmap(
              nullptr, compactDataSize, PROT_WRITE, MAP_SHARED, outputFd, 0);
          // NOLINTNEXTLINE(performance-no-int-to-ptr)
          if (outputPtr != MAP_FAILED) {
            char* dst = static_cast<char*>(outputPtr);
            if (gapSize == 0) {
              std::memcpy(dst, srcData, compactDataSize);
            } else {
              // Segment A: adjusted header
              std::memcpy(dst, &adjustedHeader, sizeof(FileHeader));
              // Segment B: snapshot records (after header, up to gap)
              std::memcpy(
                  dst + sizeof(FileHeader),
                  srcData + sizeof(FileHeader),
                  snapshotRecordsEnd - sizeof(FileHeader));
              // Segment C: object heap + trailing sections
              std::memcpy(
                  dst + snapshotRecordsEnd,
                  srcData + objectHeapStart_,
                  totalDataSize - objectHeapStart_);
            }
            msync(outputPtr, compactDataSize, MS_SYNC);
            munmap(outputPtr, compactDataSize);
          }
        }
        close(outputFd);
        SnapshotStatsCollector::getInstance().setFinalizeCompressedDataSize(
            compactDataSize);
      }
    } else {
      size_t maxCompressedSize = ZSTD_compressBound(compactDataSize);

      int outputFd = open(path.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0644);
      if (outputFd >= 0) {
        if (ftruncate(outputFd, static_cast<off_t>(maxCompressedSize)) == 0) {
          void* outputPtr = mmap(
              nullptr, maxCompressedSize, PROT_WRITE, MAP_SHARED, outputFd, 0);
          // NOLINTNEXTLINE(performance-no-int-to-ptr)
          if (outputPtr != MAP_FAILED) {
            size_t actualCompressedSize = 0;

            if (gapSize == 0) {
              // No gap — use single-buffer compression (avoids streaming
              // overhead)
              auto compressTimer = SnapshotStatsCollector::getInstance()
                                       .finalizeCompressionTimer();
              size_t ret = ZSTD_compress(
                  outputPtr,
                  maxCompressedSize,
                  srcData,
                  compactDataSize,
                  compressionLevel);
              if (!ZSTD_isError(ret)) {
                actualCompressedSize = ret;
              }
            } else {
              // Gap present — use streaming compression to feed three
              // segments with the adjusted header
              ZSTD_CCtx* cctx = ZSTD_createCCtx();
              if (cctx != nullptr) {
                size_t paramRet = ZSTD_CCtx_setParameter(
                    cctx, ZSTD_c_compressionLevel, compressionLevel);
                if (!ZSTD_isError(paramRet)) {
                  paramRet = ZSTD_CCtx_setPledgedSrcSize(cctx, compactDataSize);
                }

                if (!ZSTD_isError(paramRet)) {
                  auto compressTimer = SnapshotStatsCollector::getInstance()
                                           .finalizeCompressionTimer();

                  ZSTD_outBuffer outBuf = {outputPtr, maxCompressedSize, 0};
                  bool compressionFailed = false;

                  // Segment A: adjusted header
                  ZSTD_inBuffer inBufA = {
                      &adjustedHeader, sizeof(FileHeader), 0};
                  while (inBufA.pos < inBufA.size) {
                    size_t ret = ZSTD_compressStream2(
                        cctx, &outBuf, &inBufA, ZSTD_e_continue);
                    if (ZSTD_isError(ret)) {
                      compressionFailed = true;
                      break;
                    }
                  }

                  // Segment B: snapshot records (after header, up to gap)
                  if (!compressionFailed) {
                    ZSTD_inBuffer inBufB = {
                        srcData + sizeof(FileHeader),
                        snapshotRecordsEnd - sizeof(FileHeader),
                        0};
                    while (inBufB.pos < inBufB.size) {
                      size_t ret = ZSTD_compressStream2(
                          cctx, &outBuf, &inBufB, ZSTD_e_continue);
                      if (ZSTD_isError(ret)) {
                        compressionFailed = true;
                        break;
                      }
                    }
                  }

                  // Segment C: object heap + trailing sections
                  if (!compressionFailed) {
                    ZSTD_inBuffer inBufC = {
                        srcData + objectHeapStart_,
                        totalDataSize - objectHeapStart_,
                        0};
                    size_t ret = 0;
                    do {
                      ret = ZSTD_compressStream2(
                          cctx, &outBuf, &inBufC, ZSTD_e_end);
                      if (ZSTD_isError(ret)) {
                        compressionFailed = true;
                        break;
                      }
                    } while (ret != 0);
                  }

                  if (!compressionFailed) {
                    actualCompressedSize = outBuf.pos;
                  }
                }

                ZSTD_freeCCtx(cctx);
              }
            }

            msync(outputPtr, actualCompressedSize, MS_SYNC);
            munmap(outputPtr, maxCompressedSize);

            if (actualCompressedSize > 0) {
              ftruncate(outputFd, static_cast<off_t>(actualCompressedSize));
              SnapshotStatsCollector::getInstance()
                  .setFinalizeCompressedDataSize(actualCompressedSize);
            } else {
              // Compression failed — remove the garbage output file
              unlink(path.c_str());
              SnapshotStatsCollector::getInstance().incrementErrors();
            }
          }
        }
        close(outputFd);
      }
    }
  }
}

void SnapshotWriter::cleanupUnlocked() {
  // Invalidate the borrowed reader before unmapping since it shares our memory
  if (borrowedReader_ != nullptr) {
    borrowedReader_->updateMemory(nullptr, 0);
    borrowedReader_ = nullptr;
  }

  // Unmap and close the temporary file
  // NOLINTNEXTLINE(performance-no-int-to-ptr)
  if (snapshotFilePtr_ != nullptr && snapshotFilePtr_ != MAP_FAILED) {
    munmap(snapshotFilePtr_, currentFileSize_);
    snapshotFilePtr_ = nullptr;
  }

  if (snapshotFileFd_ >= 0) {
    close(snapshotFileFd_);
    snapshotFileFd_ = -1;
  }

  // Remove the temporary file
  if (!tempFilePath_.empty()) {
    unlink(tempFilePath_.c_str());
    tempFilePath_.clear();
  }

  // Reset state
  writeOffset_ = 0;
  snapshotRecordsStart_ = 0;
  objectHeapStart_ = 0;
  objectHeapOffset_ = 0;
  lastSnapshotPos_ = 0;
  currentFileSize_ = 0;
  referencedFiles_.clear();
  objectIdToOffset_.clear();
  globalObjectCache_.clear();
  currentSnapshotPos_ = 0;
  currentFrameCount_ = 0;
  snapshotInProgress_ = false;
  initialized_ = false;
}

void SnapshotWriter::finalize(
    const std::string& path,
    const std::string& metadataJson,
    int compressionLevel) {
  // Validate compression level before taking the lock
  if (compressionLevel < 0 || compressionLevel > 22) {
    throw std::runtime_error(
        "Invalid compression level: " + std::to_string(compressionLevel) +
        ". Must be 0 (no compression) or 1-22.");
  }

  std::lock_guard<std::mutex> lock(mutex_);

  if (!initialized_) {
    return;
  }

  auto finalizeTimer = SnapshotStatsCollector::getInstance().finalizeTimer();

  if (!path.empty()) {
    // --- File table ---
    auto fileTable = writeFileTableUnlocked();

    // --- Environment, manifest, metadata ---
    auto sections = writeTrailingSectionsUnlocked(fileTable.end, metadataJson);

    // --- Statistics section (written if stats collection was enabled) ---
    uint64_t statsPos = 0;
    uint32_t statsCount = 0;
    size_t statsEnd = sections.end;

    if (SnapshotStatsCollector::getInstance().isCollecting()) {
      auto flatStats =
          SnapshotStatsCollector::getInstance().getStats().flatten();
      statsPos = sections.end;
      statsCount = static_cast<uint32_t>(flatStats.size());

      // Write each stat entry: value (u64) | nameLength (u32) | name (char[])
      for (const auto& [name, value] : flatStats) {
        uint32_t nameLen = static_cast<uint32_t>(name.size());
        size_t entrySize = sizeof(uint64_t) + sizeof(uint32_t) + nameLen;

        // Ensure file has enough space (statsEnd is already an absolute
        // position)
        if (statsEnd + entrySize > currentFileSize_) {
          if (!extendToFitUnlocked(statsEnd + entrySize)) {
            break;
          }
        }

        char* ptr = static_cast<char*>(snapshotFilePtr_) + statsEnd;
        std::memcpy(ptr, &value, sizeof(uint64_t));
        ptr += sizeof(uint64_t);
        std::memcpy(ptr, &nameLen, sizeof(uint32_t));
        ptr += sizeof(uint32_t);
        std::memcpy(ptr, name.data(), nameLen);
        statsEnd += sizeof(uint64_t) + sizeof(uint32_t) + nameLen;
      }
    }

    // --- Header update ---
    FileHeader* header = static_cast<FileHeader*>(snapshotFilePtr_);
    header->lastSnapshotPos = lastSnapshotPos_;
    header->objectHeapPos = objectHeapStart_;
    header->fileTablePos = fileTable.start;
    header->fileTableCount = fileTable.filesWritten;
    header->envPos = sections.envStart;
    header->envSize = sections.envSize;
    header->manifestPos = sections.manifestStart;
    header->manifestSize = sections.manifestSize;
    header->metadataPos = sections.metadataStart;
    header->metadataSize = sections.metadataSize;
    header->statsPos = statsPos;
    header->statsCount = statsCount;

    size_t totalDataSize = statsEnd;

    SnapshotStatsCollector::getInstance().setFinalizeUncompressedDataSize(
        totalDataSize);

    // --- msync + output file ---
    writeOutputFileUnlocked(
        path, totalDataSize, writeOffset_, compressionLevel);
  }

  // --- Cleanup ---
  {
    auto cleanupTimer =
        SnapshotStatsCollector::getInstance().finalizeCleanupTimer();
    cleanupUnlocked();
  }
}

SnapshotStats SnapshotWriter::getStats() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return SnapshotStatsCollector::getInstance().getStats();
}

void SnapshotWriter::resetStats() {
  std::lock_guard<std::mutex> lock(mutex_);
  SnapshotStatsCollector::getInstance().beginCollection();
}

void SnapshotWriter::setBorrowedReader(SnapshotReader* reader) {
  std::lock_guard<std::mutex> lock(mutex_);
  borrowedReader_ = reader;
}

bool SnapshotWriter::isInitialized() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return initialized_;
}

const std::string& SnapshotWriter::getTempFilePath() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return tempFilePath_;
}

void* SnapshotWriter::getSnapshotFilePtr() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return snapshotFilePtr_;
}

size_t SnapshotWriter::getSnapshotFileSize() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return currentFileSize_;
}

void SnapshotWriter::setWriteOffset(size_t offset) {
  std::lock_guard<std::mutex> lock(mutex_);
  writeOffset_ = offset;
}

size_t SnapshotWriter::getWriteOffset() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return writeOffset_;
}

uint64_t SnapshotWriter::writeString(const std::string& value) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer = SnapshotStatsCollector::getInstance().objectWriteTimer(
      ObjectType::String);

  size_t requiredSpace =
      sizeof(ObjectHeapRecordHeader) + sizeof(uint32_t) + value.size();
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeObjectHeaderUnlocked(ObjectType::String);
  writeStringToHeapUnlocked(value);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::String, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeBytes(const std::string& value) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer =
      SnapshotStatsCollector::getInstance().objectWriteTimer(ObjectType::Bytes);

  size_t requiredSpace =
      sizeof(ObjectHeapRecordHeader) + sizeof(uint32_t) + value.size();
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeObjectHeaderUnlocked(ObjectType::Bytes);
  writeStringToHeapUnlocked(value);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::Bytes, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeInt(const std::string& value) {
  std::lock_guard<std::mutex> lock(mutex_);

  // Try to parse as int64, otherwise use IntBignum (string representation)
  ObjectType intType = ObjectType::IntBignum;
  int64_t int64Value = 0;

  bool isNegative = !value.empty() && value[0] == '-';
  size_t digitStart = isNegative ? 1 : 0;
  size_t numDigits = value.size() - digitStart;

  // Try int64_t (max 19 digits for positive, 19 digits for negative)
  if (numDigits <= 18 || (numDigits == 19 && value.size() <= 20)) {
    try {
      int64Value = std::stoll(value);
      intType = ObjectType::Int64;
    } catch (...) {
    }
  }

  auto timer = SnapshotStatsCollector::getInstance().objectWriteTimer(intType);

  // Calculate and ensure space
  size_t requiredSpace = sizeof(ObjectHeapRecordHeader);
  if (intType == ObjectType::Int64) {
    requiredSpace += sizeof(int64_t);
  } else {
    requiredSpace += sizeof(uint32_t) + value.size();
  }
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeObjectHeaderUnlocked(intType);

  if (intType == ObjectType::Int64) {
    writeToHeapUnlocked(&int64Value, sizeof(int64Value));
  } else {
    writeStringToHeapUnlocked(value);
  }

  // Record stats based on which int type was used
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      intType, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeInt64(int64_t value) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer =
      SnapshotStatsCollector::getInstance().objectWriteTimer(ObjectType::Int64);

  size_t requiredSpace = sizeof(ObjectHeapRecordHeader) + sizeof(int64_t);
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeObjectHeaderUnlocked(ObjectType::Int64);
  writeToHeapUnlocked(&value, sizeof(value));

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::Int64, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeFloat(double value) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer =
      SnapshotStatsCollector::getInstance().objectWriteTimer(ObjectType::Float);

  size_t requiredSpace = sizeof(ObjectHeapRecordHeader) + sizeof(double);
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeObjectHeaderUnlocked(ObjectType::Float);
  writeToHeapUnlocked(&value, sizeof(value));

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::Float, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeList(const std::vector<uint64_t>& elementIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer =
      SnapshotStatsCollector::getInstance().objectWriteTimer(ObjectType::List);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeSequenceUnlocked(ObjectType::List, elementIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::List, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeTuple(const std::vector<uint64_t>& elementIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer =
      SnapshotStatsCollector::getInstance().objectWriteTimer(ObjectType::Tuple);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeSequenceUnlocked(ObjectType::Tuple, elementIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::Tuple, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeSet(const std::vector<uint64_t>& elementIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer =
      SnapshotStatsCollector::getInstance().objectWriteTimer(ObjectType::Set);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeSequenceUnlocked(ObjectType::Set, elementIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::Set, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeDict(
    const std::vector<std::pair<uint64_t, uint64_t>>& keyValueIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer =
      SnapshotStatsCollector::getInstance().objectWriteTimer(ObjectType::Dict);

  size_t requiredSpace = sizeof(ObjectHeapRecordHeader) + sizeof(uint32_t) +
      (keyValueIds.size() * 2 * sizeof(uint64_t));
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeObjectHeaderUnlocked(ObjectType::Dict);
  writeKeyValuePairsToHeapUnlocked(keyValueIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::Dict, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeSerializedObject(
    const std::string& typeName,
    const std::string& repr,
    const std::vector<std::pair<uint64_t, uint64_t>>& attrIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer = SnapshotStatsCollector::getInstance().objectWriteTimer(
      ObjectType::SerializedObject);

  size_t requiredSpace = sizeof(ObjectHeapRecordHeader) + sizeof(uint32_t) +
      typeName.size() + sizeof(uint32_t) + repr.size() + sizeof(uint32_t) +
      (attrIds.size() * 2 * sizeof(uint64_t));
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset =
      writeObjectHeaderUnlocked(ObjectType::SerializedObject);
  writeStringToHeapUnlocked(typeName);
  writeStringToHeapUnlocked(repr);
  writeKeyValuePairsToHeapUnlocked(attrIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::SerializedObject, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeSerializedList(
    const std::vector<uint64_t>& elementIds,
    const std::string& typeName,
    const std::string& repr,
    const std::vector<std::pair<uint64_t, uint64_t>>& attrIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer = SnapshotStatsCollector::getInstance().objectWriteTimer(
      ObjectType::SerializedList);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeSerializedSequenceUnlocked(
      ObjectType::SerializedList, elementIds, typeName, repr, attrIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::SerializedList, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeSerializedSet(
    const std::vector<uint64_t>& elementIds,
    const std::string& typeName,
    const std::string& repr,
    const std::vector<std::pair<uint64_t, uint64_t>>& attrIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer = SnapshotStatsCollector::getInstance().objectWriteTimer(
      ObjectType::SerializedSet);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeSerializedSequenceUnlocked(
      ObjectType::SerializedSet, elementIds, typeName, repr, attrIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::SerializedSet, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeSerializedTuple(
    const std::vector<uint64_t>& elementIds,
    const std::string& typeName,
    const std::string& repr,
    const std::vector<std::pair<uint64_t, uint64_t>>& attrIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer = SnapshotStatsCollector::getInstance().objectWriteTimer(
      ObjectType::SerializedTuple);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeSerializedSequenceUnlocked(
      ObjectType::SerializedTuple, elementIds, typeName, repr, attrIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::SerializedTuple, bytesWritten);

  return objectOffset;
}

uint64_t SnapshotWriter::writeSerializedDict(
    const std::vector<std::pair<uint64_t, uint64_t>>& keyValueIds,
    const std::string& typeName,
    const std::string& repr,
    const std::vector<std::pair<uint64_t, uint64_t>>& attrIds) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto timer = SnapshotStatsCollector::getInstance().objectWriteTimer(
      ObjectType::SerializedDict);

  size_t requiredSpace = sizeof(ObjectHeapRecordHeader) + sizeof(uint32_t) +
      (keyValueIds.size() * 2 * sizeof(uint64_t)) + sizeof(uint32_t) +
      typeName.size() + sizeof(uint32_t) + repr.size() + sizeof(uint32_t) +
      (attrIds.size() * 2 * sizeof(uint64_t));
  ensureHeapSpaceUnlocked(requiredSpace);

  size_t startOffset = objectHeapOffset_;
  uint64_t objectOffset = writeObjectHeaderUnlocked(ObjectType::SerializedDict);
  writeKeyValuePairsToHeapUnlocked(keyValueIds);
  writeStringToHeapUnlocked(typeName);
  writeStringToHeapUnlocked(repr);
  writeKeyValuePairsToHeapUnlocked(attrIds);

  // Record stats
  size_t bytesWritten = objectHeapOffset_ - startOffset;
  SnapshotStatsCollector::getInstance().addObjectWriteStats(
      ObjectType::SerializedDict, bytesWritten);

  return objectOffset;
}

size_t SnapshotWriter::getObjectHeapOffset() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return objectHeapOffset_;
}

uint64_t SnapshotWriter::lookupObject(uint64_t pythonId) const {
  std::lock_guard<std::mutex> lock(mutex_);
  auto it = objectIdToOffset_.find(pythonId);
  if (it != objectIdToOffset_.end()) {
    return it->second;
  }
  return 0;
}

bool SnapshotWriter::hasObject(uint64_t pythonId) const {
  // No lock needed - only called during single-threaded snapshot processing
  return objectIdToOffset_.find(pythonId) != objectIdToOffset_.end();
}

bool SnapshotWriter::tryLookupObject(uint64_t pythonId, uint64_t& outOffset)
    const {
  // No lock needed - only called during single-threaded snapshot processing
  auto it = objectIdToOffset_.find(pythonId);
  if (it != objectIdToOffset_.end()) {
    outOffset = it->second;
    return true;
  }
  return false;
}

void SnapshotWriter::registerObject(uint64_t pythonId, uint64_t offset) {
  // No lock needed - only called during single-threaded snapshot processing
  objectIdToOffset_[pythonId] = offset;
}

uint64_t SnapshotWriter::lookupObjectWithHash(
    uint64_t pythonId,
    uint64_t identityHash) const {
  // No lock needed - only called during single-threaded snapshot processing
  auto it = globalObjectCache_.find(pythonId);
  if (it != globalObjectCache_.end()) {
    // Check if the identity hash matches (object hasn't changed)
    if (it->second.second == identityHash) {
      SnapshotStatsCollector::getInstance().incrementObjectsCacheHit();
      return it->second.first; // Return the cached heap offset
    }
  }
  return 0; // Not found or changed
}

void SnapshotWriter::registerObjectWithHash(
    uint64_t pythonId,
    uint64_t offset,
    uint64_t identityHash) {
  // No lock needed - only called during single-threaded snapshot processing
  globalObjectCache_[pythonId] = {offset, identityHash};
}

void SnapshotWriter::registerObjectWithBothCaches(
    uint64_t pythonId,
    uint64_t offset,
    uint64_t identityHash) {
  // No lock needed - only called during single-threaded snapshot processing
  objectIdToOffset_[pythonId] = offset;
  globalObjectCache_[pythonId] = {offset, identityHash};
}

void SnapshotWriter::addReferencedFile(const std::string& filePath) {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!initialized_) {
    throw std::runtime_error("SnapshotWriter not initialized");
  }

  referencedFiles_.insert(filePath);
}

uint64_t SnapshotWriter::beginSnapshot() {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!initialized_) {
    throw std::runtime_error("SnapshotWriter not initialized");
  }

  if (snapshotInProgress_) {
    throw std::runtime_error("Snapshot already in progress");
  }

  // Record start time for this snapshot
  SnapshotStatsCollector::getInstance().recordSnapshotStart();

  // Ensure snapshot records section has space for the header
  ensureSnapshotRecordSpaceUnlocked(sizeof(SnapshotRecordHeader));

  // Record where this snapshot starts
  currentSnapshotPos_ = writeOffset_;
  currentStacktraceCount_ = 0;
  currentFrameCount_ = 0;
  stacktraceInProgress_ = false;
  snapshotInProgress_ = true;

  // Write the snapshot record header with placeholder values
  // We'll update objectMapPos and objectMapCount in endSnapshot()
  SnapshotRecordHeader header{};

  // Get current timestamp in microseconds
  auto now = std::chrono::system_clock::now();
  auto duration = now.time_since_epoch();
  header.timestamp =
      std::chrono::duration_cast<std::chrono::microseconds>(duration).count();

  header.prevSnapshotPos = lastSnapshotPos_;
  header.stacktraceCount = 0; // Will be updated in endSnapshot()
  header.objectMapPos = 0; // Will be updated in endSnapshot()
  header.objectMapCount = 0; // Will be updated in endSnapshot()

  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + writeOffset_,
      &header,
      sizeof(header));
  writeOffset_ += sizeof(header);

  return currentSnapshotPos_;
}

void SnapshotWriter::beginStacktrace(
    uint64_t id,
    const std::string& threadName,
    uint64_t exceptionPythonId,
    uint64_t causeId,
    uint64_t contextId) {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!initialized_) {
    throw std::runtime_error("SnapshotWriter not initialized");
  }

  if (!snapshotInProgress_) {
    throw std::runtime_error("No snapshot in progress");
  }

  if (stacktraceInProgress_) {
    throw std::runtime_error("Stacktrace already in progress");
  }

  // Ensure snapshot records section has space for the header + thread name
  ensureSnapshotRecordSpaceUnlocked(
      sizeof(StacktraceRecordHeader) + threadName.size());

  // Record where this stacktrace starts
  currentStacktracePos_ = writeOffset_;
  currentFrameCount_ = 0;
  stacktraceInProgress_ = true;

  // Write the stacktrace record header with placeholder values
  StacktraceRecordHeader header{};
  header.id = id;
  header.frameCount = 0; // Will be updated in endStacktrace()
  header.exceptionPythonId = exceptionPythonId;
  // Use self-reference (id) when UINT64_MAX is passed to indicate "none"
  header.causeId = (causeId == UINT64_MAX) ? id : causeId;
  header.contextId = (contextId == UINT64_MAX) ? id : contextId;
  header.threadNameLength = static_cast<uint32_t>(threadName.size());

  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + writeOffset_,
      &header,
      sizeof(header));
  writeOffset_ += sizeof(header);

  // Write the thread name bytes immediately after the header
  if (!threadName.empty()) {
    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + writeOffset_,
        threadName.data(),
        threadName.size());
    writeOffset_ += threadName.size();
  }
}

void SnapshotWriter::endStacktrace(bool truncated, bool objectDepthTruncated) {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!initialized_) {
    throw std::runtime_error("SnapshotWriter not initialized");
  }

  if (!snapshotInProgress_) {
    throw std::runtime_error("No snapshot in progress");
  }

  if (!stacktraceInProgress_) {
    throw std::runtime_error("No stacktrace in progress");
  }

  // Update the stacktrace header with final frame count and flags
  StacktraceRecordHeader* header = reinterpret_cast<StacktraceRecordHeader*>(
      static_cast<char*>(snapshotFilePtr_) + currentStacktracePos_);
  header->frameCount = currentFrameCount_;
  header->flags = (truncated ? kStacktraceTruncated : 0) |
      (objectDepthTruncated ? kObjectDepthTruncated : 0);

  // Increment stacktrace count for this snapshot
  currentStacktraceCount_++;

  // Reset stacktrace state
  currentStacktracePos_ = 0;
  currentFrameCount_ = 0;
  stacktraceInProgress_ = false;
}

void SnapshotWriter::discardCurrentStacktrace() {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!stacktraceInProgress_) {
    return;
  }

  // Roll back writeOffset to the stacktrace header position
  writeOffset_ = currentStacktracePos_;

  // Reset stacktrace state without incrementing stacktrace count
  currentStacktracePos_ = 0;
  currentFrameCount_ = 0;
  stacktraceInProgress_ = false;
}

void SnapshotWriter::writeFrameRecord(
    const std::string& filePath,
    const std::string& functionName,
    const std::string& functionQualName,
    uint32_t lineNumber,
    const std::vector<std::pair<std::string, uint64_t>>& localVars) {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!initialized_) {
    throw std::runtime_error("SnapshotWriter not initialized");
  }

  if (!snapshotInProgress_) {
    throw std::runtime_error("No snapshot in progress");
  }

  if (!stacktraceInProgress_) {
    throw std::runtime_error("No stacktrace in progress");
  }

  // Calculate required space
  uint32_t filePathLength = static_cast<uint32_t>(filePath.size());
  uint32_t coNameLength = static_cast<uint32_t>(functionName.size());
  uint32_t coQualNameLength = static_cast<uint32_t>(functionQualName.size());
  uint32_t localVarCount = static_cast<uint32_t>(localVars.size());

  // Calculate total bytes needed for this frame record
  size_t frameRecordSize = sizeof(filePathLength) + filePath.size() +
      sizeof(coNameLength) + functionName.size() + sizeof(coQualNameLength) +
      functionQualName.size() + sizeof(lineNumber) + sizeof(localVarCount);
  for (const auto& [varName, pythonId] : localVars) {
    frameRecordSize += sizeof(pythonId) + sizeof(uint32_t) + varName.size();
  }
  ensureSnapshotRecordSpaceUnlocked(frameRecordSize);

  // Write filePathLength
  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + writeOffset_,
      &filePathLength,
      sizeof(filePathLength));
  writeOffset_ += sizeof(filePathLength);

  // Write file path bytes
  if (!filePath.empty()) {
    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + writeOffset_,
        filePath.data(),
        filePath.size());
    writeOffset_ += filePath.size();
  }

  // Write coNameLength
  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + writeOffset_,
      &coNameLength,
      sizeof(coNameLength));
  writeOffset_ += sizeof(coNameLength);

  // Write co_name bytes
  if (!functionName.empty()) {
    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + writeOffset_,
        functionName.data(),
        functionName.size());
    writeOffset_ += functionName.size();
  }

  // Write coQualNameLength
  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + writeOffset_,
      &coQualNameLength,
      sizeof(coQualNameLength));
  writeOffset_ += sizeof(coQualNameLength);

  // Write co_qualname bytes
  if (!functionQualName.empty()) {
    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + writeOffset_,
        functionQualName.data(),
        functionQualName.size());
    writeOffset_ += functionQualName.size();
  }

  // Write lineNumber
  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + writeOffset_,
      &lineNumber,
      sizeof(lineNumber));
  writeOffset_ += sizeof(lineNumber);

  // Write localVarCount
  std::memcpy(
      static_cast<char*>(snapshotFilePtr_) + writeOffset_,
      &localVarCount,
      sizeof(localVarCount));
  writeOffset_ += sizeof(localVarCount);

  // Write each local variable record
  for (const auto& [varName, pythonId] : localVars) {
    // Write pythonId
    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + writeOffset_,
        &pythonId,
        sizeof(pythonId));
    writeOffset_ += sizeof(pythonId);

    // Write nameLength
    uint32_t nameLength = static_cast<uint32_t>(varName.size());
    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + writeOffset_,
        &nameLength,
        sizeof(nameLength));
    writeOffset_ += sizeof(nameLength);

    // Write variable name bytes
    if (!varName.empty()) {
      std::memcpy(
          static_cast<char*>(snapshotFilePtr_) + writeOffset_,
          varName.data(),
          varName.size());
      writeOffset_ += varName.size();
    }

    // Note: objectHeapOffset is not stored here - readers should look up
    // the pythonId in the snapshot's object map to get the heap offset
  }

  currentFrameCount_++;
}

void SnapshotWriter::endSnapshot(bool truncated) {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!initialized_) {
    throw std::runtime_error("SnapshotWriter not initialized");
  }

  if (!snapshotInProgress_) {
    throw std::runtime_error("No snapshot in progress");
  }

  if (stacktraceInProgress_) {
    throw std::runtime_error(
        "Stacktrace still in progress - call endStacktrace first");
  }

  // Ensure space for the object map table
  ensureSnapshotRecordSpaceUnlocked(
      objectIdToOffset_.size() * sizeof(ObjectMapRecord));

  // Record where the object map table starts
  uint64_t objectMapPos = writeOffset_;
  uint32_t objectMapCount = static_cast<uint32_t>(objectIdToOffset_.size());

  // Write the object map table
  for (const auto& [pythonId, offset] : objectIdToOffset_) {
    ObjectMapRecord record{};
    record.pythonId = pythonId;
    record.objectHeapOffset = offset;

    std::memcpy(
        static_cast<char*>(snapshotFilePtr_) + writeOffset_,
        &record,
        sizeof(record));
    writeOffset_ += sizeof(record);
  }

  // Update the snapshot header with final values
  SnapshotRecordHeader* header = reinterpret_cast<SnapshotRecordHeader*>(
      static_cast<char*>(snapshotFilePtr_) + currentSnapshotPos_);
  header->stacktraceCount = currentStacktraceCount_;
  header->objectMapPos = objectMapPos;
  header->objectMapCount = objectMapCount;
  header->flags = truncated ? kSnapshotTruncated : 0;

  // Update lastSnapshotPos and snapshot count
  lastSnapshotPos_ = currentSnapshotPos_;
  snapshotCount_++;

  // Update the file header so readers sharing this memory can see the change
  updateHeader();

  // Reset snapshot state
  currentSnapshotPos_ = 0;
  currentStacktraceCount_ = 0;
  currentFrameCount_ = 0;
  stacktraceInProgress_ = false;
  snapshotInProgress_ = false;

  // Record snapshot timing
  SnapshotStatsCollector::getInstance().recordSnapshotEnd();

  // Clear object ID mapping for next snapshot
  objectIdToOffset_.clear();
}

void SnapshotWriter::discardCurrentSnapshot() {
  std::lock_guard<std::mutex> lock(mutex_);

  if (!snapshotInProgress_) {
    return;
  }

  SnapshotStatsCollector::getInstance().incrementSnapshotsDiscarded();

  // Roll back writeOffset to the snapshot header position
  writeOffset_ = currentSnapshotPos_;

  // Reset all snapshot state without updating lastSnapshotPos
  currentSnapshotPos_ = 0;
  currentStacktraceCount_ = 0;
  currentFrameCount_ = 0;
  stacktraceInProgress_ = false;
  snapshotInProgress_ = false;

  // Clear object ID mapping
  objectIdToOffset_.clear();
}

void SnapshotWriter::rollbackWriteOffset(size_t savedOffset) {
  std::lock_guard<std::mutex> lock(mutex_);
  writeOffset_ = savedOffset;
  if (currentFrameCount_ > 0) {
    currentFrameCount_--;
  }
}

uint32_t SnapshotWriter::getCurrentFrameCount() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return currentFrameCount_;
}

uint32_t SnapshotWriter::getCurrentStacktraceCount() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return currentStacktraceCount_;
}

} // namespace facebook::tintype::snapshot
