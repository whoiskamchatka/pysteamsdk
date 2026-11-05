"""Check that metadata agrees with itself, after extraction and whenever it is read back.

Errors mean that declarations conflict or refer to the wrong kind of declaration. A type name
that nothing declares is only a warning: steam_api.json leaves out several legitimate SDK types,
and codegen/overrides.toml locates the known ones as external types.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator

from codegen import ir
from codegen.cdecl import TypeSyntaxError, parse_type
from codegen.cexpr import evaluate_constants

# C and C++ fundamental types, spelled as cdecl joins them.
FUNDAMENTAL = frozenset(
    {
        "void",
        "bool",
        "char",
        "signed char",
        "unsigned char",
        "short",
        "unsigned short",
        "int",
        "unsigned int",
        "long",
        "unsigned long",
        "long long",
        "unsigned long long",
        "float",
        "double",
    }
)
# Typedefs from the C standard headers the SDK includes; several have platform-defined widths.
STANDARD = frozenset(
    {
        "size_t",
        "intptr_t",
        "uintptr_t",
        "int8_t",
        "uint8_t",
        "int16_t",
        "uint16_t",
        "int32_t",
        "uint32_t",
        "int64_t",
        "uint64_t",
    }
)


def validate(metadata: ir.Metadata) -> list[ir.Issue]:
    return [
        *_duplicates(metadata),
        *_callbacks(metadata),
        *_external_types(metadata),
        *_unresolved_types(metadata),
        *_typedef_cycles(metadata),
        *_type_spellings(metadata),
        *_constant_values(metadata),
    ]


def type_uses(metadata: ir.Metadata) -> Iterator[tuple[ir.CType, str]]:
    for function in metadata.functions():
        yield function.returns, function.source
        if function.flat_returns:
            yield function.flat_returns, function.source
        for param in function.params:
            yield param.type, function.source
            if param.flat_type:
                yield param.flat_type, function.source
    for struct in metadata.structs:
        for item in struct.fields:
            yield item.type, struct.source
    for constant in metadata.constants:
        yield constant.type, constant.source
    for typedef in metadata.typedefs:
        yield typedef.type, typedef.source


def _named_parts(ctype: ir.CType) -> Iterator[ir.CType]:
    if ctype.kind == "named":
        yield ctype
    elif ctype.kind == "function_pointer":
        for part in (ctype.returns, *ctype.params):
            if part is not None:
                yield from _named_parts(part)


def _repeated(code: str, entries: Iterable[tuple[str, str]]) -> Iterator[ir.Issue]:
    """An error for each name given more than once among ``(name, source)`` pairs."""
    sources: dict[str, list[str]] = defaultdict(list)
    for name, source in entries:
        sources[name].append(source)
    for name, found in sorted(sources.items()):
        if len(found) > 1:
            message = f"{name} is declared {len(found)} times: {', '.join(found)}"
            yield ir.Issue("error", code, message, found[0])


def _duplicates(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    # Interfaces, structs, enums, typedefs and external types share one C++ namespace.
    yield from _repeated(
        "duplicate-declaration",
        [
            *((item.name, item.source) for item in metadata.interfaces),
            *((item.name, item.source) for item in metadata.structs),
            *((item.qualified_name, item.source) for item in metadata.enums),
            *((item.name, item.source) for item in metadata.typedefs),
            *((item.name, item.source) for item in metadata.external_types),
        ],
    )
    # So do constants and the enumerators of unscoped enums declared in the same scope.
    yield from _repeated(
        "duplicate-declaration",
        [
            *((_scoped(item.scope, item.name), item.source) for item in metadata.constants),
            *(
                (_scoped(enum.scope, value.name), enum.source)
                for enum in metadata.enums
                for value in enum.values
            ),
        ],
    )
    for struct in metadata.structs:
        yield from _repeated("duplicate-member", ((f.name, struct.source) for f in struct.fields))
    for function in metadata.functions():
        params = ((p.name, function.source) for p in function.params)
        yield from _repeated("duplicate-member", params)
    accessors = [accessor for item in metadata.interfaces for accessor in item.accessors]
    yield from _repeated(
        "duplicate-flat-function",
        [
            *((f.flat_name, f.source) for f in metadata.functions()),
            *((a.flat_name, a.source) for a in accessors),
        ],
    )
    yield from _repeated(
        "duplicate-version",
        ((i.version, i.source) for i in metadata.interfaces if i.version is not None),
    )


def _scoped(scope: str | None, name: str) -> str:
    return f"{scope}::{name}" if scope else name


def _callbacks(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    structs = {item.name: item for item in metadata.structs}
    by_id: dict[int, list[ir.Struct]] = defaultdict(list)
    for struct in metadata.structs:
        if struct.callback_id is not None:
            by_id[struct.callback_id].append(struct)
    reviewed: dict[int, list[ir.SharedCallback]] = defaultdict(list)
    for shared in metadata.shared_callbacks:
        reviewed[shared.callback_id].append(shared)

    for callback_id, owners in sorted(by_id.items()):
        if len(owners) > 1 and callback_id not in reviewed:
            names = ", ".join(sorted(struct.name for struct in owners))
            message = f"callback ID {callback_id} is shared by {names}"
            yield ir.Issue("warning", "shared-callback-id", message, owners[0].source)
    for callback_id, entries in sorted(reviewed.items()):
        sharing = sorted(struct.name for struct in by_id.get(callback_id, []))
        for shared in entries:
            named = sorted(set(shared.structs))
            fields = {
                tuple((f.name, f.type.shape(), f.private) for f in structs[name].fields)
                for name in named
                if name in structs
            }
            if len(entries) > 1:
                problem = f"callback ID {callback_id} is reviewed {len(entries)} times"
            elif len(named) < 2 or len(named) != len(shared.structs):
                problem = "a shared callback must name at least two different structs"
            elif named != sharing:
                found = ", ".join(sharing) or "no struct"
                problem = f"callback ID {callback_id} belongs to {found}, not {', '.join(named)}"
            elif len(fields) > 1:
                problem = f"{', '.join(named)} share callback ID {callback_id} but differ in fields"
            else:
                continue
            yield ir.Issue("error", "shared-callback-mismatch", problem, shared.source)

    callbacks = {struct.name for owners in by_id.values() for struct in owners}
    for function in metadata.functions():
        for kind, target in (("callresult", function.callresult), ("callback", function.callback)):
            if target is not None and target not in callbacks:
                message = f"{function.flat_name}: {kind} {target} is not a callback struct"
                yield ir.Issue("error", "unknown-callback-struct", message, function.source)


def _external_types(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    uses: dict[str, list[ir.CType]] = defaultdict(list)
    for ctype, _ in type_uses(metadata):
        for part in _named_parts(ctype):
            uses[part.base].append(part)
    for external in metadata.external_types:
        found = uses.get(external.name, [])
        if not found:
            message = f"nothing uses {external.name}"
            yield ir.Issue("error", "unused-external-type", message, external.source)
        elif external.declaration == "incomplete" and any(
            part.pointers == 0 and not part.reference for part in found
        ):
            message = f"{external.name} is incomplete but used by value"
            yield ir.Issue("error", "incomplete-type-by-value", message, external.source)


def _unresolved_types(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    known = set(FUNDAMENTAL | STANDARD)
    known.update(item.name for item in metadata.interfaces)
    known.update(item.name for item in metadata.structs)
    known.update(item.qualified_name for item in metadata.enums)
    known.update(item.name for item in metadata.typedefs)
    known.update(item.name for item in metadata.external_types)
    unresolved: dict[str, list[str]] = defaultdict(list)
    for ctype, source in type_uses(metadata):
        for name in ctype.referenced_names():
            if name not in known:
                unresolved[name].append(source)
    for name, sources in sorted(unresolved.items()):
        uses = f"{len(sources)} time{'s' if len(sources) != 1 else ''}"
        message = f"{name} is used {uses} but defined nowhere in the metadata"
        yield ir.Issue("warning", "unresolved-type", message, sources[0])


def _typedef_cycles(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    targets = {item.name: item.type.base for item in metadata.typedefs if item.type.kind == "named"}
    for typedef in metadata.typedefs:
        chain = [typedef.name]
        while chain[-1] in targets and targets[chain[-1]] not in chain:
            chain.append(targets[chain[-1]])
        if targets.get(chain[-1]) == typedef.name:
            message = f"typedef {' -> '.join([*chain, typedef.name])} never reaches a type"
            yield ir.Issue("error", "typedef-cycle", message, typedef.source)


def _type_spellings(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    """Every parsed type must be what its spelling parses to, so either can be trusted."""
    for ctype, source in type_uses(metadata):
        if ctype.kind == "unparsed":
            continue
        try:
            matches = parse_type(ctype.spelling) == ctype
        except TypeSyntaxError:
            matches = False
        if not matches:
            message = f"{ctype.spelling!r} does not match its recorded parts"
            yield ir.Issue("error", "inconsistent-type", message, source)


def _constant_values(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    evaluated, _ = evaluate_constants(metadata.constants, metadata.typedefs)
    for stored, fresh in zip(metadata.constants, evaluated, strict=True):
        # 600 and 600.0 differ here: the type of the value says whether the constant is a float.
        if type(stored.value) is not type(fresh.value) or stored.value != fresh.value:
            label = _scoped(stored.scope, stored.name)
            message = (
                f"{label} is {stored.value!r}, but {stored.expression!r} gives {fresh.value!r}"
            )
            yield ir.Issue("error", "constant-value-mismatch", message, stored.source)
