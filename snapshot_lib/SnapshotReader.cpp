// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#include "SnapshotReader.h"

#include <pybind11/pybind11.h>

#include <dirent.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <zstd.h>

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <vector>

namespace facebook::tintype::snapshot {

SnapshotReader::SnapshotReader(const std::string& path) {
  if (!open(path)) {
    throw std::runtime_error(lastError_);
  }
}

SnapshotReader::SnapshotReader(
    void* data,
    size_t dataSize,
    const std::string& filePath)
    : tempFilePath_(filePath), ownsData_(false) {
  if (!initFromMemory(data, dataSize)) {
    throw std::runtime_error(lastError_);
  }
}

SnapshotReader::~SnapshotReader() {
  close();
}

SnapshotReader::SnapshotReader(SnapshotReader&& other) noexcept
    : data_(other.data_),
      dataSize_(other.dataSize_),
      fileFd_(other.fileFd_),
      tempFd_(other.tempFd_),
      tempFilePath_(std::move(other.tempFilePath_)),
      isOpen_(other.isOpen_),
      ownsData_(other.ownsData_),
      lastError_(std::move(other.lastError_)) {
  other.data_ = nullptr;
  other.dataSize_ = 0;
  other.tempFd_ = -1;
  other.fileFd_ = -1;
  other.isOpen_ = false;
  other.ownsData_ = true;
}

SnapshotReader& SnapshotReader::operator=(SnapshotReader&& other) noexcept {
  if (this != &other) {
    close();
    data_ = other.data_;
    dataSize_ = other.dataSize_;
    tempFd_ = other.tempFd_;
    tempFilePath_ = std::move(other.tempFilePath_);
    fileFd_ = other.fileFd_;
    isOpen_ = other.isOpen_;
    ownsData_ = other.ownsData_;
    lastError_ = std::move(other.lastError_);
    other.data_ = nullptr;
    other.dataSize_ = 0;
    other.tempFd_ = -1;
    other.fileFd_ = -1;
    other.isOpen_ = false;
    other.ownsData_ = true;
  }
  return *this;
}

bool SnapshotReader::open(const std::string& path) {
  close();
  lastError_.clear();

  // Read the file
  std::ifstream file(path, std::ios::binary | std::ios::ate);
  if (!file) {
    lastError_ = "Failed to open file: " + path;
    return false;
  }

  std::streamsize fileSize = file.tellg();
  if (fileSize <= 0) {
    lastError_ = "File is empty or failed to get file size: " + path;
    return false;
  }
  file.seekg(0, std::ios::beg);

  // Read enough to check the magic number (4 bytes)
  uint32_t magic = 0;
  if (fileSize >= 4) {
    file.read(reinterpret_cast<char*>(&magic), sizeof(magic));
    file.seekg(0, std::ios::beg);
  }

  if (magic == SnapshotWriter::kMagicNumber) {
    // File is uncompressed — mmap it directly
    file.close();

    int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) {
      lastError_ = "Failed to open file for mmap: " + path +
          " (errno: " + std::to_string(errno) + ")";
      return false;
    }

    data_ = mmap(nullptr, fileSize, PROT_READ, MAP_SHARED, fd, 0);

    // NOLINTNEXTLINE(performance-no-int-to-ptr)
    if (data_ == MAP_FAILED) {
      lastError_ = "Failed to mmap file (errno: " + std::to_string(errno) + ")";
      data_ = nullptr;
      ::close(fd);
      return false;
    }
    dataSize_ = fileSize;
    // Keep the fd open so we can re-mmap if the file grows
    fileFd_ = fd;
  } else {
    // Assume file is zstd compressed
    std::vector<char> compressedData(fileSize);
    if (!file.read(compressedData.data(), fileSize)) {
      lastError_ = "Failed to read file contents: " + path;
      return false;
    }
    file.close();

    // Get decompressed size
    unsigned long long decompressedSize =
        ZSTD_getFrameContentSize(compressedData.data(), fileSize);
    if (decompressedSize == ZSTD_CONTENTSIZE_ERROR) {
      lastError_ = "Not a valid snapshot file (unrecognized format)";
      return false;
    }
    if (decompressedSize == ZSTD_CONTENTSIZE_UNKNOWN) {
      lastError_ = "Zstd compressed data has unknown content size";
      return false;
    }

    // Create a unique temporary file for the decompressed data.
    // mkstemp guarantees a fresh path so concurrent readers of the same
    // compressed source file (e.g., a sibling process attaching
    // tintype_debug_launcher to the same .pytb) cannot collide on this temp
    // file. With a shared name, one reader's O_TRUNC + ftruncate corrupts
    // another reader's MAP_SHARED mmap (zero-fills the buffer through the
    // shared inode) — see SnapshotWriter::open which already uses mkstemp
    // for the same reason.
    std::string tempTemplate = path + ".decompressed.XXXXXX";
    tempFd_ = ::mkstemp(tempTemplate.data());
    if (tempFd_ < 0) {
      lastError_ = "Failed to create temporary file: " + tempTemplate +
          " (errno: " + std::to_string(errno) + ")";
      return false;
    }
    tempFilePath_ = std::move(tempTemplate);

    // Extend the file to the decompressed size
    if (ftruncate(tempFd_, decompressedSize) < 0) {
      lastError_ = "Failed to extend temporary file to " +
          std::to_string(decompressedSize) +
          " bytes (errno: " + std::to_string(errno) + ")";
      ::close(tempFd_);
      tempFd_ = -1;
      unlink(tempFilePath_.c_str());
      tempFilePath_.clear();
      return false;
    }

    // mmap the temporary file
    data_ = mmap(
        nullptr,
        decompressedSize,
        PROT_READ | PROT_WRITE,
        MAP_SHARED,
        tempFd_,
        0);
    // NOLINTNEXTLINE(performance-no-int-to-ptr)
    if (data_ == MAP_FAILED) {
      lastError_ =
          "Failed to mmap temporary file (errno: " + std::to_string(errno) +
          ")";
      data_ = nullptr;
      ::close(tempFd_);
      tempFd_ = -1;
      unlink(tempFilePath_.c_str());
      tempFilePath_.clear();
      return false;
    }
    dataSize_ = decompressedSize;

    // Decompress directly into the mmap'd buffer
    size_t result = ZSTD_decompress(
        data_, decompressedSize, compressedData.data(), fileSize);
    if (ZSTD_isError(result)) {
      lastError_ = "Zstd decompression failed: " +
          std::string(ZSTD_getErrorName(result));
      munmap(data_, dataSize_);
      data_ = nullptr;
      dataSize_ = 0;
      ::close(tempFd_);
      tempFd_ = -1;
      unlink(tempFilePath_.c_str());
      tempFilePath_.clear();
      return false;
    }
  }

