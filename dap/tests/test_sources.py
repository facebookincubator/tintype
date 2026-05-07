# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype.dap.sources."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from tintype.dap.sources import SourceRegistry


class DescribeTest(unittest.TestCase):
    def test_on_disk_path_returns_path(self) -> None:
        registry = SourceRegistry()
        # Use this test file itself as a path guaranteed to exist.
        this_file = os.path.abspath(__file__)
        source = registry.describe(this_file)
        self.assertEqual(source["path"], this_file)
        self.assertEqual(source["name"], os.path.basename(this_file))
        self.assertNotIn("sourceReference", source)

    def test_embedded_path_returns_reference(self) -> None:
        registry = SourceRegistry()
        # Simulate a SnapshotReader.get_all_source_files() result.
        reader = MagicMock()
        fake_file = MagicMock()
        fake_file.path = "/virtual/mypkg/mymodule.py"
        fake_file.content = "x = 1\n"
        reader.get_all_source_files.return_value = [fake_file]
        registry.load_from_reader(reader)

        source = registry.describe("/virtual/mypkg/mymodule.py")
        self.assertNotIn("path", source)
        self.assertIn("sourceReference", source)
        self.assertEqual(source["name"], "mymodule.py")

        # Second describe() for the same path should return the same reference.
        source2 = registry.describe("/virtual/mypkg/mymodule.py")
        self.assertEqual(source["sourceReference"], source2["sourceReference"])

    def test_missing_path_returns_deemphasized(self) -> None:
        registry = SourceRegistry()
        source = registry.describe("/no/such/path.py")
        self.assertEqual(source["presentationHint"], "deemphasize")


