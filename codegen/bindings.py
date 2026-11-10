"""Generate a ctypes declarations module for one target from metadata a compiler has checked.

The module holds the records, enums, typedefs and constants the metadata declares and the
signature of every flat function ctypes can call safely, for pysteamsdk._native.bindings to
bind. Sizes, offsets, packing and integer widths come from the target's layouts, and only from
those a native compiler verified (codegen.abi.probe). A declaration that depends on anything
else is skipped with its reason, never guessed, and so is any function ctypes cannot call
safely: one that takes a callback or an object the caller implements, one that passes a record
by value without a reviewed by_value override making it direct on the target, and one that
needs a type whose size is unknown. The same metadata and checks always give the same text.
"""

import contextlib
import json
import keyword
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from codegen import ir
from codegen.abi import TARGETS, probe
from codegen.abi.checks import by_value_status
from codegen.validate import FUNDAMENTAL, validate
from pysteamsdk._native.bindings import PROTOCOL

Kind = Literal["function", "accessor", "export", "record", "enum", "value", "typedef", "constant"]
KINDS: tuple[Kind, ...] = (
    "function",
    "accessor",
    "export",
    "record",
    "enum",
    "value",
    "typedef",
    "constant",
)
# A compiler status that makes a layout usable. A partly verified record had every member
# checked; what it lacks is listed as a note.
USABLE = frozenset({"verified", "derived", "partly verified"})
# Why a declaration is skipped, by the category each Skip names.
CATEGORIES = {
    "callback": "takes a C function pointer that no callback review says Steam calls back on "
    "a thread making a call into Steam, or of a form a Python function cannot take",
    "callback-object": "involves a C++ interface the caller implements for Steam to call back, "
    "which needs a native shim",
    "by-value-shim": "passes a record by value where ctypes was shown to differ from the compiler",
    "by-value-unverified": "passes a record by value that no by_value review makes direct here",
    "opaque": "uses a type the SDK never defines, whose size is unknown",
    "unverified-layout": "uses a layout the compiler did not verify on this target",
    "unsupported-record": "uses a record ctypes cannot lay out as the compiler does",
    "unsupported-type": "uses a type these declarations cannot express",
    "unknown-type": "uses a type the metadata does not declare",
    "game-server": "is a game server accessor, and pysteamsdk cannot start a game server yet",
    "no-signature": "is exported by the headers, but steam_api.json gives no signature",
    "invalid-name": "has a name that is not a usable Python identifier",
    "retained-pointer": "keeps a pointer that Steam may use after any object a binding could "
    "keep alive for it",
}