  // Validate header
  if (dataSize_ < sizeof(FileHeader)) {
    lastError_ = "Data too small for header (got " + std::to_string(dataSize_) +
        " bytes, need " + std::to_string(sizeof(FileHeader)) + ")";
    munmap(data_, dataSize_);
    data_ = nullptr;
    dataSize_ = 0;
    if (tempFd_ >= 0) {
      ::close(tempFd_);
      tempFd_ = -1;
    }
    if (!tempFilePath_.empty()) {
      unlink(tempFilePath_.c_str());
      tempFilePath_.clear();
    }
    return false;
  }

  FileHeader header;
  std::memcpy(&header, data_, sizeof(FileHeader));

  if (header.magic != SnapshotWriter::kMagicNumber) {
    lastError_ = "Invalid magic number: expected 0x" +
        std::to_string(SnapshotWriter::kMagicNumber) + ", got 0x" +
        std::to_string(header.magic);
    munmap(data_, dataSize_);
    data_ = nullptr;
    dataSize_ = 0;
    if (tempFd_ >= 0) {
      ::close(tempFd_);
      tempFd_ = -1;
    }
    if (!tempFilePath_.empty()) {
      unlink(tempFilePath_.c_str());
      tempFilePath_.clear();
    }
    return false;
  }

  if (header.version != SnapshotWriter::kFormatVersion) {
    lastError_ = "Unsupported format version: expected " +
        std::to_string(SnapshotWriter::kFormatVersion) + ", got " +
        std::to_string(header.version);
    munmap(data_, dataSize_);
    data_ = nullptr;
    dataSize_ = 0;
    if (tempFd_ >= 0) {
      ::close(tempFd_);
      tempFd_ = -1;
    }
    if (!tempFilePath_.empty()) {
      unlink(tempFilePath_.c_str());
      tempFilePath_.clear();
    }
    return false;
  }

  isOpen_ = true;

  // Extract source files from file table to temporary directory
  if (!extractSourceFiles()) {
    // Extraction failed, but we can still use the reader
    // The error message is set in extractSourceFiles()
  }

  return true;
}

bool SnapshotReader::initFromMemory(void* data, size_t dataSize) {
  close();
  lastError_.clear();
  ownsData_ = false;

  if (data == nullptr || dataSize == 0) {
    lastError_ = "Invalid memory buffer (null or zero size)";
    return false;
  }

  data_ = data;
  dataSize_ = dataSize;

  // Validate header
  if (dataSize_ < sizeof(FileHeader)) {
    lastError_ = "Data too small for header (got " + std::to_string(dataSize_) +
        " bytes, need " + std::to_string(sizeof(FileHeader)) + ")";
    data_ = nullptr;
    dataSize_ = 0;
    return false;
  }

  FileHeader header;
  std::memcpy(&header, data_, sizeof(FileHeader));

  if (header.magic != SnapshotWriter::kMagicNumber) {
    lastError_ = "Invalid magic number: expected 0x" +
        std::to_string(SnapshotWriter::kMagicNumber) + ", got 0x" +
        std::to_string(header.magic);
    data_ = nullptr;
    dataSize_ = 0;
    return false;
  }

  if (header.version != SnapshotWriter::kFormatVersion) {
    lastError_ = "Unsupported format version: expected " +
        std::to_string(SnapshotWriter::kFormatVersion) + ", got " +
        std::to_string(header.version);
    data_ = nullptr;
    dataSize_ = 0;
    return false;
  }

  isOpen_ = true;

  // Note: We don't extract source files for borrowed-memory readers.
  // Source files are only finalized when the writer calls finalize(),
  // but this reader is created before that. Source files can be accessed
  // after finalize() by creating a new reader from the output file.

  return true;
}

