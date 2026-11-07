"""Check extracted layouts against a native C++ compiler building the same headers.

The probe is a C++ program generated from one target's layouts. It includes the headers after the
compiler's own system headers, never codegen/abi/include, and prints what the compiler says about
each record, member, bit-field, enum and typedef. Only names and canonical types go into the
program; the sizes and offsets being checked stay here, so the compiler is never told the answer.
Members are named inside explicit instantiations, where C++ does not check access, so private
members are read without undefined behavior, and bit-fields are found by setting them and
comparing the object's bytes. Where the compiler has a builtin that clears padding, the bits it
leaves hold the record's value, which shows a member the extractor missed and where an enum
bit-field ends. C++ cannot name an anonymous record, so its size and alignment are derived from
what the compiler measured of its members and of the record holding it.

The probe has to run, so it checks only the target the compiler builds for, normally the host.
"""

import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from codegen import ir
from codegen.abi import FUNDAMENTAL, TARGETS
from codegen.abi.checks import (
    by_value_records,
    by_value_status,
    hazards,
    parse,
    sysv_in_memory,
)

COMPILE_TIMEOUT = 600.0
RUN_TIMEOUT = 60.0
# Rebuilds without the checks the compiler rejected, before giving up.
_RETRIES = 3
_HEADER = "pysteamsdk-probe 1"
_EXTENTS = 4
_QUALIFIED = re.compile(r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_TYPE = re.compile(r"(const )?([^*\[]+?)(\**)((?:\[\d+\])*)")
_REFERENCE = re.compile(r"probe\.cpp(?:\((\d+)[,)]|:(\d+):)")
_ERROR = re.compile(r"(?:^|[\s:])(?:fatal )?error\b[^:]*:\s*(.*)")
_WARNING = re.compile(r"(?:^|[\s:])warning\b[^:]*:")

Kind = Literal["record", "opaque", "field", "bits", "mask", "enum", "typedef"]
Group = Literal["record", "enum", "typedef"]
# How many values the probe prints for each kind of check; a mask prints 2 + 2 per run.
COLUMNS: dict[Kind, int | None] = {
    "record": 4,
    "opaque": 1,
    "field": 5 + _EXTENTS,
    "bits": 4,
    "mask": None,
    "enum": 3,
    "typedef": 4,
}
# The most runs of value bits a mask reports; more make the check impossible, not wrong.
_RUNS = 64
_MASKS = {
    0: "it is not trivially copyable and default-constructible, so setting its bytes is undefined",
    2: "this compiler has no builtin that clears padding, as Clang has none",
    3: f"its value bits form more than {_RUNS} runs",
}
# The C++ properties the probe reports for each record, as bits of one number, for the audit of
# records passed by value: none of them changes a layout.
FACTS = (
    "standard-layout",
    "trivially copyable",
    "aggregate",
    "trivially default-constructible",
    "trivially copy-constructible",
    "trivially move-constructible",
    "trivially copy-assignable",
    "trivially destructible",
)


class ProbeError(Exception):
    """The probe could not be built or run, or printed something unexpected."""


@dataclass(frozen=True)
class Compiler:
    """A C++ compiler: its resolved executable, then any flags to pass before the probe's own."""

    command: tuple[str, ...]

    @property
    def msvc_style(self) -> bool:
        return Path(self.command[0]).stem.lower() in ("cl", "clang-cl")


@dataclass(frozen=True)
class Check:
    kind: Kind
    group: Group
    owner: str
    label: str
    # What the probe must print, in the order COLUMNS counts; None where anything is accepted.
    expected: tuple[int | None, ...]
    # C++ that performs the check, with $ID where its row number goes.
    code: str
    # The canonical type the check expects, for messages.
    type: str = ""
    # For a mask, whose ``expected`` is the record's size in bits: the bits, from the record's
    # start, that extracted members cover, and each enum bit-field as (label, first bit, bit
    # where the next member starts, width).
    spans: tuple[tuple[int, int], ...] = ()
    widths: tuple[tuple[str, int, int, int], ...] = ()


@dataclass(frozen=True)
class Node:
    """A member, for working out an anonymous record's size from what the compiler measured.

    ``kind`` is "field" for a member checked under ``label``, "bits" for a bit-field, or
    "union" or "struct" for an anonymous record holding ``members``, which ``expected`` lays
    out as (offset, size, alignment) within the named record it belongs to.
    """

    label: str
    kind: Literal["field", "bits", "union", "struct"]
    expected: tuple[int, int, int] = (0, 0, 0)
    members: tuple["Node", ...] = ()


@dataclass(frozen=True)
class Context:
    """The members of a named record type that holds anonymous ones; ``label`` is its check's."""

    owner: str
    label: str
    members: tuple[Node, ...]
    union: bool


@dataclass(frozen=True)
class Gap:
    """Something the probe cannot check, and why. Never counted as a pass."""

    group: Group
    owner: str
    label: str
    reason: str


@dataclass(frozen=True)
class Failure:
    group: Group
    owner: str
    label: str
    message: str


@dataclass(frozen=True)
class Probe:
    target: str
    checks: tuple[Check, ...]
    gaps: tuple[Gap, ...]
    # Layouts the probe cannot even spell in C++, which is a failure.
    invalid: tuple[Failure, ...]
    contexts: tuple[Context, ...] = ()


@dataclass(frozen=True)
class Identity:
    compiler: str
    os: str
    arch: str
    pointer: int

    @property
    def target(self) -> str:
        return f"{self.os}-{self.arch}"


@dataclass(frozen=True)
class Report:
    target: str
    identity: Identity
    # The compiler, then every flag the probe was built with.
    command: tuple[str, ...]
    checks: tuple[Check, ...]
    failures: tuple[Failure, ...]
    gaps: tuple[Gap, ...]
    # Each record's FACTS bits, by name.
    facts: dict[str, int] = field(default_factory=dict)
    # Anonymous records whose size and alignment were derived and matched, with the values.
    derived: tuple[Gap, ...] = ()


def host_target() -> str | None:
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower()
    )
    name = f"{system}-{arch}"
    return name if name in TARGETS else None


