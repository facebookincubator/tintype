# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# @nolint
import os

from pybind11.setup_helpers import build_ext, Pybind11Extension
from setuptools import setup

# The repo root is the tintype package itself.
# After ShipIt exports fbcode/tintype/ → repo root, the directory layout is:
#
#   ./                    ← repo root = tintype package
#   ├── __init__.py
#   ├── _snapshot.cpp     ← pybind11 extension source
#   ├── snapshot_lib/     ← C++ library sources
#   ├── _exception_hook.py
#   ├── _sampling.py
#   ├── _traceback.py
#   └── ...


def find_zstd():
    """Find zstd library by checking common system include/lib paths."""
    include_dirs = []
    library_dirs = []

    search_prefixes = ["/usr", "/usr/local", "/opt/homebrew"]

    # Also check conda environment if active
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        search_prefixes.insert(0, conda_prefix)

    for prefix in search_prefixes:
        header = os.path.join(prefix, "include", "zstd.h")
        if os.path.exists(header):
            include_dirs.append(os.path.join(prefix, "include"))
            library_dirs.append(os.path.join(prefix, "lib"))
            break

    return include_dirs, library_dirs


zstd_include_dirs, zstd_library_dirs = find_zstd()

if not zstd_include_dirs:
    import warnings
    warnings.warn(
        "zstd headers not found in /usr, /usr/local, or /opt/homebrew. "
        "Install zstd (e.g., apt install libzstd-dev, brew install zstd) "
        "or set CFLAGS/LDFLAGS to point to your zstd installation.",
        stacklevel=1,
    )

ext_modules = [
    Pybind11Extension(
        "tintype._snapshot",
        sources=[
            "_snapshot.cpp",
            "snapshot_lib/SnapshotCapture.cpp",
            "snapshot_lib/SnapshotReader.cpp",
            "snapshot_lib/SnapshotStats.cpp",
            "snapshot_lib/SnapshotWriter.cpp",
        ],
        include_dirs=["."] + zstd_include_dirs,
        library_dirs=zstd_library_dirs,
        libraries=["zstd"],
        cxx_std=17,
        define_macros=[("PYBIND11_DETAILED_ERROR_MESSAGES", None)],
    ),
]

setup(
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
    packages=["tintype", "tintype.tests", "tintype.utils"],
    package_dir={
        "tintype": ".",
        "tintype.tests": "tests",
        "tintype.utils": "utils",
    },
    package_data={
        "tintype": ["_snapshot.pyi", "py.typed"],
    },
)
