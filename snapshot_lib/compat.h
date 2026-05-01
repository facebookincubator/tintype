// Copyright (c) Meta Platforms, Inc. and affiliates.
// This source code is licensed under the MIT license found in the
// LICENSE file in the root directory of this source tree.

#pragma once

// Compatibility header for Folly containers.
//
// Auto-detects Folly availability via __has_include (C++17). When Folly is
// available, uses folly::F14FastMap, folly::F14FastSet, and folly::errnoStr
// for better performance. Otherwise, falls back to std::unordered_map,
// std::unordered_set, and std::strerror.

#if __has_include(<folly/container/F14Map.h>)
#include <folly/String.h>
#include <folly/container/F14Map.h>
#include <folly/container/F14Set.h>
template <typename K, typename V>
using FastMap = folly::F14FastMap<K, V>;
template <typename K>
using FastSet = folly::F14FastSet<K>;
inline std::string errnoString(int err) {
  return folly::errnoStr(err);
}
#else
#include <cerrno>
#include <cstring>
#include <string>
#include <unordered_map>
#include <unordered_set>
template <typename K, typename V>
using FastMap = std::unordered_map<K, V>;
template <typename K>
using FastSet = std::unordered_set<K>;
inline std::string errnoString(int err) {
  return std::strerror(err);
}
#endif
