# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for ``tintype.dap.cli`` (the DAP-server CLI).

Covers argparse wiring (stdio default + ``--listen`` opt-in), file
validation, and exit-code mapping. The end-to-end wire-format tests
live in ``test_integration.py``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock

from tintype.dap import cli as module
from tintype.dap.cli import LauncherExitCode


class ListenArgParsingTest(unittest.TestCase):
    """``--listen`` defaults to off (stdio); a value opts into TCP."""

    def test_stdio_is_default(self) -> None:
        args = module._parse_args(["/tmp/snap.pytb"])
        self.assertEqual(args.pytb_file, "/tmp/snap.pytb")
        self.assertIsNone(args.listen)

    def test_parses_port_only_as_loopback(self) -> None:
        args = module._parse_args(["/tmp/snap.pytb", "--listen", "1234"])
        self.assertEqual(args.listen, ("127.0.0.1", 1234))

    def test_parses_host_and_port(self) -> None:
        args = module._parse_args(["/tmp/snap.pytb", "--listen", "127.0.0.1:45678"])
        self.assertEqual(args.listen, ("127.0.0.1", 45678))

    def test_parses_ephemeral_port(self) -> None:
        args = module._parse_args(["/tmp/snap.pytb", "--listen", "0"])
        self.assertEqual(args.listen, ("127.0.0.1", 0))

    def test_rejects_malformed_listen(self) -> None:
        with self.assertRaises(SystemExit):
            module._parse_args(["/tmp/snap.pytb", "--listen", "not-a-port"])


class MainFileValidationTest(unittest.TestCase):
    """The CLI fast-fails on obvious path mistakes before touching transports."""

    def test_returns_invalid_pytb_for_missing_pytb_file(self) -> None:
        self.assertEqual(
            module.main(["/no/such/file.pytb"]),
            LauncherExitCode.INVALID_PYTB,
        )

    def test_returns_invalid_pytb_for_directory_as_pytb(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(module.main([tmpdir]), LauncherExitCode.INVALID_PYTB)


class MainDispatchTest(unittest.TestCase):
    """The CLI dispatches to stdio by default and TCP under ``--listen``."""

    def _make_pytb(self) -> str:
        f = tempfile.NamedTemporaryFile(suffix=".pytb", delete=False)
        f.write(b"not a real tintype file, but exists")
        f.close()
        self.addCleanup(lambda: os.unlink(f.name))
        return f.name

    def test_stdio_default_calls_run_session_on_stdio(self) -> None:
        path = self._make_pytb()
        with (
            unittest.mock.patch(
                "tintype.dap.cli.run_session_on_stdio", return_value=0
            ) as stdio_mock,
            unittest.mock.patch("tintype.dap.cli.serve") as serve_mock,
        ):
            rc = module.main([path])
        self.assertEqual(rc, LauncherExitCode.OK)
        stdio_mock.assert_called_once_with()
        serve_mock.assert_not_called()

    def test_listen_routes_to_serve(self) -> None:
        path = self._make_pytb()
        with (
            unittest.mock.patch("tintype.dap.cli.serve", return_value=0) as serve_mock,
            unittest.mock.patch("tintype.dap.cli.run_session_on_stdio") as stdio_mock,
        ):
            rc = module.main([path, "--listen", "127.0.0.1:4242"])
        self.assertEqual(rc, LauncherExitCode.OK)
        serve_mock.assert_called_once_with(host="127.0.0.1", port=4242)
        stdio_mock.assert_not_called()

    def test_nonzero_return_maps_to_error(self) -> None:
        """A non-zero session-loop return maps to
        :attr:`LauncherExitCode.ERROR` so unexpected shutdown rcs
        surface as a generic error rather than leaking raw values."""
        path = self._make_pytb()
        with unittest.mock.patch(
            "tintype.dap.cli.run_session_on_stdio", return_value=7
        ):
            self.assertEqual(module.main([path]), LauncherExitCode.ERROR)

    def test_keyboard_interrupt_maps_to_exit_code(self) -> None:
        """SIGINT during the serve loop surfaces as the conventional
        128 + SIGINT(2) exit code via the enum."""
        path = self._make_pytb()
        with unittest.mock.patch(
            "tintype.dap.cli.run_session_on_stdio", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(module.main([path]), LauncherExitCode.KEYBOARD_INTERRUPT)


class LauncherExitCodeTest(unittest.TestCase):
    """Sanity checks on the enum itself so callers relying on the
    integer values (``sys.exit(rc)`` in ``main()``, POSIX exit-code
    conventions, existing shell scripts) don't silently regress."""

    def test_values_match_posix_conventions(self) -> None:
        self.assertEqual(LauncherExitCode.OK.value, 0)
        self.assertEqual(LauncherExitCode.ERROR.value, 1)
        self.assertEqual(LauncherExitCode.INVALID_PYTB.value, 2)
        self.assertEqual(LauncherExitCode.KEYBOARD_INTERRUPT.value, 130)

    def test_is_int_enum_so_values_pass_through_sys_exit(self) -> None:
        # IntEnum members compare equal to their underlying int value,
        # which ``sys.exit()`` needs for the entrypoint's
        # ``sys.exit(main())`` call to produce the expected OS exit code.
        self.assertEqual(LauncherExitCode.INVALID_PYTB, 2)
        self.assertEqual(int(LauncherExitCode.OK), 0)


if __name__ == "__main__":
    unittest.main()