_CANONICAL = re.compile(r"(const )?([^*\[]+?)(\**)((?:\[\d+\])*)")
_SIGNED = frozenset({"signed char", "short", "int", "long", "long long"})
_UNSIGNED = frozenset(f"unsigned {name}" for name in ("char", "short", "int", "long", "long long"))
_INTEGERS = {
    (signed, size): f"ctypes.c_{'' if signed else 'u'}int{size * 8}"
    for signed in (True, False)
    for size in (1, 2, 4, 8)
}
# Exact-width types are fixed by the C standard; the others are as wide as a pointer.
_STANDARD = {
    **{f"int{bits}_t": (True, bits // 8) for bits in (8, 16, 32, 64)},
    **{f"uint{bits}_t": (False, bits // 8) for bits in (8, 16, 32, 64)},
}
_POINTER_SIZED = {"size_t": False, "intptr_t": True, "uintptr_t": False}
_STORAGE = {1: "ctypes.c_uint8", 2: "ctypes.c_uint16", 4: "ctypes.c_uint32", 8: "ctypes.c_uint64"}
# Names ctypes gives a meaning to on a record class, and those made up here for members C++
# leaves unnamed; a member with one of them would break the class.
_MEMBER_NAMES = frozenset(
    {"_align_", "_anonymous_", "_b_base_", "_b_needsfree_", "_fields_", "_layout_", "_length_"}
    | {"_objects", "_pack_", "_storage", "_swappedbytes_", "_type_", "from_address", "in_dll"}
    | {"from_buffer", "from_buffer_copy", "from_param"}
)
_PACKS = (None, 1, 2, 4, 8, 16)
# Names the module itself defines.
_RESERVED = frozenset(
    {"bindings", "ctypes", "enum", "COMPILER", "FUNCTIONS", "LAYOUTS", "PROTOCOL", "SDK_VERSION"}
    | {"SKIPPED", "TARGET"}
)
_WIDTH = 99


class GenerationError(Exception):
    """Metadata or checks that no declarations can be generated from."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Verification:
    """What a native compiler confirmed about one target's layouts.

    ``statuses`` maps "record", "enum" and "typedef" to each name's status, as
    codegen.abi.probe.statuses gives it, and ``gaps`` what could not be checked, by name.
    """

    target: str
    compiler: str
    statuses: Mapping[str, Mapping[str, str]]
    gaps: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_report(cls, report: probe.Report, layout: ir.TargetLayout) -> "Verification":
        gaps: dict[str, str] = {}
        for gap in report.gaps:
            gaps.setdefault(gap.owner, gap.reason)
        statuses = {str(group): names for group, names in probe.statuses(report, layout).items()}
        return cls(report.target, report.identity.compiler, statuses, gaps)


@dataclass(frozen=True)
class Skip:
    kind: Kind
    name: str
    category: str
    reason: str


@dataclass(frozen=True)
class Note:
    """A limitation of something that was generated."""

    kind: Kind
    name: str
    message: str


@dataclass(frozen=True)
class Generated:
    target: str
    sdk_version: str | None
    compiler: str
    text: str
    totals: dict[Kind, int]
    generated: dict[Kind, tuple[str, ...]]
    skipped: tuple[Skip, ...]
    notes: tuple[Note, ...]


def generate(metadata: ir.Metadata, verification: Verification) -> Generated:
    """The declarations module for ``verification.target``.

    Raises GenerationError when the metadata has errors, has no layouts for the target, or
    contradicts what the compiler found.
    """
    issues = dict.fromkeys([*metadata.issues, *validate(metadata)])
    problems = [
        f"{issue.code}: {issue.message}  [{issue.source}]"
        for issue in ir.sort_issues(issues)
        if issue.severity == "error"
    ]
    target = verification.target
    computed = metadata.abi.targets if metadata.abi else ()
    layouts = [item for item in computed if item.target == target]
    if target not in TARGETS:
        problems.append(f"{target} is not a target codegen.abi knows")
    elif not layouts:
        problems.append(f"the metadata has no layouts for {target}")
    mismatched = sorted(
        f"{group} {name}"
        for group, names in verification.statuses.items()
        for name, status in names.items()
        if status == "mismatched"
    )
    if mismatched:
        problems.append(f"the compiler contradicts the layouts of {', '.join(mismatched)}")
    if problems:
        raise GenerationError(problems)
    return _Generator(metadata, layouts[0], verification).run()


def summary(generated: Generated) -> str:
    """The coverage of ``generated``: counts, then every skipped declaration and every note."""
    lines = [
        f"Declarations for {generated.target} from SDK {generated.sdk_version or 'unknown'}, "
        f"layouts checked by {generated.compiler}"
    ]
    skipped = Counter(item.kind for item in generated.skipped)
    for kind in KINDS:
        total = generated.totals[kind]
        made = len(generated.generated[kind])
        detail = f"{made} generated" + (f", {skipped[kind]} skipped" if skipped[kind] else "")
        lines.append(f"  {kind + 's':12} {total:5}  {detail}")
    if generated.skipped:
        lines.append("Skipped, by reason")
        reasons = Counter(item.category for item in generated.skipped)
        for category, count in sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0])):
            lines.append(f"  {category:21} {count:4}  {CATEGORIES[category]}")
        lines.append("Skipped")
        lines += [f"  {item.kind} {item.name}: {item.reason}" for item in generated.skipped]
    if generated.notes:
        lines.append("Notes")
        lines += [f"  {item.kind} {item.name}: {item.message}" for item in generated.notes]
    return "\n".join(lines) + "\n"


class _Unsupported(Exception):
    def __init__(self, category: str, reason: str) -> None:
        super().__init__(reason)
        self.category = category
        self.reason = reason


@dataclass(frozen=True)
class _Base:
    """A type name resolved for a signature, before the signature's own pointers apply.

    ``kind`` is "scalar", "char", "enum", "record", "array", "void", "interface", "opaque" or
    "callback", a reviewed callback type; ``expr`` names its ctypes type. A typedef of a pointer
    adds ``stars``, and ``const`` says whether what it points to is const.
    """

    kind: str
    expr: str = ""
    name: str = ""
    stars: int = 0
    const: bool = False


@dataclass
class _Class:
    """A ctypes record class to emit; ``nested`` are its anonymous or unnamed member types."""

    name: str
    label: str
    base: str
    pack: int | None
    anonymous: tuple[str, ...]
    fields: list[tuple[str, str, str]]
    layout: tuple[int, int, tuple[tuple[str, int, int], ...]]
    depends: set[str]
    nested: list["_Class"]


def _canonical(text: str) -> tuple[bool, str, int, tuple[int, ...]] | None:
    match = _CANONICAL.fullmatch(text)
    if match is None:
        return None
    const, base, stars, dimensions = match.groups()
    return bool(const), base, len(stars), tuple(int(n) for n in re.findall(r"\d+", dimensions))


def _scalar(name: str, size: int) -> str | None:
    known = {"bool": (1, "ctypes.c_bool"), "char": (1, "ctypes.c_char")}
    known |= {"float": (4, "ctypes.c_float"), "double": (8, "ctypes.c_double")}
    if name in known:
        width, expr = known[name]
        return expr if size == width else None
    if name in _SIGNED or name in _UNSIGNED:
        return _INTEGERS.get((name in _SIGNED, size))
    return None


def _storage_only(record: ir.RecordLayout) -> bool:
    """Whether ctypes gets only the record's bytes: it has bit-fields, whose ctypes layout differs
    by compiler and Python version, or no members, which C++ still gives a byte."""
    return not record.fields or any(item.bit_width is not None for item in record.fields)


def _walk(fields: Iterable[ir.FieldLayout]) -> Iterator[ir.FieldLayout]:
    for item in fields:
        yield item
        if item.record is not None:
            yield from _walk(item.record.fields)


def _simulate(
    kind: str, members: list[tuple[int, int]], pack: int | None
) -> tuple[list[int], int, int]:
    """Offsets, size and alignment ctypes gives members of (size, alignment) under ``pack``."""
    offsets, end, align = [], 0, 1
    for size, natural in members:
        aligned = min(natural, pack) if pack else natural
        offset = 0 if kind == "union" else -(-end // aligned) * aligned
        offsets.append(offset)
        end = max(end, offset + size)
        align = max(align, aligned)
    return offsets, -(-end // align) * align, align


def _pack(record: ir.RecordLayout, label: str) -> int | None:
    """The _pack_ that makes ctypes lay out ``record`` as compiled, or None if it needs none."""
    members = [(item.size, item.align) for item in record.fields]
    wanted = ([item.offset for item in record.fields], record.size, record.align)
    for pack in _PACKS:
        if _simulate(record.kind, members, pack) == wanted:
            return pack
    raise _Unsupported("unsupported-record", f"no _pack_ makes ctypes lay out {label} as compiled")


def _identifier(name: str) -> bool:
    # A leading underscore would make a name private, or special to enum and ctypes.
    return name.isidentifier() and not keyword.iskeyword(name) and not name.startswith("_")


def _python_name(name: str) -> str | None:
    found = name.replace("::", "_")
    return found if _identifier(found) and found not in _RESERVED else None


def _member_name(name: str) -> bool:
    if not name.isidentifier() or keyword.iskeyword(name) or name in _MEMBER_NAMES:
        return False
    dunder = name.startswith("__") and name.endswith("__")
    return not dunder and not re.fullmatch(r"_\d+", name)


def _callback_interfaces(metadata: ir.Metadata) -> set[str]:
    """Interfaces only the caller can provide: no accessor or function returns one, yet some
    function takes one."""
    interfaces = {item.name for item in metadata.interfaces if not item.accessors}
    functions = metadata.functions()
    returned = {(f.flat_returns or f.returns).base for f in functions}
    taken = {(p.flat_type or p.type).base for f in functions for p in f.params}
    return (interfaces & taken) - returned


def _tuple(items: list[str]) -> str:
    return f"({items[0]},)" if len(items) == 1 else f"({', '.join(items)})"


class _Generator:
    def __init__(
        self, metadata: ir.Metadata, layout: ir.TargetLayout, verification: Verification
    ) -> None:
        self.metadata = metadata
        self.target = layout.target
        self.verification = verification
        self.records = {item.name: item for item in layout.records}
        self.enum_layouts = {item.name: item for item in layout.enums}
        self.typedef_layouts = {item.name: item for item in layout.typedefs}
        self.enums = {item.qualified_name: item for item in metadata.enums}
        self.typedef_names = {item.name for item in metadata.typedefs}
        self.interfaces = {item.name: item for item in metadata.interfaces}
        self.callback_interfaces = _callback_interfaces(metadata)
        self.hooks = {(hook.function, hook.param): hook for hook in metadata.hooks}
        self.hook_types = {hook.typedef: hook for hook in metadata.hooks}
        self.retained = {(item.function, item.param): item for item in metadata.retained}
        self.kept: dict[str, tuple[int, ...]] = {}
        self.skipped: list[Skip] = []
        self.notes: list[Note] = []
        self.generated: dict[Kind, list[str]] = {kind: [] for kind in KINDS}
        self.checked: dict[str, _Unsupported | None] = {}
        self.classes: dict[str, _Class] = {}
        self.class_names: set[str] = set()
        self.enum_storage: dict[str, str | _Unsupported] = {}
        self.typedef_bases: dict[str, _Base | _Unsupported] = {}
        self.python, self.invalid = self._names(layout)
        self.sizes = self._sizes(layout)

    def status(self, group: str, name: str) -> str:
        return self.verification.statuses.get(group, {}).get(name, "not checked")

    def _names(self, layout: ir.TargetLayout) -> tuple[dict[str, str], dict[str, str]]:
        """The Python name of each declaration, and why the others have none."""
        declared = [
            *(item.name for item in self.metadata.interfaces),
            *(item.name for item in layout.records),
            *(item.name for item in layout.enums),
            *(item.name for item in layout.typedefs),
            *(_scoped(item.scope, item.name) for item in self.metadata.constants),
        ]
        owners: dict[str, list[str]] = {}
        invalid: dict[str, str] = {}
        for name in dict.fromkeys(declared):
            python = _python_name(name)
            if python is None:
                invalid[name] = f"{name} has no usable Python name"
            else:
                owners.setdefault(python, []).append(name)
        python_names = {}
        for python, names in owners.items():
            for name in names:
                if len(names) > 1:
                    invalid[name] = f"{name} and {' and '.join(n for n in names if n != name)} "
                    invalid[name] += f"would both be {python} in Python"
                else:
                    python_names[name] = python
        return python_names, invalid

    def _sizes(self, layout: ir.TargetLayout) -> dict[str, int]:
        """The size of each fundamental type, and of a pointer, from verified layouts."""
        # C and C++ define the size of every char type as 1.
        sizes = {"char": 1, "signed char": 1, "unsigned char": 1}
        uses = [
            (item.type, item.size)
            for item in layout.typedefs
            if item.size is not None and self.status("typedef", item.name) in USABLE
        ]
        uses += [
            (item.type, item.size)
            for record in layout.records
            if record.status == "complete" and self.status("record", record.name) in USABLE
            for item in _walk(record.fields)
        ]
        for text, size in uses:
            parsed = _canonical(text)
            if parsed is None or not math.prod(parsed[3]):
                continue
            _, base, stars, dimensions = parsed
            count = math.prod(dimensions)
            if stars:
                sizes.setdefault("*", size // count)
            elif base in FUNDAMENTAL:
                sizes.setdefault(base, size // count)
        return sizes

    def note(self, kind: Kind, name: str, message: str) -> None:
        self.notes.append(Note(kind, name, message))

    def skip(self, kind: Kind, name: str, problem: _Unsupported) -> None:
        self.skipped.append(Skip(kind, name, problem.category, problem.reason))

    def name(self, declared: str) -> str:
        python = self.python.get(declared)
        if python is None:
            reason = self.invalid.get(declared, f"{declared} has no Python name")
            raise _Unsupported("invalid-name", reason)
        return python

    # Records

    def check(self, name: str) -> _Unsupported | None:
        """Why ``name`` cannot be a record class, or None when it can.

        Only members held by value count: a pointer member that cannot be typed stays untyped.
        """
        if name in self.checked:
            return self.checked[name]
        self.checked[name] = _Unsupported("unsupported-record", f"{name} contains itself")
        try:
            self._check_record(name)
            problem = None
        except _Unsupported as exc:
            problem = exc
        self.checked[name] = problem
        return problem

    def _check_record(self, name: str) -> None:
        record = self.records.get(name)
        if record is None:
            raise _Unsupported("unknown-type", f"{name} has no layout on {self.target}")
        if record.status == "opaque":
            raise _Unsupported("opaque", f"{name} is never defined, so its size is unknown")
        if record.status != "complete":
            raise _Unsupported("unsupported-record", f"{name} is {record.status}: {record.reason}")
        status = self.status("record", name)
        if status not in USABLE:
            message = f"the compiler did not verify {name} on {self.target}: it is {status}"
            raise _Unsupported("unverified-layout", message)
        self._check_members(record, name, self.name(name))

    def _check_members(self, record: ir.RecordLayout, label: str, python: str) -> None:
        if _storage_only(record):
            if record.align not in _STORAGE:
                message = f"{label} cannot be kept as storage aligned to {record.align}"
                raise _Unsupported("unsupported-record", message)
            return
        for index, item in enumerate(record.fields):
            where = f"{label}.{item.name or '(anonymous)'}"
            if item.name and not _member_name(item.name):
                raise _Unsupported("invalid-name", f"{where} cannot be a ctypes member")
            if item.record is not None:
                # Its class is named here, so that no two nested classes share a name.
                inner = f"_{python}_{item.name or index}"
                if inner in self.class_names:
                    raise _Unsupported("invalid-name", f"{where} would be a second {inner}")
                self.class_names.add(inner)
                self._check_members(item.record, where, inner)
            else:
                self._field(item, where, typed=False)
        _pack(record, label)

    def record_class(self, name: str) -> str:
        """The class for a record check() accepts, built on first use."""
        if name not in self.classes:
            self.classes[name] = self._build(self.records[name], self.name(name), name)
        return self.classes[name].name

    def _build(self, record: ir.RecordLayout, python: str, label: str) -> _Class:
        base = "ctypes.Union" if record.kind == "union" else "ctypes.Structure"
        assert record.size is not None
        assert record.align is not None
        if _storage_only(record):
            bits = [item.name for item in record.fields if item.bit_width is not None and item.name]
            if record.fields:
                message = f"its bit-fields {', '.join(bits) or '(unnamed)'} are not exposed: "
            else:
                message = "it has no data members: "
            kept = "bytes are" if record.size > 1 else "byte is"
            message += f"its {record.size} {kept} kept as storage of the same alignment"
            self.note("record", label, message)
            storage = f"{_STORAGE[record.align]} * {record.size // record.align}"
            storage_layout = (record.size, record.align, (("_storage", 0, record.size),))
            return _Class(
                python,
                label,
                "ctypes.Structure",
                None,
                (),
                [("_storage", storage, "")],
                storage_layout,
                set(),
                [],
            )
        fields, places, anonymous, depends, nested = [], [], [], set(), []
        for index, item in enumerate(record.fields):
            member = item.name or f"_{index}"
            where = f"{label}.{item.name or '(anonymous)'}"
            if item.record is not None:
                inner = self._build(item.record, f"_{python}_{item.name or index}", where)
                nested.append(inner)
                parsed = _canonical(item.type)
                dimensions = parsed[3] if parsed else ()
                expr = inner.name + "".join(f" * {n}" for n in reversed(dimensions))
                comment = ""
                if not item.name:
                    anonymous.append(member)
            else:
                expr, comment, depend = self._field(item, where, typed=True)
                if depend:
                    depends.add(depend)
            assert item.offset is not None
            fields.append((member, expr, comment))
            places.append((member, item.offset, item.size))
        layout = (record.size, record.align, tuple(places))
        pack = _pack(record, label)
        return _Class(python, label, base, pack, tuple(anonymous), fields, layout, depends, nested)

    def _field(self, item: ir.FieldLayout, where: str, *, typed: bool) -> tuple[str, str, str]:
        """A member's ctypes type, a comment for it, and the record it holds by value, if any."""
        parsed = _canonical(item.type)
        if parsed is None:
            raise _Unsupported("unsupported-type", f"{where} has the type {item.type!r}")
        _, base, stars, dimensions = parsed
        size = item.size // max(math.prod(dimensions), 1)
        comment, depend = "", ""
        if stars == 0:
            keyword_, _, name = base.partition(" ")
            if keyword_ == "enum":
                expr, comment = self.enum(name), name
            elif keyword_ in ("struct", "union", "class") and name:
                problem = self.check(name)
                if problem is not None:
                    raise _Unsupported(problem.category, f"{where} holds {name}: {problem.reason}")
                expr = self.record_class(name) if typed else name
                depend = name
            else:
                found = _scalar(base, size)
                if found is None:
                    raise _Unsupported("unsupported-type", f"{where} is a {size}-byte {base}")
                expr = found
        elif stars == 1:
            expr = self._pointer_field(base, where, typed)
        elif stars == 2 and base in ("char", "void"):
            expr = f"ctypes.POINTER({self._pointer_field(base, where, typed)})"
        else:
            expr = "bindings.VoidPointer"
            if typed:
                self.note("record", where, f"{item.type} is kept as an untyped pointer")
        return expr + "".join(f" * {n}" for n in reversed(dimensions)), comment, depend

    def _pointer_field(self, base: str, where: str, typed: bool) -> str:
        if base == "char":
            return "bindings.CString"
        if base == "void":
            return "bindings.VoidPointer"
        if base == "function":
            if typed:
                self.note("record", where, "a function pointer, kept as an address to pass on")
            return "bindings.FunctionPointer"
        if not typed:
            return "bindings.VoidPointer"
        keyword_, _, name = base.partition(" ")
        target: str | None = None
        if keyword_ == "enum":
            with contextlib.suppress(_Unsupported):
                target = self.enum(name)
        elif keyword_ in ("struct", "union", "class") and name:
            # Every record check() accepts gets its class, so naming it is enough; building it
            # here could come back to this record through its own pointers.
            target = self.name(name) if self.check(name) is None else None
        elif base in self.sizes:
            target = _scalar(base, self.sizes[base])
        if target is None:
            self.note("record", where, f"a pointer to {base}, which has no class, kept untyped")
            return "bindings.VoidPointer"
        return f"ctypes.POINTER({target})"

    # Enums and typedefs

    def enum(self, name: str) -> str:
        """The ctypes type an enum is stored as."""
        found = self.enum_storage.get(name)
        if found is None:
            try:
                found = self._enum_storage(name)
            except _Unsupported as exc:
                found = exc
            self.enum_storage[name] = found
        if isinstance(found, _Unsupported):
            raise found
        return found

    def _enum_storage(self, name: str) -> str:
        layout = self.enum_layouts.get(name)
        if layout is None:
            raise _Unsupported("unknown-type", f"enum {name} has no layout on {self.target}")
        status = self.status("enum", name)
        if status not in USABLE:
            message = f"the compiler did not verify enum {name} on {self.target}: it is {status}"
            raise _Unsupported("unverified-layout", message)
        self.name(name)
        signed = layout.underlying in _SIGNED
        if not signed and layout.underlying not in _UNSIGNED:
            message = f"enum {name} is stored as {layout.underlying}, whose sign is not known"
            raise _Unsupported("unsupported-type", message)
        return _INTEGERS[signed, layout.size]

    def typedef(self, name: str) -> _Base:
        found = self.typedef_bases.get(name)
        if found is None:
            try:
                found = self._typedef(name)
            except _Unsupported as exc:
                found = exc
            self.typedef_bases[name] = found
        if isinstance(found, _Unsupported):
            raise found
        return found

    def _typedef(self, name: str) -> _Base:
        layout = self.typedef_layouts.get(name)
        if layout is None:
            raise _Unsupported("unknown-type", f"typedef {name} has no layout on {self.target}")
        status = self.status("typedef", name)
        if status not in USABLE:
            message = f"the compiler did not verify typedef {name} on {self.target}: it is {status}"
            raise _Unsupported("unverified-layout", message)
        alias = self.name(name)
        parsed = _canonical(layout.type)
        if parsed is None or layout.size is None:
            raise _Unsupported("unsupported-type", f"typedef {name} is {layout.type!r}")
        const, base, stars, dimensions = parsed
        if base == "function":
            hook = self.hook_types.get(name)
            if hook is None:
                message = f"{name} is a C function pointer type no callback review uses"
                raise _Unsupported("callback", message)
            self._callback(hook)
            return _Base("callback", alias, name)
        if stars:
            if base == "void":
                inner = _Base("void")
            elif base == "char":
                inner = _Base("char", "ctypes.c_char")
            else:
                inner = self._name_base(base)
            return _Base(inner.kind, inner.expr, inner.name, stars=stars, const=const)
        value = self._name_base(base, layout.size // max(math.prod(dimensions), 1))
        if dimensions:
            return _Base("array", alias, name)
        # A record keeps its own name, which by_value reviews use.
        return _Base(value.kind, alias, value.name if value.kind == "record" else name)

    def typedef_expr(self, name: str) -> str:
        """What a typedef's alias stands for, for typedefs typedef() accepts."""
        layout = self.typedef_layouts[name]
        parsed = _canonical(layout.type)
        assert parsed is not None
        assert layout.size is not None
        _, base, stars, dimensions = parsed
        if base == "function":
            return self._callback(self.hook_types[name])
        if stars:
            found = self.typedef(name)
            if found.kind == "char" and stars == 1:
                return "bindings.CString"
            if found.kind in ("void", "opaque", "char") or stars > 1:
                return "bindings.VoidPointer"
            return f"ctypes.POINTER({found.expr})"
        size = layout.size // max(math.prod(dimensions), 1)
        value = self._name_base(base, size)
        return value.expr + "".join(f" * {n}" for n in reversed(dimensions))

    def _name_base(self, base: str, size: int | None = None) -> _Base:
        """A canonical layout base type, as typedefs use them."""
        keyword_, _, name = base.partition(" ")
        if keyword_ == "enum":
            return _Base("enum", self.enum(name), name)
        if keyword_ in ("struct", "union", "class") and name:
            return self.record_base(name)
        found = self.sizes.get(base) if size is None else size
        expr = _scalar(base, found) if found is not None else None
        if expr is None:
            raise _Unsupported("unsupported-type", f"no verified layout gives {base} a ctypes type")
        return _Base("char" if base == "char" else "scalar", expr, base)

    def record_base(self, name: str) -> _Base:
        record = self.records.get(name)
        if record is not None and record.status == "opaque":
            return _Base("opaque", name=name)
        problem = self.check(name)
        if problem is not None:
            raise problem
        return _Base("record", self.record_class(name), name)

    # Signatures

    def base(self, name: str) -> _Base:
        """A type name a signature uses, resolved through this target's verified layouts."""
        if name in self.interfaces:
            if name in self.callback_interfaces:
                message = f"{name} is an interface the caller implements for Steam to call back"
                raise _Unsupported("callback-object", message)
            return _Base("interface", self.name(name), name)
        if name in self.typedef_layouts or name in self.typedef_names:
            return self.typedef(name)
        if name in self.enum_layouts or name in self.enums:
            return _Base("enum", self.enum(name), name)
        if name in self.records:
            return self.record_base(name)
        if name == "void":
            return _Base("void")
        if name in FUNDAMENTAL:
            size = self.sizes.get(name)
            expr = _scalar(name, size) if size is not None else None
            if expr is None:
                message = f"no verified layout on {self.target} gives the size of {name}"
                raise _Unsupported("unsupported-type", message)
            return _Base("char" if name == "char" else "scalar", expr, name)
        if name in _STANDARD:
            signed, size = _STANDARD[name]
            return _Base("scalar", _INTEGERS[signed, size], name)
        if name in _POINTER_SIZED and "*" in self.sizes:
            return _Base("scalar", _INTEGERS[_POINTER_SIZED[name], self.sizes["*"]], name)
        raise _Unsupported("unknown-type", f"{name} is not declared in the metadata")

    def _resolve(self, ctype: ir.CType) -> tuple[_Base, int, bool]:
        """The base type, the pointers in all, and whether what they point to is const."""
        if ctype.kind == "unparsed":
            message = f"{ctype.spelling!r} is not a type these declarations parse"
            raise _Unsupported("unsupported-type", message)
        if ctype.kind == "function_pointer":
            raise _Unsupported("callback", f"{ctype.spelling} is a C function pointer")
        if ctype.array is not None:
            raise _Unsupported("unsupported-type", f"{ctype.spelling} is an array")
        found = self.base(ctype.base)
        const = found.const if found.stars else ctype.const
        return found, found.stars + ctype.pointers, const

    def by_value(self, found: _Base, verb: str) -> str:
        status = by_value_status(self.metadata, found.name, self.target)
        if status == "direct":
            return found.expr
        if status == "shim":
            message = (
                f"{found.name} is {verb} by value, and a by_value review shows ctypes doing that "
                f"differently from the compiler on {self.target}"
            )
            raise _Unsupported("by-value-shim", message)
        message = f"{found.name} is {verb} by value, which no by_value review makes direct on "
        raise _Unsupported("by-value-unverified", message + self.target)

    def pointee(self, found: _Base) -> str:
        """The ctypes type a single pointer to ``found`` points to."""
        if found.kind == "opaque":
            raise _Unsupported("opaque", f"{found.name} is never defined, so its size is unknown")
        if found.kind == "interface":
            raise _Unsupported("unsupported-type", f"a pointer to a {found.name} pointer")
        return "ctypes.c_char" if found.kind == "char" else found.expr

    def _callback(self, hook: ir.Hook) -> str:
        """The prototype of a reviewed callback type. Steam's arguments are valid only during
        the call, so the Python function gets copies: bytes for strings, records copied."""
        returns, _, _ = self._resolve(hook.returns)
        if returns.kind != "void" or hook.returns.pointers:
            message = f"{hook.typedef} returns {hook.returns.spelling}, not nothing"
            raise _Unsupported("unsupported-type", message)
        args = []
        for ctype in hook.params:
            found, stars, _ = self._resolve(ctype)
            if stars == 0 and found.kind in ("scalar", "char", "enum") and not ctype.reference:
                args.append(found.expr)
            elif stars == 1 and found.kind in ("char", "void", "record") and not ctype.reference:
                args.append(
                    {"char": "ctypes.c_char_p", "void": "ctypes.c_void_p"}.get(
                        found.kind, f"ctypes.POINTER({found.expr})"
                    )
                )
            else:
                message = f"{hook.typedef} passes {ctype.spelling} to its callback"
                raise _Unsupported("unsupported-type", message)
        return f"bindings.Callback({_tuple(args) if args else '()'})"

    def param(self, ctype: ir.CType, *, reviewed: bool = False) -> str:
        found, stars, const = self._resolve(ctype)
        if found.kind == "callback":
            if stars or ctype.reference:
                raise _Unsupported("unsupported-type", f"a pointer to {ctype.base}")
            if not reviewed:
                message = f"{ctype.base} is a callback, and no callback review covers this call"
                raise _Unsupported("callback", message)
            return found.expr
        if ctype.reference:
            if stars or found.kind in ("void", "interface", "opaque"):
                raise _Unsupported("unsupported-type", f"a reference to {ctype.spelling}")
            return f"bindings.Ref({self.pointee(found)})"
        if stars == 0:
            if found.kind == "record":
                return self.by_value(found, "passed")
            if found.kind in ("scalar", "char", "enum"):
                return found.expr
            raise _Unsupported("unsupported-type", f"{ctype.spelling} passed by value")
        if stars == 1:
            if found.kind == "interface":
                return found.expr
            if found.kind == "void":
                return f"bindings.{'In' if const else 'Out'}(None)"
            return f"bindings.{'In' if const else 'Out'}({self.pointee(found)})"
        if stars == 2:
            # The slots a pointer to pointers reaches are themselves writable.
            return f"bindings.Out({self._pointer_value(found)})"
        raise _Unsupported("unsupported-type", f"{ctype.spelling} has too many pointers")

    def returns(self, ctype: ir.CType) -> str:
        found, stars, _ = self._resolve(ctype)
        if found.kind == "callback":
            raise _Unsupported("callback", f"returns {ctype.spelling}, a C function pointer")
        if ctype.reference:
            raise _Unsupported("unsupported-type", f"returns a reference, {ctype.spelling}")
        if stars == 0:
            if found.kind == "void":
                return "None"
            if found.kind == "record":
                return self.by_value(found, "returned")
            if found.kind in ("scalar", "char", "enum"):
                return found.expr
            raise _Unsupported("unsupported-type", f"returns {ctype.spelling} by value")
        if stars == 1:
            return found.expr if found.kind == "interface" else self._pointer_value(found)
        raise _Unsupported("unsupported-type", f"returns {ctype.spelling}")

    def _pointer_value(self, found: _Base) -> str:
        """The ctypes type of a pointer to ``found`` held as a value, not converted."""
        if found.kind == "char":
            return "bindings.CString"
        if found.kind == "void":
            return "bindings.VoidPointer"
        return f"ctypes.POINTER({self.pointee(found)})"

    # The module

    def run(self) -> Generated:
        typedefs = self._typedefs()
        enums = self._enums()
        constants = self._constants()
        interfaces = self._interfaces()
        functions = self._functions()
        records = self._records()
        exports = self.metadata.exports
        for export in exports:
            reason = f"declared at {export.source}, but steam_api.json gives no signature"
            self.skip("export", export.name, _Unsupported("no-signature", reason))
        accessors = sum(len(item.accessors) for item in self.metadata.interfaces)
        totals: dict[Kind, int] = {
            "function": len(self.metadata.functions()),
            "accessor": accessors,
            "export": len(exports),
            "record": len(self.records),
            "enum": len(self.enum_layouts),
            # Only the values of the enums that were generated.
            "value": sum(
                len(self.enums[e].values) for e in self.generated["enum"] if e in self.enums
            ),
            "typedef": len(self.typedef_layouts),
            "constant": len(self.metadata.constants),
        }
        text = self._render(typedefs, enums, constants, interfaces, records, functions)
        order = {kind: index for index, kind in enumerate(KINDS)}
        return Generated(
            self.target,
            self.metadata.sdk_version,
            self.verification.compiler,
            text,
            totals,
            {kind: tuple(sorted(names)) for kind, names in self.generated.items()},
            tuple(sorted(self.skipped, key=lambda item: (order[item.kind], item.name))),
            tuple(sorted(self.notes, key=lambda item: (order[item.kind], item.name))),
        )

    def _typedefs(self) -> list[tuple[str, str]]:
        aliases = []
        for name in sorted(self.typedef_layouts):
            try:
                self.typedef(name)
                aliases.append((self.name(name), self.typedef_expr(name)))
            except _Unsupported as exc:
                self.skip("typedef", name, exc)
            else:
                self.generated["typedef"].append(name)
        return aliases

    def _enums(self) -> list[tuple[str, str, str, list[tuple[str, int]]]]:
        found = []
        for name in sorted(self.enum_layouts):
            try:
                storage = self.enum(name)
            except _Unsupported as exc:
                self.skip("enum", name, exc)
                continue
            layout = self.enum_layouts[name]
            declared = self.enums.get(name)
            if declared is None:
                self.note("enum", name, "steam_api.json gives none of its values")
            values = []
            for value in declared.values if declared else ():
                label = f"{name}::{value.name}"
                problem = None
                if value.value is None:
                    problem = _Unsupported("unsupported-type", f"{label} has no evaluated value")
                elif not _identifier(value.name) or value.name == "mro":
                    problem = _Unsupported("invalid-name", f"{label} cannot be an enum member")
                if problem is not None:
                    self.skip("value", label, problem)
                    continue
                assert value.value is not None
                values.append((value.name, _stored(value.value, layout)))
                self.generated["value"].append(label)
            found.append((self.name(name), name, storage, values))
            self.generated["enum"].append(name)
        return found

    def _constants(self) -> list[tuple[str, int | float]]:
        found = []
        for constant in sorted(self.metadata.constants, key=lambda c: _scoped(c.scope, c.name)):
            label = _scoped(constant.scope, constant.name)
            if constant.value is None:
                self.skip("constant", label, _Unsupported("unsupported-type", "no evaluated value"))
                continue
            try:
                found.append((self.name(label), constant.value))
            except _Unsupported as exc:
                self.skip("constant", label, exc)
            else:
                self.generated["constant"].append(label)
        return found

    def _interfaces(self) -> list[tuple[str, str]]:
        found = []
        for name in sorted(self.interfaces):
            if name in self.callback_interfaces:
                continue
            with contextlib.suppress(_Unsupported):
                found.append((self.name(name), name))
        return found

    def _functions(self) -> dict[str, tuple[str, list[str], str]]:
        functions: dict[str, tuple[str, list[str], str]] = {}
        owners: list[ir.Interface | ir.Struct] = [*self.metadata.interfaces, *self.metadata.structs]
        for owner in sorted(owners, key=lambda item: item.name):
            for method in owner.methods:
                params = [(p.name, p.flat_type or p.type) for p in method.params]
                returns = method.flat_returns or method.returns
                comment = _signature(returns, method.flat_name, [("self", owner.name), *params])
                where = "self"
                kept = []
                try:
                    argtypes = [self._self(owner)]
                    for index, (name, ctype) in enumerate(params, 1):
                        where = f"parameter {name}"
                        hook = (method.flat_name, name) in self.hooks
                        argtypes.append(self.param(ctype, reviewed=hook))
                        retained = self.retained.get((method.flat_name, name))
                        if retained is not None and retained.holder == "none":
                            message = "Steam keeps this pointer beyond any object a binding "
                            message += "could keep alive for it"
                            raise _Unsupported("retained-pointer", message)
                        if retained is not None:
                            kept.append(index)
                    where = "the result"
                    functions[method.flat_name] = (self.returns(returns), argtypes, comment)
                    if kept:
                        self.kept[method.flat_name] = tuple(kept)
                except _Unsupported as exc:
                    problem = _Unsupported(exc.category, f"{where}: {exc.reason}")
                    self.skip("function", method.flat_name, problem)
                else:
                    self.generated["function"].append(method.flat_name)
        for interface in sorted(self.interfaces.values(), key=lambda item: item.name):
            for accessor in interface.accessors:
                comment = f"{interface.name} *{accessor.flat_name}()"
                try:
                    if accessor.kind == "gameserver":
                        message = "a game server accessor, and pysteamsdk cannot start one yet"
                        raise _Unsupported("game-server", message)
                    handle = self.base(interface.name).expr
                except _Unsupported as exc:
                    self.skip("accessor", accessor.flat_name, exc)
                else:
                    functions[accessor.flat_name] = (handle, [], comment)
                    self.generated["accessor"].append(accessor.flat_name)
        return dict(sorted(functions.items()))

    def _self(self, owner: ir.Interface | ir.Struct) -> str:
        if isinstance(owner, ir.Interface):
            return self.base(owner.name).expr
        found = self.record_base(owner.name)
        if found.kind == "opaque":
            raise _Unsupported("opaque", f"{owner.name} is never defined, so its size is unknown")
        return f"bindings.Out({found.expr})"

    def _records(self) -> list[_Class]:
        for name in sorted(self.records):
            problem = self.check(name)
            if problem is not None:
                self.skip("record", name, problem)
                continue
            self.record_class(name)
            self.generated["record"].append(name)
            gap = self.verification.gaps.get(name)
            if self.status("record", name) == "partly verified":
                self.note(
                    "record", name, f"partly verified: {gap or 'not every check was possible'}"
                )
        ordered: list[_Class] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            built = self.classes[name]
            for depend in sorted(_depends(built)):
                visit(depend)
            ordered.append(built)

        for name in sorted(self.classes):
            visit(name)
        return ordered

    def _render(
        self,
        typedefs: list[tuple[str, str]],
        enums: list[tuple[str, str, str, list[tuple[str, int]]]],
        constants: list[tuple[str, int | float]],
        interfaces: list[tuple[str, str]],
        records: list[_Class],
        functions: dict[str, tuple[str, list[str], str]],
    ) -> str:
        version = self.metadata.sdk_version or "unknown"
        compiler = self.verification.compiler
        lines = [
            f"# Generated by python -m codegen bindings from Steamworks SDK {version} for "
            f"{self.target},",
            f"# with layouts checked by {compiler}. Do not edit; generate it again instead.",
            f'"""ctypes declarations of the Steamworks flat API for {self.target}.',
            "",
            "Bind them with pysteamsdk._native.bindings.bind(). Importing this module checks that",
            "ctypes lays out every record as the compiler did.",
            '"""',
            "",
            "import ctypes",
            "import enum",
            "",
            "from pysteamsdk._native import bindings",
            "",
            f"PROTOCOL = {PROTOCOL}",
            f"TARGET = {_text(self.target)}",
            f"SDK_VERSION = {_text(version) if self.metadata.sdk_version else None}",
            f"COMPILER = {_text(compiler)}",
        ]
        late = [item for item in typedefs if not item[1].startswith("ctypes.c_")]
        early = [item for item in typedefs if item not in late]
        if early:
            lines += ["", "# Typedefs", ""]
            lines += [f"{alias} = {expr}" for alias, expr in early]
        if enums:
            lines += ["", "# Enums, each stored as the ctypes type its comment names"]
        for python, name, storage, values in enums:
            lines += ["", "", f"# enum {name}: {storage}", f"class {python}(enum.IntEnum):"]
            lines += [f"    {member} = {number}" for member, number in values] or ["    pass"]
        if constants:
            lines += ["", "", "# Constants", ""]
            lines += [f"{name} = {value!r}" for name, value in constants]
        if interfaces:
            lines += ["", "", "# Interfaces"]
        for python, _ in interfaces:
            lines += ["", "", f"class {python}(bindings.Interface):", "    __slots__ = ()"]
        classes = [inner for built in records for inner in _flatten(built)]
        if classes:
            lines += ["", "", "# Records, given their members once every class exists"]
        for built in classes:
            lines += ["", "", f"# {built.label}", f"class {built.name}({built.base}):"]
            body = []
            if built.pack is not None:
                body += [f"    _pack_ = {built.pack}", '    _layout_ = "ms"']
            if built.anonymous:
                names = _tuple([_text(name) for name in built.anonymous])
                body.append(f"    _anonymous_ = {names}")
            lines += body or ["    pass"]
        if classes:
            lines.append("")
        for built in classes:
            lines += ["", f"{built.name}._fields_ = ("]
            for member, expr, comment in built.fields:
                lines.append(f"    ({_text(member)}, {expr}),{f'  # {comment}' if comment else ''}")
            lines.append(")")
        if late:
            lines += ["", "# Typedefs of records and pointers", ""]
            lines += [f"{alias} = {expr}" for alias, expr in late]
        lines += ["", "# Each record's size, alignment and members, as the compiler laid them out."]
        lines.append("LAYOUTS = {")
        for built in classes:
            size, align, places = built.layout
            lines.append(f"    {built.name}: ({size}, {align}, (")
            lines += [
                f"        ({_text(name)}, {offset}, {length})," for name, offset, length in places
            ]
            lines.append("    )),")
        lines += ["}", "bindings.check_declarations(TARGET, PROTOCOL, LAYOUTS)", ""]
        lines += [
            "# Each bound flat function's ctypes result and parameter types.",
            "FUNCTIONS = {",
        ]
        for name, (restype, argtypes, comment) in functions.items():
            lines.append(f"    # {comment}")
            entry = f"    {_text(name)}: ({restype}, {_tuple(argtypes) if argtypes else '()'}),"
            if len(entry) <= _WIDTH:
                lines.append(entry)
            else:
                lines += [f"    {_text(name)}: (", f"        {restype},", "        ("]
                lines += [f"            {argtype}," for argtype in argtypes]
                lines += ["        ),", "    ),"]
        lines += [
            "}",
            "",
            "# The arguments, by position with self as 0, that a function keeps in the record self",
            "# points to, so they must live as long as that record.",
            "RETAINED = {",
        ]
        lines += [
            f"    {_text(name)}: {_tuple(list(map(str, kept)))},"
            for name, kept in self.kept.items()
        ]
        lines += [
            "}",
            "",
            "# The ID Steam gives each callback record it delivers.",
            "CALLBACK_IDS = {",
        ]
        lines += [f"    {python}: {number}," for python, number in self._callback_ids()]
        lines += [
            "}",
            "",
            "# The record each asynchronous call completes with.",
            "CALL_RESULTS = {",
        ]
        lines += [f"    {_text(name)}: {python}," for name, python in self._call_results(functions)]
        lines += ["}", "", "# Flat functions left out, and why.", "SKIPPED = {"]
        for item in sorted(self.skipped, key=lambda skip: skip.name):
            if item.kind in ("function", "accessor", "export"):
                lines.append(f"    {_text(item.name)}: {_text(item.reason)},")
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _callback_ids(self) -> list[tuple[str, int]]:
        return [
            (self.classes[struct.name].name, struct.callback_id)
            for struct in sorted(self.metadata.structs, key=lambda item: item.name)
            if struct.callback_id is not None and struct.name in self.classes
        ]

    def _call_results(self, functions: Mapping[str, object]) -> list[tuple[str, str]]:
        found = [
            (method.flat_name, self.classes[method.callresult].name)
            for method in self.metadata.functions()
            if method.callresult in self.classes and method.flat_name in functions
        ]
        return sorted(found)


def _scoped(scope: str | None, name: str) -> str:
    return f"{scope}::{name}" if scope else name


def _text(value: str) -> str:
    # A JSON string with only ASCII is also a Python string literal, in double quotes.
    return json.dumps(value)


def _stored(value: int, layout: ir.EnumLayout) -> int:
    """``value`` as the target stores it in the enum's underlying type."""
    bits = layout.size * 8
    stored = value % (1 << bits)
    if layout.underlying in _SIGNED and stored >= 1 << (bits - 1):
        stored -= 1 << bits
    return stored


def _depends(built: _Class) -> set[str]:
    return built.depends | {name for inner in built.nested for name in _depends(inner)}


def _flatten(built: _Class) -> Iterator[_Class]:
    for inner in built.nested:
        yield from _flatten(inner)
    yield built


def _signature(returns: ir.CType, name: str, params: list[tuple[str, ir.CType | str]]) -> str:
    spelled = [
        f"{ctype}* {param}" if isinstance(ctype, str) else f"{ctype.spelling} {param}"
        for param, ctype in params
    ]
    return f"{returns.spelling} {name}( {', '.join(spelled)} )"