void SnapshotReader::close() {
  // Clear the object cache first since it holds Python object references
  objectCache_.clear();

  if (data_ != nullptr) {
    if (ownsData_) {
      munmap(data_, dataSize_);
    }
    data_ = nullptr;
    dataSize_ = 0;
  }

  if (tempFd_ >= 0) {
    ::close(tempFd_);
    tempFd_ = -1;
  }

  if (fileFd_ >= 0) {
    ::close(fileFd_);
    fileFd_ = -1;
  }

  if (ownsData_ && !tempFilePath_.empty()) {
    unlink(tempFilePath_.c_str());
    tempFilePath_.clear();
  }

  // Clean up extracted files directory
  if (!extractedFilesDir_.empty()) {
    removeDirectory(extractedFilesDir_);
    extractedFilesDir_.clear();
  }
  extractedFilePathMap_.clear();

  isOpen_ = false;
  ownsData_ = true;
}

bool SnapshotReader::isOpen() const {
  return isOpen_;
}

void SnapshotReader::updateMemory(void* data, size_t dataSize) {
  data_ = data;
  dataSize_ = dataSize;
}

const std::string& SnapshotReader::getExtractedFilesDir() const {
  return extractedFilesDir_;
}

const std::string& SnapshotReader::getWorkingFilePath() const {
  return tempFilePath_;
}

void SnapshotReader::removeDirectory(const std::string& path) {
  DIR* dir = opendir(path.c_str());
  if (dir == nullptr) {
    return;
  }

  struct dirent* entry;
  while ((entry = readdir(dir)) != nullptr) {
    // Skip . and ..
    if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
      continue;
    }

    std::string fullPath = path + "/" + entry->d_name;

    struct stat statBuf{};
    if (lstat(fullPath.c_str(), &statBuf) == 0) {
      if (S_ISDIR(statBuf.st_mode)) {
        // Recursively remove subdirectory
        removeDirectory(fullPath);
      } else {
        // Remove file
        unlink(fullPath.c_str());
      }
    }
  }

  closedir(dir);
  rmdir(path.c_str());
}

bool SnapshotReader::extractSourceFiles() {
  auto header = getHeader();
  if (!isOpen_ || header.fileTableCount == 0) {
    return true; // Nothing to extract
  }

  // Track whether we need to create the temp directory (lazy creation)
  bool tempDirCreated = false;

  // Extract each file from the file table
  size_t offset = header.fileTablePos;
  for (uint32_t i = 0; i < header.fileTableCount; ++i) {
    auto [file, bytesRead] = readSourceFileAt(offset);
    if (bytesRead == 0) {
      // Error reading file, but continue with other files
      offset += sizeof(FileTableRecordHeader);
      continue;
    }

    if (!file.path.empty()) {
      // Check if the original file exists and has matching content.
      // Use open()+fstat() instead of stat() to avoid TOCTOU races.
      bool useOriginalFile = false;
      int fd = ::open(file.path.c_str(), O_RDONLY);
      if (fd >= 0) {
        struct stat statBuf{};
        if (fstat(fd, &statBuf) == 0 && S_ISREG(statBuf.st_mode)) {
          // File exists, check if size matches first (quick check)
          if (static_cast<size_t>(statBuf.st_size) == file.content.size()) {
            // Size matches, read and compare content
            std::string existingContent(file.content.size(), '\0');
            ssize_t nread =
                ::read(fd, existingContent.data(), file.content.size());
            if (nread == static_cast<ssize_t>(file.content.size()) &&
                existingContent == file.content) {
              // Content matches, use the original file path
              useOriginalFile = true;
            }
          }
        }
        ::close(fd);
      }

      if (useOriginalFile) {
        // Original file matches, no mapping needed (readFrameAt will use
        // the original path directly when it's not found in the map)
      } else {
        // Need to extract to temp directory
        // Create temp directory if not already created
        if (!tempDirCreated) {
          char tempDirTemplate[] = "/tmp/snapshot_files_XXXXXX";
          char* tempDir = mkdtemp(tempDirTemplate);
          if (tempDir == nullptr) {
            lastError_ =
                "Failed to create temporary directory for extracted files";
            return false;
          }
          extractedFilesDir_ = tempDir;
          tempDirCreated = true;
        }

        // Create the full path within the temp directory
        std::string extractedPath = extractedFilesDir_ + file.path;

        // Create parent directories as needed
        size_t pos = 0;
        while ((pos = extractedPath.find('/', pos + 1)) != std::string::npos) {
          std::string dirPath = extractedPath.substr(0, pos);
          mkdir(dirPath.c_str(), 0755);
        }

        // Write the file content
        std::ofstream outFile(extractedPath, std::ios::binary);
        if (outFile) {
          outFile.write(file.content.data(), file.content.size());
        }

        // Map original path to extracted path
        extractedFilePathMap_[file.path] = extractedPath;
      }
    }

    offset += bytesRead;
  }

  return true;
}

const std::string& SnapshotReader::getLastError() const {
  return lastError_;
}

FileHeader SnapshotReader::getHeader() const {
  return readValue<FileHeader>(0);
}

std::string SnapshotReader::getMetadata() const {
  if (!isOpen_) {
    return "";
  }

  auto header = getHeader();
  if (header.metadataSize == 0) {
    return "{}";
  }

  // Read the JSON string directly using the size from the header
  return readString(header.metadataPos, header.metadataSize);
}

