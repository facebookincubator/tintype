# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from unittest.mock import patch

import tintype
from tintype import vscode as tintype_vscode


class VSCodeIntegrationTest(unittest.TestCase):
    def tearDown(self) -> None:
        try:
            tintype.finalize()
        except RuntimeError:
            pass

    def test_capture_and_finalize_return_session_information(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "snapshot.pytb"
            result = tintype_vscode.capture()

            self.assertEqual(
                result["protocolVersion"], tintype_vscode.CAPTURE_PROTOCOL_VERSION
            )
            self.assertTrue(result["captured"])
            self.assertTrue(Path(result["workingFile"]).is_file())

            tintype_vscode.finalize(str(destination))
            self.assertTrue(destination.is_file())

    def test_snappoint_never_stops_execution(self) -> None:
        self.assertFalse(tintype_vscode.snappoint())

    def test_snappoint_event_returns_capture_information(self) -> None:
        event = tintype_vscode.snappoint_event()
        self.assertNotIn("error", event)
        self.assertEqual(
            event["protocolVersion"], tintype_vscode.CAPTURE_PROTOCOL_VERSION
        )

    def test_snappoint_event_returns_capture_errors(self) -> None:
        with patch.object(
            tintype_vscode,
            "capture",
            side_effect=RuntimeError("capture failed"),
        ):
            event = tintype_vscode.snappoint_event()
        error_event = cast(tintype_vscode.SnappointError, event)
        self.assertEqual(error_event["error"], "capture failed")

    def test_capture_event_sequence_is_unique_under_concurrency(self) -> None:
        capture_count = 1000
        session_info: tintype_vscode.SessionInfo = {
            "protocolVersion": tintype_vscode.CAPTURE_PROTOCOL_VERSION,
            "pid": 1,
            "cwd": "/tmp",
            "workingFile": "/tmp/snapshot.pytb",
        }
        with (
            patch.object(tintype_vscode, "session_info", return_value=session_info),
            patch.object(tintype, "take_snapshot", return_value=object()),
            patch.object(tintype, "snapshot_all_threads", return_value=object()),
            ThreadPoolExecutor(max_workers=16) as executor,
        ):
            results = list(
                executor.map(lambda _: tintype_vscode.capture(), range(capture_count))
            )

        sequences = [result["eventSequence"] for result in results]
        self.assertEqual(len(set(sequences)), capture_count)
        self.assertEqual(max(sequences) - min(sequences), capture_count - 1)


if __name__ == "__main__":
    unittest.main()