def find_compiler(cxx: str | None = None, flags: Sequence[str] = ()) -> Compiler:
    """``cxx``, else $CXX, else cl on Windows and c++ elsewhere, found on PATH."""
    name = cxx or os.environ.get("CXX") or ("cl" if os.name == "nt" else "c++")
    path = shutil.which(name)
    if path is None:
        hint = "; run from a Visual Studio developer prompt" if Path(name).stem == "cl" else ""
        raise ProbeError(f"C++ compiler {name!r} not found{hint}; pass --cxx or set CXX")
    return Compiler((path, *flags))


def plan(layout: ir.TargetLayout, only: Iterable[str] | None = None) -> Probe:
    """The checks for ``layout``, or for the records, enums and typedefs named in ``only``."""
    planner = _Planner()
    wanted = None if only is None else set(only)
    for record in layout.records:
        if wanted is None or record.name in wanted:
            planner.record(record)
    for enum in layout.enums:
        if wanted is None or enum.name in wanted:
            planner.enum(enum)
    for typedef in layout.typedefs:
        if wanted is None or typedef.name in wanted:
            planner.typedef(typedef)
    return Probe(
        layout.target,
        tuple(planner.checks),
        tuple(planner.gaps),
        tuple(planner.invalid),
        tuple(planner.contexts),
    )


@dataclass
class _Scope:
    """A record type C++ can name: offsets are measured from its start, and its bytes are read."""

    owner: str
    spelled: str
    spans: list[tuple[int, int]] = field(default_factory=list)
    widths: list[tuple[str, int, int, int]] = field(default_factory=list)


class _Planner:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.gaps: list[Gap] = []
        self.invalid: list[Failure] = []
        self.contexts: list[Context] = []

    def record(self, record: ir.RecordLayout) -> None:
        name = record.name
        if not _QUALIFIED.fullmatch(name):
            self.invalid.append(Failure("record", name, name, "not a name C++ can spell"))
            return
        spelled = f"::{name}"
        if record.status == "opaque":
            self._add("opaque", "record", name, name, (0,), f"opaque<$ID, {spelled}>")
            return
        if record.size is None or record.align is None:
            self.gaps.append(Gap("record", name, name, "the layouts record no size for it"))
        else:
            expected = (record.size, record.align, int(record.kind == "union"), None)
            self._add("record", "record", name, name, expected, f"record<$ID, {spelled}>")
        if record.status == "unsupported":
            reason = f"its members are not laid out: {record.reason}"
            self.gaps.append(Gap("record", name, name, reason))
            return
        self._scope(name, record, spelled, name, name)

    def _scope(
        self, owner: str, record: ir.RecordLayout, spelled: str, label: str, check: str
    ) -> None:
        # ``check`` labels the record check that measures the type ``spelled`` names.
        scope = _Scope(owner, spelled)
        end = (record.size or 0) * 8
        nodes = self._members(scope, record.fields, label, 0, record.kind == "union", end)
        self._add(
            "mask",
            "record",
            owner,
            f"{label} value bits",
            (end,),
            f"mask<$ID, {spelled}>",
            spans=tuple(scope.spans),
            widths=tuple(scope.widths),
        )
        if any(node.kind in ("union", "struct") for node in nodes):
            self.contexts.append(Context(owner, check, tuple(nodes), record.kind == "union"))

    def _members(
        self,
        scope: _Scope,
        fields: Sequence[ir.FieldLayout],
        label: str,
        base: int,
        union: bool,
        end: int,
    ) -> list[Node]:
        # ``fields`` start ``base`` bytes into the scope's type and end before bit ``end``.
        starts = [_start(item, base) for item in fields]
        nodes = []
        for index, item in enumerate(fields):
            where = f"{label}.{item.name or f'(anonymous {item.type})'}"
            if item.name and not _IDENTIFIER.fullmatch(item.name):
                message = "not a name C++ can spell"
                self.invalid.append(Failure("record", scope.owner, where, message))
                continue
            if item.bit_width is not None and item.bit_offset is not None:
                first = base * 8 + item.bit_offset
                # In a union every member starts at once, so nothing bounds a bit-field.
                later = [start for start in starts[index + 1 :] if start and start > first]
                self._bits(scope, item, where, first, None if union else min(later, default=end))
                nodes.append(Node(where, "bits"))
                continue
            if item.offset is None:
                self.invalid.append(Failure("record", scope.owner, where, "it has no offset"))
                continue
            offset = base + item.offset
            inner = item.record
            if inner is not None and not item.name:
                kind: Literal["union", "struct"] = "union" if inner.kind == "union" else "struct"
                size = inner.size or 0
                inside = self._members(
                    scope, inner.fields, where, offset, kind == "union", (offset + size) * 8
                )
                nodes.append(Node(where, kind, (offset, size, inner.align or 0), tuple(inside)))
                continue
            spelled = _spell(item.type)
            if spelled is None:
                message = f"{item.type!r} is not a canonical type"
                self.invalid.append(Failure("record", scope.owner, where, message))
                continue
            dimensions = [int(number) for number in re.findall(r"\[(\d+)\]", item.type)]
            extents = (dimensions + [0] * _EXTENTS)[:_EXTENTS]
            expected: tuple[int | None, ...]
            expected = (offset, item.size, item.align, 1, len(dimensions), *extents)
            code = (
                f"field<$ID, offsetof({scope.spelled}, {item.name}), "
                f"decltype({scope.spelled}::{item.name}), {spelled}>"
            )
            self._add("field", "record", scope.owner, where, expected, code, item.type)
            scope.spans.append((offset * 8, (offset + item.size) * 8))
            nodes.append(Node(where, "field"))
            if inner is not None:
                # A named member of an unnamed record type, which decltype can name.
                named = f"std::remove_all_extents_t<decltype({scope.spelled}::{item.name})>"
                expected = (inner.size, inner.align, int(inner.kind == "union"), None)
                check = f"{where} (its type)"
                self._add("record", "record", scope.owner, check, expected, f"record<$ID, {named}>")
                self._scope(scope.owner, inner, named, where, check)
        return nodes

    def _bits(
        self, scope: _Scope, item: ir.FieldLayout, where: str, first: int, bound: int | None
    ) -> None:
        # ``first`` is the bit of the scope's type the bit-field starts at, and ``bound`` the
        # bit where the next member starts, or None in a union.
        assert item.bit_width is not None
        if not item.name:
            # An unnamed bit-field is padding: the mask reads it, and it places what follows.
            return
        parsed = parse(item.type)
        if parsed is None or parsed.pointers or parsed.count != 1:
            message = f"a bit-field of {item.type!r}"
            self.invalid.append(Failure("record", scope.owner, where, message))
            return
        if parsed.base.startswith("enum "):
            # Only values in an enum's range may be stored, so only its lowest bit is set; the
            # mask shows where its value bits end.
            fill, width = "static_cast<F>(1)", 1
            if bound is None:
                reason = "an enum bit-field in a union cannot be filled or bounded"
                self.gaps.append(Gap("record", scope.owner, f"{where} width", reason))
            else:
                scope.widths.append((where, first, bound, item.bit_width))
        elif parsed.base == "bool":
            fill, width = "true", 1
            if item.bit_width != 1:
                reason = "a bool bit-field holds one bit, so its width is not checked"
                self.gaps.append(Gap("record", scope.owner, f"{where} width", reason))
        else:
            fill, width = "static_cast<F>(-1)", item.bit_width
        scope.spans.append((first, first + item.bit_width))
        member = f"o.{item.name}"
        code = (
            "namespace pysteamsdk_probe {\n"
            "template <class T> struct bits_$ID { static const bool done; };\n"
            "template <class T> const bool bits_$ID<T>::done = bits<T>($ID, [](T &o, bool on) {\n"
            f"  using F = decltype({member}); {member} = on ? {fill} : F(); }});\n"
            "}\n"
            f"template struct pysteamsdk_probe::bits_$ID<{scope.spelled}>;"
        )
        expected = (1, first, width, 1)
        self._add("bits", "record", scope.owner, where, expected, code, item.type)

    def enum(self, enum: ir.EnumLayout) -> None:
        if not _QUALIFIED.fullmatch(enum.name) or enum.underlying not in FUNDAMENTAL:
            self.invalid.append(Failure("enum", enum.name, enum.name, "not a type C++ can spell"))
            return
        code = f"enumeration<$ID, ::{enum.name}, {enum.underlying}>"
        self._add("enum", "enum", enum.name, enum.name, (1, enum.size, 1), code, enum.underlying)

    def typedef(self, typedef: ir.TypedefLayout) -> None:
        spelled = _spell(typedef.type)
        if not _QUALIFIED.fullmatch(typedef.name) or spelled is None:
            message = "not a type C++ can spell"
            self.invalid.append(Failure("typedef", typedef.name, typedef.name, message))
            return
        complete = typedef.size is not None
        expected = (1, int(complete), typedef.size, typedef.align) if complete else (1, 0, -1, -1)
        code = f"alias<$ID, ::{typedef.name}, {spelled}>"
        self._add("typedef", "typedef", typedef.name, typedef.name, expected, code, typedef.type)

    def _add(
        self,
        kind: Kind,
        group: Group,
        owner: str,
        label: str,
        expected: tuple[int | None, ...],
        code: str,
        ctype: str = "",
        spans: tuple[tuple[int, int], ...] = (),
        widths: tuple[tuple[str, int, int, int], ...] = (),
    ) -> None:
        if not code.startswith("namespace"):
            code = f"template struct pysteamsdk_probe::{code};"
        self.checks.append(Check(kind, group, owner, label, expected, code, ctype, spans, widths))