class GetByReferenceTest(unittest.TestCase):
    def test_retrieves_embedded_content(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        fake_file = MagicMock()
        fake_file.path = "/virtual/a.py"
        fake_file.content = "print('hi')\n"
        reader.get_all_source_files.return_value = [fake_file]
        registry.load_from_reader(reader)

        source = registry.describe("/virtual/a.py")
        ref = source["sourceReference"]
        content = registry.get_by_reference(ref)
        self.assertEqual(content, "print('hi')\n")

    def test_unknown_reference_returns_none(self) -> None:
        registry = SourceRegistry()
        self.assertIsNone(registry.get_by_reference(12345))


class DescribeFallbackTest(unittest.TestCase):
    """Tintype extracts source files to a temp directory, so frames often
    have ``file_path`` pointing at an extracted location while the
    SourceFile entries are keyed by ``original_file_path``. The registry
    must bridge the two so embedded sources still resolve."""

    def test_original_file_path_fallback_returns_reference(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        fake = MagicMock()
        fake.path = "/orig/home/alice/project/mymod.py"
        fake.content = "# original\n"
        reader.get_all_source_files.return_value = [fake]
        registry.load_from_reader(reader)

        # file_path looks like an extracted temp path (does not exist on
        # disk in this test env); original_file_path matches the embedded
        # key.
        source = registry.describe(
            "/tmp/tintype_extract_xxx/home/alice/project/mymod.py",
            original_file_path="/orig/home/alice/project/mymod.py",
        )
        self.assertIn("sourceReference", source)
        content = registry.get_by_reference(source["sourceReference"])
        self.assertEqual(content, "# original\n")

    def test_on_disk_original_path_preferred_over_embedded(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        fake = MagicMock()
        fake.path = "/virtual/missing.py"
        fake.content = "x = 1\n"
        reader.get_all_source_files.return_value = [fake]
        registry.load_from_reader(reader)

        this_file = os.path.abspath(__file__)
        source = registry.describe(
            "/not/on/disk.py",
            original_file_path=this_file,
        )
        # On-disk original should win over an embedded reference.
        self.assertEqual(source["path"], this_file)
        self.assertNotIn("sourceReference", source)


class RelativePathTest(unittest.TestCase):
    """A real-world bug: frame.file_path was ``"./empty_script.py"`` (a
    relative path remembered from ``python ./empty_script.py``) while
    SourceFile.path was the absolutized form. Without flexible matching
    this would fall through to the ``deemphasize`` branch and the client
    could never load the embedded source."""

    def test_relative_path_matches_by_basename(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        fake = MagicMock()
        fake.path = "/abs/home/alice/scripts/empty_script.py"
        fake.content = "# empty\n"
        reader.get_all_source_files.return_value = [fake]
        registry.load_from_reader(reader)

        source = registry.describe("./empty_script.py")
        self.assertIn("sourceReference", source)
        ref = source["sourceReference"]
        self.assertEqual(registry.get_by_reference(ref), "# empty\n")

    def test_ambiguous_basename_does_not_match(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        a, b = MagicMock(), MagicMock()
        a.path, a.content = "/abs/one/foo.py", "# one\n"
        b.path, b.content = "/abs/two/foo.py", "# two\n"
        reader.get_all_source_files.return_value = [a, b]
        registry.load_from_reader(reader)

        # Ambiguous basename — registry should decline to guess.
        source = registry.describe("./foo.py")
        self.assertEqual(source["presentationHint"], "deemphasize")
        self.assertNotIn("sourceReference", source)


class GetByPathTest(unittest.TestCase):
    def test_get_by_path_exact(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        f = MagicMock()
        f.path = "/abs/a.py"
        f.content = "x\n"
        reader.get_all_source_files.return_value = [f]
        registry.load_from_reader(reader)
        self.assertEqual(registry.get_by_path("/abs/a.py"), "x\n")

    def test_get_by_path_basename_fallback(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        f = MagicMock()
        f.path = "/abs/nested/b.py"
        f.content = "y\n"
        reader.get_all_source_files.return_value = [f]
        registry.load_from_reader(reader)
        self.assertEqual(registry.get_by_path("./b.py"), "y\n")

    def test_get_by_path_suffix_fallback(self) -> None:
        registry = SourceRegistry()
        reader = MagicMock()
        f = MagicMock()
        f.path = "/abs/pkg/mod/c.py"
        f.content = "z\n"
        reader.get_all_source_files.return_value = [f]
        registry.load_from_reader(reader)
        self.assertEqual(registry.get_by_path("pkg/mod/c.py"), "z\n")
        self.assertEqual(registry.get_by_path("mod/c.py"), "z\n")

    def test_get_by_path_unknown_returns_none(self) -> None:
        registry = SourceRegistry()
        self.assertIsNone(registry.get_by_path("/no/such.py"))
        self.assertIsNone(registry.get_by_path(None))
        self.assertIsNone(registry.get_by_path(""))


class ExtractedFilesDirTest(unittest.TestCase):
    """Regression: ``get_all_source_files()`` omits files that matched
    an on-disk original at capture time, so a frame whose ``file_path``
    is ``./empty_script.py`` would never match any embedded key. The
    reader still extracts the content to
    ``get_extracted_files_dir()``, so the registry walks that dir at
    load time and indexes whatever it finds there."""

    def test_extracted_dir_walk_indexes_unembedded_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Simulate the reader's extracted-files layout.
            script = os.path.join(tmp, "empty_script.py")
            with open(script, "w") as f:
                f.write("print('hi')\n")

            registry = SourceRegistry()
            reader = MagicMock()
            reader.get_all_source_files.return_value = []
            reader.get_extracted_files_dir.return_value = tmp
            registry.load_from_reader(reader)

            # Frame captured with a relative path; on-disk lookup fails
            # (tmp isn't the DAP server's cwd), so we should fall
            # through to the extracted-dir walk and get a reference.
            source = registry.describe("./empty_script.py")
            self.assertIn("sourceReference", source)
            self.assertEqual(
                registry.get_by_reference(source["sourceReference"]),
                "print('hi')\n",
            )

    def test_extracted_dir_lazy_lookup_for_missed_file(self) -> None:
        """Files that appear in the extracted dir *after* load_from_reader
        are still resolvable via :meth:`get_by_path`."""
        with tempfile.TemporaryDirectory() as tmp:
            registry = SourceRegistry()
            reader = MagicMock()
            reader.get_all_source_files.return_value = []
            reader.get_extracted_files_dir.return_value = tmp
            registry.load_from_reader(reader)

            # Create the file after load.
            script = os.path.join(tmp, "late.py")
            with open(script, "w") as f:
                f.write("# late\n")

            content = registry.get_by_path("./late.py")
            self.assertEqual(content, "# late\n")


if __name__ == "__main__":
    unittest.main()
