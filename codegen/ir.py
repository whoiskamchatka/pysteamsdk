"""Steamworks declarations as the SDK states them, with names and type spellings kept verbatim.

steam_api.json has no sizes or layouts, and they differ by platform, so those are kept apart in
``Metadata.abi``, one set per target, computed by compiling the headers. There is no Valve prose.
Declarations are sorted by name and members keep their source order, so the same SDK always
serializes to the same bytes.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import math
import re
import types
import typing
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

# Bumped whenever the serialized shape or the meaning of a field changes.
FORMAT = 4
# Metadata from SDK 1.65 nests 11 levels deep; each nested function pointer adds two more and
# each anonymous record inside another three.
MAX_DEPTH = 32

Severity = Literal["error", "warning", "note"]
SEVERITIES: tuple[Severity, ...] = ("error", "warning", "note")
Declaration = Literal["class", "struct", "typedef", "incomplete"]
RecordKind = Literal["struct", "union", "class"]
LayoutStatus = Literal["complete", "opaque", "unsupported"]


@dataclass(frozen=True, slots=True)
class CType:
    """A C or C++ type split into the parts a binding generator needs.

    ``kind`` is "named" for ``[const] base [*...] [&] [[N]]``, where ``const`` qualifies the
    base type and ``array`` makes an array of N elements of the rest, as in a C declaration;
    "function_pointer" for ``returns (*)(params)``; and "unparsed" for spellings this tool does
    not understand, which keep only ``spelling`` and are reported as issues.
    """

    spelling: str
    kind: Literal["named", "function_pointer", "unparsed"] = "named"
    base: str = ""
    const: bool = False
    pointers: int = 0
    reference: bool = False
    array: int | None = None
    returns: CType | None = None
    params: tuple[CType, ...] = ()

    def shape(self) -> tuple[object, ...]:
        """Everything but the spelling, to compare types written with different spacing."""
        if self.kind == "unparsed":
            return (self.kind, self.spelling)
        returns = self.returns.shape() if self.returns else None
        params = tuple(param.shape() for param in self.params)
        qualifiers = (self.const, self.pointers, self.reference, self.array)
        return (self.kind, self.base, *qualifiers, returns, params)

    def referenced_names(self) -> tuple[str, ...]:
        if self.kind == "function_pointer":
            assert self.returns is not None
            parts = (self.returns, *self.params)
            return tuple(name for part in parts for name in part.referenced_names())
        return (self.base,) if self.kind == "named" else ()


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    type: CType
    # steam_api.json "paramtype_flat": the type the flat C function takes instead of ``type``.
    flat_type: CType | None = None
    # Valve's buffer and output annotations, such as out_string_count, verbatim.
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Function:
    """A C++ method and the flat C function that exports it.

    The flat function takes a pointer to the owning interface or struct first, then each
    parameter's ``flat_type`` or else its ``type``, and returns ``flat_returns`` or else
    ``returns``. The extractor checks that rule against steam_api_flat.h for every function.
    """

    name: str
    flat_name: str
    returns: CType
    params: tuple[Param, ...]
    source: str
    # steam_api.json "returntype_flat": the type the flat C function returns instead.
    flat_returns: CType | None = None
    # The callback struct a SteamAPICall_t result completes with, or one the method posts.
    callresult: str | None = None
    callback: str | None = None


@dataclass(frozen=True, slots=True)
class Accessor:
    """An exported function returning an interface pointer; the JSON gives no signature."""

    kind: str
    name: str
    flat_name: str
    source: str


@dataclass(frozen=True, slots=True)
class Interface:
    name: str
    methods: tuple[Function, ...]
    source: str
    version: str | None = None
    accessors: tuple[Accessor, ...] = ()
    # The override that supplied ``version``, when steam_api.json has none.
    version_source: str | None = None


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    type: CType
    private: bool = False


@dataclass(frozen=True, slots=True)
class Struct:
    """A struct from "structs", or from "callback_structs" when it has a callback ID."""

    name: str
    fields: tuple[Field, ...]
    source: str
    callback_id: int | None = None
    methods: tuple[Function, ...] = ()


@dataclass(frozen=True, slots=True)
class EnumValue:
    name: str
    spelling: str
    # None when the spelling is not an integer expression codegen.cexpr can evaluate, which is
    # reported.
    value: int | None


@dataclass(frozen=True, slots=True)
class Enum:
    name: str
    qualified_name: str
    values: tuple[EnumValue, ...]
    source: str
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class Constant:
    name: str
    type: CType
    expression: str
    source: str
    # The expression's value converted to ``type`` as C converts it, a float only for
    # floating-point types. None when it cannot be evaluated exactly, which is reported.
    value: int | float | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class Typedef:
    name: str
    type: CType
    source: str


@dataclass(frozen=True, slots=True)
class ExternalType:
    """A type steam_api.json uses without describing it, located in a header by an override.

    An "incomplete" type is declared but never defined in the SDK, so only pointers to it work.
    The layout or signature of any other kind comes from compiling the header, not from here.
    """

    name: str
    declaration: Declaration
    header: str
    source: str


@dataclass(frozen=True, slots=True)
class SharedCallback:
    """Callback structs an override accepts as sharing one ID; they must have identical fields."""

    callback_id: int
    structs: tuple[str, ...]
    source: str


@dataclass(frozen=True, slots=True)
class ByValue:
    """How ctypes may pass a record the flat API takes or returns by value, from an override.

    ``direct`` lists the targets where a ctypes call has been shown to pass the record as the
    native compiler does, and ``shim`` those where it does not, so a native shim must take it by
    pointer instead. Every other target is unverified: it gets no direct binding either.
    """

    record: str
    direct: tuple[str, ...]
    shim: tuple[str, ...]
    source: str


@dataclass(frozen=True, slots=True)
class Export:
    """A function a header exports with S_API that steam_api.json does not describe."""

    name: str
    source: str


@dataclass(frozen=True, slots=True)
class Issue:
    severity: Severity
    code: str
    message: str
    source: str


@dataclass(frozen=True, slots=True)
class Input:
    """A file the metadata was read from, by its path inside the SDK."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class FieldLayout:
    """A data member as one target lays it out; sizes and offsets are in bytes.

    ``type`` is canonical: a fundamental type such as "unsigned long long"; "enum X", "struct X",
    "union X" or "class X" with X qualified; "T*" for a pointer, where T may be "void" or
    "function" and starts with "const " when the innermost target is const; and "T[N]" for an
    array. An anonymous or unnamed record type is just its keyword, and ``record`` describes it.

    ``size`` and ``align`` are the type's own, before packing or alignas apply. A bit-field has
    ``bit_offset``, in bits from the start of the record, and ``bit_width`` instead of
    ``offset``. Anonymous struct and union members and unnamed bit-fields have no name.
    """

    name: str
    type: str
    size: int
    align: int
    offset: int | None = None
    bit_offset: int | None = None
    bit_width: int | None = None
    record: RecordLayout | None = None


