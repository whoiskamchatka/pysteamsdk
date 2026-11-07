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
from codegen.extract.api_json import MetadataError
from codegen.extract.headers import HeaderScan

DEFAULT = Path(__file__).resolve().parent.parent / "overrides.toml"

_SCHEMAS: dict[str, dict[str, type]] = {
    "interface_version": {"interface": str, "macro": str, "version": str, "reason": str},
    "shared_callback": {"callback_id": int, "structs": list, "reason": str},
    "external_type": {"name": str, "declaration": str, "header": str, "reason": str},
    "by_value": {"record": str, "direct": list, "shim": list, "reason": str},
}
_KINDS = {str: "a string", int: "an integer", list: "an array"}
_DECLARATIONS: tuple[ir.Declaration, ...] = typing.get_args(ir.Declaration)
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class InterfaceVersion:
    interface: str
    macro: str
    version: str
    source: str


@dataclass(frozen=True)
class Overrides:
    interface_versions: tuple[InterfaceVersion, ...] = ()
    shared_callbacks: tuple[ir.SharedCallback, ...] = ()
    external_types: tuple[ir.ExternalType, ...] = ()
    by_value: tuple[ir.ByValue, ...] = ()


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
    for table, key in (
        ("interface_version", "interface"),
        ("shared_callback", "callback_id"),
        ("external_type", "name"),
        ("by_value", "record"),
    ):
        seen: dict[object, str] = {}
        for source, entry in tables.get(table, []):
            target = entry.get(key)
            if not isinstance(target, str | int):
                continue
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
    return dataclasses.replace(applied, by_value=tuple(reviews)), issues


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
