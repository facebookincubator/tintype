# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Source-file bookkeeping for DAP ``source`` / ``sourceReference`` support.

For each source file referenced by a snapshot's frames we prefer to return
an on-disk path (``source: {path, name}``) so VS Code can open the real
file with its usual hover / breakpoint machinery. When the file is not
reachable (e.g. captured on another machine or captured under a relative
path that can no longer be resolved), we fall back to the
``sourceReference`` flow: we return an integer reference, and VS Code
follows up with a ``source`` request that we answer with the embedded
file's content.

Tintype populates :class:`tintype.Frame` with two path fields:

* ``file_path`` — sometimes rewritten to point at a temp directory where
  the reader extracted the bundled source, sometimes left as whatever
  ``co_filename`` was at capture time (so it can be relative, e.g.
  ``./empty_script.py`` when the program was launched as
  ``python ./empty_script.py``).
* ``original_file_path`` — the path as it was captured on the writer
  machine.

Source content arrives in two places:

1. :meth:`tintype.SnapshotReader.get_all_source_files` returns every
   source file that the reader has a copy of. Tintype sometimes omits
   files that matched an on-disk original at capture time (see
   ``tintype/tests/test_edge_cases.py``).
2. :meth:`tintype.SnapshotReader.get_extracted_files_dir` is a temp
   directory the reader populates with embedded files. It's the
   authoritative store for source the reader can serve.

To make relative paths like ``./empty_script.py`` resolvable we index
*both* — the in-memory SourceFile entries and anything we find on disk
under the extracted-files dir — keyed by the full path, the basename,
and any trailing suffix we might receive.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tintype import SnapshotReader


logger: logging.Logger = logging.getLogger(__name__)


