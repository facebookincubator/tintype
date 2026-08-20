# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import os
import sys
import time
from typing import Any

import tintype


class ChildDict(dict[str, str]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.dict_var1 = 1
        self.dict_var2 = 2


class ChildList(list[str]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.list_var1 = 1
        self.list_var2 = 2


class ChildSet(set[str]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.set_var1 = 1
        self.set_var2 = 2


class ChildTuple(tuple[str, ...]):
    def __new__(cls, *args: Any, **kwargs: Any) -> "ChildTuple":
        return super().__new__(cls, *args, **kwargs)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.tuple_var1 = 1
        self.tuple_var2 = 2


class ObjSlots:
    __slots__ = ["name", "description"]

    def __init__(self, name: str) -> None:
        self.name = name

    def getName(self) -> str:
        return self.name


class SlotsChild(ObjSlots):
    def __init__(self) -> None:
        super().__init__("child")
        self.value = 3
        self.string = "slots child member"


class ClassA(list[int]):
    a_1 = "a1"
    a_2 = "a2"


class ClassB(ClassA):
    b_1 = "b1"
    b_2 = "b2"


class ClassC(ClassB):
    c_1 = "c1"
    c_2 = "c2"


class ObjBadRepr:
    def __repr__(self) -> str:
        raise Exception("exception in repr")


class BadReprParent:
    def __init__(self) -> None:
        self.child = ObjBadRepr()


class Obj:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ref = self
        self.listkind = [1, 23, 4, 5]
        self.dictkind = {1: "a", 2: "b", 3: "c"}
        a = 12
        self.lmbd = lambda x: x + a
        self.self_ref = [[self, self], ObjSlots("test"), self]
        # pyrefly: ignore [missing-attribute]
        self.self_ref[0].append(self.self_ref)
        self.bad_repr = BadReprParent()
        self.denom = 0

    @property
    def bad_property(self) -> None:
        raise Exception("oops")

    def func(self) -> int:
        return 1

    def __repr__(self) -> str:
        return f"Obj({self.name})"


def trigger_exception(num: int) -> int:
    some_object = Obj(name="some_object")  # noqa
    some_slots = ObjSlots(name="some_slots")  # noqa
    slots_child = SlotsChild()  # noqa
    multiple_inheritance = ClassC([1, 2, 3])  # noqa

    child_list = ChildList(["10", "9", "8"])  # noqa
    child_set = ChildSet(["7", "6", "5"])  # noqa
    child_tuple = ChildTuple(["4", "3", "2"])  # noqa
    child_dict = ChildDict({"one": "1", "zero": "0"})  # noqa

    tintype.take_snapshot()

    try:
        # pyrefly: ignore [missing-attribute]
        result = num / some_object.self_ref[2].denom
    except Exception as e:
        handle_exception(e)

    # pyrefly: ignore [unbound-name]
    return result


def handle_exception(e: Exception) -> None:
    exception_name = "NewException"
    raise Exception(exception_name) from e


def intermediate_frame() -> None:
    num = 12345
    tintype.take_snapshot()
    trigger_exception(num)


def _exception_hook_callback(path: str) -> None:
    print(f"Snapshot written to {path}")
    _print_stats()


def _print_stats() -> None:
    """Print snapshot stats after finalize."""
    stats = tintype.get_stats()
    if not stats:
        print("No stats available.")
        return

    print("\n=== Snapshot Stats ===")
    print(f"  take_snapshot: {stats.get('total_snapshot_time_ms', 0):.2f}ms")
    print(f"  finalize:     {stats.get('finalize_time_ms', 0):.2f}ms")
    print(f"  objects:      {stats.get('total_objects', 0)}")

    bd = stats.get("snapshot_breakdown", {})
    if bd:
        print(f"  frames:         {bd.get('total_frame_count', 0)}")
        print(f"  objects queued: {bd.get('total_objects_processed', 0)}")

    fd = stats.get("finalize_breakdown", {})
    if fd:
        uncomp = fd.get("uncompressed_data_size", 0)
        print(f"  uncompressed:   {uncomp} bytes ({uncomp / 1024:.1f} KB)")
        print(f"  compression:    {fd.get('compression_time_ms', 0):.2f}ms")

    file_size = os.path.getsize("/tmp/snapshot_demo.pytb")
    print(f"  file size:      {file_size} bytes ({file_size / 1024:.1f} KB)")
    print("=====================\n")


def main() -> None:
    tintype.install_exception_hook(
        collect_stats=True,
        path="/tmp/snapshot_demo.pytb",
        metadata={
            "demo": "snapshot_demo",
            "timestamp": time.time(),
            "pid": os.getpid(),
            "trigger": "excepthook",
        },
        callback=_exception_hook_callback,
    )
    tintype.initialize(collect_stats=True)
    local_var = 1  # noqa
    intermediate_frame()
    tintype.finalize(
        "/tmp/snapshot_demo.pytb",
        metadata={
            "demo": "snapshot_demo",
            "timestamp": time.time(),
            "pid": os.getpid(),
            "python_version": sys.version,
        },
    )
    _print_stats()


if __name__ == "__main__":
    main()
