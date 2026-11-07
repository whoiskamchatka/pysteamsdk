"""Call one function of the fixtures/calls.cpp library through ctypes, for test_abi_calls.

    python -I call_child.py LIBRARY CASE

Prints "ok", or what came back instead. A convention ctypes gets wrong may crash this process
instead, which the test counts as wrong too.
"""

import ctypes
import sys
from collections.abc import Callable
from typing import Any

# The packed records use MSVC's rules on every platform, as #pragma pack does.
WINDOWS = sys.platform == "win32"


class Location(ctypes.Structure):
    _pack_ = 8 if WINDOWS else 4
    _layout_ = "ms"
    _fields_ = (("kind", ctypes.c_int), ("id", ctypes.c_uint64))


class Stick(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (
        ("mode", ctypes.c_int),
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("active", ctypes.c_bool),
    )


class Motion(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = tuple(
        (name, ctypes.c_float)
        for name in ("qx", "qy", "qz", "qw", "ax", "ay", "az", "vx", "vy", "vz")
    )


class Button(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (("state", ctypes.c_bool), ("active", ctypes.c_bool))


class _Ip(ctypes.Union):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (
        ("v4", ctypes.c_uint32),
        ("v6", ctypes.c_uint8 * 16),
        ("words", ctypes.c_uint64 * 2),
    )


class Address(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _anonymous_ = ("ip",)
    _fields_ = (("ip", _Ip), ("kind", ctypes.c_int))


class Id(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (("all", ctypes.c_uint64),)


class Game(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = (("value", ctypes.c_uint64),)


ID = 0x0110000100000042


def address() -> Address:
    found = Address(kind=2)
    found.v6[:] = range(1, 17)
    return found


def _address_sum() -> int:
    sample = address()
    low, high = int(sample.words[0]), int(sample.words[1])
    return low ^ ((high << 1) & (2**64 - 1)) ^ 2


# Each case: the result type, the arguments, how to read the result, and what it should be.
CASES: dict[str, tuple[type, tuple[object, ...], Callable[[Any], object], object]] = {
    "TakeLocation": (ctypes.c_uint64, (Location(1, 0x123456789),), int, 0x123456789 * 16 + 1),
    "ReturnStick": (Stick, (), lambda r: (r.mode, r.x, r.y, r.active), (2, 1.5, -2.25, True)),
    "ReturnMotion": (
        Motion,
        (),
        lambda r: tuple(getattr(r, n) for n, _ in r._fields_),
        (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0),
    ),
    "ReturnButton": (Button, (), lambda r: (r.state, r.active), (True, False)),
    "ReturnAddress": (Address, (), lambda r: (tuple(r.v6), r.kind), (tuple(range(1, 17)), 2)),
    "TakeAddress": (ctypes.c_uint64, (address(),), int, _address_sum()),
    "ReturnId": (Id, (), lambda r: r.all, ID),
    "TakeId": (ctypes.c_uint64, (Id(ID),), int, ID),
    "ReturnGame": (Game, (), lambda r: r.value, 0x1234),
    "TakeGame": (ctypes.c_uint64, (Game(0x1234),), int, 0x1234),
}


def main(library: str, case: str) -> None:
    restype, args, read, expected = CASES[case]
    function = getattr(ctypes.CDLL(library), case)
    function.restype = restype
    function.argtypes = [type(arg) for arg in args]
    try:
        found = read(function(*args))
    except OSError as exc:
        # On Windows ctypes turns an access violation into OSError.
        print(f"crashed: {exc}")
        return
    print("ok" if found == expected else f"wrong: {found!r}, not {expected!r}")


if __name__ == "__main__":
    main(*sys.argv[1:])