def _start(item: ir.FieldLayout, base: int) -> int | None:
    """The bit a member starts at, ``base`` bytes into its scope."""
    if item.bit_offset is not None:
        return base * 8 + item.bit_offset
    return None if item.offset is None else (base + item.offset) * 8


def _spell(text: str) -> str | None:
    """A canonical layout type as a C++ type-id the probe's ``same`` trait compares against."""
    match = _TYPE.fullmatch(text)
    if parse(text) is None or match is None:
        return None
    const, base, stars, dimensions = match.groups()
    keyword, _, name = base.partition(" ")
    if base == "function":
        base = "pysteamsdk_probe::anyfn"
    elif base in ("struct", "class", "union"):
        base = "pysteamsdk_probe::anyunion" if base == "union" else "pysteamsdk_probe::anystruct"
    elif name and keyword in ("struct", "class", "union", "enum"):
        base = f"::{name}"
    return f"{const or ''}{base}{stars}{dimensions}"


_PRELUDE = """\
// Generated by pysteamsdk's codegen to check layouts against this compiler. Throwaway.
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <type_traits>
"""

_TEMPLATES = """\
namespace pysteamsdk_probe {

struct anyfn;
struct anystruct;
struct anyunion;

// Whether a member's type is the canonical type the layouts record, which keeps const only on
// the innermost pointee and spells an unnamed record type by its keyword.
template <class M, class E> struct same : std::is_same<M, E> {};
template <class M> struct same<M, anyfn> : std::is_function<M> {};
template <class M> struct same<M, anystruct> : std::is_class<M> {};
template <class M> struct same<M, anyunion> : std::is_union<M> {};
template <class M, class E> struct same<M *, E *>
    : same<typename std::conditional<std::is_pointer<typename std::remove_cv<M>::type>::value,
                                     typename std::remove_cv<M>::type, M>::type,
           E> {};
template <class M, class E, std::size_t N>
struct same<M[N], E[N]> : same<typename std::remove_cv<M>::type, E> {};
template <class M, class E> constexpr bool matches() {
  return same<typename std::remove_cv<M>::type, E>::value;
}

template <class T, class = void> struct complete : std::false_type {};
template <class T> struct complete<T, decltype(void(sizeof(T)))> : std::true_type {};
template <class T> constexpr long long size_of() {
  if constexpr (complete<T>::value) return sizeof(T); else return -1;
}
template <class T> constexpr long long align_of() {
  if constexpr (complete<T>::value) return alignof(T); else return -1;
}
template <class T, bool = std::is_enum<T>::value> struct underlying { using type = void; };
template <class T> struct underlying<T, true> {
  using type = typename std::underlying_type<T>::type;
};

template <class T> constexpr long long facts() {
  return (long long)std::is_standard_layout<T>::value
       | (long long)std::is_trivially_copyable<T>::value << 1
       | (long long)std::is_aggregate<T>::value << 2
       | (long long)std::is_trivially_default_constructible<T>::value << 3
       | (long long)std::is_trivially_copy_constructible<T>::value << 4
       | (long long)std::is_trivially_move_constructible<T>::value << 5
       | (long long)std::is_trivially_copy_assignable<T>::value << 6
       | (long long)std::is_trivially_destructible<T>::value << 7;
}

// Zero-initialized before any check runs; rows[id][0] counts the values a check stored.
constexpr int runs = $RUNS;
long long rows[$ROWS][2 * runs + 3];
template <class... V> bool put(int id, V... values) {
  long long *row = rows[id];
  int column = 0;
  ((row[++column] = static_cast<long long>(values)), ...);
  row[0] = column;
  return true;
}
bool put_row(int id, const long long *values, int count) {
  for (int column = 0; column < count; ++column) rows[id][column + 1] = values[column];
  rows[id][0] = count;
  return true;
}

// Explicit instantiations of these run each check during static initialization.
template <int Id, class T> struct record { static const bool done; };
template <int Id, class T>
const bool record<Id, T>::done =
    put(Id, sizeof(T), alignof(T), std::is_union<T>::value, facts<T>());

template <int Id, class T> struct opaque { static const bool done; };
template <int Id, class T> const bool opaque<Id, T>::done = put(Id, complete<T>::value);

template <int Id, std::size_t Offset, class M, class E> struct field { static const bool done; };
template <int Id, std::size_t Offset, class M, class E>
const bool field<Id, Offset, M, E>::done =
    put(Id, Offset, sizeof(M), alignof(M), matches<M, E>(), std::rank<M>::value,
        std::extent<M, 0>::value, std::extent<M, 1>::value, std::extent<M, 2>::value,
        std::extent<M, 3>::value);

template <int Id, class T, class E> struct enumeration { static const bool done; };
template <int Id, class T, class E>
const bool enumeration<Id, T, E>::done =
    put(Id, std::is_enum<T>::value, sizeof(T),
        std::is_same<typename underlying<T>::type, E>::value);

template <int Id, class T, class E> struct alias { static const bool done; };
template <int Id, class T, class E>
const bool alias<Id, T, E>::done =
    put(Id, matches<T, E>(), complete<T>::value, size_of<T>(), align_of<T>());

// Finds a bit-field from the bits that differ once set() has filled and then cleared it. Bit n
// is bit n % 8 of byte n / 8, counted from the least significant, as on little-endian targets.
template <class T, class Set> bool bits(int id, Set set) {
  if constexpr (std::is_default_constructible<T>::value && std::is_trivially_copyable<T>::value) {
    T object{};
    unsigned char filled[sizeof(T)], cleared[sizeof(T)];
    set(object, true);
    std::memcpy(filled, &object, sizeof(T));
    set(object, false);
    std::memcpy(cleared, &object, sizeof(T));
    long long first = -1, last = -1, count = 0;
    for (std::size_t bit = 0; bit < sizeof(T) * 8; ++bit) {
      if ((filled[bit / 8] ^ cleared[bit / 8]) >> (bit % 8) & 1) {
        if (first < 0) first = static_cast<long long>(bit);
        last = static_cast<long long>(bit);
        ++count;
      }
    }
    return put(id, 1, first, count, count > 0 && last - first + 1 == count);
  } else {
    return put(id, 0, -1, 0, 0);
  }
}

// The bits that hold T's value, as runs of [start, end): every byte is set, then the compiler's
// own builtin clears the padding. memset is defined only for trivially copyable types, and the
// copy assignment rules out const members. Clang has no such builtin.
#if defined(_MSC_VER) && !defined(__clang__)
#define PYSTEAMSDK_CLEAR_PADDING __builtin_zero_non_value_bits
#elif defined(__has_builtin)
#if __has_builtin(__builtin_clear_padding)
#define PYSTEAMSDK_CLEAR_PADDING __builtin_clear_padding
#endif
#endif
template <class T> bool value_bits(int id) {
#ifdef PYSTEAMSDK_CLEAR_PADDING
  if constexpr (std::is_trivially_copyable<T>::value &&
                std::is_trivially_copy_assignable<T>::value &&
                std::is_default_constructible<T>::value) {
    T object{};
    std::memset(&object, 0xff, sizeof(T));
    PYSTEAMSDK_CLEAR_PADDING(&object);
    unsigned char bytes[sizeof(T)];
    std::memcpy(bytes, &object, sizeof(T));
    long long row[2 * runs + 2] = {1, 0};
    long long start = -1;
    for (std::size_t bit = 0; bit <= sizeof(T) * 8; ++bit) {
      bool set = bit < sizeof(T) * 8 && (bytes[bit / 8] >> (bit % 8) & 1);
      if (set && start < 0) start = static_cast<long long>(bit);
      if (!set && start >= 0) {
        if (row[1] == runs) return put(id, 3, 0);
        row[2 + 2 * row[1]] = start;
        row[3 + 2 * row[1]] = static_cast<long long>(bit);
        ++row[1];
        start = -1;
      }
    }
    return put_row(id, row, static_cast<int>(2 + 2 * row[1]));
  } else {
    return put(id, 0, 0);
  }
#else
  return put(id, 2, 0);
#endif
}
// sizeof rejects an incomplete record here, where every compiler names the instantiation.
template <int Id, class T> struct mask { static const bool done; };
template <int Id, class T> const bool mask<Id, T>::done = sizeof(T) && value_bits<T>(Id);

}  // namespace pysteamsdk_probe
"""

