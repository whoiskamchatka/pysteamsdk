"""Load and apply codegen/overrides.toml, the reviewed corrections to the SDK's metadata.

An entry is applied only while its target in the SDK still looks as the entry describes.
Otherwise it is reported as an error and left out, so a new SDK release forces a fresh review
instead of a silently wrong correction. What the metadata alone can confirm, such as identical
fields in structs that share a callback ID, is checked by codegen.validate.
"""

import dataclasses
import re
import tomllib
import typing
from dataclasses import dataclass
from pathlib import Path

from codegen import ir
from codegen.abi.checks import by_value_records
from codegen.cdecl import TypeSyntaxError, parse_param, parse_type, split_params
from codegen.extract.api_json import MetadataError
from codegen.extract.headers import HeaderScan

DEFAULT = Path(__file__).resolve().parent.parent / "overrides.toml"

_SCHEMAS: dict[str, dict[str, type]] = {
    "interface_version": {"interface": str, "macro": str, "version": str, "reason": str},
    "shared_callback": {"callback_id": int, "structs": list, "reason": str},
    "external_type": {"name": str, "declaration": str, "header": str, "reason": str},
    "by_value": {"record": str, "direct": list, "shim": list, "reason": str},
    "callback": {"function": str, "param": str, "reason": str},
    "retained_pointer": {"function": str, "param": str, "holder": str, "reason": str},
}
_HOLDERS: tuple[ir.Holder, ...] = typing.get_args(ir.Holder)
_KINDS = {str: "a string", int: "an integer", list: "an array"}
_DECLARATIONS: tuple[ir.Declaration, ...] = typing.get_args(ir.Declaration)
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
# typedef R ( [S_CALLTYPE] *Name )( params ); as the SDK declares function pointer types.
_FUNCTION_TYPEDEF = (
    r"\btypedef\s+([^;{{}}()]+?)\(\s*(?:S_CALLTYPE\s*)?\*\s*{name}\s*\)"
    r"\s*\(([^;{{}}()]*)\)\s*;"
)


@dataclass(frozen=True)
class InterfaceVersion:
    interface: str
    macro: str
    version: str
    source: str


@dataclass(frozen=True)
class CallbackReview:
    """A function pointer parameter reviewed as safe to call back; apply() finds its signature."""

    function: str
    param: str
    source: str


@dataclass(frozen=True)
class Overrides:
    interface_versions: tuple[InterfaceVersion, ...] = ()
    shared_callbacks: tuple[ir.SharedCallback, ...] = ()
    external_types: tuple[ir.ExternalType, ...] = ()
    by_value: tuple[ir.ByValue, ...] = ()
    callbacks: tuple[CallbackReview, ...] = ()
    retained: tuple[ir.Retained, ...] = ()