class SourceRegistry:
    """Tracks embedded source files and assigns ``sourceReference`` IDs."""

    def __init__(self) -> None:
        # Canonical key -> content. The key is the path the reader gave
        # us (SourceFile.path) or the absolute path on the extracted
        # dir. Either way it's an absolute-ish string we can use to
        # index-reuse references.
        self._embedded: dict[str, str] = {}
        # Basename -> canonical key for fallback lookup when the frame
        # carries a relative path that doesn't match any absolute key.
        # ``None`` means the basename is ambiguous (multiple files with
        # the same name) and should be skipped.
        self._by_basename: dict[str, str | None] = {}
        # Reference bookkeeping. IDs start at 1 — DAP treats 0 as
        # "no source reference".
        self._ref_to_key: dict[int, str] = {}
        self._key_to_ref: dict[str, int] = {}
        self._next_ref: int = 1
        # Extracted-files dir (if the reader has one). Used as a last
        # resort when :meth:`_canonical_key` is asked about a path that
        # isn't registered yet — we try resolving against this dir and
        # load the file lazily if it's there. Eagerly walking the dir
        # in :meth:`load_from_reader` covers the common case.
        self._extracted_dir: str = ""

    def load_from_reader(self, reader: SnapshotReader) -> None:
        """Populate the registry from a :class:`SnapshotReader`."""
        # 1) SourceFile entries the reader hands us directly.
        for source_file in reader.get_all_source_files():
            self._record(source_file.path, source_file.content)

        # 2) Files extracted to a temp dir. Tintype uses this store for
        # snapshots whose capture path doesn't match any on-disk file
        # at read time (e.g. ``./empty_script.py`` in a different
        # working directory). Walking the dir up front lets us serve
        # relative-path ``source`` requests without any fallback logic
        # deeper in the stack.
        extracted = reader.get_extracted_files_dir() or ""
        self._extracted_dir = extracted
        if extracted and os.path.isdir(extracted):
            for dirpath, _dirs, files in os.walk(extracted):
                for name in files:
                    full = os.path.join(dirpath, name)
                    if full in self._embedded:
                        continue
                    try:
                        with open(full, encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    except OSError:
                        continue
                    self._record(full, content)

    def _record(self, key: str, content: str) -> None:
        """Insert ``key`` -> ``content`` into the registry and update
        the basename index."""
        if not key:
            return
        self._embedded[key] = content
        base = os.path.basename(key)
        if not base:
            return
        if base in self._by_basename:
            # Ambiguous basename — disable fallback for it.
            self._by_basename[base] = None
        else:
            self._by_basename[base] = key

    # ---------------------------------------------------------------
    # Lookup helpers
    # ---------------------------------------------------------------

    def _match_basename(self, base: str) -> str | None:
        """Return the unique embedded key for ``base``, or ``None``."""
        if not base:
            return None
        # ``None`` in the map means ambiguous; absence means unknown.
        return self._by_basename.get(base)

    def _match_suffix(self, file_path: str) -> str | None:
        """Find an embedded key ending with ``/<file_path>``."""
        # ``removeprefix`` handles the ``./`` prefix correctly;
        # ``lstrip("./")`` treats its argument as a *char set* and
        # would incorrectly strip leading ``.`` characters from paths
        # like ``.hidden/foo.py`` (turning it into ``hidden/foo.py``).
        stripped = file_path.removeprefix("./").lstrip("/")
        if "/" not in stripped:
            return None
        needle = "/" + stripped
        for key in self._embedded:
            if key.endswith(needle):
                return key
        return None

    def _match_in_extracted_dir(self, file_path: str, base: str | None) -> str | None:
        """Resolve ``file_path`` under the extracted-files dir.

        Loads the file into :attr:`_embedded` on demand (covers races
        with reader teardown and edge cases the eager walk missed).
        """
        if not self._extracted_dir or not os.path.isdir(self._extracted_dir):
            return None
        # Same rationale as in ``_match_suffix``: use ``removeprefix``
        # instead of ``lstrip("./")`` to avoid stripping leading ``.``
        # characters from dotfile paths.
        stripped = file_path.removeprefix("./").lstrip("/")
        candidates = [os.path.join(self._extracted_dir, stripped)]
        if base:
            candidates.append(os.path.join(self._extracted_dir, base))
        for candidate in candidates:
            if not os.path.isfile(candidate):
                continue
            if candidate not in self._embedded:
                try:
                    with open(candidate, encoding="utf-8", errors="replace") as f:
                        self._record(candidate, f.read())
                except OSError:
                    continue
            return candidate
        return None

    def _canonical_key(self, file_path: str | None) -> str | None:
        """Try to resolve an input path to an embedded-source key.

        Accepts any shape the frame might give us (absolute, relative,
        ``./foo.py``) and returns the key under which the content is
        stored in ``self._embedded`` — or ``None`` if we can't find a
        match.
        """
        if not file_path:
            return None
        # 1. Exact match on the stored key.
        if file_path in self._embedded:
            return file_path
        # 2. Absolutized match (turns ``./script.py`` into a path
        #    relative to the DAP server's cwd — occasionally useful).
        try:
            abs_path = os.path.abspath(file_path)
        except (TypeError, ValueError):
            abs_path = None
        if abs_path and abs_path in self._embedded:
            return abs_path
        # 3. Basename / 4. suffix / 5. extracted-dir on-disk fallbacks.
        base = os.path.basename(file_path)
        return (
            self._match_basename(base)
            or self._match_suffix(file_path)
            or self._match_in_extracted_dir(file_path, base)
        )

    # ---------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------

    def describe(
        self,
        file_path: str,
        *,
        original_file_path: str | None = None,
    ) -> dict[str, Any]:
        """Build a DAP ``Source`` object for a given frame's file path."""
        name = os.path.basename(file_path) or file_path

        # Prefer the on-disk copy (extracted by the reader, the user's
        # actual checkout, etc.) so VS Code can use the real file.
        if file_path and os.path.isfile(file_path):
            return {"name": name, "path": os.path.abspath(file_path)}
        if original_file_path and os.path.isfile(original_file_path):
            return {
                "name": os.path.basename(original_file_path) or original_file_path,
                "path": os.path.abspath(original_file_path),
            }

        # Fall back to embedded content (in-memory, or on disk under
        # the extracted-files dir).
        key = self._canonical_key(file_path)
        if key is None:
            key = self._canonical_key(original_file_path)

        if key is not None:
            ref = self._key_to_ref.get(key)
            if ref is None:
                ref = self._next_ref
                self._next_ref += 1
                self._key_to_ref[key] = ref
                self._ref_to_key[ref] = key
            return {
                "name": os.path.basename(key) or key,
                "sourceReference": ref,
                "origin": "tintype snapshot",
                "presentationHint": "normal",
            }

        return {
            "name": name,
            "path": file_path or original_file_path or name,
            "presentationHint": "deemphasize",
        }

    def get_by_reference(self, reference: int) -> str | None:
        """Return the embedded content for a previously assigned reference."""
        key = self._ref_to_key.get(reference)
        if key is None:
            return None
        return self._embedded.get(key)

    def get_by_path(self, file_path: str | None) -> str | None:
        """Return the embedded content for a file path (flexible matching)."""
        key = self._canonical_key(file_path)
        if key is None:
            return None
        return self._embedded.get(key)

    def get_content(self, file_path: str) -> str | None:
        """Return embedded content for a file path, or ``None`` if unknown.

        Kept for backward compat with earlier tests. Prefer
        :meth:`get_by_path` for flexible matching.
        """
        return self._embedded.get(file_path)
