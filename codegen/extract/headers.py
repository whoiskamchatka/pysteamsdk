"""Read what steam_api.json lacks from the SDK headers, without a C++ parser.

Valve generates steam_api_flat.h with one declaration per line, so every code line must match one
of three exact forms, and any other line is reported rather than skipped. The other headers are
only scanned for S_API function names and interface version macros; their signatures and layouts
need a compiler frontend such as libclang.
"""

import re
from dataclasses import dataclass

from codegen import ir
from codegen.extract.cdecl import TypeSyntaxError, parse_param, parse_type, split_params

FLAT_HEADER = "steam_api_flat.h"

_DECLARATION = re.compile(r"S_API\s+(?P<returns>.*?)\b(?P<name>\w+)\s*\((?P<params>.*)\)\s*;")
_TYPEDEF = re.compile(r"typedef\s+(?P<type>[^;]+?)\s+(?P<name>\w+)\s*;")
_INLINE = re.compile(
    r"inline\s+(?P<returns>[^{]*?)\b(?P<name>\w+)\s*\(\s*\)\s*"
    r"\{\s*return\s+(?P<target>\w+)\s*\(\s*\)\s*;\s*\}"
)
_EXPORT = re.compile(r"^[ \t]*S_API\b[^;{}()]*?\b(?P<name>\w+)\s*\(", re.MULTILINE)
_VERSION = re.compile(
    r'^[ \t]*#[ \t]*define[ \t]+(?P<macro>\w*INTERFACE_VERSION\w*)[ \t]+"(?P<version>[^"]*)"',
    re.MULTILINE,
)


@dataclass(frozen=True)
class FlatParam:
    name: str
    type: ir.CType


@dataclass(frozen=True)
class FlatFunction:
    name: str
    returns: ir.CType
    params: tuple[FlatParam, ...]
    source: str


@dataclass(frozen=True)
class InlineAccessor:
    """An unexported convenience accessor that forwards to a versioned, exported one."""

    name: str
    target: str
    source: str


@dataclass(frozen=True)
class FlatHeader:
    functions: tuple[FlatFunction, ...]
    typedefs: tuple[ir.Typedef, ...]
    inline_accessors: tuple[InlineAccessor, ...]
    issues: tuple[ir.Issue, ...]


@dataclass(frozen=True)
class VersionMacro:
    macro: str
    version: str
    source: str


@dataclass(frozen=True)
class HeaderScan:
    exports: tuple[ir.Export, ...]
    versions: tuple[VersionMacro, ...]


def parse_flat_header(text: str) -> FlatHeader:
    functions: list[FlatFunction] = []
    typedefs: list[ir.Typedef] = []
    inline: list[InlineAccessor] = []
    issues: list[ir.Issue] = []
    for number, line in enumerate(text.splitlines(), 1):
        code = line.split("//", 1)[0].strip()
        if not code or code.startswith("#"):
            continue
        source = f"{FLAT_HEADER}:{number}"
        try:
            if match := _DECLARATION.fullmatch(code):
                params = tuple(
                    FlatParam(name, ctype)
                    for ctype, name in map(parse_param, split_params(match["params"]))
                )
                returns = parse_type(match["returns"])
                functions.append(FlatFunction(match["name"], returns, params, source))
            elif match := _TYPEDEF.fullmatch(code):
                typedefs.append(ir.Typedef(match["name"], parse_type(match["type"]), source))
            elif match := _INLINE.fullmatch(code):
                inline.append(InlineAccessor(match["name"], match["target"], source))
            else:
                issues.append(ir.Issue("error", "unparsed-flat-line", code, source))
        except TypeSyntaxError as exc:
            issues.append(ir.Issue("error", "unparsed-flat-line", str(exc), source))
    return FlatHeader(tuple(functions), tuple(typedefs), tuple(inline), tuple(issues))


def scan_headers(texts: dict[str, str]) -> HeaderScan:
    """``texts`` maps each header's file name to its contents."""
    exports: list[ir.Export] = []
    versions: list[VersionMacro] = []
    for name in sorted(texts):
        text = texts[name]
        for match in _VERSION.finditer(text):
            source = f"{name}:{_line(text, match.start('macro'))}"
            versions.append(VersionMacro(match["macro"], match["version"], source))
        if name == FLAT_HEADER:
            continue
        for match in _EXPORT.finditer(text):
            exports.append(ir.Export(match["name"], f"{name}:{_line(text, match.start('name'))}"))
    return HeaderScan(tuple(exports), tuple(versions))


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