std::string SnapshotReader::getManifest() const {
  if (!isOpen_) {
    return "";
  }

  auto header = getHeader();
  if (header.manifestSize == 0) {
    return "";
  }

  // Read the JSON string directly using the size from the header
  return readString(header.manifestPos, header.manifestSize);
}

std::unordered_map<std::string, std::string> SnapshotReader::getEnvironment()
    const {
  std::unordered_map<std::string, std::string> result;
  if (!isOpen_) {
    return result;
  }

  auto header = getHeader();
  if (header.envSize == 0) {
    return result;
  }

  // Bounds check: ensure the environment section is within the mapped region
  if (header.envPos + header.envSize > dataSize_) {
    return result;
  }

  // Parse null-terminated KEY=VALUE strings
  const char* data = static_cast<const char*>(data_) + header.envPos;
  const char* end = data + header.envSize;

  while (data < end) {
    // Find the null terminator for this entry
    const char* entryEnd =
        static_cast<const char*>(std::memchr(data, '\0', end - data));
    if (!entryEnd) {
      break;
    }

    // Split on first '='
    const char* eq =
        static_cast<const char*>(std::memchr(data, '=', entryEnd - data));
    if (eq) {
      std::string key(data, eq - data);
      std::string value(eq + 1, entryEnd - (eq + 1));
      result[std::move(key)] = std::move(value);
    }

    data = entryEnd + 1; // skip past the null terminator
  }

  return result;
}

std::optional<Snapshot> SnapshotReader::getLatestSnapshot() {
  if (!isOpen_) {
    return std::nullopt;
  }
  auto header = getHeader();
  if (header.lastSnapshotPos == 0) {
    return std::nullopt;
  }

  return readSnapshotAt(header.lastSnapshotPos);
}

std::optional<Snapshot> SnapshotReader::getSnapshotAtIndex(size_t index) {
  if (!isOpen_) {
    return std::nullopt;
  }
  auto header = getHeader();
  if (header.snapshotCount == 0 || index >= header.snapshotCount) {
    return std::nullopt;
  }

  // Walk backwards from lastSnapshotPos to reach the target index.
  // Chronological index 0 = oldest, so we need to walk
  // (snapshotCount - 1 - index) steps from the newest.
  size_t stepsBack = header.snapshotCount - 1 - index;
  uint64_t offset = header.lastSnapshotPos;
  for (size_t i = 0; i < stepsBack; ++i) {
    SnapshotRecordHeader snapshotHeader;
    std::memcpy(
        &snapshotHeader,
        static_cast<const char*>(data_) + offset,
        sizeof(snapshotHeader));
    offset = snapshotHeader.prevSnapshotPos;
  }

  return readSnapshotAt(offset);
}

std::vector<Snapshot> SnapshotReader::getAllSnapshots() {
  std::vector<Snapshot> snapshots;

  if (!isOpen_) {
    return snapshots;
  }
  auto header = getHeader();
  if (header.lastSnapshotPos == 0) {
    return snapshots;
  }

  // Walk the linked list backwards
  uint64_t offset = header.lastSnapshotPos;
  while (offset != 0) {
    Snapshot snapshot = readSnapshotAt(offset);
    snapshots.push_back(std::move(snapshot));
    offset = snapshots.back().prevSnapshotPos;
  }

  // Reverse to get chronological order
  std::reverse(snapshots.begin(), snapshots.end());
  return snapshots;
}

Snapshot SnapshotReader::getSnapshotAtPosition(uint64_t position) {
  return readSnapshotAt(position);
}

Snapshot SnapshotReader::readSnapshotAt(uint64_t offset) {
  Snapshot snapshot;

  // Read snapshot header
  SnapshotRecordHeader snapshotHeader;
  std::memcpy(
      &snapshotHeader,
      static_cast<const char*>(data_) + offset,
      sizeof(snapshotHeader));

  snapshot.timestamp = snapshotHeader.timestamp;
  snapshot.prevSnapshotPos = snapshotHeader.prevSnapshotPos;
  snapshot.position = offset;
  snapshot.truncated =
      (snapshotHeader.flags & SnapshotWriter::kSnapshotTruncated) != 0;

  // Read object map first so we can resolve exception objects inline
  size_t mapOffset = snapshotHeader.objectMapPos;
  for (uint32_t i = 0; i < snapshotHeader.objectMapCount; ++i) {
    ObjectMapRecord record;
    std::memcpy(
        &record, static_cast<const char*>(data_) + mapOffset, sizeof(record));
    snapshot.objectMap[record.pythonId] = record.objectHeapOffset;
    mapOffset += sizeof(ObjectMapRecord);
  }

  // Read stacktraces
  size_t currentOffset = offset + sizeof(SnapshotRecordHeader);
  for (uint32_t t = 0; t < snapshotHeader.stacktraceCount; ++t) {
    // Read stacktrace header
    StacktraceRecordHeader stacktraceHeader;
    std::memcpy(
        &stacktraceHeader,
        static_cast<const char*>(data_) + currentOffset,
        sizeof(stacktraceHeader));
    currentOffset += sizeof(StacktraceRecordHeader);

    Stacktrace stacktrace;
    stacktrace.id = stacktraceHeader.id;
    stacktrace.causeId = stacktraceHeader.causeId;
    stacktrace.contextId = stacktraceHeader.contextId;
    stacktrace.truncated =
        (stacktraceHeader.flags & SnapshotWriter::kStacktraceTruncated) != 0;
    stacktrace.objectDepthTruncated =
        (stacktraceHeader.flags & SnapshotWriter::kObjectDepthTruncated) != 0;

    // Read thread name bytes following the header
    if (stacktraceHeader.threadNameLength > 0) {
      stacktrace.threadName =
          readString(currentOffset, stacktraceHeader.threadNameLength);
      currentOffset += stacktraceHeader.threadNameLength;
    }

    // Resolve exception object from pythonId
    uint64_t excPythonId = stacktraceHeader.exceptionPythonId;
    if (excPythonId != 0) {
      stacktrace.exceptionObject =
          getPythonObject(excPythonId, snapshot.objectMap);
    } else {
      stacktrace.exceptionObject = py::none();
    }

    // Read frames for this stacktrace
    for (uint32_t i = 0; i < stacktraceHeader.frameCount; ++i) {
      auto [frame, bytesRead] = readFrameAt(currentOffset);
      stacktrace.frames.push_back(std::move(frame));
      currentOffset += bytesRead;
    }

    auto& stacktraceEntry = snapshot.stacktraces[stacktrace.id];
    stacktraceEntry = std::move(stacktrace);
  }

  return snapshot;
}

