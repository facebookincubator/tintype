// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#include "SnapshotStats.h"

namespace facebook::tintype::snapshot {

// ============================================================================
// SnapshotStatsCollector Implementation
// ============================================================================

SnapshotStatsCollector& SnapshotStatsCollector::getInstance() {
  static SnapshotStatsCollector instance;
  return instance;
}

void SnapshotStatsCollector::beginCollection() {
  collecting_ = true;
  stats_ = SnapshotStats{}; // Reset all stats
  currentSnapshotStartNs_ = 0;
}

void SnapshotStatsCollector::endCollection() {
  collecting_ = false;
  stats_ = SnapshotStats{}; // Reset all stats
  currentSnapshotStartNs_ = 0;
}

// === Main operation timers ===

ScopedTimer SnapshotStatsCollector::initializeTimer() {
  return ScopedTimer(collecting_, stats_.initializeTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeTimer() {
  return ScopedTimer(collecting_, stats_.finalizeTimeNs);
}

// === Finalize step timers ===

ScopedTimer SnapshotStatsCollector::finalizeFileTableTimer() {
  return ScopedTimer(collecting_, stats_.finalizeFileTableTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeEnvironmentTimer() {
  return ScopedTimer(collecting_, stats_.finalizeEnvironmentTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeManifestTimer() {
  return ScopedTimer(collecting_, stats_.finalizeManifestTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeMetadataTimer() {
  return ScopedTimer(collecting_, stats_.finalizeMetadataTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeMsyncTimer() {
  return ScopedTimer(collecting_, stats_.finalizeMsyncTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeCompressionTimer() {
  return ScopedTimer(collecting_, stats_.finalizeCompressionTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeOutputFileTimer() {
  return ScopedTimer(collecting_, stats_.finalizeOutputFileTimeNs);
}

ScopedTimer SnapshotStatsCollector::finalizeCleanupTimer() {
  return ScopedTimer(collecting_, stats_.finalizeCleanupTimeNs);
}

// === Snapshot/frame timers ===

ScopedTimer SnapshotStatsCollector::writeFrameRecordTimer() {
  return ScopedTimer(collecting_, stats_.writeFrameRecordTimeNs);
}

// === Object processing timers ===

ScopedTimer SnapshotStatsCollector::objectLookupTimer() {
  return ScopedTimer(collecting_, stats_.objectLookupTimeNs);
}

ScopedTimer SnapshotStatsCollector::objectProcessingTimer() {
  return ScopedTimer(collecting_, stats_.objectProcessingTimeNs);
}

ScopedTimer SnapshotStatsCollector::reprTimer() {
  return ScopedTimer(collecting_, stats_.reprTimeNs);
}

ScopedTimer SnapshotStatsCollector::attrAccessTimer() {
  return ScopedTimer(collecting_, stats_.attrAccessTimeNs);
}

ScopedTimer SnapshotStatsCollector::slotsTimer() {
  return ScopedTimer(collecting_, stats_.slotsTimeNs);
}

ScopedTimer SnapshotStatsCollector::classMembersTimer() {
  return ScopedTimer(collecting_, stats_.classMembersTimeNs);
}

// === File extension timer ===

ScopedTimer SnapshotStatsCollector::fileExtensionTimer() {
  return ScopedTimer(collecting_, stats_.fileExtensionTimeNs);
}

// === Object write timer ===

ScopedTimer SnapshotStatsCollector::objectWriteTimer(ObjectType type) {
  return ScopedTimer(
      collecting_, stats_.objectStats[static_cast<size_t>(type)].totalTimeNs);
}

// === Counter methods ===

void SnapshotStatsCollector::incrementFrameCount() {
  if (collecting_) {
    stats_.totalFrameCount++;
  }
}

void SnapshotStatsCollector::incrementFramesFiltered() {
  if (collecting_) {
    stats_.framesFiltered++;
  }
}

void SnapshotStatsCollector::incrementObjectsProcessed() {
  if (collecting_) {
    stats_.totalObjectsProcessed++;
  }
}

void SnapshotStatsCollector::incrementObjectsSkipped() {
  if (collecting_) {
    stats_.objectsSkipped++;
  }
}

void SnapshotStatsCollector::incrementObjectsCacheHit() {
  if (collecting_) {
    stats_.objectsCacheHit++;
  }
}

void SnapshotStatsCollector::incrementStringBytesCacheHit() {
  if (collecting_) {
    stats_.stringBytesCacheHit++;
  }
}

void SnapshotStatsCollector::incrementErrors() {
  if (collecting_) {
    stats_.errors++;
  }
}

void SnapshotStatsCollector::incrementSnapshotsDiscarded() {
  if (collecting_) {
    stats_.snapshotsDiscarded++;
  }
}

void SnapshotStatsCollector::incrementFileExtensionCount() {
  if (collecting_) {
    stats_.fileExtensionCount++;
  }
}

void SnapshotStatsCollector::addFileExtensionBytes(uint64_t bytes) {
  if (collecting_) {
    stats_.fileExtensionBytes += bytes;
  }
}

void SnapshotStatsCollector::addObjectWriteStats(
    ObjectType type,
    uint64_t bytes) {
  if (collecting_) {
    auto& typeStats = stats_.objectStats[static_cast<size_t>(type)];
    typeStats.count++;
    typeStats.totalBytes += bytes;
  }
}

void SnapshotStatsCollector::setFinalizeUncompressedDataSize(uint64_t size) {
  if (collecting_) {
    stats_.finalizeUncompressedDataSize = size;
  }
}

void SnapshotStatsCollector::setFinalizeCompressedDataSize(uint64_t size) {
  if (collecting_) {
    stats_.finalizeCompressedDataSize = size;
  }
}

void SnapshotStatsCollector::setFinalizeFileCount(uint32_t count) {
  if (collecting_) {
    stats_.finalizeFileCount = count;
  }
}

// === Snapshot timing ===

void SnapshotStatsCollector::recordSnapshotStart() {
  if (collecting_) {
    currentSnapshotStartNs_ = getCurrentTimeNs();
  }
}

void SnapshotStatsCollector::recordSnapshotEnd() {
  if (collecting_ && currentSnapshotStartNs_ > 0) {
    stats_.totalSnapshotTimeNs += getCurrentTimeNs() - currentSnapshotStartNs_;
    stats_.snapshotCount++;
    currentSnapshotStartNs_ = 0;
  }
}

// === Data retrieval ===

SnapshotStats SnapshotStatsCollector::getStats() const {
  return stats_;
}

// ============================================================================
// SnapshotStats::flatten Implementation
// ============================================================================

std::map<std::string, uint64_t> SnapshotStats::flatten() const {
  std::map<std::string, uint64_t> result;

  // Main operation timings
  result["initializeTimeNs"] = initializeTimeNs;
  result["finalizeTimeNs"] = finalizeTimeNs;
  result["totalSnapshotTimeNs"] = totalSnapshotTimeNs;
  result["snapshotCount"] = snapshotCount;

  // Finalize breakdown
  result["finalizeFileTableTimeNs"] = finalizeFileTableTimeNs;
  result["finalizeEnvironmentTimeNs"] = finalizeEnvironmentTimeNs;
  result["finalizeManifestTimeNs"] = finalizeManifestTimeNs;
  result["finalizeMetadataTimeNs"] = finalizeMetadataTimeNs;
  result["finalizeMsyncTimeNs"] = finalizeMsyncTimeNs;
  result["finalizeCompressionTimeNs"] = finalizeCompressionTimeNs;
  result["finalizeOutputFileTimeNs"] = finalizeOutputFileTimeNs;
  result["finalizeCleanupTimeNs"] = finalizeCleanupTimeNs;
  result["finalizeUncompressedDataSize"] = finalizeUncompressedDataSize;
  result["finalizeCompressedDataSize"] = finalizeCompressedDataSize;
  result["finalizeFileCount"] = finalizeFileCount;

  // Snapshot breakdown
  result["writeFrameRecordTimeNs"] = writeFrameRecordTimeNs;
  result["totalFrameCount"] = totalFrameCount;
  result["framesFiltered"] = framesFiltered;
  result["totalObjectsProcessed"] = totalObjectsProcessed;
  result["snapshotsDiscarded"] = snapshotsDiscarded;

  // Object queue processing breakdown
  result["objectLookupTimeNs"] = objectLookupTimeNs;
  result["objectProcessingTimeNs"] = objectProcessingTimeNs;
  result["reprTimeNs"] = reprTimeNs;
  result["slotsTimeNs"] = slotsTimeNs;
  result["attrAccessTimeNs"] = attrAccessTimeNs;
  result["classMembersTimeNs"] = classMembersTimeNs;
  result["objectsSkipped"] = objectsSkipped;
  result["objectsCacheHit"] = objectsCacheHit;
  result["stringBytesCacheHit"] = stringBytesCacheHit;
  result["errors"] = errors;

  // File extension stats
  result["fileExtensionTimeNs"] = fileExtensionTimeNs;
  result["fileExtensionCount"] = fileExtensionCount;
  result["fileExtensionBytes"] = fileExtensionBytes;

  // Per-type object stats
  const char* typeNames[] = {
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
    const auto& stats = objectStats[i];
    std::string prefix = std::string("objectType_") + typeNames[i] + "_";
    result[prefix + "count"] = stats.count;
    result[prefix + "totalTimeNs"] = stats.totalTimeNs;
    result[prefix + "totalBytes"] = stats.totalBytes;
  }

  return result;
}

} // namespace facebook::tintype::snapshot
