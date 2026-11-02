"""Steamworks declarations as the SDK states them, with names and type spellings kept verbatim.

There are no sizes or layouts here: steam_api.json has none, and they differ by platform. Nor is
there Valve prose. Declarations are sorted by name and members keep their source order, so the
same SDK always serializes to the same bytes.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Literal

# Bumped whenever the serialized shape changes.
FORMAT = 1

Severity = Literal["error", "warning", "note"]
SEVERITIES: tuple[Severity, ...] = ("error", "warning", "note")


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
    # None when the spelling is not an integer literal; such values are reported.
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
    # Only integer literals are evaluated; other expressions keep None and are reported.
    value: int | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class Typedef:
    name: str
    type: CType
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
    exports: tuple[Export, ...]
    issues: tuple[Issue, ...]

    def functions(self) -> tuple[Function, ...]:
        """Interface and struct methods; accessors are not included."""
        owners: tuple[Interface | Struct, ...] = (*self.interfaces, *self.structs)
        return tuple(method for owner in owners for method in owner.methods)


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
