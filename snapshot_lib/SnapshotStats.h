// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <string>

#include "SnapshotTypes.h"

namespace facebook::tintype::snapshot {

// ============================================================================
// Timing Utilities
// ============================================================================

/**
 * Get current time in nanoseconds since epoch.
 * Used for performance timing measurements.
 */
inline uint64_t getCurrentTimeNs() {
  auto now = std::chrono::high_resolution_clock::now();
  return static_cast<uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          now.time_since_epoch())
          .count());
}

// ============================================================================
// ScopedTimer - RAII timing helper
// ============================================================================

/**
 * RAII timer for accumulating elapsed time into a target variable.
 * When collectStats is false, no timing is performed (zero overhead).
 *
 * Usage:
 *   ScopedTimer timer(collectStats_, stats_.someTimeNs);
 *   // ... do work ...
 *   // (automatically accumulates elapsed time on scope exit)
 */
class ScopedTimer {
  uint64_t* target_;
  uint64_t startNs_;

 public:
  ScopedTimer(bool collect, uint64_t& target)
      : target_(collect ? &target : nullptr),
        startNs_(collect ? getCurrentTimeNs() : 0) {}

  ~ScopedTimer() {
    if (target_) {
      *target_ += getCurrentTimeNs() - startNs_;
    }
  }

  // Non-copyable, non-movable
  ScopedTimer(const ScopedTimer&) = delete;
  ScopedTimer& operator=(const ScopedTimer&) = delete;
  ScopedTimer(ScopedTimer&&) = delete;
  ScopedTimer& operator=(ScopedTimer&&) = delete;
};

// ============================================================================
// Statistics Structures
// ============================================================================

/**
 * Statistics for a single object type.
 */
struct ObjectTypeStats {
  uint64_t count = 0; // Number of objects serialized
  uint64_t totalTimeNs = 0; // Total time spent serializing in nanoseconds
  uint64_t totalBytes = 0; // Total bytes written for this type

  double averageTimeUs() const {
    return count > 0 ? static_cast<double>(totalTimeNs) / count / 1000.0 : 0.0;
  }

  double averageBytesPerObject() const {
    return count > 0 ? static_cast<double>(totalBytes) / count : 0.0;
  }
};

/**
 * Overall statistics for the snapshot module.
 */
struct SnapshotStats {
  // Timing for main operations (in nanoseconds)
  uint64_t initializeTimeNs = 0;
  uint64_t finalizeTimeNs = 0;
  uint64_t totalSnapshotTimeNs = 0; // Sum of all take_snapshot calls
  uint32_t snapshotCount = 0; // Number of snapshots taken

  // Granular timing within finalize (in nanoseconds)
  uint64_t finalizeFileTableTimeNs =
      0; // Time building file table (reading source files)
  uint64_t finalizeEnvironmentTimeNs =
      0; // Time capturing environment variables
  uint64_t finalizeManifestTimeNs = 0; // Time reading manifest JSON
  uint64_t finalizeMetadataTimeNs = 0; // Time writing metadata section
  uint64_t finalizeMsyncTimeNs = 0; // Time in msync before compression
  uint64_t finalizeCompressionTimeNs = 0; // Time in zstd compression
  uint64_t finalizeOutputFileTimeNs =
      0; // Time producing output file (includes compression as a subset)
  uint64_t finalizeCleanupTimeNs =
      0; // Time unmapping and cleaning up temp file
  uint64_t finalizeUncompressedDataSize =
      0; // Total uncompressed data size in bytes
  uint64_t finalizeCompressedDataSize =
      0; // Compressed output file size in bytes (0 if no compression)
  uint32_t finalizeFileCount = 0; // Number of source files read for file table

  // Granular timing within take_snapshot (in nanoseconds)
  uint64_t writeFrameRecordTimeNs = 0; // Time writing frame records
  uint32_t totalFrameCount = 0; // Total frames across all snapshots
  uint32_t framesFiltered = 0; // Frames skipped by file path filters
  uint64_t totalObjectsProcessed = 0; // Total objects processed in queue
  uint32_t snapshotsDiscarded =
      0; // Snapshots discarded (no frames, timeout, etc.)

  // Granular timing within object queue processing (in nanoseconds)
  uint64_t objectLookupTimeNs = 0; // Time checking if object already processed
  uint64_t objectProcessingTimeNs = 0; // Total time in processObject() calls
  uint64_t reprTimeNs = 0; // Time calling repr() on objects
  uint64_t slotsTimeNs = 0; // Time iterating __slots__ via MRO
  uint64_t attrAccessTimeNs = 0; // Time accessing object attributes
  uint64_t classMembersTimeNs = 0; // Time extracting class members from type
  uint64_t objectsSkipped = 0; // Objects skipped (already processed)
  uint64_t objectsCacheHit =
      0; // Objects reused via cross-snapshot identity hash cache
  // stringBytesCacheHit is a strict subset of objectsCacheHit:
  // lookupObjectWithHash() increments objectsCacheHit on every hit, and the
  // str/bytes callers additionally increment stringBytesCacheHit. Do not sum
  // the two counters — that would double-count.
  uint64_t stringBytesCacheHit = 0;
  uint64_t errors =
      0; // Total errors (failed serializations, file failures, etc.)

