# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Shared test helper classes for tintype tests."""


class TestClass:
    """Test class for serialization testing."""

    def __init__(self, value: int, name: str) -> None:
        self.value = value
        self.name = name

    def __repr__(self) -> str:
        return f"TestClass(value={self.value}, name={self.name!r})"


class SlotsClass:
    """Test class with __slots__ for serialization testing."""

    __slots__ = ("x", "y", "z")

    def __init__(self, x: int, y: str, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z

    def __repr__(self) -> str:
        return f"SlotsClass(x={self.x}, y={self.y!r}, z={self.z})"


class IntSubclass(int):
    """Subclass of int for testing primitive subclass serialization."""

    pass


class StrSubclass(str):
    """Subclass of str for testing primitive subclass serialization."""

    pass


class FloatSubclass(float):
    """Subclass of float for testing primitive subclass serialization."""

    pass


class ListSubclass(list):  # pyre-ignore[T484]
    """Subclass of list for testing SerializedList."""

    def __init__(self, *args: object, extra_attr: str = "default") -> None:
        super().__init__(*args)
        self.extra_attr = extra_attr


class DictSubclass(dict):  # pyre-ignore[T484]
    """Subclass of dict for testing SerializedDict."""

    def __init__(self, *args: object, extra_attr: str = "default") -> None:
        super().__init__(*args)
        self.extra_attr = extra_attr


class SetSubclass(set):  # pyre-ignore[T484]
    """Subclass of set for testing SerializedSet."""

    def __init__(self, *args: object, extra_attr: str = "default") -> None:
        super().__init__(*args)
        self.extra_attr = extra_attr


class TupleSubclass(tuple):  # pyre-ignore[T484]
    """Subclass of tuple for testing SerializedTuple."""

    extra_attr: str = "default"

    def __new__(
        cls, iterable: object = (), extra_attr: str = "default"
    ) -> "TupleSubclass":
        instance = super().__new__(cls, iterable)  # pyre-ignore[T6]
        instance.extra_attr = extra_attr
        return instance
