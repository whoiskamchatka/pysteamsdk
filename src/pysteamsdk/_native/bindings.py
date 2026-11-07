"""Bind generated declarations of the Steamworks flat API to the library a session holds.

``python -m codegen bindings`` writes a declarations module for one target: ctypes records,
enums and typedefs, and the signature of each flat function it could bind safely, all from
layouts a native compiler checked. Importing the module checks that ctypes lays out every record
as that compiler did. bind() then attaches the functions to the library of an open Session,
never loading another copy.

A bound function runs through Session.call, so nothing reaches Steam once shutdown has begun and
shutdown waits for calls still in Steam; an interface pointer comes back as an Interface that
refuses to be used once its session closes. Arguments are converted strictly: an integer must
fit its C type, bytes never go where native code may write, and a reference is never null.
Pointers that functions return stay pointers borrowed from native code; nothing is copied until
the caller reads it. A pointer passed in is valid only during the call, so a caller must keep
alive whatever native code holds on to.
"""

import ctypes
import operator
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import Any, SupportsIndex

from pysteamsdk._native.lifecycle import Session
from pysteamsdk._native.loader import _LIBRARIES, _host
from pysteamsdk.errors import MissingExportsError, NotInitializedError, SteamError

# The version of the declarations format, which the generator writes as PROTOCOL.
PROTOCOL = 1

_SYSTEMS = {"win32": "windows", "linux": "linux", "darwin": "macos"}
# ctypes' type codes for integers; lower case is signed.
_INTEGER_CODES = frozenset("bBhHiIlLqQ")
_BYREF = type(ctypes.byref(ctypes.c_int()))
_VALUES = (ctypes._Pointer, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_wchar_p)
_POINTERS = (*_VALUES, _BYREF)
_DATA = (ctypes._SimpleCData, ctypes.Structure, ctypes.Union, ctypes.Array)

# A record's size, alignment and members, each as (name, offset, size), as the compiler has them.
Layouts = Mapping[type, tuple[int, int, tuple[tuple[str, int, int], ...]]]


class BindingError(SteamError):
    """Generated declarations that this process cannot use."""


class CString(ctypes.c_char_p):
    """A char pointer kept as a pointer: ``value`` copies the string it points to, or is None."""


class VoidPointer(ctypes.c_void_p):
    """A void pointer kept as a pointer instead of becoming an int."""


class FunctionPointer(ctypes.c_void_p):
    """A function pointer a record holds. These declarations give no way to call it."""


class Interface:
    """A Steamworks interface pointer, usable only while the session that returned it is open."""

    __slots__ = ("_address", "_session")

    def __init__(self, session: Session, address: int) -> None:
        self._session = session
        self._address = address

    @property
    def closed(self) -> bool:
        return self._session.closed

    @classmethod
    def from_param(cls, value: object) -> ctypes.c_void_p:
        if not isinstance(value, cls):
            raise TypeError(f"expected {cls.__name__}, not {type(value).__name__}")
        if value.closed:
            raise NotInitializedError(f"Steam has been shut down; this {cls.__name__} is stale.")
        return ctypes.c_void_p(value._address)

    def __repr__(self) -> str:
        state = "stale" if self.closed else "open"
        return f"<{type(self).__name__} at {self._address:#x}, {state}>"