  // File extension statistics
  uint64_t fileExtensionTimeNs = 0; // Time spent extending the file
  uint32_t fileExtensionCount = 0; // Number of times file was extended
  uint64_t fileExtensionBytes = 0; // Total bytes the file grew by

  // Per-object-type statistics (indexed by ObjectType enum value)
  ObjectTypeStats objectStats[kNumObjectTypes];

  // Total objects across all types
  uint64_t totalObjects() const {
    uint64_t total = 0;
    for (const auto& stats : objectStats) {
      total += stats.count;
    }
    return total;
  }

  // Total serialization time across all types
  uint64_t totalSerializationTimeNs() const {
    uint64_t total = 0;
    for (const auto& stats : objectStats) {
      total += stats.totalTimeNs;
    }
    return total;
  }

  // Flatten stats to a map for serialization
  std::map<std::string, uint64_t> flatten() const;
};

// ============================================================================
// SnapshotStatsCollector Singleton
// ============================================================================

/**
 * Singleton class for collecting snapshot statistics.
 *
 * This class centralizes all stats collection, providing:
 * - Timer accessor methods that return ScopedTimer objects
 * - Counter increment methods
 * - Cross-function timing (snapshot start/end)
 *
 * Usage:
 *   // Enable collection
 *   SnapshotStatsCollector::getInstance().beginCollection();
 *
 *   // Use timer accessors
 *   {
 *     auto timer =
 * SnapshotStatsCollector::getInstance().writeFrameRecordTimer();
 *     // ... do work ...
 *   }
 *
 *   // Increment counters
 *   SnapshotStatsCollector::getInstance().incrementFrameCount();
 *
 *   // Get final stats
 *   auto stats = SnapshotStatsCollector::getInstance().getStats();
 */
class SnapshotStatsCollector {
 public:
  static SnapshotStatsCollector& getInstance();

  // === Lifecycle ===

  /** Reset all stats and enable collection. */
  void beginCollection();

  /** Disable collection and reset all accumulated statistics. */
  void endCollection();

  /** Check if collection is enabled. */
  bool isCollecting() const {
    return collecting_;
  }

  // === Timing Accessors (return ScopedTimer) ===

  // Main operations
  ScopedTimer initializeTimer();
  ScopedTimer finalizeTimer();

  // Finalize steps
  ScopedTimer finalizeFileTableTimer();
  ScopedTimer finalizeEnvironmentTimer();
  ScopedTimer finalizeManifestTimer();
  ScopedTimer finalizeMetadataTimer();
  ScopedTimer finalizeMsyncTimer();
  ScopedTimer finalizeCompressionTimer();
  ScopedTimer finalizeOutputFileTimer();
  ScopedTimer finalizeCleanupTimer();

  // Snapshot/frame operations
  ScopedTimer writeFrameRecordTimer();

  // Object processing
  ScopedTimer objectLookupTimer();
  ScopedTimer objectProcessingTimer();
  ScopedTimer reprTimer();
  ScopedTimer attrAccessTimer();
  ScopedTimer slotsTimer();
  ScopedTimer classMembersTimer();

  // File extension
  ScopedTimer fileExtensionTimer();

  // Object write (by type)
  ScopedTimer objectWriteTimer(ObjectType type);

  // === Counter/Value Methods ===

  void incrementFrameCount();
  void incrementFramesFiltered();
  void incrementObjectsProcessed();
  void incrementObjectsSkipped();
  void incrementObjectsCacheHit();
  void incrementStringBytesCacheHit();
  void incrementErrors();
  void incrementSnapshotsDiscarded();
  void incrementFileExtensionCount();

  void addFileExtensionBytes(uint64_t bytes);
  void addObjectWriteStats(ObjectType type, uint64_t bytes);
  void setFinalizeUncompressedDataSize(uint64_t size);
  void setFinalizeCompressedDataSize(uint64_t size);
  void setFinalizeFileCount(uint32_t count);

  // === Snapshot Timing (cross-function) ===

  /** Call at the start of a snapshot. */
  void recordSnapshotStart();

  /** Call at the end of a snapshot to accumulate duration. */
  void recordSnapshotEnd();

  // === Data Retrieval ===

  /** Get a copy of the current stats. */
  SnapshotStats getStats() const;

 private:
  SnapshotStatsCollector() = default;

  bool collecting_ = false;
  SnapshotStats stats_;
  uint64_t currentSnapshotStartNs_ = 0;
};

} // namespace facebook::tintype::snapshot
