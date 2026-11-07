"""pysteamsdk._native.bindings against the C runtime library, which every platform has."""

import ctypes
import enum
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from pysteamsdk._native import bindings
from pysteamsdk._native.bindings import In, Interface, Out, Ref
from pysteamsdk._native.lifecycle import Lifecycle, Session
from pysteamsdk.errors import MissingExportsError, NotInitializedError

LIBC = ctypes.CDLL("msvcrt") if sys.platform == "win32" else ctypes.CDLL(None)
TARGET = bindings.host_target()


class Point(ctypes.Structure):
    _fields_ = (("x", ctypes.c_int32), ("y", ctypes.c_int32))


class Other(ctypes.Structure):
    _fields_ = (("x", ctypes.c_int32),)


class IThing(Interface):
    __slots__ = ()


class Color(enum.IntEnum):
    RED = 3


def session() -> Session:
    return Session(cast(Any, SimpleNamespace(library=LIBC, path=Path("libc"))), 1, 2)


def declarations(functions: dict[str, Any], **values: object) -> ModuleType:
    module = ModuleType("declarations")
    module.__dict__.update(
        {"TARGET": TARGET, "PROTOCOL": bindings.PROTOCOL, "FUNCTIONS": functions, "SKIPPED": {}}
    )
    module.__dict__.update(values)
    return module


@pytest.mark.parametrize(
    ("ctype", "low", "high"),
    [
        (ctypes.c_int8, -(2**7), 2**7 - 1),
        (ctypes.c_uint8, 0, 2**8 - 1),
        (ctypes.c_int16, -(2**15), 2**15 - 1),
        (ctypes.c_uint16, 0, 2**16 - 1),
        (ctypes.c_int32, -(2**31), 2**31 - 1),
        (ctypes.c_uint32, 0, 2**32 - 1),
        (ctypes.c_int64, -(2**63), 2**63 - 1),
        (ctypes.c_uint64, 0, 2**64 - 1),
    ],
)
def test_integers_must_fit_their_c_type(ctype: Any, low: int, high: int) -> None:
    adapter = bindings._argument(ctype)
    assert adapter is bindings._argument(ctype)
    for value in (low, high, Color.RED, True):
        assert adapter.from_param(value).value == value
    for value in (low - 1, high + 1):
        with pytest.raises(OverflowError, match=f"{value} does not fit in u?int"):
            adapter.from_param(value)
    for wrong in (1.0, "1", None):
        with pytest.raises(TypeError):
            adapter.from_param(wrong)


def test_pointers_to_const_chars_take_bytes_and_nothing_else_text_like() -> None:
    strlen = ctypes.CFUNCTYPE(ctypes.c_size_t, cast(Any, In(ctypes.c_char)))(("strlen", LIBC))
    assert strlen(b"gadget") == 6
    assert strlen(ctypes.create_string_buffer(b"four")) == 4
    for wrong in ("text", 5, bytearray(b"x"), memoryview(b"x")):
        with pytest.raises(ctypes.ArgumentError, match="expected a ctypes object"):
            strlen(wrong)


def test_writable_pointers_never_take_bytes() -> None:
    memset = ctypes.CFUNCTYPE(ctypes.c_void_p, cast(Any, Out(None)), ctypes.c_int, ctypes.c_size_t)
    fill = memset(("memset", LIBC))
    buffer = (ctypes.c_uint8 * 4)()
    fill(buffer, 7, 3)
    assert list(buffer) == [7, 7, 7, 0]
    point = Point(1, 2)
    fill(point, 0, ctypes.sizeof(point))
    assert (point.x, point.y) == (0, 0)
    fill(ctypes.byref(point, 4), 0xFF, 4)
    assert (point.x, point.y) == (0, -1)
    # A pointer passes its value: memset clears the text, not the pointer to it.
    text = ctypes.c_wchar_p("ab")
    fill(text, 0, 2)
    assert text.value == ""
    data = b"keep"
    for wrong in (data, ctypes.c_char_p(data), bindings.CString(data), Out(ctypes.c_char)):
        with pytest.raises((ctypes.ArgumentError, TypeError)):
            fill(wrong, 0, 1)
    assert data == b"keep"
    for target in (ctypes.c_char, None):
        with pytest.raises(TypeError, match="cannot take a pointer into bytes"):
            Out(target).from_param(ctypes.c_char_p(data))
        assert In(target).from_param(ctypes.c_char_p(data)) is not None
    # Native code writes a pointer into the slot, not into the bytes it points to.
    assert Out(bindings.CString).from_param(bindings.CString(data)) is not None
    with pytest.raises(TypeError, match="cannot take bytes"):
        Out(ctypes.c_char).from_param(b"x")