_MAIN = """\
int main() {
  std::printf("pysteamsdk-probe 1\\n");
#if defined(__clang__) && defined(__apple_build_version__)
  std::printf("compiler Apple clang %s\\n", __clang_version__);
#elif defined(__clang__) && defined(_MSC_VER)
  std::printf("compiler clang %s (MSVC ABI)\\n", __clang_version__);
#elif defined(__clang__)
  std::printf("compiler clang %s\\n", __clang_version__);
#elif defined(_MSC_VER)
  std::printf("compiler MSVC %d.%d.%d\\n", _MSC_FULL_VER / 10000000,
              _MSC_FULL_VER / 100000 % 100, _MSC_FULL_VER % 100000);
#elif defined(__GNUC__)
  std::printf("compiler GCC %s\\n", __VERSION__);
#else
  std::printf("compiler unknown\\n");
#endif
#if defined(_WIN32)
  std::printf("os windows\\n");
#elif defined(__APPLE__)
  std::printf("os macos\\n");
#elif defined(__linux__)
  std::printf("os linux\\n");
#else
  std::printf("os unknown\\n");
#endif
#if defined(_M_ARM64EC)
  std::printf("arch arm64ec\\n");
#elif defined(_M_X64) || defined(__x86_64__)
  std::printf("arch x86_64\\n");
#elif defined(_M_ARM64) || defined(__aarch64__)
  std::printf("arch arm64\\n");
#else
  std::printf("arch unknown\\n");
#endif
  std::printf("pointer %d\\n", static_cast<int>(sizeof(void *)));
  for (int id = 0; id < $COUNT; ++id) {
    std::printf("%d", id);
    for (long long column = 1; column <= pysteamsdk_probe::rows[id][0]; ++column)
      std::printf(" %lld", pysteamsdk_probe::rows[id][column]);
    std::printf("\\n");
  }
  std::printf("end\\n");
}
"""