@dataclass(frozen=True, slots=True)
class RecordLayout:
    """A struct, union or class as one target lays it out.

    A "complete" record has a size, an alignment and every data member. An "opaque" one is
    declared but never defined, so it only works through pointers. An "unsupported" one uses
    something these layouts cannot describe, named in ``reason``; its size and alignment are kept
    when the compiler knows them. ``name`` is qualified, or empty for a record described in place.
    """

    name: str
    kind: RecordKind
    status: LayoutStatus
    source: str
    size: int | None = None
    align: int | None = None
    fields: tuple[FieldLayout, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EnumLayout:
    """``underlying`` is the integer type the target stores the enum as, such as "int"."""

    name: str
    underlying: str
    size: int


@dataclass(frozen=True, slots=True)
class TypedefLayout:
    """``type`` is canonical, as in FieldLayout; size and alignment are None when incomplete."""

    name: str
    type: str
    size: int | None
    align: int | None


@dataclass(frozen=True, slots=True)
class TargetLayout:
    """The layouts for one key of codegen.abi.TARGETS, compiled for ``triple``."""

    target: str
    triple: str
    records: tuple[RecordLayout, ...]
    enums: tuple[EnumLayout, ...]
    typedefs: tuple[TypedefLayout, ...]


@dataclass(frozen=True, slots=True)
class Abi:
    """Layouts computed by libclang, whose version is ``clang``, one entry per target."""

    clang: str
    targets: tuple[TargetLayout, ...]


@dataclass(frozen=True, slots=True)
class Metadata:
    # No default, so to_data always writes it.
    format: int
    sdk_version: str | None
    inputs: tuple[Input, ...]
    interfaces: tuple[Interface, ...]
    structs: tuple[Struct, ...]
    enums: tuple[Enum, ...]
    constants: tuple[Constant, ...]
    typedefs: tuple[Typedef, ...]
    external_types: tuple[ExternalType, ...]
    shared_callbacks: tuple[SharedCallback, ...]
    by_value: tuple[ByValue, ...]
    exports: tuple[Export, ...]
    # None when the metadata was extracted without computing layouts.
    abi: Abi | None
    issues: tuple[Issue, ...]

    def functions(self) -> tuple[Function, ...]:
        """Interface and struct methods; accessors are not included."""
        owners: tuple[Interface | Struct, ...] = (*self.interfaces, *self.structs)
        return tuple(method for owner in owners for method in owner.methods)


def sort_issues(issues: Iterable[Issue]) -> tuple[Issue, ...]:
    return tuple(
        sorted(issues, key=lambda i: (SEVERITIES.index(i.severity), i.code, i.source, i.message))
    )


_REQUIRED = object()


def to_data(value: object) -> object:
    """Convert IR objects to JSON-ready data, leaving out fields that hold their default."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        data: dict[str, object] = {}
        for item in dataclasses.fields(value):
            current = getattr(value, item.name)
            if item.default is not dataclasses.MISSING:
                default: object = item.default
            elif item.default_factory is not dataclasses.MISSING:
                default = item.default_factory()
            else:
                default = _REQUIRED
            if current != default:
                data[item.name] = to_data(current)
        return data
    if isinstance(value, dict):
        return {key: to_data(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [to_data(item) for item in value]
    return value


def dumps(metadata: Metadata) -> str:
    """Serialize to JSON text with LF line endings and a trailing newline."""
    return json.dumps(to_data(metadata), indent=1, ensure_ascii=False) + "\n"


class ReadError(ValueError):
    """Serialized metadata that cannot be read; ``problems`` lists everything found."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


def loads(text: str) -> Metadata:
    """Read what dumps wrote, checking that every field is present and of the right type.

    Only this FORMAT is read: metadata is cheap to extract again, so older formats are not
    converted. Text nested more than MAX_DEPTH levels is refused before it is parsed. Raises
    ReadError. Whether the declarations agree with each other is left to codegen.validate.
    """
    # json's own limit is not portable: since Python 3.14 it is however much C stack is left.
    # A fixed limit also bounds the recursion over CType here and in codegen.validate.
    if _too_deep(text):
        raise ReadError([f"nested more than {MAX_DEPTH} levels deep"])
    try:
        data = json.loads(
            text,
            object_pairs_hook=_unique_keys,
            parse_constant=_not_a_number,
            parse_float=_finite_float,
        )
    except ValueError as exc:
        raise ReadError([f"not valid JSON: {exc}"]) from None
    if type(data) is not dict or "format" not in data:
        raise ReadError(["not pysteamsdk metadata: no format"])
    if type(data["format"]) is not int or data["format"] != FORMAT:
        message = f"format {data['format']!r} is not supported; extract the SDK again for {FORMAT}"
        raise ReadError([message])
    reader = _Reader()
    metadata = reader.read(data, Metadata, "")
    if reader.problems:
        raise ReadError(reader.problems)
    assert isinstance(metadata, Metadata)
    return metadata


# A string, whose brackets do not count, or a bracket. An unterminated string runs to the end of
# the text, so the scan stays linear however hostile the text is.
_STRUCTURE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"?|[\[\]{}]', re.DOTALL)


def _too_deep(text: str) -> bool:
    # Exact for valid JSON. Invalid text is parsed only up to its first error, and up to there
    # this count matches the parser's nesting, so the parser never goes deeper than counted.
    depth = 0
    for match in _STRUCTURE.finditer(text):
        token = match.group()
        if token in ("[", "{"):
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif token in ("]", "}"):
            depth -= 1
    return False


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate key {key!r}")
        data[key] = value
    return data


def _not_a_number(name: str) -> object:
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    # 1e400 is valid JSON but would read as infinity, which dumps cannot write back.
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"{text} is out of range")
    return number