class Pointer:
    """A pointer parameter to ``target``, a ctypes type, or to void when it is None."""

    __slots__ = ("_typed", "nullable", "target", "writable")

    def __init__(self, target: Any, *, writable: bool, nullable: bool = True) -> None:
        self.target = target
        self.writable = writable
        self.nullable = nullable
        self._typed = None if target is None else ctypes.POINTER(target)

    def from_param(self, value: object) -> object:
        if value is None:
            if not self.nullable:
                raise TypeError("a reference cannot be None")
            return None
        if not self.nullable and isinstance(value, _VALUES) and not value:
            raise TypeError("a reference cannot be a null pointer")
        # A char pointer made from bytes points into them, and so passes on their memory.
        into_bytes = isinstance(value, ctypes.c_char_p) and isinstance(value._objects, bytes)
        if self.writable and into_bytes and self.target in (None, ctypes.c_char):
            raise TypeError(
                "native code may write through this pointer, so it cannot take a pointer into "
                "bytes; pass a ctypes buffer such as ctypes.create_string_buffer(size)"
            )
        if isinstance(value, bytes):
            if self.writable:
                raise TypeError(
                    "native code may write through this pointer, so it cannot take bytes; "
                    "pass a ctypes buffer such as ctypes.create_string_buffer(size)"
                )
            if self.target not in (None, ctypes.c_char):
                raise TypeError(f"expected a pointer to {self.target.__name__}, not bytes")
        elif isinstance(value, str | int | bytearray | memoryview):
            # Text needs an encoding, and an int is not known to be an address.
            raise TypeError(f"expected a ctypes object, not {type(value).__name__}")
        if self._typed is not None:
            return self._typed.from_param(value)
        # A void pointer takes any object's address, and a pointer's value.
        if isinstance(value, _DATA) and not isinstance(value, _POINTERS):
            return ctypes.byref(value)
        return ctypes.c_void_p.from_param(value)

    def __repr__(self) -> str:
        name = "void" if self.target is None else self.target.__name__
        return f"{type(self).__name__}({name})"


class In(Pointer):
    """A pointer to const data, which native code only reads; bytes are accepted for chars."""

    __slots__ = ()

    def __init__(self, target: Any) -> None:
        super().__init__(target, writable=False)


class Out(Pointer):
    """A pointer native code may write through."""

    __slots__ = ()

    def __init__(self, target: Any) -> None:
        super().__init__(target, writable=True)


class Ref(Pointer):
    """A C++ reference, passed as a pointer that cannot be null."""

    __slots__ = ()

    def __init__(self, target: Any) -> None:
        super().__init__(target, writable=True, nullable=False)


class _Integer:
    """An integer parameter that refuses values its C type cannot hold, where ctypes wraps them."""

    __slots__ = ("high", "low", "name", "type")

    def __init__(self, ctype: Any) -> None:
        bits = ctypes.sizeof(ctype) * 8
        signed = str(ctype._type_).islower()
        self.type = ctype
        self.name = f"{'' if signed else 'u'}int{bits}"
        self.low = -(1 << (bits - 1)) if signed else 0
        self.high = (1 << (bits - 1 if signed else bits)) - 1

    def from_param(self, value: object) -> object:
        if not isinstance(value, SupportsIndex):
            raise TypeError(f"expected an integer, not {type(value).__name__}")
        number = operator.index(value)
        if not self.low <= number <= self.high:
            raise OverflowError(f"{number} does not fit in {self.name}")
        return self.type(number)


_integers: dict[Any, _Integer] = {}


def _argument(argtype: Any) -> Any:
    if (
        isinstance(argtype, type)
        and issubclass(argtype, ctypes._SimpleCData)
        and getattr(argtype, "_type_", None) in _INTEGER_CODES
    ):
        found = _integers.get(argtype)
        if found is None:
            found = _integers[argtype] = _Integer(argtype)
        return found
    return argtype


class Function:
    """A flat function bound for one session."""

    __slots__ = ("_call", "_returns", "_session", "name")

    def __init__(
        self,
        name: str,
        call: Callable[..., Any],
        session: Session,
        returns: type[Interface] | None = None,
    ) -> None:
        self.name = name
        self._call = call
        self._session = session
        self._returns = returns

    def __call__(self, *args: object) -> Any:
        # Arguments are converted inside the call too, so a stale handle is refused there.
        result = self._session.call(self.name, self._call, *args)
        if self._returns is not None and result is not None:
            return self._returns(self._session, result)
        return result

    def __repr__(self) -> str:
        return f"<bound {self.name}>"