def source(include: str, checks: Sequence[Check]) -> tuple[str, dict[int, int]]:
    """The probe's C++ text, and the index into ``checks`` of each line that performs one."""
    lines = [*_PRELUDE.splitlines(), *include.splitlines()]
    templates = _TEMPLATES.replace("$ROWS", str(max(len(checks), 1)))
    lines += templates.replace("$RUNS", str(_RUNS)).splitlines()
    owners: dict[int, int] = {}
    for index, check in enumerate(checks):
        for line in check.code.replace("$ID", str(index)).splitlines():
            lines.append(line)
            owners[len(lines)] = index
    lines += _MAIN.replace("$COUNT", str(len(checks))).splitlines()
    return "\n".join(lines) + "\n", owners


def verify(
    layout: ir.TargetLayout,
    include: str,
    folders: Sequence[Path],
    compiler: Compiler,
    only: Iterable[str] | None = None,
    timeout: float = COMPILE_TIMEOUT,
) -> Report:
    """Build and run the probe for ``layout`` and compare what it prints with the layouts.

    ``include`` is C++ text that includes the headers, searched for in ``folders``. Raises
    ProbeError when the probe cannot be built or run, or was built for another target.
    """
    probe = plan(layout, only)
    checks = list(probe.checks)
    failures = list(probe.invalid)
    # The compiler runs in a temporary folder.
    folders = [Path(item).resolve() for item in folders]
    with tempfile.TemporaryDirectory(prefix="pysteamsdk-probe-", ignore_cleanup_errors=True) as tmp:
        folder = Path(tmp)
        for _ in range(_RETRIES + 1):
            text, owners = source(include, checks)
            built, output = _build(compiler, text, folders, folder, timeout)
            if built:
                break
            # A check naming something the compiler does not see fails, and the rest still run.
            rejected = rejections(output, owners)
            if not rejected:
                raise ProbeError(f"the probe does not compile:\n{_excerpt(output)}")
            for index, message in sorted(rejected.items()):
                check = checks[index]
                failure = f"the compiler rejects it: {message}"
                failures.append(Failure(check.group, check.owner, check.label, failure))
            checks = [check for index, check in enumerate(checks) if index not in rejected]
        else:
            raise ProbeError(f"the probe still does not compile:\n{_excerpt(output)}")
        result = _execute([str(_executable(folder))], folder, RUN_TIMEOUT, "running the probe")
    if result.returncode:
        message = f"the probe exited with status {result.returncode}"
        raise ProbeError(f"{message}:\n{_excerpt(result.stderr)}")
    identity, rows = parse_output(result.stdout, checks)
    if identity.target != layout.target:
        message = f"the compiler builds for {identity.target}, not {layout.target}"
        raise ProbeError(f"{message}; pick the target with --target or the compiler's flags")
    found, gaps, derived = compare(checks, rows, probe.contexts)
    facts = {
        check.owner: row[3]
        for check, row in zip(checks, rows, strict=True)
        if check.kind == "record" and check.label == check.owner
    }
    return Report(
        layout.target,
        identity,
        (*compiler.command, *_flags(compiler)),
        probe.checks,
        tuple([*failures, *found]),
        tuple([*probe.gaps, *gaps]),
        facts,
        tuple(derived),
    )


def _executable(folder: Path) -> Path:
    return folder / ("probe.exe" if os.name == "nt" else "probe")


def _flags(compiler: Compiler) -> tuple[str, ...]:
    if compiler.msvc_style:
        return ("/nologo", "/std:c++17", "/EHsc")
    return ("-std=c++17", "-Wno-invalid-offsetof")


def _build(
    compiler: Compiler, text: str, folders: Sequence[Path], folder: Path, timeout: float
) -> tuple[bool, str]:
    path = folder / "probe.cpp"
    path.write_text(text, encoding="utf-8", newline="\n")
    executable = _executable(folder)
    executable.unlink(missing_ok=True)
    if compiler.msvc_style:
        arguments = [
            *compiler.command,
            *_flags(compiler),
            *(f"/I{item}" for item in folders),
            f"/Fe{executable}",
            f"/Fo{folder / 'probe.obj'}",
            str(path),
        ]
    else:
        arguments = [
            *compiler.command,
            *_flags(compiler),
            *(f"-I{item}" for item in folders),
            *("-o", str(executable), str(path)),
        ]
    result = _execute(arguments, folder, timeout, "compiling the probe")
    return result.returncode == 0 and executable.is_file(), result.stdout + result.stderr