std::pair<Frame, size_t> SnapshotReader::readFrameAt(size_t offset) const {
  Frame frame;
  size_t currentOffset = offset;

  // Read filePathLength and file path string
  uint32_t filePathLength = readValue<uint32_t>(currentOffset);
  currentOffset += sizeof(uint32_t);

  std::string originalFilePath = readString(currentOffset, filePathLength);
  currentOffset += filePathLength;

  // Preserve original path before potential move
  frame.originalFilePath = originalFilePath;

  // Look up extracted path; if not in map, use original path directly
  auto it = extractedFilePathMap_.find(originalFilePath);
  if (it != extractedFilePathMap_.end()) {
    frame.filePath = it->second;
  } else {
    frame.filePath = std::move(originalFilePath);
  }

  // Read coNameLength and co_name
  uint32_t coNameLength = readValue<uint32_t>(currentOffset);
  currentOffset += sizeof(uint32_t);

  frame.functionName = readString(currentOffset, coNameLength);
  currentOffset += coNameLength;

  // Read coQualNameLength and co_qualname
  uint32_t coQualNameLength = readValue<uint32_t>(currentOffset);
  currentOffset += sizeof(uint32_t);

  frame.functionQualName = readString(currentOffset, coQualNameLength);
  currentOffset += coQualNameLength;

  // Read lineNumber
  frame.lineNumber = readValue<uint32_t>(currentOffset);
  currentOffset += sizeof(uint32_t);

  // Read localVarCount
  uint32_t localVarCount = readValue<uint32_t>(currentOffset);
  currentOffset += sizeof(uint32_t);

  // Read local variables
  for (uint32_t i = 0; i < localVarCount; ++i) {
    LocalVariable var;

    // Read pythonId
    var.pythonId = readValue<uint64_t>(currentOffset);
    currentOffset += sizeof(uint64_t);

    // Read nameLength and name
    uint32_t nameLength = readValue<uint32_t>(currentOffset);
    currentOffset += sizeof(uint32_t);

    var.name = readString(currentOffset, nameLength);
    currentOffset += nameLength;

    frame.localVariables.push_back(std::move(var));
  }

  return {std::move(frame), currentOffset - offset};
}

std::vector<SourceFile> SnapshotReader::getAllSourceFiles() const {
  std::vector<SourceFile> files;

  if (!isOpen_) {
    return files;
  }

  auto header = getHeader();
  size_t offset = header.fileTablePos;
  for (uint32_t i = 0; i < header.fileTableCount; ++i) {
    auto [file, bytesRead] = readSourceFileAt(offset);
    files.push_back(std::move(file));
    offset += bytesRead;
  }

  return files;
}

std::pair<SourceFile, size_t> SnapshotReader::readSourceFileAt(
    size_t offset) const {
  SourceFile file;
  size_t currentOffset = offset;

  // Read the FileTableRecordHeader which contains both lengths
  FileTableRecordHeader recordHeader;
  std::memcpy(
      &recordHeader,
      static_cast<const char*>(data_) + currentOffset,
      sizeof(recordHeader));
  currentOffset += sizeof(recordHeader);

  // Sanity check path length
  if (recordHeader.pathLength > 100000 ||
      currentOffset + recordHeader.pathLength > dataSize_) {
    return {std::move(file), 0};
  }

  file.path = readString(currentOffset, recordHeader.pathLength);
  currentOffset += recordHeader.pathLength;

  // Sanity check content length
  if (recordHeader.contentLength > 10000000 ||
      currentOffset + recordHeader.contentLength > dataSize_) {
    return {std::move(file), currentOffset - offset};
  }

  file.content = readString(currentOffset, recordHeader.contentLength);
  currentOffset += recordHeader.contentLength;

  return {std::move(file), currentOffset - offset};
}