def load(text: str, name: str = DEFAULT.name) -> Overrides:
    """Read an overrides file. Raises MetadataError listing every problem with its shape."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MetadataError([f"{name}: {exc}"]) from None
    problems = [f"{name}: unknown table {key!r}" for key in sorted(data.keys() - _SCHEMAS.keys())]
    tables: dict[str, list[tuple[str, dict[str, typing.Any]]]] = {}
    for table, schema in _SCHEMAS.items():
        entries = data.get(table, [])
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            problems.append(f"{name}: {table} must be an array of tables, [[{table}]]")
            continue
        tables[table] = [(f"{name}#/{table}/{index}", entry) for index, entry in enumerate(entries)]
        for source, entry in tables[table]:
            problems.extend(f"{source}: {problem}" for problem in _entry_problems(entry, schema))
    for table, keys in (
        ("interface_version", ("interface",)),
        ("shared_callback", ("callback_id",)),
        ("external_type", ("name",)),
        ("by_value", ("record",)),
        ("callback", ("function", "param")),
        ("retained_pointer", ("function", "param")),
    ):
        seen: dict[object, str] = {}
        for source, entry in tables.get(table, []):
            parts = [entry.get(key) for key in keys]
            if not all(isinstance(part, str | int) for part in parts):
                continue
            target = parts[0] if len(parts) == 1 else ".".join(map(str, parts))
            if target in seen:
                problems.append(f"{source}: {target!r} is already overridden at {seen[target]}")
            seen.setdefault(target, source)
    if problems:
        raise MetadataError(problems)
    return Overrides(
        tuple(
            InterfaceVersion(e["interface"], e["macro"], e["version"], source)
            for source, e in tables["interface_version"]
        ),
        tuple(
            ir.SharedCallback(e["callback_id"], tuple(e["structs"]), source)
            for source, e in tables["shared_callback"]
        ),
        tuple(
            ir.ExternalType(e["name"], e["declaration"], e["header"], source)
            for source, e in tables["external_type"]
        ),
        tuple(
            ir.ByValue(e["record"], tuple(e["direct"]), tuple(e["shim"]), source)
            for source, e in tables["by_value"]
        ),
        tuple(
            CallbackReview(e["function"], e["param"], source) for source, e in tables["callback"]
        ),
        tuple(
            ir.Retained(e["function"], e["param"], e["holder"], source)
            for source, e in tables["retained_pointer"]
        ),
    )


def _entry_problems(entry: dict[str, object], schema: dict[str, type]) -> list[str]:
    problems = [f"unknown key {key!r}" for key in sorted(entry.keys() - schema.keys())]
    for key, kind in schema.items():
        value = entry.get(key)
        if value is None:
            problems.append(f"missing {key!r}")
        elif type(value) is not kind:
            problems.append(f"{key!r} must be {_KINDS[kind]}")
    if isinstance(entry.get("reason"), str) and not str(entry["reason"]).strip():
        problems.append("'reason' must say why the override exists")
    for key, what in (("structs", "struct names"), ("direct", "targets"), ("shim", "targets")):
        items = entry.get(key)
        if isinstance(items, list) and not all(isinstance(item, str) for item in items):
            problems.append(f"{key!r} must list {what}")
    declaration = entry.get("declaration")
    if isinstance(declaration, str) and declaration not in _DECLARATIONS:
        problems.append(f"'declaration' must be one of {', '.join(_DECLARATIONS)}")
    holder = entry.get("holder")
    if isinstance(holder, str) and holder not in _HOLDERS:
        problems.append(f"'holder' must be one of {', '.join(_HOLDERS)}")
    return problems


def apply(
    metadata: ir.Metadata, overrides: Overrides, headers: dict[str, str], scan: HeaderScan
) -> tuple[ir.Metadata, list[ir.Issue]]:
    """Apply the entries whose targets still match, and report each other one as an error.

    ``headers`` maps every SDK header's file name to its text.
    """
    issues: list[ir.Issue] = []
    versions: dict[str, ir.Interface] = {}
    interfaces = {item.name: item for item in metadata.interfaces}
    for entry in overrides.interface_versions:
        problem = _version_problem(entry, interfaces.get(entry.interface), scan)
        if problem:
            issues.append(ir.Issue("error", "override-mismatch", problem, entry.source))
        else:
            interface = interfaces[entry.interface]
            versions[interface.name] = dataclasses.replace(
                interface, version=entry.version, version_source=entry.source
            )
    # Without comments, so a declaration mentioned in prose cannot pass for a real one.
    code = {name: _COMMENTS.sub(" ", text) for name, text in headers.items()}
    external_types = []
    for external in overrides.external_types:
        problem = _declaration_problem(external, code)
        if problem:
            issues.append(ir.Issue("error", "override-mismatch", problem, external.source))
        else:
            external_types.append(external)
    applied = dataclasses.replace(
        metadata,
        interfaces=tuple(versions.pop(item.name, item) for item in metadata.interfaces),
        external_types=tuple(sorted(external_types, key=lambda item: item.name)),
        shared_callbacks=tuple(sorted(overrides.shared_callbacks, key=lambda i: i.callback_id)),
    )
    # A record the flat API no longer passes by value needs no review; one it does is checked
    # against its layouts by codegen.abi.checks.
    passed = {name for _, _, name in by_value_records(applied)}
    reviews = []
    for review in overrides.by_value:
        if review.record in passed:
            reviews.append(review)
        else:
            message = f"no flat function takes or returns {review.record} by value"
            issues.append(ir.Issue("error", "override-mismatch", message, review.source))
    reviews.sort(key=lambda item: item.record)
    owners: tuple[ir.Interface | ir.Struct, ...] = (*applied.interfaces, *applied.structs)
    methods: Methods = {
        method.flat_name: (owner, method) for owner in owners for method in owner.methods
    }
    hooks = []
    for callback in overrides.callbacks:
        found = _hook(callback, methods, applied, code)
        if isinstance(found, str):
            issues.append(ir.Issue("error", "override-mismatch", found, callback.source))
        else:
            hooks.append(found)
    retained = []
    for kept in overrides.retained:
        problem = _retained_problem(kept, methods)
        if problem:
            issues.append(ir.Issue("error", "override-mismatch", problem, kept.source))
        else:
            retained.append(kept)
    return (
        dataclasses.replace(
            applied,
            by_value=tuple(reviews),
            hooks=tuple(sorted(hooks, key=lambda item: (item.function, item.param))),
            retained=tuple(sorted(retained, key=lambda item: (item.function, item.param))),
        ),
        issues,
    )


Methods = dict[str, tuple[ir.Interface | ir.Struct, ir.Function]]


def _param(function: str, name: str, methods: Methods) -> ir.CType | str:
    """The type a flat function's parameter has, or what is wrong with the reference to it."""
    found = methods.get(function)
    if found is None:
        return f"{function} is not a flat function in steam_api.json"
    params = {param.name: param for param in found[1].params}
    if name not in params:
        return f"{function} has no parameter {name}"
    return params[name].flat_type or params[name].type