def _execute(
    arguments: Sequence[str], folder: Path, timeout: float, what: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            cwd=folder,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError(f"{what} timed out after {timeout:g} seconds") from None
    except OSError as exc:
        raise ProbeError(f"{what} failed: {exc}") from None


def _excerpt(output: str, lines: int = 40) -> str:
    found = output.strip().splitlines()
    more = f"\n... {len(found) - lines} more lines" if len(found) > lines else ""
    return "\n".join(found[:lines]) + more


def rejections(output: str, owners: dict[int, int]) -> dict[int, str]:
    """Which checks a failed build's errors point at, each with the nearest error's message.

    GCC names the line that instantiated a template before the error, MSVC and clang after it,
    so each line that mentions a check belongs to the closest diagnostic. A warning, such as
    clang's about offsetof on a non-standard-layout type, rejects nothing.
    """
    lines = output.splitlines()
    diagnostics = [
        index for index, line in enumerate(lines) if _ERROR.search(line) or _WARNING.search(line)
    ]
    found: dict[int, str] = {}
    for index, line in enumerate(lines):
        for match in _REFERENCE.finditer(line):
            check = owners.get(int(match.group(1) or match.group(2)))
            if check is None or check in found or not diagnostics:
                continue
            nearest = min(diagnostics, key=lambda other: (abs(other - index), other))
            error = _ERROR.search(lines[nearest])
            if error is not None:
                found[check] = error.group(1).strip()
    return found


def parse_output(text: str, checks: Sequence[Check]) -> tuple[Identity, list[tuple[int, ...]]]:
    """What the probe printed: who built it, then one row of values for each check."""
    lines = text.splitlines()
    if len(lines) < 5 or lines[0] != _HEADER:
        raise ProbeError(f"the probe printed {_excerpt(text, 5)!r}, not a probe report")
    fields = {}
    keys = ("compiler", "os", "arch", "pointer")
    for number, (line, key) in enumerate(zip(lines[1:5], keys, strict=True), 2):
        name, _, value = line.partition(" ")
        if name != key or not value.strip():
            raise ProbeError(f"line {number} of the probe's output should give its {key}")
        # __clang_version__ ends in a space on some builds.
        fields[key] = " ".join(value.split())
    if not fields["pointer"].isdigit():
        raise ProbeError("the probe gave no pointer size")
    identity = Identity(fields["compiler"], fields["os"], fields["arch"], int(fields["pointer"]))
    body = lines[5:]
    if len(body) != len(checks) + 1 or body[-1] != "end":
        raise ProbeError(
            f"the probe printed {len(body) - 1} rows for {len(checks)} checks, or stopped early"
        )
    rows = []
    for index, (line, check) in enumerate(zip(body[:-1], checks, strict=True)):
        parts = line.split()
        numbers = all(re.fullmatch(r"-?\d{1,20}", part) for part in parts)
        if not numbers or parts[:1] != [str(index)]:
            raise ProbeError(f"row {index} of the probe's output is {line!r}")
        values = tuple(int(part) for part in parts[1:])
        if not values:
            raise ProbeError(f"check {index} ({check.label}) never ran")
        columns = COLUMNS[check.kind]
        if columns is None:
            # A mask: its status, how many runs follow, then each run's start and end.
            columns = 2 + 2 * values[1] if len(values) > 1 and 0 <= values[1] <= _RUNS else -1
        if len(values) != columns:
            message = f"{len(values)} values for {check.label}"
            if columns >= 0:
                message += f", not {columns}"
            raise ProbeError(f"row {index} of the probe's output has {message}")
        rows.append(values)
    return identity, rows


def compare(
    checks: Sequence[Check],
    rows: Sequence[tuple[int, ...]],
    contexts: Sequence[Context] = (),
) -> tuple[list[Failure], list[Gap], list[Gap]]:
    """What differs from the layouts, what turned out uncheckable, and what was derived.

    Each anonymous record in ``contexts`` gets a size and alignment derived from what the
    compiler measured of its members and of the record holding it.
    """
    failures: list[Failure] = []
    gaps: list[Gap] = []
    for check, row in zip(checks, rows, strict=True):
        if check.kind == "mask":
            found, missing = _mask(check, row)
            failures += found
            gaps += missing
            continue
        problems = _problems(check, row)
        if problems is None:
            reason = "its record is not trivially copyable and default-constructible"
            gaps.append(Gap(check.group, check.owner, check.label, reason))
        failures += [Failure(check.group, check.owner, check.label, p) for p in problems or []]
    measured = {
        check.label: row
        for check, row in zip(checks, rows, strict=True)
        if check.kind in ("record", "field")
    }
    derived: list[Gap] = []
    for context in contexts:
        _derive(context, measured, failures, gaps, derived)
    return failures, gaps, derived


def _mask(check: Check, row: tuple[int, ...]) -> tuple[list[Failure], list[Gap]]:
    status, _, *values = row
    if status != 1:
        reason = _MASKS.get(status, f"the probe gave status {status}")
        gaps = []
        # Members that leave no byte uncovered leave no room for one the extractor missed.
        if _uncovered([(0, check.expected[0] or 0)], check.spans):
            message = f"no missing member is ruled out: {reason}"
            gaps.append(Gap(check.group, check.owner, check.label, message))
        gaps += [
            Gap(check.group, check.owner, f"{label} width", f"its width is not checked: {reason}")
            for label, *_ in check.widths
        ]
        return [], gaps
    runs = list(zip(values[::2], values[1::2], strict=True))
    failures = [
        Failure(
            check.group,
            check.owner,
            check.label,
            f"{_span(start, end)} hold a value that no extracted member covers, so a member is "
            "missing",
        )
        for start, end in _uncovered(runs, check.spans)
    ]
    for label, first, bound, width in check.widths:
        # An enum bit-field's value bits run from its first bit until padding or the next member.
        end = next((end for start, end in runs if start <= first < end), first)
        found = min(end, bound) - first
        if found != width:
            message = f"width: extracted {width}, compiler {found}"
            failures.append(Failure(check.group, check.owner, label, message))
    return failures, []


def _uncovered(
    runs: Iterable[tuple[int, int]], spans: Iterable[tuple[int, int]]
) -> list[tuple[int, int]]:
    covered = sorted(spans)
    found = []
    for start, end in runs:
        position = start
        for low, high in covered:
            if high <= position or low >= end:
                continue
            if low > position:
                found.append((position, low))
            position = max(position, high)
        if position < end:
            found.append((position, end))
    return found


def _span(start: int, end: int) -> str:
    if start % 8 == 0 and end % 8 == 0:
        return f"bytes {start // 8} to {end // 8}"
    return f"bits {start} to {end}"


def _derive(
    context: Context,
    measured: dict[str, tuple[int, ...]],
    failures: list[Failure],
    gaps: list[Gap],
    derived: list[Gap],
) -> None:
    # C++ cannot name an anonymous record, but its size and alignment follow from the members
    # the compiler measured. One #pragma pack value N covers the record and the anonymous record
    # inside it, so each is aligned to min(N, its most aligned member); the record's measured
    # alignment is that minimum, which makes the anonymous record's min(the record's alignment,
    # its most aligned member). An alignas inside would break this; then the record is more
    # aligned than any member, unless the alignas is smaller, when derivation errs and fails.
    row = measured.get(context.label)
    if row is None:
        return
    size, align = row[0], row[1]
    tops = [_measure(node, measured, align) for node in context.members]
    natural = [top[3] for top in tops if not isinstance(top, str)]
    if len(natural) == len(tops) and natural and align > max(natural):
        reason = "the record is more aligned than any member, so an alignment attribute is in play"
        for node in _anonymous(context.members):
            gaps.append(Gap("record", context.owner, node.label, reason))
        return

    def walk(
        nodes: Sequence[Node], measures: Sequence[_Measure | str], end: int, union: bool
    ) -> None:
        for index, (node, found) in enumerate(zip(nodes, measures, strict=True)):
            if node.kind not in ("union", "struct"):
                continue
            if isinstance(found, str):
                for each in (node, *_anonymous(node.members)):
                    gaps.append(Gap("record", context.owner, each.label, f"not derived: {found}"))
                continue
            offset, length, alignment, _ = found
            later = [m[0] for m in measures[index + 1 :] if not isinstance(m, str)]
            limit = end if union or not later else later[0]
            problems = [
                f"{name}: extracted {value}, derived {result}"
                for name, value, result in zip(
                    ("offset", "size", "alignment"), node.expected, found[:3], strict=True
                )
                if value != result
            ]
            if offset + length > limit:
                problems.append(f"it would run {offset + length - limit} bytes into what follows")
            for problem in problems:
                failures.append(Failure("record", context.owner, node.label, problem))
            if not problems:
                value = f"size {length}, alignment {alignment}"
                derived.append(Gap("record", context.owner, node.label, value))
            inside = [_measure(member, measured, align) for member in node.members]
            walk(node.members, inside, offset + length, node.kind == "union")

    walk(context.members, tops, size, context.union)


# A member's measured or derived offset, size and alignment, and its most aligned member's
# alignment, which for an anonymous record can exceed its own.
_Measure = tuple[int, int, int, int]


def _measure(node: Node, measured: dict[str, tuple[int, ...]], cap: int) -> _Measure | str:
    if node.kind == "field":
        row = measured.get(node.label)
        return (row[0], row[1], row[2], row[2]) if row else "a member's check did not run"
    if node.kind == "bits":
        return "it holds a bit-field, whose alignment C++ cannot measure"
    parts = [_measure(member, measured, cap) for member in node.members]
    reason = next((part for part in parts if isinstance(part, str)), None)
    if reason is not None:
        return reason
    found = [part for part in parts if not isinstance(part, str)]
    if not found:
        return "it has no members"
    offset = min(part[0] for part in found)
    if node.kind == "union" and any(part[0] != offset for part in found):
        return "the compiler puts its members at different offsets"
    natural = max(part[3] for part in found)
    alignment = min(cap, natural)
    extent = max(part[0] + part[1] for part in found) - offset
    return offset, -(-extent // alignment) * alignment, alignment, natural


def _anonymous(nodes: Iterable[Node]) -> list[Node]:
    found = []
    for node in nodes:
        if node.kind in ("union", "struct"):
            found += [node, *_anonymous(node.members)]
    return found


def _problems(check: Check, row: tuple[int, ...]) -> list[str] | None:
    expected = check.expected

    def differ(*names: str, start: int = 0) -> list[str]:
        return [
            f"{name}: extracted {expected[start + i]}, compiler {row[start + i]}"
            for i, name in enumerate(names)
            if expected[start + i] is not None and expected[start + i] != row[start + i]
        ]

    if check.kind == "record":
        problems = differ("size", "alignment")
        if expected[2] != row[2]:
            problems.append("the compiler says it is a union" if row[2] else "it is not a union")
        return problems
    if check.kind == "opaque":
        return ["the compiler sees it defined, so it is not opaque"] if row[0] else []
    if check.kind == "field":
        problems = differ("offset", "size", "alignment")
        if not row[3]:
            problems.append(f"the compiler's type is not {check.type}")
        if expected[4:] != row[4:]:
            problems.append(
                f"array dimensions: extracted {_dimensions(expected[4:])}, "
                f"compiler {_dimensions(row[4:])}"
            )
        return problems
    if check.kind == "bits":
        if not row[0]:
            return None
        problems = differ("bit offset", "width", start=1)
        if not row[3] and row[2]:
            problems.append("the compiler stores it in bits that are not contiguous")
        return problems
    if check.kind == "enum":
        if not row[0]:
            return ["the compiler says it is not an enum"]
        problems = differ("size", start=1)
        if not row[2]:
            problems.append(f"the compiler's underlying type is not {check.type}")
        return problems
    problems = [] if row[0] else [f"the compiler's type is not {check.type}"]
    if expected[1] != row[1]:
        problems.append("the compiler sees it complete" if row[1] else "it is incomplete")
    elif row[1]:
        problems += differ("size", "alignment", start=2)
    return problems


def _dimensions(values: Sequence[int | None]) -> str:
    rank, *extents = values
    return "".join(f"[{extent}]" for extent in extents[: rank or 0]) or "none"


def statuses(report: Report, layout: ir.TargetLayout) -> dict[Group, dict[str, str]]:
    """How each record, enum and typedef of ``layout`` fared."""
    checked: set[tuple[str, str]] = {(check.group, check.owner) for check in report.checks}
    failed = {(item.group, item.owner) for item in report.failures}
    partial = {(item.group, item.owner) for item in report.gaps}
    derived = {(item.group, item.owner) for item in report.derived}
    records = {item.name: item for item in layout.records}

    def status(group: Group, name: str) -> str:
        key = (group, name)
        if key in failed:
            return "mismatched"
        if key not in checked:
            return "not checked"
        record = records.get(name) if group == "record" else None
        if record is not None and record.status != "complete":
            return record.status
        if key in partial:
            return "partly verified"
        return "derived" if key in derived else "verified"

    return {
        "record": {item.name: status("record", item.name) for item in layout.records},
        "enum": {item.name: status("enum", item.name) for item in layout.enums},
        "typedef": {item.name: status("typedef", item.name) for item in layout.typedefs},
    }


_ORDER = (
    "verified",
    "derived",
    "partly verified",
    "opaque",
    "unsupported",
    "not checked",
    "mismatched",
)


def render(report: Report, layout: ir.TargetLayout, metadata: ir.Metadata | None = None) -> str:
    identity = report.identity
    lines = [
        f"Layouts for {report.target} checked by {identity.compiler}",
        f"  compiler        {report.command[0]}",
        f"  flags           {' '.join(report.command[1:]) or 'none'}",
        f"  target          {identity.os} {identity.arch}, {identity.pointer}-byte pointers",
    ]
    for group, found in statuses(report, layout).items():
        counts = Counter(found.values())
        detail = ", ".join(f"{counts[name]} {name}" for name in _ORDER if counts[name])
        lines.append(f"  {group + 's':14} {len(found):5}  {detail or 'none'}")
    failing = {(item.group, item.owner, item.label) for item in report.failures}
    total = len({(c.group, c.owner, c.label) for c in report.checks} | failing)
    lines.append(
        f"  {'checks':14} {total:5}  {total - len(failing)} passed, {len(failing)} failed, "
        f"{len(report.gaps)} not possible"
    )
    if report.derived:
        lines.append(
            "Derived from what the compiler measured of the members and the record holding them"
        )
        lines += [f"  {item.group} {item.label}: {item.reason}" for item in report.derived]
    if report.gaps:
        lines.append("Not possible to check")
        lines += [f"  {item.group} {item.label}: {item.reason}" for item in report.gaps]
    if report.failures:
        lines.append("Mismatches")
        lines += [f"  {item.group} {item.label}: {item.message}" for item in report.failures]
    if metadata is not None:
        lines += by_value(report, layout, metadata)
    return "\n".join(lines) + "\n"


def by_value(report: Report, layout: ir.TargetLayout, metadata: ir.Metadata) -> list[str]:
    """What the compiler says about each record the flat API passes or returns by value.

    Nothing here checks a calling convention; it lists what one depends on.
    """
    records = {item.name: item for item in layout.records}
    uses: dict[str, dict[str, list[str]]] = {}
    for function, verb, name in by_value_records(metadata):
        uses.setdefault(name, {}).setdefault(verb, []).append(function.flat_name)
    if not uses:
        return []
    lines = ["Records passed or returned by value (calling conventions are not checked)"]
    for name in sorted(uses):
        record, facts = records.get(name), report.facts.get(name)
        if record is None or record.size is None or record.align is None or facts is None:
            lines.append(f"  {name}: no checked layout")
            continue
        found = sorted(hazards(record, records)) if record.status == "complete" else []
        lacks = [label for bit, label in enumerate(FACTS) if not facts >> bit & 1]
        lines += [
            f"  {name}: {record.size} bytes, aligned to {record.align}",
            f"    layout has {', '.join(found) or 'no union, bit-field or packing'}",
            f"    C++ type is not {', '.join(lacks)}"
            if lacks
            else "    C++ type is a trivially copyable standard-layout aggregate",
            *(
                f"    {verb}: {', '.join(sorted(set(functions)))}"
                for verb, functions in sorted(uses[name].items())
            ),
            *(f"    risk: {risk}" for risk in risks(report.target, record, facts, uses[name])),
            f"    reviewed: {by_value_status(metadata, name, report.target)} on {report.target}",
        ]
    return lines


def risks(target: str, record: ir.RecordLayout, facts: int, uses: Iterable[str]) -> list[str]:
    """Where a native ABI passes a record differently from how ctypes would.

    ``uses`` holds "takes" and "returns" as the flat API uses the record. An empty list does not
    make a by-value call safe; only a native call test does.
    """
    size = record.size or 0
    has = {label for bit, label in enumerate(FACTS) if facts >> bit & 1}
    calls = {
        "trivially copy-constructible",
        "trivially move-constructible",
        "trivially destructible",
    }
    # MSVC x64 returns a 1, 2, 4 or 8-byte record in RAX only if it is a C++14 aggregate with a
    # trivial copy assignment, destructor and default constructor; C returns any such struct so.
    msvc = {
        "aggregate",
        "trivially copy-assignable",
        "trivially destructible",
        "trivially default-constructible",
    }
    if not calls <= has:
        return ["not trivial for calls, so C++ passes and returns it through a hidden pointer"]
    windows = target.startswith("windows") and "returns" in uses and size in (1, 2, 4, 8)
    if windows and not msvc <= has:
        return ["MSVC returns it through a hidden pointer, where C would use RAX"]
    if sysv_in_memory(target, record):
        return ["a misaligned member makes SysV pass it in memory, but ctypes uses registers"]
    return []