def test_typed_pointers_take_their_type_and_none() -> None:
    pointer = Out(Point)
    point = Point()
    for value in (point, ctypes.pointer(point), (Point * 2)(), ctypes.byref(point)):
        assert pointer.from_param(value) is not None
    assert pointer.from_param(None) is None
    with pytest.raises(TypeError):
        pointer.from_param(Other())
    with pytest.raises(TypeError, match="expected a pointer to Point, not bytes"):
        In(Point).from_param(bytes(8))
    assert repr(In(None)) == "In(void)"
    assert repr(Out(Point)) == "Out(Point)"


def test_references_cannot_be_null() -> None:
    reference = Ref(Point)
    assert reference.from_param(Point()) is not None
    with pytest.raises(TypeError, match="a reference cannot be None"):
        reference.from_param(None)
    for null in (ctypes.POINTER(Point)(), ctypes.c_void_p()):
        with pytest.raises(TypeError, match="a reference cannot be a null pointer"):
            reference.from_param(null)
    assert reference.from_param(ctypes.pointer(Point())) is not None
    with pytest.raises(TypeError, match="cannot take bytes"):
        Ref(ctypes.c_char * 4).from_param(b"abc")


def test_interfaces_check_their_type_and_session() -> None:
    current = session()
    thing = IThing(current, 0x1234)
    assert IThing.from_param(thing).value == 0x1234
    assert repr(thing) == "<IThing at 0x1234, open>"
    for wrong in (0x1234, None, ctypes.c_void_p(0x1234)):
        with pytest.raises(TypeError, match="expected IThing"):
            IThing.from_param(wrong)
    current._open = False
    assert thing.closed
    assert repr(thing) == "<IThing at 0x1234, stale>"
    with pytest.raises(NotInitializedError, match="this IThing is stale"):
        IThing.from_param(thing)


def test_bound_functions_call_the_library_while_the_session_is_open() -> None:
    current = session()
    api = bindings.bind(
        declarations(
            {
                "labs": (ctypes.c_long, (ctypes.c_long,)),
                "strchr": (IThing, (In(ctypes.c_char), ctypes.c_int)),
            },
            SKIPPED={"strtok": "it keeps a pointer between calls"},
        ),
        current,
    )
    assert api.labs(-5) == 5
    assert repr(api.labs) == "<bound labs>"
    assert dir(api) == ["labs", "strchr"]
    text = ctypes.create_string_buffer(b"gadget")
    found = api.strchr(text, ord("d"))
    assert isinstance(found, IThing)
    assert IThing.from_param(found).value == ctypes.addressof(text) + 2
    assert api.strchr(text, ord("z")) is None
    with pytest.raises(ctypes.ArgumentError, match="OverflowError"):
        api.labs(2**64)
    with pytest.raises(AttributeError, match="strtok is not bound: it keeps a pointer"):
        api.strtok  # noqa: B018
    with pytest.raises(AttributeError, match="no flat function named strcpy"):
        api.strcpy  # noqa: B018
    current._open = False
    with pytest.raises(NotInitializedError, match="labs can no longer be called"):
        api.labs(-5)
    with pytest.raises(NotInitializedError):
        bindings.bind(declarations({}), current)


def test_attaching_leaves_the_library_signatures_alone() -> None:
    before = (LIBC.labs.restype, LIBC.labs.argtypes)
    found = bindings.attach(declarations({"labs": (ctypes.c_long, (ctypes.c_long,))}), LIBC)
    assert (LIBC.labs.restype, LIBC.labs.argtypes) == before
    assert found["labs"][0](-3) == 3