class Bindings:
    """The flat functions of one declarations module, bound for one session, as attributes."""

    __slots__ = ("_functions", "_skipped")

    def __init__(self, functions: dict[str, Function], skipped: Mapping[str, str]) -> None:
        self._functions = functions
        self._skipped = dict(skipped)

    def __getattr__(self, name: str) -> Function:
        if name.startswith("_"):
            raise AttributeError(name)
        found = self._functions.get(name)
        if found is not None:
            return found
        reason = self._skipped.get(name)
        if reason is not None:
            raise AttributeError(f"{name} is not bound: {reason}")
        raise AttributeError(f"no flat function named {name}")

    def __dir__(self) -> list[str]:
        return sorted(self._functions)


def host_target() -> str | None:
    """This process's target, as the generator names it, or None when it is not supported."""
    system, machine, is_64bit = _host()
    if not is_64bit or (system, machine) not in _LIBRARIES:
        return None
    return f"{_SYSTEMS[system]}-{machine}"


def check_declarations(target: str, protocol: int, layouts: Layouts) -> None:
    """Refuse declarations for another target or format, or records ctypes lays out otherwise.

    Generated modules call this as they are imported.
    """
    if protocol != PROTOCOL:
        raise BindingError(
            f"These declarations use format {protocol}, but this pysteamsdk reads {PROTOCOL}; "
            "generate them again."
        )
    host = host_target()
    if target != host:
        raise BindingError(
            f"These declarations are for {target}, but this process is "
            f"{host or 'not a supported platform'}."
        )
    problems = []
    for record, (size, align, fields) in layouts.items():
        found = (ctypes.sizeof(record), ctypes.alignment(record))
        if found != (size, align):
            problems.append(
                f"{record.__name__} is {found[0]} bytes aligned to {found[1]}, "
                f"not {size} aligned to {align}"
            )
        for name, offset, length in fields:
            member = getattr(record, name)
            if (member.offset, member.size) != (offset, length):
                problems.append(
                    f"{record.__name__}.{name} is {member.size} bytes at {member.offset}, "
                    f"not {length} at {offset}"
                )
    if problems:
        raise BindingError(
            "ctypes lays out these records differently from the compiler the declarations "
            "were checked against:\n  " + "\n  ".join(problems)
        )


def attach(
    declarations: ModuleType, library: ctypes.CDLL
) -> dict[str, tuple[Callable[..., Any], type[Interface] | None]]:
    """Each declared function in ``library``, typed, with the Interface it returns if any.

    New function objects are made, so the library's own attributes keep the signatures others
    gave them. Raises MissingExportsError naming every function the library lacks.
    """
    found: dict[str, tuple[Callable[..., Any], type[Interface] | None]] = {}
    missing = []
    for name, (restype, argtypes) in sorted(declarations.FUNCTIONS.items()):
        returns = restype if isinstance(restype, type) and issubclass(restype, Interface) else None
        native = ctypes.c_void_p if returns is not None else restype
        prototype = ctypes.CFUNCTYPE(native, *map(_argument, argtypes))
        try:
            found[name] = (prototype((name, library)), returns)
        except AttributeError:
            missing.append(name)
    if missing:
        raise MissingExportsError(
            f"The Steamworks library does not export {', '.join(missing)}, which these "
            "declarations need. It may be older than the SDK they were generated from.",
            missing=tuple(missing),
        )
    return found


def bind(declarations: ModuleType, session: Session) -> Bindings:
    """The functions ``declarations`` describes, bound to the library ``session`` holds."""
    check = (getattr(declarations, "TARGET", None), getattr(declarations, "PROTOCOL", None))
    if check != (host_target(), PROTOCOL):
        message = f"{declarations.__name__} is not a declarations module for this process."
        raise BindingError(message)
    functions = {
        name: Function(name, call, session, returns)
        for name, (call, returns) in attach(declarations, session.library).items()
    }
    return Bindings(functions, declarations.SKIPPED)
