// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#pragma once

#include <Python.h>
#include <pybind11/pybind11.h>

#include <atomic>
#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "compat.h"

#include "SnapshotStats.h"

namespace facebook::tintype::snapshot {

namespace py = pybind11;

/// BFS queue for object processing: each entry is (object, depth).
using ObjectQueue = std::deque<std::pair<py::object, uint32_t>>;

/**
 * Singleton class for capturing Python snapshots.
 *
 * This class handles the Python object introspection layer:
 * - Walking frame stacks to capture call context
 * - Processing Python objects (type checking, attribute extraction)
 * - Serializing Python objects to the binary format via SnapshotWriter
 *
 * The SnapshotCapture delegates binary file operations to SnapshotWriter.
 */
class SnapshotCapture {
 public:
  /**
   * Get the singleton instance.
   */
  static SnapshotCapture& getInstance();

  // Delete copy and move operations for singleton
  SnapshotCapture(const SnapshotCapture&) = delete;
  SnapshotCapture& operator=(const SnapshotCapture&) = delete;
  SnapshotCapture(SnapshotCapture&&) = delete;
  SnapshotCapture& operator=(SnapshotCapture&&) = delete;

  /**
   * Initialize the snapshot system.
   * @param collectStats Whether to collect timing statistics.
   * @param frameFilePathFilters Optional list of substrings. Frames whose file
   *        path contains any of these substrings will be silently skipped
   *        (without setting truncation flags).
   */
  void initialize(
      bool collectStats,
      const std::vector<std::string>& frameFilePathFilters = {});

  /**
   * Check if the snapshot system is initialized.
   */
  bool isInitialized() const;

  /**
   * Request cancellation of the current snapshot operation.
   * Thread-safe: can be called from any thread (e.g., a timer thread).
   */
  void requestCancel();

  /**
   * Clear the cancellation flag.
   * Called at the end of takeSnapshot()/takeSnapshotFromException().
   */
  void clearCancel();

  /**
   * Check if cancellation has been requested.
   */
  bool isCancelRequested() const;

  /**
   * Take a snapshot of the current call stack or a given traceback.
   * If traceback is provided, walks the traceback's tb_next chain.
   * Otherwise, walks the current thread's frame stack.
   * @param traceback Optional Python traceback object. If nullptr, captures
   *        the current thread's call stack.
   * @param maxFrames Optional maximum number of frames to capture per
   *        stacktrace. nullopt means no limit.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   *        nullopt means no limit. When the limit is reached, non-primitive
   *        objects are serialized as their repr() with no children.
   * @param timeoutSeconds Optional timeout in seconds for the entire snapshot
   *        operation. nullopt means no timeout.
   * @param skipFrames Number of frames to skip from the top of the call stack.
   *        Only applies when traceback is nullptr (current stack capture).
   * @return true if a snapshot was written, false if cancelled/discarded.
   */
  bool takeSnapshot(
      PyObject* traceback = nullptr,
      std::optional<uint32_t> maxFrames = std::nullopt,
      std::optional<uint32_t> maxObjectDepth = std::nullopt,
      std::optional<double> timeoutSeconds = std::nullopt,
      uint32_t skipFrames = 0);

  /**
   * Take a snapshot from an exception, including its full
   * __cause__/__context__ chain.
   * Each exception in the chain gets its own stacktrace with
   * the exception-specific fields populated.
   * @param exception A Python BaseException object.
   * @param maxFrames Optional maximum number of frames to capture per
   *        stacktrace. nullopt means no limit.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   *        nullopt means no limit.
   * @param timeoutSeconds Optional timeout in seconds for the entire snapshot
   *        operation. nullopt means no timeout.
   * @return true if a snapshot was written, false if cancelled/discarded.
   */
  bool takeSnapshotFromException(
      PyObject* exception,
      std::optional<uint32_t> maxFrames = std::nullopt,
      std::optional<uint32_t> maxObjectDepth = std::nullopt,
      std::optional<double> timeoutSeconds = std::nullopt);

