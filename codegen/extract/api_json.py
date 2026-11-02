"""Read steam_api.json into IR declarations, validating its shape on the way.

Shape problems (bad JSON, a missing key, a value of the wrong type) are collected and raised
together as MetadataError. Anything readable but questionable becomes an Issue: unknown keys,
duplicate or conflicting declarations, type spellings outside the supported grammar.
"""

import dataclasses
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from codegen import ir
from codegen.extract.cdecl import TypeSyntaxError, parse_type

SOURCE = "steam_api.json"
ROOT_KEYS = ("callback_structs", "consts", "enums", "interfaces", "structs", "typedefs")
PARAM_ANNOTATIONS = frozenset(
    {
        "array_count",
        "buffer_count",
        "out_array_call",
        "out_array_count",
        "out_buffer_count",
        "out_string",
        "out_string_count",
        "out_struct",
    }
)
ACCESSOR_KINDS = frozenset({"user", "gameserver", "global"})

_INTEGER = re.compile(
    r"([+-]?)\s*(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)(?:[uU](?:ll|LL|l|L)?|(?:ll|LL|l|L)[uU]?)?"
)

Schema = dict[str, type]


class MetadataError(Exception):
    """The SDK metadata cannot be read: missing, not JSON, or not in the expected shape."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Declarations:
    interfaces: tuple[ir.Interface, ...]
    structs: tuple[ir.Struct, ...]
    enums: tuple[ir.Enum, ...]
    constants: tuple[ir.Constant, ...]
    typedefs: tuple[ir.Typedef, ...]
    issues: tuple[ir.Issue, ...]


def load(text: str) -> Declarations:
    """Raises MetadataError listing every shape problem found, not just the first."""
    try:
        data = json.loads(text, object_pairs_hook=_Pairs)
    except json.JSONDecodeError as exc:
        raise MetadataError([f"{SOURCE}: not valid JSON: {exc}"]) from None
    reader = _Reader()
    declarations = reader.root(data)
    if reader.problems:
        raise MetadataError(reader.problems)
    return declarations


def integer_literal(text: str) -> int | None:
    """The value of a signed C integer literal, or None for any other expression.

    The constant's declared type is not applied: "-1" stays -1 even for a uint32.
    """
    match = _INTEGER.fullmatch(text.strip())
    if match is None:
        return None
    sign, digits = match.groups()
    if digits[:2] in ("0x", "0X"):
        value = int(digits, 16)
    elif len(digits) > 1 and digits[0] == "0":
        value = int(digits, 8)
    else:
        value = int(digits)
    return -value if sign == "-" else value


class _Pairs(list[tuple[str, object]]):
    """A JSON object's pairs in order, so duplicate keys are seen instead of silently merged."""


def _pointer(path: str, key: str | int) -> str:
    return f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"


_KIND_NAMES: dict[type, str] = {
    _Pairs: "an object",
    list: "a list",
    str: "a string",
    int: "an integer",
    float: "a number",
    bool: "a boolean",
    type(None): "null",
}