def test_every_missing_export_is_reported() -> None:
    module = declarations(
        {
            "labs": (ctypes.c_long, (ctypes.c_long,)),
            "pysteamsdk_absent_one": (None, ()),
            "pysteamsdk_absent_two": (None, ()),
        }
    )
    with pytest.raises(MissingExportsError) as caught:
        bindings.attach(module, LIBC)
    assert caught.value.missing == ("pysteamsdk_absent_one", "pysteamsdk_absent_two")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"TARGET": "plan9-mips"}, "not a declarations module for this process"),
        ({"PROTOCOL": bindings.PROTOCOL + 1}, "not a declarations module for this process"),
    ],
)
def test_declarations_for_another_process_are_not_bound(
    changes: dict[str, object], message: str
) -> None:
    module = declarations({})
    module.__dict__.update(changes)
    with pytest.raises(bindings.BindingError, match=message):
        bindings.bind(module, session())


def test_declarations_check_their_target_format_and_layouts() -> None:
    assert TARGET is not None
    good: bindings.Layouts = {Point: (8, 4, (("x", 0, 4), ("y", 4, 4)))}
    bindings.check_declarations(TARGET, bindings.PROTOCOL, good)
    with pytest.raises(bindings.BindingError, match="are for plan9-mips, but this process is"):
        bindings.check_declarations("plan9-mips", bindings.PROTOCOL, good)
    with pytest.raises(bindings.BindingError, match="use format 0, but this pysteamsdk reads"):
        bindings.check_declarations(TARGET, 0, good)
    wrong: bindings.Layouts = {Point: (12, 8, (("x", 0, 4), ("y", 6, 2)))}
    with pytest.raises(bindings.BindingError) as caught:
        bindings.check_declarations(TARGET, bindings.PROTOCOL, wrong)
    assert str(caught.value).splitlines()[1:] == [
        "  Point is 8 bytes aligned to 4, not 12 aligned to 8",
        "  Point.y is 4 bytes at 4, not 2 at 6",
    ]


def test_strings_stay_pointers_until_read() -> None:
    getenv = ctypes.CFUNCTYPE(bindings.CString, cast(Any, In(ctypes.c_char)))(("getenv", LIBC))
    found = getenv(b"PYSTEAMSDK_SURELY_UNSET")
    assert isinstance(found, bindings.CString)
    assert found.value is None
    record = type("Holder", (ctypes.Structure,), {"_fields_": (("name", bindings.CString),)})()
    record.name = b"gadget"
    assert isinstance(record.name, bindings.CString)
    assert record.name.value == b"gadget"


def test_shutdown_waits_for_calls_still_in_steam_and_refuses_new_ones(tmp_path: Path) -> None:
    order: list[str] = []

    def native(name: str, result: object = None) -> Callable[..., object]:
        def call(*args: object) -> object:
            order.append(name)
            return result

        return call

    steam = SimpleNamespace(
        SteamAPI_InitFlat=native("InitFlat", 0),
        SteamAPI_Shutdown=native("Shutdown"),
        SteamAPI_GetHSteamPipe=native("GetHSteamPipe", 1),
        SteamAPI_GetHSteamUser=native("GetHSteamUser", 2),
    )
    path = tmp_path / "steam_api"
    path.write_bytes(b"")
    life = Lifecycle(load=lambda _: cast(Any, steam))
    current = life.init(path)
    inside, leave = threading.Event(), threading.Event()

    def slow() -> int:
        inside.set()
        leave.wait(10)
        order.append("Slow")
        return 7

    function = bindings.Function("Slow", slow, current)
    results: list[object] = []
    caller = threading.Thread(target=lambda: results.append(function()))
    caller.start()
    assert inside.wait(10)
    closer = threading.Thread(target=life.shutdown)
    closer.start()
    deadline = time.monotonic() + 10
    while not current.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(NotInitializedError, match="Slow can no longer be called"):
        function()
    closer.join(0.2)
    assert closer.is_alive()
    assert "Shutdown" not in order
    leave.set()
    caller.join(10)
    closer.join(10)
    assert results == [7]
    assert order[-2:] == ["Slow", "Shutdown"]