  /**
   * Finalize the snapshot file.
   * If path is non-empty, reads source file contents, compresses, and writes
   * the output file. If path is empty, discards the snapshot data.
   * @param path Optional output file path for the compressed snapshot.
   * @param metadataJson Optional JSON string of user metadata.
   * @param compressionLevel Zstd compression level (1-22), or 0 to write
   *        uncompressed. Values outside 0-22 will throw.
   */
  void finalize(
      const std::string& path = "",
      const std::string& metadataJson = "{}",
      int compressionLevel = 3);

  /**
   * Get statistics as a Python dict.
   */
  py::dict getStats() const;

  /**
   * Reset statistics to zero.
   */
  void resetStats();

  /**
   * Capture all Python threads' stacks in a single snapshot record.
   * Uses sys._current_frames() to get all thread frames while holding the GIL.
   * The GIL ensures all threads are paused during capture, providing a
   * consistent snapshot.
   * @param maxFrames Optional maximum number of frames per stacktrace.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   * @param timeoutSeconds Timeout in seconds for the capture operation.
   *        Defaults to 1.0.
   * @param alreadyHoldsLock If true, caller already holds snapshotInProgress_
   *        and this function should not try to acquire/release it.
   * @return true if a snapshot was written, false if reentrancy guard blocked.
   */
  bool snapshotAllThreads(
      std::optional<uint32_t> maxFrames = std::nullopt,
      std::optional<uint32_t> maxObjectDepth = std::nullopt,
      double timeoutSeconds = 1.0,
      bool alreadyHoldsLock = false);

  /**
   * Take a snapshot of a single thread given its frame object.
   * Used by sampling fallback when the target thread is in native code.
   * @param frame The thread's current frame (from sys._current_frames()).
   * @param threadId The thread ID for the stacktrace.
   * @param maxFrames Optional maximum number of frames to capture.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   * @param timeoutSeconds Optional timeout for the operation.
   * @param alreadyHoldsLock If true, caller already holds snapshotInProgress_.
   * @return true if a snapshot was written.
   */
  bool takeSnapshotFromFrame(
      PyFrameObject* frame,
      uint64_t threadId,
      std::optional<uint32_t> maxFrames = std::nullopt,
      std::optional<uint32_t> maxObjectDepth = std::nullopt,
      std::optional<double> timeoutSeconds = std::nullopt,
      bool alreadyHoldsLock = false);

  // ---- Sampling API ----

  enum class SamplingMode { SINGLE_THREAD, ALL_THREADS };

  /**
   * Start periodic sampling.
   * Spawns a C++ timer thread that periodically takes snapshots.
   * @param interval Seconds between samples.
   * @param mode SINGLE_THREAD or ALL_THREADS.
   * @param targetThreadId Thread ID to sample in SINGLE_THREAD mode.
   * @param maxFrames Optional max frames per stacktrace.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   * @param timeout Timeout per sample in seconds.
   */
  void enableSampling(
      double interval,
      SamplingMode mode,
      uint64_t targetThreadId,
      std::optional<uint32_t> maxFrames,
      std::optional<uint32_t> maxObjectDepth,
      double timeout);

  /**
   * Stop periodic sampling. Blocks until the sampling thread exits.
   */
  void disableSampling();

  /**
   * Check if sampling is currently active.
   */
  bool isSamplingActive() const;

 private:
  SnapshotCapture() = default;
  ~SnapshotCapture() = default;

  /**
   * Internal helper to add a referenced file path.
   * File contents are loaded later during finalize() to reduce memory usage.
   */
  void addReferencedFile(const std::string& filePath);

  /**
   * Process a Python object: write to heap if needed, queue children.
   * Returns the object heap offset for this object.
   * @param currentDepth The current depth in the object graph.
   * @param maxObjectDepth Maximum depth for traversal; nullopt means no limit.
   * @param objectDepthHit Set to true if depth limiting was triggered.
   */
  uint64_t processObject(
      py::handle obj,
      ObjectQueue& processingQueue,
      uint32_t currentDepth,
      std::optional<uint32_t> maxObjectDepth,
      bool& objectDepthHit);

