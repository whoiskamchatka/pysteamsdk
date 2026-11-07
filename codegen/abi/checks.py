"""Check layouts against themselves and against the declarations they lay out.

Errors mean a layout contradicts itself, its target or its declaration, or a reviewed by_value
override contradicts the layouts. Warnings flag what a binding generator must not take on
trust: a declaration without a complete layout, a field list in steam_api.json that the headers
contradict, records passed by value whose unions, bit-fields or packing ctypes may not pass
correctly, and records passed by value that no override classifies. Notes list enum values a
target stores wrapped.
"""

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal

from codegen import ir
from codegen.abi import FUNDAMENTAL, TARGETS

CallStatus = Literal["direct", "shim", "unverified"]

_KEYWORDS = ("struct", "union", "class")
_NAME = re.compile(r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*")
_TYPE = re.compile(r"(const )?([^*\[]+?)(\**)((?:\[\d+\])*)")
_SIGNED = frozenset({"signed char", "short", "int", "long", "long long"})
_UNSIGNED = frozenset(f"unsigned {name}" for name in ("char", "short", "int", "long", "long long"))
# char, wchar_t and the like are integers whose signedness the layouts do not record.
_INTEGERS = _SIGNED | _UNSIGNED | {"bool", "char", "wchar_t", "char16_t", "char32_t"}


@dataclass(frozen=True)
class LayoutType:
    """A canonical layout type taken apart; ``count`` multiplies the array dimensions."""

    base: str
    pointers: int
    count: int

    def by_value(self) -> tuple[str, str] | None:
        """The keyword and name of a record or enum stored by value, as in "struct X[2]"."""
        keyword, _, name = self.base.partition(" ")
        if self.pointers or not name or keyword not in ("enum", *_KEYWORDS):
            return None
        return keyword, name


def parse(text: str) -> LayoutType | None:
    """Take apart a type spelled as ir.FieldLayout describes, or give None."""
    match = _TYPE.fullmatch(text)
    if match is None:
        return None
    const, base, stars, dimensions = match.groups()
    keyword, _, name = base.partition(" ")
    if base in FUNDAMENTAL:
        pass
    elif base in ("void", "function"):
        if not stars or (const and base == "function"):
            return None
    elif keyword == "enum" or (keyword in _KEYWORDS and name):
        if not _NAME.fullmatch(name):
            return None
    elif base not in _KEYWORDS or stars:
        return None
    if const and not stars:
        return None
    count = math.prod(int(number) for number in re.findall(r"\d+", dimensions))
    return LayoutType(base, len(stars), count)


def check(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    yield from _reviews(metadata)
    abi = metadata.abi
    if abi is None:
        return
    yield from _targets(abi)
    for target in abi.targets:
        yield from _consistency(target)
        yield from _sizes(target)
    yield from _coverage(metadata, abi)
    yield from _field_lists(metadata, abi)
    yield from _by_value(metadata, abi)
    yield from _wrapped_enums(metadata, abi)


def _error(code: str, target: str, message: str, source: str) -> ir.Issue:
    return ir.Issue("error", code, f"{target}: {message}", source or "abi")


def _on(targets: Iterable[str], abi: ir.Abi) -> str:
    found = list(targets)
    if len(found) == len(abi.targets) > 1:
        return "on every target"
    return "on " + ", ".join(found)


def _power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def _targets(abi: ir.Abi) -> Iterator[ir.Issue]:
    if not abi.clang.strip():
        yield ir.Issue("error", "invalid-layout", "no libclang version is recorded", "abi")
    for name, count in sorted(Counter(item.target for item in abi.targets).items()):
        if count > 1:
            yield _error("duplicate-layout", name, f"laid out {count} times", "abi")
    for item in abi.targets:
        known = TARGETS.get(item.target)
        if known is None:
            yield _error("unknown-target", item.target, "not a target codegen.abi knows", "abi")
        elif known.triple != item.triple:
            message = f"compiled for {item.triple}, not {known.triple}"
            yield _error("unknown-target", item.target, message, "abi")


def _consistency(target: ir.TargetLayout) -> Iterator[ir.Issue]:
    name = target.target
    for kind, names in (
        ("record", [item.name for item in target.records]),
        ("enum", [item.name for item in target.enums]),
        ("typedef", [item.name for item in target.typedefs]),
    ):
        for repeated, count in sorted(Counter(names).items()):
            if count > 1:
                message = f"{kind} {repeated} is laid out {count} times"
                yield _error("duplicate-layout", name, message, "abi")
    records = {item.name: item for item in target.records}
    enums = {item.name for item in target.enums}
    for record in target.records:
        problems = _record_problems(record, records, enums)
        if not record.name:
            problems.insert(0, "a record laid out on its own needs a name")
        for problem in problems:
            yield _error("invalid-layout", name, f"{record.name}: {problem}", record.source)
    for enum in target.enums:
        if enum.underlying not in _INTEGERS or enum.size not in (1, 2, 4, 8):
            message = f"enum {enum.name} cannot be stored as a {enum.size}-byte {enum.underlying}"
            yield _error("invalid-layout", name, message, "abi")
    for typedef in target.typedefs:
        problems = _type_problems(typedef.type, records, enums)
        if (typedef.size is None) != (typedef.align is None):
            problems.append("needs both a size and an alignment, or neither")
        elif typedef.align is not None and not _power_of_two(typedef.align):
            problems.append(f"alignment {typedef.align} is not a power of two")
        elif typedef.size is not None and typedef.align and typedef.size % typedef.align:
            problems.append(f"size {typedef.size} is not a multiple of the alignment")
        for problem in problems:
            yield _error("invalid-layout", name, f"typedef {typedef.name}: {problem}", "abi")


def _type_problems(text: str, records: dict[str, ir.RecordLayout], enums: set[str]) -> list[str]:
    parsed = parse(text)
    if parsed is None:
        return [f"{text!r} is not a canonical layout type"]
    reference = parsed.by_value()
    if reference is None:
        return []
    keyword, name = reference
    if keyword == "enum":
        return [] if name in enums else [f"enum {name} has no layout"]
    record = records.get(name)
    if record is None or record.status == "opaque":
        return [f"{name} is used by value but has no layout"]
    if record.kind != keyword:
        return [f"{name} is a {record.kind}, not a {keyword}"]
    return []


def _record_problems(
    record: ir.RecordLayout, records: dict[str, ir.RecordLayout], enums: set[str]
) -> list[str]:
    if record.status == "opaque":
        if record.size is not None or record.align is not None or record.fields or record.reason:
            return ["an opaque record has no size, alignment, members or reason"]
        return []
    problems = []
    if record.status == "unsupported":
        if not (record.reason or "").strip():
            problems.append("an unsupported record must give a reason")
        if record.fields:
            problems.append("an unsupported record lists no members")
        if (record.size is None) != (record.align is None):
            problems.append("needs both a size and an alignment, or neither")
    elif record.size is None or record.align is None:
        return ["a complete record needs a size and an alignment"]
    elif record.reason is not None:
        problems.append("a complete record gives no reason")
    if record.size is None or record.align is None:
        return problems
    if not _power_of_two(record.align):
        return [*problems, f"alignment {record.align} is not a power of two"]
    if record.size <= 0:
        problems.append(f"size {record.size} is impossible: every C++ object takes a byte")
    elif record.size % record.align:
        problems.append(f"size {record.size} is not a multiple of the alignment {record.align}")
    end = 0
    for item in record.fields:
        label = item.name or "an unnamed member"
        found = _field_problems(item, record.size, records, enums)
        problems += [f"{label}: {problem}" for problem in found]
        if found:
            continue
        if item.bit_offset is not None and item.bit_width is not None:
            start, stop = item.bit_offset, item.bit_offset + item.bit_width
        else:
            assert item.offset is not None
            start, stop = item.offset * 8, (item.offset + item.size) * 8
        if record.kind == "union" and start:
            problems.append(f"{label}: a union member starts at the union's beginning")
        elif record.kind != "union" and start < end:
            problems.append(f"{label}: overlaps the member before it")
        end = max(end, stop)
    return problems


def _field_problems(
    item: ir.FieldLayout, size: int, records: dict[str, ir.RecordLayout], enums: set[str]
) -> list[str]:
    problems = _type_problems(item.type, records, enums)
    parsed = parse(item.type)
    if parsed is None:
        return problems
    if item.size < 0 or not _power_of_two(item.align):
        problems.append(f"size {item.size} or alignment {item.align} is impossible")
    if item.bit_width is not None:
        if item.offset is not None or item.bit_offset is None:
            problems.append("a bit-field has a bit offset instead of an offset")
        elif not 0 <= item.bit_width <= item.size * 8:
            problems.append(f"{item.bit_width} bits do not fit a {item.size}-byte type")
        elif item.bit_width == 0 and item.name:
            problems.append("a zero-width bit-field has no name")
        elif item.bit_offset < 0 or item.bit_offset + item.bit_width > size * 8:
            problems.append("lies outside the record")
        integral = parsed.base in _INTEGERS or parsed.base.startswith("enum ")
        if not integral or parsed.pointers or parsed.count != 1:
            problems.append(f"a bit-field cannot be {item.type}")
    elif item.offset is None or item.bit_offset is not None:
        problems.append("needs a byte offset and no bit offset")
    elif item.offset < 0 or item.offset + item.size > size:
        problems.append(f"bytes {item.offset} to {item.offset + item.size} lie outside the record")
    if (parsed.base in _KEYWORDS) != (item.record is not None):
        problems.append("an anonymous record type needs its layout described in place")
    elif item.record is not None:
        inner = item.record
        if inner.name or inner.kind != parsed.base or inner.status != "complete":
            problems.append(f"the record described in place must be an unnamed {parsed.base}")
        elif inner.size is None or (inner.size * parsed.count, inner.align) != (
            item.size,
            item.align,
        ):
            problems.append("its size or alignment differs from the record described in place")
        problems += _record_problems(inner, records, enums)
    elif not item.name and item.bit_width is None:
        problems.append("only an anonymous record or a bit-field can have no name")
    return problems


def _sizes(target: ir.TargetLayout) -> Iterator[ir.Issue]:
    # Every use of a type in one target has that type's size and alignment: a record or enum's
    # own, and for anything else the size and alignment its first use gives.
    records = {item.name: item for item in target.records}
    enums = {item.name: item.size for item in target.enums}
    uses: list[tuple[str, str, int, int]] = [
        (f"typedef {t.name}", t.type, t.size, t.align)
        for t in target.typedefs
        if t.size is not None and t.align is not None
    ]
    uses += [
        (label, item.type, item.size, item.align)
        for record in target.records
        for label, item in _members(record.name, record.fields)
    ]
    first: dict[str, tuple[int, int, str]] = {}
    for where, text, size, align in uses:
        parsed = parse(text)
        if parsed is None or parsed.base in _KEYWORDS or not parsed.count:
            continue
        if size % parsed.count:
            message = f"{where}: {size} bytes cannot hold {parsed.count} equal elements"
            yield _error("invalid-layout", target.target, message, "abi")
            continue
        element = size // parsed.count
        reference = parsed.by_value()
        if reference is None:
            key = "a pointer" if parsed.pointers else parsed.base
            known = first.setdefault(key, (element, align, where))
            if known[:2] != (element, align):
                message = (
                    f"{where}: {key} is {element} bytes aligned to {align} here but "
                    f"{known[0]} bytes aligned to {known[1]} in {known[2]}"
                )
                yield _error("invalid-layout", target.target, message, "abi")
            continue
        keyword, name = reference
        record = records.get(name)
        if keyword == "enum":
            expected: tuple[int | None, int | None] = (enums.get(name), None)
        else:
            expected = (record.size, record.align) if record else (None, None)
        if expected[0] is not None and expected[0] != element:
            message = f"{where}: {keyword} {name} is {expected[0]} bytes, not {element}"
            yield _error("invalid-layout", target.target, message, "abi")
        elif expected[1] is not None and expected[1] != align:
            message = f"{where}: {keyword} {name} is aligned to {expected[1]}, not {align}"
            yield _error("invalid-layout", target.target, message, "abi")


def _members(owner: str, fields: Iterable[ir.FieldLayout]) -> Iterator[tuple[str, ir.FieldLayout]]:
    for item in fields:
        label = f"{owner}.{item.name or '(anonymous)'}"
        yield label, item
        if item.record is not None:
            yield from _members(label, item.record.fields)


def _coverage(metadata: ir.Metadata, abi: ir.Abi) -> Iterator[ir.Issue]:
    # An override that the layouts contradict is an error; any other gap is a warning.
    found: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    external = {item.name for item in metadata.external_types}
    for target in abi.targets:
        records = {item.name: item for item in target.records}
        enums = {item.name for item in target.enums}
        typedefs = {item.name for item in target.typedefs}
        problems = [
            # steam_api.json calls classes such as servernetadr_t structs too.
            *(
                (s.name, _record_coverage(records.get(s.name), ""), s.source)
                for s in metadata.structs
            ),
            *(
                (e.name, _external_coverage(e, records, typedefs), e.source)
                for e in metadata.external_types
            ),
            *(
                (e.qualified_name, None if e.qualified_name in enums else "has no layout", e.source)
                for e in metadata.enums
            ),
            *(
                (t.name, None if t.name in typedefs else "has no layout", t.source)
                for t in metadata.typedefs
            ),
        ]
        for name, problem, source in problems:
            if problem:
                found[(name, problem, source)].append(target.target)
    for (name, problem, source), targets in found.items():
        severity: ir.Severity = "error" if name in external else "warning"
        code = "external-type-mismatch" if name in external else "missing-layout"
        yield ir.Issue(severity, code, f"{name} {problem} {_on(targets, abi)}", source)


def _external_coverage(
    external: ir.ExternalType, records: dict[str, ir.RecordLayout], typedefs: set[str]
) -> str | None:
    if external.declaration == "typedef":
        return None if external.name in typedefs else "has no layout"
    return _record_coverage(records.get(external.name), external.declaration)


def _record_coverage(record: ir.RecordLayout | None, declared: str) -> str | None:
    if record is None:
        return "has no layout"
    if declared == "incomplete":
        return None if record.status == "opaque" else f"is {record.status}, not opaque"
    if record.status != "complete":
        return f"is {record.status}" + (f": {record.reason}" if record.reason else "")
    if declared in _KEYWORDS and record.kind != declared:
        return f"is laid out as a {record.kind}"
    return None


def _field_lists(metadata: ir.Metadata, abi: ir.Abi) -> Iterator[ir.Issue]:
    records = [{item.name: item for item in target.records} for target in abi.targets]
    for struct in metadata.structs:
        listed = tuple(item.name for item in struct.fields)
        layouts: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for index, target in enumerate(abi.targets):
            record = records[index].get(struct.name)
            if record is None or record.status != "complete":
                continue
            names = tuple(
                item.name or f"(anonymous {item.type})"
                for item in record.fields
                if item.name or item.record is not None
            )
            if names != listed:
                layouts[names].append(target.target)
        for names, targets in layouts.items():
            message = (
                f"steam_api.json lists {struct.name} as {', '.join(listed) or 'empty'}, but "
                f"the headers lay it out as {', '.join(names) or 'empty'} {_on(targets, abi)}"
            )
            yield ir.Issue("warning", "json-fields-differ", message, struct.source)


def by_value_records(metadata: ir.Metadata) -> Iterator[tuple[ir.Function, str, str]]:
    """Each flat function, "takes" or "returns", and a record it passes that way by value."""
    typedefs = {item.name: item.type for item in metadata.typedefs}
    named = {item.name for item in metadata.structs} | {
        item.name for item in metadata.external_types if item.declaration in _KEYWORDS
    }
    for function in metadata.functions():
        uses = [("returns", function.flat_returns or function.returns)]
        uses += [("takes", param.flat_type or param.type) for param in function.params]
        for verb, ctype in uses:
            name = _record_by_value(ctype, typedefs, named)
            if name is not None:
                yield function, verb, name


def hazards(record: ir.RecordLayout, records: dict[str, ir.RecordLayout]) -> set[str]:
    """What in a record's layout, or the layouts it holds, ctypes may not pass by value."""
    return _hazards(record, records, {record.name})


def by_value_status(metadata: ir.Metadata, record: str, target: str) -> CallStatus:
    """How a binding may pass ``record`` by value on ``target``.

    Only a reviewed by_value override makes a call direct. Anything it does not list is
    unverified, so no SDK release or new target can make a by-value call direct by itself.
    """
    for entry in metadata.by_value:
        if entry.record == record:
            if target in entry.direct:
                return "direct"
            if target in entry.shim:
                return "shim"
    return "unverified"


def misaligned(record: ir.RecordLayout) -> bool:
    """Whether packing has put a member at an offset its type's alignment does not divide."""
    return _misaligned(record.fields, 0)


def _misaligned(fields: Iterable[ir.FieldLayout], base: int) -> bool:
    for item in fields:
        if item.offset is None:
            continue
        if (base + item.offset) % item.align:
            return True
        if item.record is not None and _misaligned(item.record.fields, base + item.offset):
            return True
    return False


def sysv_in_memory(target: str, record: ir.RecordLayout) -> bool:
    """Whether x86-64 SysV passes ``record`` in memory where ctypes would use registers.

    The ABI passes a record of up to 16 bytes with a misaligned member in memory, but libffi
    classifies it by its members' types alone and uses registers.
    """
    small = record.size is not None and record.size <= 16
    return target in ("linux-x86_64", "macos-x86_64") and small and misaligned(record)


def _reviews(metadata: ir.Metadata) -> Iterator[ir.Issue]:
    uses: dict[str, str] = {}
    for function, _, name in by_value_records(metadata):
        uses.setdefault(name, function.source)
    reviewed = Counter(entry.record for entry in metadata.by_value)
    layouts = {
        target.target: {item.name: item for item in target.records}
        for target in (metadata.abi.targets if metadata.abi else ())
    }
    for entry in metadata.by_value:
        problems = []
        unknown = sorted(set(entry.direct + entry.shim) - TARGETS.keys())
        if unknown:
            problems.append(f"{', '.join(unknown)} are not targets codegen.abi knows")
        if both := sorted(set(entry.direct) & set(entry.shim)):
            problems.append(f"{', '.join(both)} cannot be both direct and shim")
        if entry.record not in uses:
            problems.append("no flat function takes or returns it by value")
        if reviewed[entry.record] > 1:
            problems.append(f"it is reviewed {reviewed[entry.record]} times")
        # Layouts not computed for a target cannot contradict the review there.
        for target in (item for item in entry.direct if item in layouts):
            record = layouts[target].get(entry.record)
            if record is None or record.status != "complete":
                problems.append(f"it is direct on {target}, where it has no complete layout")
            elif sysv_in_memory(target, record):
                problems.append(
                    f"it is direct on {target}, but a misaligned member makes SysV pass it in "
                    "memory, where ctypes uses registers"
                )
        for problem in problems:
            message = f"by_value {entry.record}: {problem}"
            yield ir.Issue("error", "invalid-by-value", message, entry.source)
    for name, source in sorted(uses.items()):
        if name not in reviewed:
            message = (
                f"{name} is passed by value, but no by_value override says how ctypes may pass "
                "it, so it is unverified on every target"
            )
            yield ir.Issue("warning", "unreviewed-by-value", message, source)


def _by_value(metadata: ir.Metadata, abi: ir.Abi) -> Iterator[ir.Issue]:
    records = [{item.name: item for item in target.records} for target in abi.targets]
    for function, verb, name in by_value_records(metadata):
        found: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for index, target in enumerate(abi.targets):
            record = records[index].get(name)
            if record is None or record.status != "complete":
                found[()].append(target.target)
            elif present := hazards(record, records[index]):
                found[tuple(sorted(present))].append(target.target)
        for kinds, targets in found.items():
            where = _on(targets, abi)
            detail = (
                f"whose layout has {' and '.join(kinds)} {where}"
                if kinds
                else f"which has no complete layout {where}"
            )
            message = (
                f"{function.flat_name} {verb} {name} by value, {detail}; ctypes may not pass "
                "such a record correctly, so check it before binding the function"
            )
            yield ir.Issue("warning", "by-value-record", message, function.source)


def _record_by_value(ctype: ir.CType, typedefs: dict[str, ir.CType], named: set[str]) -> str | None:
    if ctype.kind != "named" or ctype.pointers or ctype.reference or ctype.array is not None:
        return None
    name, seen = ctype.base, set()
    while name in typedefs and name not in seen:
        target = typedefs[name]
        if target.kind != "named" or target.pointers or target.reference or target.array:
            return None
        seen.add(name)
        name = target.base
    return name if name in named else None


def _hazards(
    record: ir.RecordLayout, records: dict[str, ir.RecordLayout], seen: set[str]
) -> set[str]:
    found = set()
    if record.kind == "union":
        found.add("a union")
    natural = max((item.align for item in record.fields), default=1)
    if record.align is not None and record.align < natural:
        found.add("packing")
    for item in record.fields:
        if item.bit_width is not None:
            found.add("bit-fields")
        if item.record is not None:
            found |= _hazards(item.record, records, seen)
            continue
        parsed = parse(item.type)
        reference = parsed.by_value() if parsed else None
        if reference and reference[0] != "enum" and reference[1] not in seen:
            inner = records.get(reference[1])
            if inner is not None and inner.status == "complete":
                found |= _hazards(inner, records, seen | {reference[1]})
    return found


def _wrapped_enums(metadata: ir.Metadata, abi: ir.Abi) -> Iterator[ir.Issue]:
    layouts = [{item.name: item for item in target.enums} for target in abi.targets]
    for enum in metadata.enums:
        wrapped: dict[tuple[str, int, str, int], list[str]] = defaultdict(list)
        for index, target in enumerate(abi.targets):
            layout = layouts[index].get(enum.qualified_name)
            limits = _limits(layout) if layout else None
            if layout is None or limits is None:
                continue
            low, high = limits
            for value in enum.values:
                if value.value is None or low <= value.value <= high:
                    continue
                stored = value.value % (1 << layout.size * 8)
                if stored > high:
                    stored -= 1 << layout.size * 8
                key = (value.name, value.value, layout.underlying, stored)
                wrapped[key].append(target.target)
        for (name, number, underlying, stored), targets in wrapped.items():
            message = (
                f"{enum.qualified_name}::{name} is {number}, which {underlying} stores as "
                f"{stored} {_on(targets, abi)}"
            )
            yield ir.Issue("note", "wrapped-enum-value", message, enum.source)


def _limits(layout: ir.EnumLayout) -> tuple[int, int] | None:
    bits = layout.size * 8
    if layout.underlying in _SIGNED:
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if layout.underlying in _UNSIGNED:
        return 0, (1 << bits) - 1
    return None