class _Reader:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.issues: list[ir.Issue] = []

    def problem(self, path: str, message: str) -> None:
        self.problems.append(f"{SOURCE}#{path}: {message}")

    def issue(self, severity: ir.Severity, code: str, message: str, path: str) -> None:
        self.issues.append(ir.Issue(severity, code, message, f"{SOURCE}#{path}"))

    def record(
        self, node: object, path: str, required: Schema, optional: Schema, *, partial: bool = False
    ) -> dict[str, object] | None:
        """Check an object's keys and value types; None when it cannot be used.

        With ``partial``, a flawed object is still returned so its valid parts can be checked.
        """
        if not isinstance(node, _Pairs):
            self.problem(path, f"expected an object, found {_KIND_NAMES[type(node)]}")
            return None
        before = len(self.problems)
        record: dict[str, object] = {}
        for key, value in node:
            if key in record:
                self.problem(_pointer(path, key), "duplicate key")
            record[key] = value
        for key, kind in {**required, **optional}.items():
            if key not in record:
                if key in required:
                    self.problem(path, f"missing required key {key!r}")
            elif not _is(record[key], kind):
                found = _KIND_NAMES[type(record[key])]
                self.problem(_pointer(path, key), f"expected {_KIND_NAMES[kind]}, found {found}")
        for key in sorted(record.keys() - required.keys() - optional.keys()):
            message = f"unknown key {key!r} is not read"
            self.issue("warning", "unknown-key", message, _pointer(path, key))
        return record if partial or len(self.problems) == before else None

    def items(self, record: dict[str, object], key: str) -> list[object]:
        """A list member, or nothing when it is missing or invalid (already reported)."""
        value = record.get(key)
        return value if isinstance(value, list) and not isinstance(value, _Pairs) else []

    def ctype(self, spelling: str, path: str) -> ir.CType:
        try:
            return parse_type(spelling)
        except TypeSyntaxError as exc:
            self.issue("warning", "unsupported-type-syntax", str(exc), path)
            return ir.CType(spelling, kind="unparsed")

    def root(self, data: object) -> Declarations:
        # Sections are independent: a missing one must not hide problems in the others.
        record = self.record(data, "", {key: list for key in ROOT_KEYS}, {}, partial=True)
        if record is None:
            return Declarations((), (), (), (), (), ())
        enums: list[ir.Enum | None] = []
        constants: list[ir.Constant | None] = []
        interfaces = [
            self.interface(node, f"/interfaces/{index}", enums, constants)
            for index, node in enumerate(self.items(record, "interfaces"))
        ]
        structs = [
            self.struct(
                node, f"/{section}/{index}", section == "callback_structs", enums, constants
            )
            for section in ("structs", "callback_structs")
            for index, node in enumerate(self.items(record, section))
        ]
        for index, node in enumerate(self.items(record, "enums")):
            enums.append(self.enum(node, f"/enums/{index}", None))
        for index, node in enumerate(self.items(record, "consts")):
            constants.append(self.constant(node, f"/consts/{index}", None))
        typedefs = [
            self.typedef(node, f"/typedefs/{index}")
            for index, node in enumerate(self.items(record, "typedefs"))
        ]
        declarations = Declarations(
            tuple(sorted(filter(None, interfaces), key=lambda item: item.name)),
            tuple(sorted(filter(None, structs), key=lambda item: item.name)),
            tuple(sorted(filter(None, enums), key=lambda item: item.qualified_name)),
            tuple(sorted(filter(None, constants), key=lambda item: (item.scope or "", item.name))),
            tuple(sorted(filter(None, typedefs), key=lambda item: item.name)),
            (),
        )
        self.check(declarations)
        return dataclasses.replace(declarations, issues=tuple(self.issues))

    def interface(
        self,
        node: object,
        path: str,
        enums: list[ir.Enum | None],
        constants: list[ir.Constant | None],
    ) -> ir.Interface | None:
        record = self.record(
            node,
            path,
            {"classname": str, "methods": list},
            {"version_string": str, "accessors": list, "fields": list, "enums": list},
        )
        if record is None:
            return None
        name = str(record["classname"])
        if self.items(record, "fields"):
            self.issue(
                "warning", "interface-fields", "interface fields are not modelled", f"{path}/fields"
            )
        version = record.get("version_string")
        if version is None:
            self.issue("note", "interface-without-version", f"{name} has no version string", path)
        accessors = [
            self.accessor(item, f"{path}/accessors/{index}")
            for index, item in enumerate(self.items(record, "accessors"))
        ]
        for index, item in enumerate(self.items(record, "enums")):
            enums.append(self.enum(item, f"{path}/enums/{index}", name))
        methods = [
            self.method(item, f"{path}/methods/{index}")
            for index, item in enumerate(self.items(record, "methods"))
        ]
        return ir.Interface(
            name,
            tuple(filter(None, methods)),
            f"{SOURCE}#{path}",
            version=None if version is None else str(version),
            accessors=tuple(filter(None, accessors)),
        )

    def accessor(self, node: object, path: str) -> ir.Accessor | None:
        record = self.record(node, path, {"kind": str, "name": str, "name_flat": str}, {})
        if record is None:
            return None
        kind = str(record["kind"])
        if kind not in ACCESSOR_KINDS:
            self.issue("warning", "unknown-accessor-kind", f"accessor kind {kind!r}", path)
        return ir.Accessor(kind, str(record["name"]), str(record["name_flat"]), f"{SOURCE}#{path}")

    def method(self, node: object, path: str) -> ir.Function | None:
        record = self.record(
            node,
            path,
            {"methodname": str, "methodname_flat": str, "params": list, "returntype": str},
            {"returntype_flat": str, "callresult": str, "callback": str},
        )
        if record is None:
            return None
        params = [
            self.param(item, f"{path}/params/{index}")
            for index, item in enumerate(self.items(record, "params"))
        ]
        flat_returns = record.get("returntype_flat")
        callresult, callback = record.get("callresult"), record.get("callback")
        return ir.Function(
            str(record["methodname"]),
            str(record["methodname_flat"]),
            self.ctype(str(record["returntype"]), f"{path}/returntype"),
            tuple(filter(None, params)),
            f"{SOURCE}#{path}",
            flat_returns=None
            if flat_returns is None
            else self.ctype(str(flat_returns), f"{path}/returntype_flat"),
            callresult=None if callresult is None else str(callresult),
            callback=None if callback is None else str(callback),
        )

    def param(self, node: object, path: str) -> ir.Param | None:
        optional: Schema = dict.fromkeys(PARAM_ANNOTATIONS | {"paramtype_flat", "desc"}, str)
        record = self.record(node, path, {"paramname": str, "paramtype": str}, optional)
        if record is None:
            return None
        if "desc" in record:
            message = "parameter description left out: the IR carries no Valve prose"
            self.issue("note", "documentation-omitted", message, f"{path}/desc")
        flat_type = record.get("paramtype_flat")
        return ir.Param(
            str(record["paramname"]),
            self.ctype(str(record["paramtype"]), f"{path}/paramtype"),
            flat_type=None
            if flat_type is None
            else self.ctype(str(flat_type), f"{path}/paramtype_flat"),
            annotations={
                key: str(record[key]) for key in sorted(PARAM_ANNOTATIONS & record.keys())
            },
        )

    def struct(
        self,
        node: object,
        path: str,
        callback: bool,
        enums: list[ir.Enum | None],
        constants: list[ir.Constant | None],
    ) -> ir.Struct | None:
        required: Schema = {"struct": str, "fields": list}
        if callback:
            required["callback_id"] = int
        optional: Schema = {"methods": list, "enums": list, "consts": list}
        record = self.record(node, path, required, optional)
        if record is None:
            return None
        name = str(record["struct"])
        fields = [
            self.field(item, f"{path}/fields/{index}")
            for index, item in enumerate(self.items(record, "fields"))
        ]
        methods = [
            self.method(item, f"{path}/methods/{index}")
            for index, item in enumerate(self.items(record, "methods"))
        ]
        for index, item in enumerate(self.items(record, "enums")):
            enums.append(self.enum(item, f"{path}/enums/{index}", name))
        for index, item in enumerate(self.items(record, "consts")):
            constants.append(self.constant(item, f"{path}/consts/{index}", name))
        callback_id = record.get("callback_id")
        return ir.Struct(
            name,
            tuple(filter(None, fields)),
            f"{SOURCE}#{path}",
            callback_id=callback_id if isinstance(callback_id, int) else None,
            methods=tuple(filter(None, methods)),
        )

    def field(self, node: object, path: str) -> ir.Field | None:
        required: Schema = {"fieldname": str, "fieldtype": str}
        record = self.record(node, path, required, {"private": bool})
        if record is None:
            return None
        return ir.Field(
            str(record["fieldname"]),
            self.ctype(str(record["fieldtype"]), f"{path}/fieldtype"),
            private=record.get("private") is True,
        )

    def enum(self, node: object, path: str, scope: str | None) -> ir.Enum | None:
        record = self.record(node, path, {"enumname": str, "values": list}, {"fqname": str})
        if record is None:
            return None
        name = str(record["enumname"])
        qualified = str(record.get("fqname", name))
        if scope is not None and "fqname" not in record:
            message = f"enum {name} nested in {scope} has no fqname; its unqualified name is kept"
            self.issue("warning", "nested-enum-without-fqname", message, path)
        elif not (qualified == name or qualified.endswith(f"::{name}")):
            self.issue("warning", "inconsistent-fqname", f"{qualified} does not name {name}", path)
        values = []
        for index, item in enumerate(self.items(record, "values")):
            value = self.record(item, f"{path}/values/{index}", {"name": str, "value": str}, {})
            if value is None:
                continue
            spelling = str(value["value"])
            number = integer_literal(spelling)
            if number is None:
                message = f"{value['name']} = {spelling!r} is not an integer literal"
                self.issue("warning", "unevaluated-enum-value", message, f"{path}/values/{index}")
            values.append(ir.EnumValue(str(value["name"]), spelling, number))
        return ir.Enum(name, qualified, tuple(values), f"{SOURCE}#{path}", scope=scope)

    def constant(self, node: object, path: str, scope: str | None) -> ir.Constant | None:
        required: Schema = {"constname": str, "consttype": str, "constval": str}
        record = self.record(node, path, required, {})
        if record is None:
            return None
        expression = str(record["constval"])
        value = integer_literal(expression)
        if value is None:
            message = f"{record['constname']} = {expression!r} is kept as an expression"
            self.issue("note", "unevaluated-constant", message, f"{path}/constval")
        return ir.Constant(
            str(record["constname"]),
            self.ctype(str(record["consttype"]), f"{path}/consttype"),
            expression,
            f"{SOURCE}#{path}",
            value=value,
            scope=scope,
        )

    def typedef(self, node: object, path: str) -> ir.Typedef | None:
        record = self.record(node, path, {"typedef": str, "type": str}, {})
        if record is None:
            return None
        return ir.Typedef(
            str(record["typedef"]),
            self.ctype(str(record["type"]), f"{path}/type"),
            f"{SOURCE}#{path}",
        )

    def check(self, found: Declarations) -> None:
        """Report duplicate declarations, shared callback IDs and dangling callback names."""

        def report(severity: ir.Severity, code: str, message: str, sources: list[str]) -> None:
            self.issues.append(ir.Issue(severity, code, message, sources[0]))

        # One C++ scope: interfaces, structs, top-level enums and typedefs share a namespace.
        types: dict[str, list[str]] = defaultdict(list)
        for kind, name, source in (
            *(("interface", item.name, item.source) for item in found.interfaces),
            *(("struct", item.name, item.source) for item in found.structs),
            *(("enum", item.qualified_name, item.source) for item in found.enums),
            *(("typedef", item.name, item.source) for item in found.typedefs),
        ):
            types[name].append(f"{kind} at {source}")
        for name, places in sorted(types.items()):
            if len(places) > 1:
                sources = [place.partition(" at ")[2] for place in places]
                report("error", "duplicate-declaration", f"{name}: {'; '.join(places)}", sources)

        owners: tuple[ir.Interface | ir.Struct, ...] = (*found.interfaces, *found.structs)
        functions = [method for owner in owners for method in owner.methods]
        flat: dict[str, list[str]] = defaultdict(list)
        accessors = [accessor for i in found.interfaces for accessor in i.accessors]
        for flat_name, source in (
            *((f.flat_name, f.source) for f in functions),
            *((a.flat_name, a.source) for a in accessors),
        ):
            flat[flat_name].append(source)
        constants: dict[str, list[str]] = defaultdict(list)
        for constant in found.constants:
            label = f"{constant.scope}::{constant.name}" if constant.scope else constant.name
            constants[label].append(constant.source)
        versions: dict[str, list[str]] = defaultdict(list)
        for interface in found.interfaces:
            if interface.version is not None:
                versions[interface.version].append(interface.source)
        for code, table in (
            ("duplicate-flat-function", flat),
            ("duplicate-declaration", constants),
            ("duplicate-version", versions),
        ):
            for name, sources in sorted(table.items()):
                if len(sources) > 1:
                    report("error", code, f"{name} is declared {len(sources)} times", sources)

        callback_ids: dict[int, list[ir.Struct]] = defaultdict(list)
        for struct in found.structs:
            if struct.callback_id is not None:
                callback_ids[struct.callback_id].append(struct)
        for callback_id, structs in sorted(callback_ids.items()):
            if len(structs) > 1:
                names = ", ".join(struct.name for struct in structs)
                message = f"callback ID {callback_id} is shared by {names}"
                report("warning", "shared-callback-id", message, [s.source for s in structs])

        callbacks = {s.name for structs in callback_ids.values() for s in structs}
        for function in functions:
            for kind, target in (
                ("callresult", function.callresult),
                ("callback", function.callback),
            ):
                if target is not None and target not in callbacks:
                    message = f"{function.flat_name}: {kind} {target} is not a callback struct"
                    report("error", "unknown-callback-struct", message, [function.source])


def _is(value: object, kind: type) -> bool:
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is list:
        return isinstance(value, list) and not isinstance(value, _Pairs)
    return isinstance(value, kind)