  /**
   * Walk a traceback's tb_next chain and write frame records.
   * Processes each frame's objects inline (BFS to completion per frame).
   * Shared logic between takeSnapshot(traceback) and
   * takeSnapshotFromException.
   * @param wasCancelled Set to true if processing was cancelled mid-frame.
   * @param maxFrames Optional maximum number of frames to capture.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   * @param objectDepthHit Set to true if depth limiting was triggered.
   */
  void writeTracebackFrames(
      PyObject* traceback,
      bool& wasCancelled,
      std::optional<uint32_t> maxFrames = std::nullopt,
      std::optional<uint32_t> maxObjectDepth = std::nullopt,
      bool* objectDepthHit = nullptr);

  /**
   * Process all objects in the queue via BFS to completion.
   * Checks cancelRequested_ after each object.
   * @return true if all objects were processed, false if cancelled.
   */
  bool processFrameObjects(
      ObjectQueue& processingQueue,
      std::optional<uint32_t> maxObjectDepth,
      bool& objectDepthHit);

  /**
   * Start a background timeout timer that sets the cancel flag after the given
   * duration. Used to enforce time limits on individual snapshot operations.
   */
  void startTimeoutTimer(double timeoutSeconds);

  /**
   * Stop the background timeout timer thread, if running.
   * Safe to call even if no timer was started.
   */
  void stopTimeoutTimer();

  /**
   * Walk a frame chain and write frame records.
   * Shared helper used by writeCurrentThreadFrames(),
   * captureRemainingThreads(), and takeSnapshotFromFrame().
   * @param startFrame The frame to start walking from (borrowed reference;
   *        this function Py_INCREFs it internally).
   * @param skipFrames Number of frames to skip from the top.
   * @param maxFrames Optional maximum number of frames to capture.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   * @param objectDepthHit Set to true if depth limiting was triggered.
   * @return true if frames were written without truncation/cancellation.
   */
  bool writeFramesFromFrame(
      PyFrameObject* startFrame,
      uint32_t skipFrames,
      std::optional<uint32_t> maxFrames,
      std::optional<uint32_t> maxObjectDepth = std::nullopt,
      bool* objectDepthHit = nullptr);

  /**
   * Walk the current thread's frame stack and write frame records.
   * Thin wrapper around writeFramesFromFrame() that gets the current
   * thread's frame via PyThreadState_GetFrame().
   * @param skipFrames Number of frames to skip from the top.
   * @param maxFrames Optional maximum number of frames to capture.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   * @param objectDepthHit Set to true if depth limiting was triggered.
   * @return true if frames were written without truncation/cancellation.
   */
  bool writeCurrentThreadFrames(
      uint32_t skipFrames,
      std::optional<uint32_t> maxFrames,
      std::optional<uint32_t> maxObjectDepth = std::nullopt,
      bool* objectDepthHit = nullptr);

  /**
   * Capture all threads using sys._current_frames().
   * Called by snapshotAllThreads() to capture all thread stacks.
   *
   * In standard Python, the GIL is held throughout, so other Python threads
   * are paused and objects are stable. In free-threaded Python (3.14t+),
   * other threads continue executing - we hold references to prevent object
   * destruction, but object state may be mutated during serialization.
   *
   * @param maxFrames Optional maximum number of frames per stacktrace.
   * @param maxObjectDepth Optional maximum depth for object graph traversal.
   */
  void captureAllThreads(
      std::optional<uint32_t> maxFrames,
      std::optional<uint32_t> maxObjectDepth = std::nullopt);

  /**
   * Check if a frame should be filtered out based on its file path.
   * @param filename The file path of the frame.
   * @return true if the frame should be skipped.
   */
  bool shouldFilterFrame(const std::string& filename) const;