std::optional<DeserializedObject> SnapshotReader::readObject(
    uint64_t offset) const {
  if (!isOpen_) {
    return std::nullopt;
  }

  // Check for magic offsets
  if (isMagicOffset(offset)) {
    return std::nullopt;
  }

  DeserializedObject obj;
  // Convert relative heap offset to absolute file position
  size_t currentOffset = getHeader().objectHeapPos + offset;

  // Read header
  ObjectHeapRecordHeader header;
  std::memcpy(
      &header, static_cast<const char*>(data_) + currentOffset, sizeof(header));
  currentOffset += sizeof(header);

  obj.type = header.type;

  // Read type-specific data
  switch (header.type) {
    case ObjectType::Int64: {
      obj.value = readValue<int64_t>(currentOffset);
      break;
    }

    case ObjectType::Float: {
      obj.value = readValue<double>(currentOffset);
      break;
    }

    case ObjectType::String: {
      uint32_t length = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.value = readString(currentOffset, length);
      break;
    }

    case ObjectType::Bytes: {
      uint32_t length = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      if (currentOffset + length > dataSize_) {
        return std::nullopt;
      }
      std::vector<uint8_t> bytes(length);
      std::memcpy(
          bytes.data(),
          static_cast<const char*>(data_) + currentOffset,
          length);
      obj.value = std::move(bytes);
      break;
    }

    case ObjectType::List:
    case ObjectType::Tuple:
    case ObjectType::Set: {
      uint32_t count = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      std::vector<uint64_t> elementIds(count);
      for (uint32_t i = 0; i < count; ++i) {
        elementIds[i] = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
      }
      obj.value = std::move(elementIds);
      break;
    }

    case ObjectType::Dict: {
      uint32_t count = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      std::vector<std::pair<uint64_t, uint64_t>> keyValueIds(count);
      for (uint32_t i = 0; i < count; ++i) {
        keyValueIds[i].first = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        keyValueIds[i].second = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
      }
      obj.value = std::move(keyValueIds);
      break;
    }

    case ObjectType::IntBignum: {
      uint32_t length = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.value = readString(currentOffset, length);
      break;
    }

    case ObjectType::SerializedObject: {
      // Read type name
      uint32_t typeNameLength = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.typeName = readString(currentOffset, typeNameLength);
      currentOffset += typeNameLength;

      // Read repr
      uint32_t reprLength = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.repr = readString(currentOffset, reprLength);
      currentOffset += reprLength;

      // Read attributes
      uint32_t attrCount = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      for (uint32_t i = 0; i < attrCount; ++i) {
        uint64_t nameId = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        uint64_t valueId = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        obj.attributes.emplace_back(nameId, valueId);
      }

      obj.value = nullptr;
      break;
    }

    case ObjectType::SerializedList:
    case ObjectType::SerializedSet:
    case ObjectType::SerializedTuple: {
      // Read element IDs
      uint32_t count = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      std::vector<uint64_t> elementIds(count);
      for (uint32_t i = 0; i < count; ++i) {
        elementIds[i] = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
      }
      obj.value = std::move(elementIds);

      // Read type name
      uint32_t typeNameLength = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.typeName = readString(currentOffset, typeNameLength);
      currentOffset += typeNameLength;

      // Read repr
      uint32_t reprLength = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.repr = readString(currentOffset, reprLength);
      currentOffset += reprLength;

      // Read attributes
      uint32_t attrCount = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      for (uint32_t i = 0; i < attrCount; ++i) {
        uint64_t nameId = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        uint64_t valueId = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        obj.attributes.emplace_back(nameId, valueId);
      }
      break;
    }

    case ObjectType::SerializedDict: {
      // Read key-value IDs
      uint32_t count = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      std::vector<std::pair<uint64_t, uint64_t>> keyValueIds(count);
      for (uint32_t i = 0; i < count; ++i) {
        keyValueIds[i].first = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        keyValueIds[i].second = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
      }
      obj.value = std::move(keyValueIds);

      // Read type name
      uint32_t typeNameLength = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.typeName = readString(currentOffset, typeNameLength);
      currentOffset += typeNameLength;

      // Read repr
      uint32_t reprLength = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      obj.repr = readString(currentOffset, reprLength);
      currentOffset += reprLength;

      // Read attributes
      uint32_t attrCount = readValue<uint32_t>(currentOffset);
      currentOffset += sizeof(uint32_t);
      for (uint32_t i = 0; i < attrCount; ++i) {
        uint64_t nameId = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        uint64_t valueId = readValue<uint64_t>(currentOffset);
        currentOffset += sizeof(uint64_t);
        obj.attributes.emplace_back(nameId, valueId);
      }
      break;
    }
  }

  return obj;
}

bool SnapshotReader::isMagicOffset(uint64_t offset) {
  return offset >= SnapshotWriter::kNoneOffset;
}

std::optional<std::string> SnapshotReader::getMagicOffsetType(uint64_t offset) {
  if (offset == SnapshotWriter::kNoneOffset) {
    return "None";
  }
  if (offset == SnapshotWriter::kTrueOffset) {
    return "True";
  }
  if (offset == SnapshotWriter::kFalseOffset) {
    return "False";
  }
  return std::nullopt;
}

