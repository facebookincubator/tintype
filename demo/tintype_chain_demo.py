# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Generate a snapshot with both cause and context exception chains.

Usage:
    python -m tintype.demo.tintype_chain_demo
    python -m tintype.utils.tintype_viewer /tmp/chain_demo.pytb
"""

import os
import sys
import time

import tintype


def database_query(query: str) -> None:
    raise ConnectionError(f"Lost connection while executing: {query}")


def fetch_data() -> None:
    try:
        database_query("SELECT * FROM users")
    except ConnectionError as e:
        # Explicit cause: raise X from Y
        raise ValueError("Failed to fetch user data") from e


def process_request() -> None:
    try:
        fetch_data()
    except ValueError:
        # Implicit context: raise inside except (no "from")
        raise RuntimeError("Request processing failed")


def main() -> None:
    tintype.install_exception_hook(
        path="/tmp/chain_demo.pytb",
        metadata={
            "demo": "chain_demo",
            "timestamp": time.time(),
            "pid": os.getpid(),
            "python_version": sys.version,
        },
    )
    process_request()


if __name__ == "__main__":
    main()
