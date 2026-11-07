"""Call the invented gadget library through generated declarations, for test_bindings_native.

    python -I bindings_child.py DECLARATIONS LIBRARY SCENARIO

Steam is initialized through pysteamsdk.init(), which loads LIBRARY as it would steam_api; the
library only pretends. Prints "ok" when every check passes; a failed check raises, and a call
ctypes gets wrong may crash this process instead, which the test sees too.
"""

import ctypes
import functools
import importlib.util
import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pysteamsdk
from pysteamsdk._native import bindings, lifecycle
from pysteamsdk.errors import LifecycleError, NotInitializedError

OWNER = 0x0110000100000042


def load(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("gadget_declarations", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(found: object, expected: object, what: str) -> None:
    if found != expected or type(found) is not type(expected):
        raise AssertionError(f"{what}: {found!r}, not {expected!r}")


def refused(call: Callable[[], object], error: type[Exception], what: str) -> str:
    """Call ``call``, which must raise ``error`` without reaching native code."""
    before = calls()
    try:
        call()
    except error as exc:
        if calls() != before:
            raise AssertionError(f"{what}: refused only after the native call") from exc
        return str(exc)
    raise AssertionError(f"{what}: not refused")


def calls() -> int:
    counter = SESSION.library.GadgetTest_Calls
    counter.restype = ctypes.c_uint32
    return int(counter())


def integers(api: Any, gadgets: Any, g: ModuleType) -> None:
    widths = {"8": 8, "16": 16, "32": 32, "64": 64}
    for suffix, bits in widths.items():
        signed = getattr(api, f"GadgetAPI_IGadgets_Echo{suffix}")
        unsigned = getattr(api, f"GadgetAPI_IGadgets_EchoU{suffix}")
        low, high = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        for value in (low, -1, 0, 1, high):
            check(signed(gadgets, value), value, f"int{bits} {value}")
        for value in (0, 1, high + 1, (1 << bits) - 1):
            check(unsigned(gadgets, value), value, f"uint{bits} {value}")
        for value in (low - 1, high + 1):
            refused(functools.partial(signed, gadgets, value), ctypes.ArgumentError, f"int{bits}")
        for value in (-1, 1 << bits):
            refused(
                functools.partial(unsigned, gadgets, value), ctypes.ArgumentError, f"uint{bits}"
            )
    check(api.GadgetAPI_IGadgets_EchoFloat(gadgets, -1.5), -1.5, "float")
    check(api.GadgetAPI_IGadgets_EchoDouble(gadgets, 2.0**-30), 2.0**-30, "double")
    check(api.GadgetAPI_IGadgets_Invert(gadgets, True), False, "bool true")
    check(api.GadgetAPI_IGadgets_Invert(gadgets, False), True, "bool false")
    refused(lambda: api.GadgetAPI_IGadgets_Echo32(gadgets, 1.5), ctypes.ArgumentError, "float")


def mix(api: Any, gadgets: Any, g: ModuleType) -> None:
    def expected(a: int, b: int, c: int, d: int, e: float, f: float, h: int, *, on: bool) -> int:
        mixed = a
        for part in (b, c, d, int(e * 4), int(f * 8), int(on), h % (1 << 32)):
            mixed = mixed * 31 + part
        return mixed % (1 << 64)

    color = g.EGadgetColor
    for args in (
        (-3, 65535, -7, 1 << 63, 1.25, -2.5, color.k_EGadgetColorAll),
        (127, 0, (1 << 31) - 1, 0, -0.5, 1e10, color.k_EGadgetColorRed),
    ):
        a, b, c, d, e, f, h = args
        for on in (True, False):
            found = api.GadgetAPI_IGadgets_Mix(gadgets, a, b, c, d, e, f, on, h)
            check(found, expected(a, b, c, d, e, f, int(h), on=on), f"mix {args} {on}")


def enums(api: Any, gadgets: Any, g: ModuleType) -> None:
    result = g.EGadgetResult
    check(api.GadgetAPI_IGadgets_Classify(gadgets, 5), int(result.k_EGadgetResultOK), "ok")
    check(api.GadgetAPI_IGadgets_Classify(gadgets, -5), int(result.k_EGadgetResultFail), "fail")
    check(api.GadgetAPI_IGadgets_Classify(gadgets, 0), int(result.k_EGadgetResultBusy), "busy")
    every = int(g.EGadgetColor.k_EGadgetColorAll)
    check(api.GadgetAPI_IGadgets_GetColorAll(gadgets), every, "a value the JSON gives as -1")
    check(every, -1 if g.TARGET == "windows-x86_64" else 0xFFFFFFFF, "how the target stores it")
    check(api.GadgetAPI_IGadgets_SetMode(gadgets, g.IGadgets_EMode.k_EModeFast), 11, "mode")
    refused(lambda: api.GadgetAPI_IGadgets_SetMode(gadgets, 1 << 40), ctypes.ArgumentError, "mode")


def strings(api: Any, gadgets: Any, g: ModuleType) -> None:
    check(api.GadgetAPI_IGadgets_Length(gadgets, b"gadget"), 6, "length")
    check(api.GadgetAPI_IGadgets_Length(gadgets, b""), 0, "empty")
    check(api.GadgetAPI_IGadgets_Length(gadgets, None), 0xFFFFFFFF, "null")
    check(api.GadgetAPI_IGadgets_Length(gadgets, ctypes.create_string_buffer(b"ab", 8)), 2, "buf")
    refused(lambda: api.GadgetAPI_IGadgets_Length(gadgets, "text"), ctypes.ArgumentError, "str")
    check(api.GadgetAPI_IGadgets_FillName(gadgets, None, 0), 7, "the size a null buffer asks")
    short = ctypes.create_string_buffer(4)
    check(api.GadgetAPI_IGadgets_FillName(gadgets, short, 4), 3, "a short buffer")
    check(short.value, b"gad", "what fits")
    full = ctypes.create_string_buffer(16)
    check(api.GadgetAPI_IGadgets_FillName(gadgets, full, 16), 6, "a long buffer")
    check(full.value, b"gadget", "the name")
    immutable = b"0123456789"
    fill = api.GadgetAPI_IGadgets_FillName
    refused(lambda: fill(gadgets, immutable, 10), ctypes.ArgumentError, "bytes to write")
    check(immutable, b"0123456789", "bytes kept")
    name = api.GadgetAPI_IGadgets_GetName(gadgets)
    check(type(name), bindings.CString, "a borrowed string stays a pointer")
    check(name.value, b"gadget", "read on demand")
    check(api.GadgetAPI_IGadgets_GetNoName(gadgets).value, None, "a null string")
    names = (bindings.CString * 3)(b"a", b"bc", None)
    check(api.GadgetAPI_IGadgets_CountNames(gadgets, names, 3), 103, "an array of strings")
    check(api.GadgetAPI_IGadgets_CountNames(gadgets, None, 0), -1, "no array")


def buffers(api: Any, gadgets: Any, g: ModuleType) -> None:
    def checksum(data: bytes) -> int:
        total = 0
        for byte in data:
            total = (total * 33 + byte) % (1 << 32)
        return total

    data = bytes(range(10, 60))
    check(api.GadgetAPI_IGadgets_Checksum(gadgets, data, len(data)), checksum(data), "bytes")
    array = (ctypes.c_uint8 * 3)(1, 2, 3)
    check(api.GadgetAPI_IGadgets_Checksum(gadgets, array, 3), checksum(b"\1\2\3"), "an array")
    check(api.GadgetAPI_IGadgets_Checksum(gadgets, None, 0), 0xFFFFFFFF, "null")
    refused(lambda: api.GadgetAPI_IGadgets_Checksum(gadgets, "text", 4), ctypes.ArgumentError, "s")
    out = (ctypes.c_uint8 * 5)()
    check(api.GadgetAPI_IGadgets_Scribble(gadgets, out, 5), 5, "written")
    check(list(out), [1, 4, 7, 10, 13], "into an array")
    spot = g.GadgetSpot_t()
    check(api.GadgetAPI_IGadgets_Scribble(gadgets, spot, 4), 4, "into a record")
    check(spot.m_nX, int.from_bytes(bytes([1, 4, 7, 10]), "little"), "the record's bytes")
    for wrong in (b"xxxx", bytearray(4), 1234):
        refused(
            functools.partial(api.GadgetAPI_IGadgets_Scribble, gadgets, wrong, 4),
            ctypes.ArgumentError,
            f"{type(wrong).__name__} to write into",
        )
    check(api.GadgetAPI_IGadgets_Scribble(gadgets, None, 0), 0, "null")


def pointers(api: Any, gadgets: Any, g: ModuleType) -> None:
    count = g.int32()
    check(api.GadgetAPI_IGadgets_GetCount(gadgets, count), True, "an int out")
    check(count.value, 42, "the int")
    later = g.int32()
    check(api.GadgetAPI_IGadgets_GetCount(gadgets, ctypes.byref(later)), True, "byref")
    check(later.value, 42, "through byref")
    check(api.GadgetAPI_IGadgets_GetCount(gadgets, None), False, "null")
    wide = ctypes.c_int64()
    refused(lambda: api.GadgetAPI_IGadgets_GetCount(gadgets, wide), ctypes.ArgumentError, "int64")
    spot = g.GadgetSpot_t()
    check(api.GadgetAPI_IGadgets_Locate(gadgets, spot), True, "a record out")
    check((spot.m_nX, spot.m_nY, spot.m_flZ), (3, -4, 0.5), "the record")
    start, end = g.GadgetSpot_t(1, 2, 0), g.GadgetSpot_t(4, -2, 9)
    check(api.GadgetAPI_IGadgets_Distance(gadgets, start, end), 7, "two references")
    distance = api.GadgetAPI_IGadgets_Distance
    refused(lambda: distance(gadgets, None, end), ctypes.ArgumentError, "a null reference")
    message = g.GadgetErrMsg()
    check(api.GadgetAPI_IGadgets_Explain(gadgets, message), True, "a reference to an array")
    check(message.value, b"explained", "the array")
    refused(lambda: api.GadgetAPI_IGadgets_Explain(gadgets, None), ctypes.ArgumentError, "null")


def records(api: Any, gadgets: Any, g: ModuleType) -> None:
    value = g.GadgetValue_t()
    check(api.GadgetAPI_IGadgets_Describe(gadgets, value), True, "described")
    check(value.m_eKind, int(g.EGadgetKind.k_EGadgetKindText), "an enum member")
    check(type(value.m_val.m_psz), bindings.CString, "a string member stays a pointer")
    check(value.m_val.m_psz.value, b"gadget", "a union member")
    check(value.m_szLabel, b"label", "a char array")
    check([list(row) for row in value.m_grid], [[-5, -4, -3], [5, 6, 7]], "a 2-D array")
    check((value.m_spot.m_nX, value.m_spot.m_nY, value.m_spot.m_flZ), (1, 2, 3.0), "nested")
    check(value.m_dScale, 0.25, "a double")
    check(value.m_pfnHook.value, None, "a null function pointer")
    check(ctypes.addressof(value.m_pNext.contents), ctypes.addressof(value), "a self pointer")
    address = g.GadgetAddress_t()
    check(api.GadgetAPI_IGadgets_GetAddress(gadgets, address), True, "an address")
    check(list(address.m_rgubIPv6), list(range(16, 32)), "an anonymous union's array")
    check(address.m_unIPv4, int.from_bytes(bytes(range(16, 20)), "little"), "the same bytes")
    check((address.m_usPort, address.m_bValid), (4242, True), "after a packed union")
    check(api.GadgetAPI_GadgetAddress_t_IsLoopback(address), False, "a record method")
    address.m_unIPv4 = 0x0100007F
    check(api.GadgetAPI_GadgetAddress_t_IsLoopback(address), True, "after a change")
    api.GadgetAPI_GadgetAddress_t_Clear(address)
    check(bytes(address), bytes(ctypes.sizeof(address)), "cleared")
    ready = g.GadgetReady_t()
    check(api.GadgetAPI_IGadgets_Poll(gadgets, ready), True, "a callback record")
    check(ready.m_eResult, int(g.EGadgetResult.k_EGadgetResultFail), "its enum")
    check(ready.m_ulId, 0x123456789ABCDEF0, "its member packing moves")
    joined = g.GadgetJoined_t()
    check(api.GadgetAPI_IGadgets_Join(gadgets, joined), True, "a record holding an ID")
    check((joined.m_gadget.m_id.m_all, joined.m_ubSeat), (OWNER, 5), "the ID and after it")
    owner = g.CGadgetID()
    check(api.GadgetAPI_IGadgets_GetOwnerInto(gadgets, owner), True, "an ID out")
    check(owner.m_id.m_all, OWNER, "the ID as 64 bits")
    check(bytes(owner.m_id.m_parts._storage), OWNER.to_bytes(8, "little"), "its bit-fields")
    check(api.GadgetAPI_IGadgets_GetOwner(gadgets), OWNER, "an ID passed as uint64")
    check(api.GadgetAPI_IGadgets_Fetch(gadgets, 0x1234), 0x1234 ^ 0x5A5A, "an ID taken")
    refused(lambda: api.GadgetAPI_IGadgets_Fetch(gadgets, -1), ctypes.ArgumentError, "uint64")


def by_value(api: Any, gadgets: Any, g: ModuleType) -> None:
    spot = api.GadgetAPI_IGadgets_GetSpot(gadgets)
    check(type(spot), g.GadgetSpot_t, "a record returned by value")
    check((spot.m_nX, spot.m_nY, spot.m_flZ), (7, -8, 2.5), "its members")
    sent = g.GadgetSpot_t(2, -3, 1.5)
    check(api.GadgetAPI_IGadgets_SpotSum(gadgets, sent), 2 * 1000 - 3 + 3, "taken by value")


def interfaces(api: Any, gadgets: Any, g: ModuleType) -> None:
    box = api.GadgetAPI_IGadgets_OpenBox(gadgets)
    check(type(box), g.IGadgetBox, "an interface a method returns")
    check(api.GadgetAPI_IGadgetBox_Count(box), 3, "called through it")
    shared = api.GadgetAPI_GadgetBox_v001()
    check(type(shared), g.IGadgetBox, "a global accessor")
    check(api.GadgetAPI_IGadgetBox_Count(shared), 3, "called through that")
    count = api.GadgetAPI_IGadgetBox_Count
    refused(lambda: count(gadgets), ctypes.ArgumentError, "another interface")
    refused(lambda: count(None), ctypes.ArgumentError, "no interface")
    refused(lambda: count(ctypes.c_void_p(1)), ctypes.ArgumentError, "a raw pointer")


def skipped(api: Any, gadgets: Any, g: ModuleType) -> None:
    check(dir(api), sorted(g.FUNCTIONS), "what is bound")
    for name, reason in g.SKIPPED.items():
        try:
            getattr(api, name)
        except AttributeError as exc:
            check(str(exc), f"{name} is not bound: {reason}", "why")
        else:
            raise AssertionError(f"{name} is bound")


def shutdown(api: Any, gadgets: Any, g: ModuleType) -> None:
    box = api.GadgetAPI_GadgetBox_v001()
    check(pysteamsdk.is_initialized(), True, "initialized")
    pysteamsdk.shutdown()
    check(pysteamsdk.is_initialized(), False, "shut down")
    before = int(SESSION._api.library.GadgetTest_Calls())
    attempts: list[Callable[[], object]] = [
        lambda: api.GadgetAPI_IGadgets_Echo32(gadgets, 1),
        lambda: api.GadgetAPI_Gadgets_v001(),
        lambda: api.GadgetAPI_IGadgetBox_Count(box),
    ]
    for call in attempts:
        try:
            call()
        except NotInitializedError as exc:
            check(str(exc).startswith("Steam has been shut down; GadgetAPI_"), True, str(exc))
        else:
            raise AssertionError("called after shutdown")
    check(int(SESSION._api.library.GadgetTest_Calls()), before, "nothing reached the library")
    check(repr(gadgets).endswith(", stale>"), True, repr(gadgets))
    try:
        g.IGadgets.from_param(gadgets)
    except NotInitializedError:
        pass
    else:
        raise AssertionError("a stale interface was passed on")
    try:
        bindings.bind(g, SESSION)
    except NotInitializedError:
        pass
    else:
        raise AssertionError("bound after shutdown")
    try:
        pysteamsdk.init(sys.argv[2])
    except LifecycleError:
        pass
    else:
        raise AssertionError("initialized twice")


SCENARIOS: dict[str, Callable[[Any, Any, ModuleType], None]] = {
    function.__name__: function
    for function in (
        integers,
        mix,
        enums,
        strings,
        buffers,
        pointers,
        records,
        by_value,
        interfaces,
        skipped,
        shutdown,
    )
}


def main(declarations: str, library: str, scenario: str) -> None:
    global SESSION
    module = load(declarations)
    pysteamsdk.init(library)
    SESSION = lifecycle._lifecycle.session()
    api = bindings.bind(module, SESSION)
    gadgets = api.GadgetAPI_Gadgets_v001()
    check(type(gadgets), module.IGadgets, "the user accessor after init")
    SCENARIOS[scenario](api, gadgets, module)
    pysteamsdk.shutdown()
    print("ok")


SESSION: lifecycle.Session

if __name__ == "__main__":
    main(*sys.argv[1:])