std::map<std::string, uint64_t> SnapshotReader::getStats() const {
  std::map<std::string, uint64_t> result;

  // For borrowed-memory readers (from SnapshotWriter), get stats from singleton
  if (!ownsData_) {
    return SnapshotStatsCollector::getInstance().getStats().flatten();
  }

  // For file-based readers, read stats from the file's statistics section
  if (!isOpen_) {
    return result;
  }

  auto header = getHeader();
  if (header.statsCount == 0 || header.statsPos == 0) {
    return result;
  }

  // Bounds check: ensure the stats section is within the mapped region
  if (header.statsPos >= dataSize_) {
    return result;
  }

  // Read stats entries: each entry is value (u64) | nameLength (u32) | name
  size_t pos = header.statsPos;
  for (uint32_t i = 0; i < header.statsCount; ++i) {
    // Check we can read value and nameLength
    if (pos + sizeof(uint64_t) + sizeof(uint32_t) > dataSize_) {
      break;
    }

    uint64_t value = readValue<uint64_t>(pos);
    pos += sizeof(uint64_t);

    uint32_t nameLen = readValue<uint32_t>(pos);
    pos += sizeof(uint32_t);

    // Check we can read the name
    if (pos + nameLen > dataSize_) {
      break;
    }

    std::string name = readString(pos, nameLen);
    pos += nameLen;

    result[name] = value;
  }

  return result;
}

py::object SnapshotReader::getPythonObject(
    uint64_t pythonId,
    const std::unordered_map<uint64_t, uint64_t>& objectMap) {
  if (!isOpen_) {
    return py::none();
  }

  auto it = objectMap.find(pythonId);
  if (it == objectMap.end()) {
    // pythonId not found in object map, return None
    return py::none();
  }

  uint64_t offset = it->second;

  // Handle magic offsets
  if (isMagicOffset(offset)) {
    if (offset == SnapshotWriter::kNoneOffset) {
      return py::none();
    } else if (offset == SnapshotWriter::kTrueOffset) {
      return py::bool_(true);
    } else if (offset == SnapshotWriter::kFalseOffset) {
      return py::bool_(false);
    }
    return py::none();
  }

  // Check cache first
  auto cacheIt = objectCache_.find(offset);
  if (cacheIt != objectCache_.end()) {
    return cacheIt->second;
  }

  // Read the raw object data
  auto objOpt = readObject(offset);
  if (!objOpt) {
    return py::none();
  }

  const auto& obj = *objOpt;

  // Cache the snapshot module and serialized type classes. The module import
  // goes through sys.modules (cached), but the attr lookups are avoided
  // entirely after the first call.
  struct SerializedTypes {
    py::object SerializedObject;
    py::object SerializedListObject;
    py::object SerializedDictObject;
  };
  static std::optional<SerializedTypes> serializedTypes;
  if (!serializedTypes) {
    py::module_ snapshotModule = py::module_::import("tintype._snapshot");
    serializedTypes = SerializedTypes{
        snapshotModule.attr("SerializedObject"),
        snapshotModule.attr("SerializedListObject"),
        snapshotModule.attr("SerializedDictObject"),
    };
  }

  // Handle primitive types directly
  switch (obj.type) {
    case ObjectType::Int64: {
      int64_t val = std::get<int64_t>(obj.value);
      py::object result = py::int_(val);
      objectCache_[offset] = result;
      return result;
    }

    case ObjectType::Float: {
      double val = std::get<double>(obj.value);
      py::object result = py::float_(val);
      objectCache_[offset] = result;
      return result;
    }

    case ObjectType::String: {
      const std::string& val = std::get<std::string>(obj.value);
      py::object result = py::str(val);
      objectCache_[offset] = result;
      return result;
    }

    case ObjectType::IntBignum: {
      // IntBignum is stored as a string in the file
      const std::string& val = std::get<std::string>(obj.value);
      py::object result = py::int_(py::str(val));
      objectCache_[offset] = result;
      return result;
    }

    case ObjectType::Bytes: {
      const auto& val = std::get<std::vector<uint8_t>>(obj.value);
      py::object result =
          py::bytes(reinterpret_cast<const char*>(val.data()), val.size());
      objectCache_[offset] = result;
      return result;
    }

    case ObjectType::List: {
      const auto& elementIds = std::get<std::vector<uint64_t>>(obj.value);
      py::list result;
      // Add to cache before populating to handle cycles
      objectCache_[offset] = result;
      for (uint64_t elemId : elementIds) {
        result.append(getPythonObject(elemId, objectMap));
      }
      return result;
    }

    case ObjectType::Tuple: {
      const auto& elementIds = std::get<std::vector<uint64_t>>(obj.value);
      // For tuples, we need to create a list first, then convert
      // since tuples are immutable
      py::list tempList;
      // Add list to cache temporarily (tuple will replace it)
      objectCache_[offset] = tempList;
      for (uint64_t elemId : elementIds) {
        tempList.append(getPythonObject(elemId, objectMap));
      }
      py::tuple result(tempList);
      objectCache_[offset] = result;
      return result;
    }

    case ObjectType::Set: {
      const auto& elementIds = std::get<std::vector<uint64_t>>(obj.value);
      py::set result;
      // Add to cache before populating to handle cycles
      objectCache_[offset] = result;
      for (uint64_t elemId : elementIds) {
        result.add(getPythonObject(elemId, objectMap));
      }
      return result;
    }

    case ObjectType::Dict: {
      const auto& keyValueIds =
          std::get<std::vector<std::pair<uint64_t, uint64_t>>>(obj.value);
      py::dict result;
      // Add to cache before populating to handle cycles
      objectCache_[offset] = result;
      for (const auto& [keyId, valueId] : keyValueIds) {
        py::object key = getPythonObject(keyId, objectMap);
        py::object value = getPythonObject(valueId, objectMap);
        result[key] = value;
      }
      return result;
    }

    case ObjectType::SerializedObject: {
      py::object result = serializedTypes->SerializedObject(obj.repr);

      // Add to cache before populating attributes
      objectCache_[offset] = result;

      // Set attributes directly on the object
      for (const auto& [nameId, valueId] : obj.attributes) {
        py::object name = getPythonObject(nameId, objectMap);
        py::object value = getPythonObject(valueId, objectMap);
        std::string nameStr;
        if (py::isinstance<py::str>(name)) {
          nameStr = name.cast<std::string>();
        } else {
          nameStr = py::str(name).cast<std::string>();
        }
        PyObject_SetAttrString(result.ptr(), nameStr.c_str(), value.ptr());
      }

      return result;
    }

    case ObjectType::SerializedList:
    case ObjectType::SerializedSet:
    case ObjectType::SerializedTuple: {
      // Create with empty list initially
      py::list emptyList;
      py::object result =
          serializedTypes->SerializedListObject(emptyList, obj.repr);

      // Add to cache before populating
      objectCache_[offset] = result;

      // Populate the list elements (append directly to result, which is a list
      // subclass)
      const auto& elementIds = std::get<std::vector<uint64_t>>(obj.value);
      for (uint64_t elemId : elementIds) {
        PyList_Append(result.ptr(), getPythonObject(elemId, objectMap).ptr());
      }

      // Set attributes directly on the object
      for (const auto& [nameId, valueId] : obj.attributes) {
        py::object name = getPythonObject(nameId, objectMap);
        py::object value = getPythonObject(valueId, objectMap);
        std::string nameStr;
        if (py::isinstance<py::str>(name)) {
          nameStr = name.cast<std::string>();
        } else {
          nameStr = py::str(name).cast<std::string>();
        }
        PyObject_SetAttrString(result.ptr(), nameStr.c_str(), value.ptr());
      }

      return result;
    }

    case ObjectType::SerializedDict: {
      // Create with empty dict initially
      py::dict emptyDict;
      py::object result =
          serializedTypes->SerializedDictObject(emptyDict, obj.repr);

      // Add to cache before populating
      objectCache_[offset] = result;

      // Populate the dict entries (set items directly on result, which is a
      // dict subclass)
      const auto& keyValueIds =
          std::get<std::vector<std::pair<uint64_t, uint64_t>>>(obj.value);
      for (const auto& [keyId, valueId] : keyValueIds) {
        py::object key = getPythonObject(keyId, objectMap);
        py::object value = getPythonObject(valueId, objectMap);
        // Convert key to string if needed (matching core.py behavior)
        if (py::isinstance<py::str>(key) || py::isinstance<py::int_>(key) ||
            py::isinstance<py::float_>(key) || py::isinstance<py::bool_>(key)) {
          PyObject_SetItem(result.ptr(), key.ptr(), value.ptr());
        } else {
          py::str strKey(key);
          PyObject_SetItem(result.ptr(), strKey.ptr(), value.ptr());
        }
      }

      // Set attributes directly on the object
      for (const auto& [nameId, valueId] : obj.attributes) {
        py::object name = getPythonObject(nameId, objectMap);
        py::object value = getPythonObject(valueId, objectMap);
        std::string nameStr;
        if (py::isinstance<py::str>(name)) {
          nameStr = name.cast<std::string>();
        } else {
          nameStr = py::str(name).cast<std::string>();
        }
        PyObject_SetAttrString(result.ptr(), nameStr.c_str(), value.ptr());
      }

      return result;
    }

    default:
      return py::none();
  }
}

