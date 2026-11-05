"""Compile the SDK headers for each target with libclang and read off the layouts.

Each target parses the headers once with function bodies skipped. The only system headers on the
include path are the few in include/, which define the standard types from the compiler's own
macros for the target, so no host header is read and every host computes the same layouts. A
compiler error fails the target: layouts are never read from code the compiler rejected.
"""

import contextlib
import ctypes
import functools
import importlib.metadata
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePath
from typing import Any

from clang import cindex
from clang.cindex import CursorKind, TypeKind

from codegen import ir
from codegen.abi import TARGETS, Roots

INCLUDE = Path(__file__).resolve().parent / "include"
# Never written to disk; libclang reads it from memory.
_MAIN = str(INCLUDE.parent / "layouts.cpp")

_RECORDS: dict[Any, ir.RecordKind] = {
    CursorKind.STRUCT_DECL: "struct",
    CursorKind.UNION_DECL: "union",
    CursorKind.CLASS_DECL: "class",
}
_FUNDAMENTAL = {
    TypeKind.BOOL: "bool",
    TypeKind.CHAR_S: "char",
    TypeKind.CHAR_U: "char",
    TypeKind.SCHAR: "signed char",
    TypeKind.UCHAR: "unsigned char",
    TypeKind.SHORT: "short",
    TypeKind.USHORT: "unsigned short",
    TypeKind.INT: "int",
    TypeKind.UINT: "unsigned int",
    TypeKind.LONG: "long",
    TypeKind.ULONG: "unsigned long",
    TypeKind.LONGLONG: "long long",
    TypeKind.ULONGLONG: "unsigned long long",
    TypeKind.FLOAT: "float",
    TypeKind.DOUBLE: "double",
    TypeKind.LONGDOUBLE: "long double",
    TypeKind.WCHAR: "wchar_t",
    TypeKind.CHAR16: "char16_t",
    TypeKind.CHAR32: "char32_t",
}
_FUNCTIONS = {TypeKind.FUNCTIONPROTO, TypeKind.FUNCTIONNOPROTO}


class LayoutError(Exception):
    """libclang is not installed, or a target is not one of TARGETS."""


def version() -> str:
    try:
        return importlib.metadata.version("libclang")
    except importlib.metadata.PackageNotFoundError:
        raise LayoutError("the libclang package is not installed") from None


def lay_out(
    source: str, include: Sequence[Path], roots: Roots, targets: Iterable[str]
) -> tuple[ir.Abi, list[ir.Issue]]:
    """Compile ``source``, C++ text that includes the headers, once for each target.

    ``include`` lists the folders the headers are searched in. A name a target does not declare
    is left out, for codegen.validate to report against the metadata that needs it.
    """
    clang = version()
    index = cindex.Index.create()
    layouts: list[ir.TargetLayout] = []
    issues: list[ir.Issue] = []
    wanted = set(targets)
    if wanted - TARGETS.keys():
        raise LayoutError(f"unknown targets: {', '.join(sorted(wanted - TARGETS.keys()))}")
    # In TARGETS order, so the order they were asked for in cannot change the output.
    for name in [name for name in TARGETS if name in wanted]:
        target = TARGETS[name]
        arguments = [
            *("-x", "c++", "-std=c++17", "-nostdinc", "-isystem", str(INCLUDE)),
            *(f"-I{folder}" for folder in include),
            *("-target", target.triple, *target.arguments),
        ]
        try:
            unit = index.parse(
                _MAIN,
                args=arguments,
                unsaved_files=[(_MAIN, source)],
                options=cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES,
            )
        except cindex.TranslationUnitLoadError as exc:
            issues.append(ir.Issue("error", "compile-error", f"{name}: {exc}", "libclang"))
            continue
        # Several diagnostics on one line read the same once the column is dropped.
        found = list(dict.fromkeys(i for d in unit.diagnostics if (i := _issue(name, d))))
        issues += found
        if any(issue.severity == "error" for issue in found):
            continue
        layouts.append(_Unit(unit).lay_out(name, target.triple, roots))
    return ir.Abi(clang, tuple(layouts)), issues


def _issue(target: str, diagnostic: Any) -> ir.Issue | None:
    if diagnostic.severity < cindex.Diagnostic.Warning:
        return None
    location = diagnostic.location
    source = f"{PurePath(location.file.name).name}:{location.line}" if location.file else "libclang"
    if diagnostic.severity >= cindex.Diagnostic.Error:
        return ir.Issue("error", "compile-error", f"{target}: {diagnostic.spelling}", source)
    return ir.Issue("note", "compile-warning", f"{target}: {diagnostic.spelling}", source)


