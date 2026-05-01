# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Utility for generating Python traceback objects from snapshot stacktraces."""

import sys
from types import TracebackType
from typing import Any


class __traceback_maker(Exception):
    pass


def _generate_traceback_from_stacktrace(
    stacktrace: Any,
) -> TracebackType | None:
    top_tb = None
    tb = None
    stub = compile(
        "raise __traceback_maker",
        "<string>",
        "exec",
    )
    for frame in reversed(stacktrace.frames):
        code = stub.replace(
            co_firstlineno=frame.line_number,
            co_argcount=0,
            co_filename=frame.file_path,
            co_name=frame.function_name,
            co_freevars=(),
            co_cellvars=(),
        )
        if sys.version_info >= (3, 11):
            code = code.replace(co_qualname=frame.function_qualname)
        locals = frame.get_locals()
        try:
            exec(code, {}, locals)
        except Exception:
            caught_exc = sys.exc_info()[2]
            if caught_exc:
                next_tb = caught_exc.tb_next
                if top_tb is None:
                    top_tb = next_tb
                if tb is not None:
                    tb.tb_next = next_tb
                tb = next_tb
                del next_tb
    try:
        return top_tb
    finally:
        del top_tb
        del tb