@functools.cache
def _hints(cls: type) -> dict[str, object]:
    return typing.get_type_hints(cls)


def _describe(kind: object) -> str:
    if isinstance(kind, type) and dataclasses.is_dataclass(kind):
        return "an object"
    names: dict[object, str] = {str: "a string", int: "an integer", float: "a number"}
    names |= {bool: "a boolean", list: "a list", dict: "an object", type(None): "null"}
    return names.get(kind, str(kind))


class _Reader:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def problem(self, path: str, message: str) -> None:
        self.problems.append(f"{path or '/'}: {message}")

    def read(self, value: object, hint: object, path: str) -> object:
        """``value`` as the type ``hint`` names; a mismatch is recorded and gives None."""
        origin, args = typing.get_origin(hint), typing.get_args(hint)
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            return self.record(value, hint, path)
        if origin in (types.UnionType, typing.Union):
            if value is None and type(None) in args:
                return None
            options = [option for option in args if option is not type(None)]
            if len(options) == 1:
                return self.read(value, options[0], path)
            if type(value) in options:
                return value
            expected = " or ".join(map(_describe, args))
        elif origin is Literal:
            if type(value) is str and value in args:
                return value
            expected = "one of " + ", ".join(map(repr, args))
        elif origin is tuple:
            if type(value) is list:
                return tuple(
                    self.read(item, args[0], f"{path}/{index}") for index, item in enumerate(value)
                )
            expected = "a list"
        elif origin is dict:
            if type(value) is dict:
                return {
                    key: self.read(item, args[1], f"{path}/{key}") for key, item in value.items()
                }
            expected = "an object"
        elif type(value) is hint:
            # Exact types: JSON true is not an integer here, and 1.0 is not one either.
            return value
        else:
            expected = _describe(hint)
        self.problem(path, f"expected {expected}, found {_describe(type(value))}")
        return None

    def record(self, value: object, cls: type, path: str) -> object:
        if type(value) is not dict:
            self.problem(path, f"expected an object, found {_describe(type(value))}")
            return None
        before = len(self.problems)
        fields = {item.name: item for item in dataclasses.fields(cls)}
        for key in sorted(value.keys() - fields.keys()):
            self.problem(f"{path}/{key}", "unknown field")
        arguments = {}
        for name, item in fields.items():
            if name in value:
                arguments[name] = self.read(value[name], _hints(cls)[name], f"{path}/{name}")
            elif (
                item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING
            ):
                self.problem(path, f"missing required field {name!r}")
        return cls(**arguments) if len(self.problems) == before else None