class _Unsupported(Exception):
    pass


class _Unit:
    def __init__(self, unit: Any) -> None:
        self.records: dict[str, Any] = {}
        self.enums: dict[str, Any] = {}
        self.typedefs: dict[str, Any] = {}
        self._index(unit.cursor)
        self.pending_records: list[str] = []
        self.pending_enums: list[str] = []

    def _index(self, scope: Any) -> None:
        for child in scope.get_children():
            if child.kind == CursorKind.LINKAGE_SPEC or (
                child.kind == CursorKind.NAMESPACE and not child.is_anonymous()
            ):
                self._index(child)
            elif child.kind in _RECORDS and not child.is_anonymous():
                # A definition wins over the forward declarations around it.
                name = _qualified(child)
                if child.is_definition():
                    self.records[name] = child
                    self._index(child)
                else:
                    self.records.setdefault(name, child)
            elif child.kind == CursorKind.ENUM_DECL and not child.is_anonymous():
                self.enums.setdefault(_qualified(child), child)
            elif child.kind in (CursorKind.TYPEDEF_DECL, CursorKind.TYPE_ALIAS_DECL):
                self.typedefs.setdefault(_qualified(child), child)

    def lay_out(self, target: str, triple: str, roots: Roots) -> ir.TargetLayout:
        # A typedef or enum that cannot be described is left out, so codegen.validate reports
        # it as having no layout instead of anything being guessed.
        typedefs: dict[str, ir.TypedefLayout] = {}
        for name in roots.typedefs:
            if name in self.typedefs:
                with contextlib.suppress(_Unsupported):
                    typedefs[name] = self._typedef(name, self.typedefs[name])
        records: dict[str, ir.RecordLayout] = {}
        self.pending_records += roots.records
        while self.pending_records:
            name = self.pending_records.pop()
            if name not in records and name in self.records:
                records[name] = self._record(self.records[name], name)
        enums: dict[str, ir.EnumLayout] = {}
        for name in {*roots.enums, *self.pending_enums}:
            if name not in self.enums:
                continue
            cursor = self.enums[name]
            kind = cursor.enum_type.get_canonical().kind
            if kind in _FUNDAMENTAL:
                size = int(cursor.type.get_size())
                enums[name] = ir.EnumLayout(name, _FUNDAMENTAL[kind], size)
        return ir.TargetLayout(
            target,
            triple,
            tuple(records[name] for name in sorted(records)),
            tuple(enums[name] for name in sorted(enums)),
            tuple(typedefs[name] for name in sorted(typedefs)),
        )

    def _record(self, cursor: Any, name: str) -> ir.RecordLayout:
        kind, source = _RECORDS[cursor.kind], _source(cursor)
        size, align = _known(cursor.type.get_size()), _known(cursor.type.get_align())
        if size is None or align is None:
            return ir.RecordLayout(name, kind, "opaque", source)
        try:
            for child in cursor.get_children():
                if child.kind == CursorKind.CXX_BASE_SPECIFIER:
                    raise _Unsupported("it has base classes")
                if child.kind in (CursorKind.CXX_METHOD, CursorKind.DESTRUCTOR) and (
                    child.is_virtual_method()
                ):
                    raise _Unsupported("it has virtual functions")
            fields = tuple(self._field(item) for item in cursor.type.get_fields())
        except _Unsupported as exc:
            return ir.RecordLayout(name, kind, "unsupported", source, size, align, reason=str(exc))
        return ir.RecordLayout(name, kind, "complete", source, size, align, fields)

    def _field(self, cursor: Any) -> ir.FieldLayout:
        ctype = cursor.type
        size, align = _known(ctype.get_size()), _known(ctype.get_align())
        offset = _known(cursor.get_field_offsetof())
        if size is None or align is None or offset is None:
            raise _Unsupported(f"{cursor.spelling or 'a member'} has no layout")
        anonymous = _is_anonymous_member(ctype.get_canonical().get_declaration())
        name = "" if anonymous else str(cursor.spelling)
        description, record = self._describe(ctype, name or "an anonymous member")
        if cursor.is_bitfield():
            width = int(cursor.get_bitfield_width())
            return ir.FieldLayout(
                name, description, size, align, bit_offset=offset, bit_width=width
            )
        if offset % 8:
            raise _Unsupported(f"{name} does not start on a byte")
        return ir.FieldLayout(name, description, size, align, offset=offset // 8, record=record)

    def _typedef(self, name: str, cursor: Any) -> ir.TypedefLayout:
        description, record = self._describe(cursor.underlying_typedef_type, name)
        if record is not None:
            raise _Unsupported(f"{name} names a record without a name of its own")
        ctype = cursor.type
        return ir.TypedefLayout(
            name, description, _known(ctype.get_size()), _known(ctype.get_align())
        )

    def _describe(self, ctype: Any, owner: str) -> tuple[str, ir.RecordLayout | None]:
        """The canonical spelling of ``ctype``, and the layout of an anonymous record it is."""
        canonical = ctype.get_canonical()
        dimensions = ""
        while canonical.kind == TypeKind.CONSTANTARRAY:
            dimensions += f"[{canonical.element_count}]"
            canonical = canonical.element_type.get_canonical()
        if canonical.kind == TypeKind.POINTER:
            return self._pointer(canonical, owner) + dimensions, None
        if canonical.kind in _FUNDAMENTAL:
            return _FUNDAMENTAL[canonical.kind] + dimensions, None
        declaration = canonical.get_declaration()
        if canonical.kind == TypeKind.ENUM and not declaration.is_anonymous():
            name = _qualified(declaration)
            self.pending_enums.append(name)
            return f"enum {name}{dimensions}", None
        if canonical.kind == TypeKind.RECORD:
            keyword = _RECORDS[declaration.kind]
            if declaration.is_anonymous():
                record = self._record(declaration, "")
                if record.status != "complete":
                    raise _Unsupported(f"{owner}: {record.reason or 'it is incomplete'}")
                return keyword + dimensions, record
            name = _qualified(declaration)
            self.pending_records.append(name)
            return f"{keyword} {name}{dimensions}", None
        raise _Unsupported(f"{owner} has a {canonical.kind.spelling} type")

    def _pointer(self, canonical: Any, owner: str) -> str:
        stars = ""
        while canonical.kind == TypeKind.POINTER:
            stars += "*"
            canonical = canonical.get_pointee()
        const = "const " if canonical.is_const_qualified() else ""
        kind = canonical.kind
        if kind == TypeKind.VOID:
            return f"{const}void{stars}"
        if kind in _FUNCTIONS:
            return f"function{stars}"
        if kind in _FUNDAMENTAL:
            return f"{const}{_FUNDAMENTAL[kind]}{stars}"
        declaration = canonical.get_declaration()
        if kind in (TypeKind.RECORD, TypeKind.ENUM) and not declaration.is_anonymous():
            keyword = "enum" if kind == TypeKind.ENUM else _RECORDS[declaration.kind]
            return f"{const}{keyword} {_qualified(declaration)}{stars}"
        raise _Unsupported(f"{owner} points to a {kind.spelling} type")


def _qualified(cursor: Any) -> str:
    parts = []
    while cursor is not None and cursor.kind != CursorKind.TRANSLATION_UNIT:
        if cursor.kind != CursorKind.LINKAGE_SPEC:
            if cursor.is_anonymous():
                raise _Unsupported("it uses a type declared inside an anonymous scope")
            parts.append(str(cursor.spelling))
        cursor = cursor.semantic_parent
    return "::".join(reversed(parts))


def _known(value: int) -> int | None:
    # libclang reports an unknown size, alignment or offset as a negative error code.
    return int(value) if value >= 0 else None


def _source(cursor: Any) -> str:
    location = cursor.location
    return f"{PurePath(location.file.name).name}:{location.line}" if location.file else ""


def _is_anonymous_member(declaration: Any) -> bool:
    # True only for an anonymous struct or union member, not for the unnamed type of a named
    # member, unlike Cursor.is_anonymous.
    return declaration.kind in _RECORDS and bool(_anonymous_record_decl()(declaration))


@functools.cache
def _anonymous_record_decl() -> Any:
    # The bindings do not wrap this libclang function, so its C signature is declared here.
    function = cindex.conf.lib.clang_Cursor_isAnonymousRecordDecl
    function.argtypes, function.restype = [cindex.Cursor], ctypes.c_uint
    return function
