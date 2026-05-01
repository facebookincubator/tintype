// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#pragma once

#include <cstddef>
#include <cstdint>

namespace facebook::tintype::snapshot {

/**
 * Object type identifiers for the object heap.
 * Must match the values used in SnapshotWriter and SnapshotReader.
 */
enum class ObjectType : uint8_t {
  Int64 = 0,
  Float = 1,
  String = 2,
  Bytes = 3,
  List = 4,
  Tuple = 5,
  Dict = 6,
  Set = 7,
  IntBignum = 8,
  SerializedObject = 9,
  SerializedList = 10,
  SerializedSet = 11,
  SerializedTuple = 12,
  SerializedDict = 13,
};

/**
 * Number of object types (for array sizing).
 */
constexpr size_t kNumObjectTypes = 14;

} // namespace facebook::tintype::snapshot