  /**
   * Build a map from thread ID to thread name using threading.enumerate().
   * Must be called with the GIL held.
   * @return Map from native thread ID to thread name string.
   */
  FastMap<uint64_t, std::string> buildThreadNameMap();

  /**
   * Get the name of the current thread using threading.current_thread().
   * Must be called with the GIL held.
   * @return The thread name, or empty string if not available.
   */
  std::string getCurrentThreadName();

  // Frame file path filters for skipping frames
  std::vector<std::string> frameFilePathFilters_;

  // Cancel flag — settable from any thread
  std::atomic<bool> cancelRequested_{false};

  // Timeout timer state — enforces time limits on individual snapshot
  // operations
  std::mutex timeoutMutex_;
  std::condition_variable timeoutCv_;
  bool timeoutShouldStop_{false};
  std::unique_ptr<std::thread> timeoutThread_;

  // Cached Python sys module — avoids repeated import lookups during sampling
  py::module_ sysModule_;

  // Cached Python threading module — avoids repeated import lookups for thread
  // name resolution
  py::module_ threadingModule_;

  // ---- Snapshot coordination state ----

  // Guards against concurrent snapshot operations (takeSnapshot,
  // takeSnapshotFromException, snapshotAllThreads). Claimed via
  // compare_exchange_strong before beginSnapshot(), cleared after
  // endSnapshot()/discardCurrentSnapshot().
  std::atomic<bool> snapshotInProgress_{false};

  // ---- Sampling state ----

  /**
   * Sampling timer loop. Runs on the samplingThread_.
   * Sleeps for the configured interval, then takes a sample.
   * All parameters are passed by value to avoid member variable storage.
   */
  void samplingLoop(
      double interval,
      SamplingMode mode,
      uint64_t targetThreadId,
      std::optional<uint32_t> maxFrames,
      std::optional<uint32_t> maxObjectDepth,
      double timeout);

  /**
   * Take a single sample of the target thread in SINGLE_THREAD mode.
   * Called from samplingLoop() with the GIL held. Uses sys._current_frames()
   * to directly capture the target thread's frame.
   *
   * Note: In free-threaded Python (3.14t+), other threads continue executing
   * even with GIL held - we hold references to prevent object destruction,
   * but object state may be mutated during serialization.
   */
  void sampleSingleThread(
      uint64_t targetThreadId,
      std::optional<uint32_t> maxFrames,
      std::optional<uint32_t> maxObjectDepth,
      double timeout,
      bool alreadyHoldsLock = false);

  std::mutex samplingMutex_;
  std::condition_variable samplingCv_;
  bool samplingShouldStop_{false};
  std::unique_ptr<std::thread> samplingThread_;
  bool samplingActive_{false};
};

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Fast identity hash for detecting object changes between snapshots.
 * Combines the object's type pointer, size (for collections), and the
 * Python IDs of its immediate children. This is NOT a content hash -
 * it's designed to be fast while still detecting most changes.
 * Uses FNV-1a hash algorithm for speed.
 */
inline uint64_t computeIdentityHash(
    uint64_t typePtr,
    uint64_t size,
    const uint64_t* childIds,
    size_t childCount);

/**
 * Get the fully qualified type name using C API.
 * Returns "module.qualname" for non-builtin types, or just "qualname" for
 * builtins.
 */
inline std::string getFullyQualifiedTypeName(PyTypeObject* type);

/**
 * Extract attributes from an object using __dict__ + __slots__.
 * This is much faster than using dir() + getattr() for each attribute.
 * Populates attrIds with (nameId, valueId) pairs and queues attribute names
 * and values for processing.
 */
inline void extractAttributes(
    py::handle obj,
    ObjectQueue& processingQueue,
    std::vector<std::pair<uint64_t, uint64_t>>& attrIds,
    uint32_t childDepth);

/**
 * Convert a Python dict to JSON string using Python's json module.
 */
std::string dictToJson(const py::object& obj);

} // namespace facebook::tintype::snapshot