def _hook(
    review: CallbackReview, methods: Methods, metadata: ir.Metadata, code: dict[str, str]
) -> ir.Hook | str:
    ctype = _param(review.function, review.param, methods)
    if isinstance(ctype, str):
        return ctype
    plain = ctype.kind == "named" and not (ctype.pointers or ctype.reference or ctype.array)
    if not plain:
        return f"{review.function} {review.param} is {ctype.spelling}, not a callback typedef"
    name = ctype.base
    pattern = re.compile(_FUNCTION_TYPEDEF.format(name=re.escape(name)))
    found = {
        (returns.strip(), params.strip()): header
        for header, text in sorted(code.items())
        for returns, params in pattern.findall(text)
    }
    if len(found) != 1:
        where = "no header declares" if not found else "the headers disagree on"
        return f"{where} {name} as a function pointer type"
    (returns_text, params_text), header = next(iter(found.items()))
    try:
        returns = parse_type(returns_text)
        params = tuple(_unnamed(part) for part in split_params(params_text) if part != "void")
    except TypeSyntaxError as exc:
        return f"{name} in {header}: {exc}"
    described = {item.name: item.type for item in metadata.typedefs}.get(name)
    if described is not None and (
        described.kind != "function_pointer"
        or described.returns is None
        or described.returns.shape() != returns.shape()
        or [p.shape() for p in described.params] != [p.shape() for p in params]
    ):
        return f"{header} declares {name} otherwise than steam_api.json, {described.spelling}"
    return ir.Hook(review.function, review.param, name, returns, params, review.source)


def _unnamed(text: str) -> ir.CType:
    # A declaration may name its parameters or not.
    try:
        return parse_type(text)
    except TypeSyntaxError:
        return parse_param(text)[0]


def _retained_problem(entry: ir.Retained, methods: Methods) -> str | None:
    ctype = _param(entry.function, entry.param, methods)
    if isinstance(ctype, str):
        return ctype
    if not (ctype.pointers or ctype.reference):
        return f"{entry.function} {entry.param} is {ctype.spelling}, not a pointer"
    if entry.holder == "record" and not isinstance(methods[entry.function][0], ir.Struct):
        return f"{entry.function} is no record's method, so no record can hold {entry.param}"
    return None


def _version_problem(
    entry: InterfaceVersion, interface: ir.Interface | None, scan: HeaderScan
) -> str | None:
    if interface is None:
        return f"{entry.interface} is not in steam_api.json"
    if interface.version is not None:
        return f"steam_api.json now gives {entry.interface} version {interface.version!r}"
    defined = sorted({macro.version for macro in scan.versions if macro.macro == entry.macro})
    if not defined:
        return f"{entry.macro} is not defined in the headers"
    if defined != [entry.version]:
        found = " and ".join(map(repr, defined))
        return f"{entry.macro} is {found} in the headers, not {entry.version!r}"
    return None


def _declaration_problem(external: ir.ExternalType, code: dict[str, str]) -> str | None:
    text = code.get(external.header)
    if text is None:
        return f"{external.header} is not an SDK header"
    name = external.name
    word = re.escape(name.rpartition("::")[2])
    if external.declaration == "typedef":
        if not re.search(rf"\btypedef\b[^;{{}}]*\b{word}\b[^;{{}}]*;", text):
            return f"{external.header} declares no typedef {name}"
        return None
    if external.declaration in ("class", "struct"):
        if not re.search(_definition(external.declaration, word), text):
            return f"{external.header} does not define {external.declaration} {name}"
        return None
    if not re.search(rf"\b(?:class|struct)\s+{word}\s*;", text):
        return f"{external.header} does not declare {name}"
    for header, other in sorted(code.items()):
        if re.search(_definition("class|struct", word), other):
            return f"{name} is defined in {header}, so it is not incomplete"
    return None


def _definition(keyword: str, word: str) -> str:
    return rf"\b(?:{keyword})\s+{word}\s*(?::[^;{{]*)?\{{"