bool SnapshotReader::remapIfGrown() const {
  if (fileFd_ < 0) {
    return false;
  }
  struct stat st;
  if (fstat(fileFd_, &st) < 0) {
    return false;
  }
  auto newSize = static_cast<size_t>(st.st_size);
  if (newSize <= dataSize_) {
    return false;
  }
  // Re-mmap with the new size. Cast away const — the mutable data_/dataSize_
  // fields are an implementation detail of the lazy remap; callers still
  // observe a logically const reader.
  auto* self = const_cast<SnapshotReader*>(this);
  munmap(self->data_, self->dataSize_);
  self->data_ = mmap(nullptr, newSize, PROT_READ, MAP_SHARED, fileFd_, 0);
  // NOLINTNEXTLINE(performance-no-int-to-ptr)
  if (self->data_ == MAP_FAILED) {
    self->data_ = nullptr;
    self->dataSize_ = 0;
    return false;
  }
  self->dataSize_ = newSize;
  return true;
}

template <typename T>
T SnapshotReader::readValue(size_t offset) const {
  if (offset + sizeof(T) > dataSize_ && !remapIfGrown()) {
    return T{};
  }
  if (offset + sizeof(T) > dataSize_) {
    return T{};
  }
  T value;
  std::memcpy(&value, static_cast<const char*>(data_) + offset, sizeof(T));
  return value;
}

std::string SnapshotReader::readString(size_t offset, size_t length) const {
  if (length == 0) {
    return "";
  }
  if (offset + length > dataSize_ && !remapIfGrown()) {
    return "";
  }
  if (offset + length > dataSize_) {
    return "";
  }
  return std::string(static_cast<const char*>(data_) + offset, length);
}

} // namespace facebook::tintype::snapshot
