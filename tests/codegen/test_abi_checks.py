from collections.abc import Callable
from dataclasses import replace

import pytest

from codegen import abi, ir
from codegen.abi.checks import LayoutType, by_value_status, parse
from codegen.cdecl import parse_type
from codegen.validate import validate

LINUX = "linux-x86_64"
Change = Callable[[ir.Metadata], ir.Metadata]


def target(metadata: ir.Metadata, name: str = LINUX) -> ir.TargetLayout:
    assert metadata.abi is not None
    (found,) = [item for item in metadata.abi.targets if item.target == name]
    return found


def with_targets(metadata: ir.Metadata, *targets: ir.TargetLayout) -> ir.Metadata:
    assert metadata.abi is not None
    return replace(metadata, abi=replace(metadata.abi, targets=targets))


def change_target(metadata: ir.Metadata, name: str = LINUX, **changes: object) -> ir.Metadata:
    assert metadata.abi is not None
    targets = tuple(
        replace(item, **changes) if item.target == name else item  # type: ignore[arg-type]
        for item in metadata.abi.targets
    )
    return with_targets(metadata, *targets)


def record(metadata: ir.Metadata, name: str, target_name: str = LINUX) -> ir.RecordLayout:
    (found,) = [item for item in target(metadata, target_name).records if item.name == name]
    return found


def change_record(
    metadata: ir.Metadata, record_name: str, target_name: str = LINUX, /, **changes: object
) -> ir.Metadata:
    records = tuple(
        replace(item, **changes) if item.name == record_name else item  # type: ignore[arg-type]
        for item in target(metadata, target_name).records
    )
    return change_target(metadata, target_name, records=records)


def change_field(metadata: ir.Metadata, index: int, **changes: object) -> ir.Metadata:
    """Change one member of WidgetPos_t, laid out as m_flX, m_flY, m_rgubColor in 12 bytes."""
    fields = list(record(metadata, "WidgetPos_t").fields)
    fields[index] = replace(fields[index], **changes)  # type: ignore[arg-type]
    return change_record(metadata, "WidgetPos_t", fields=tuple(fields))


def everywhere(
    metadata: ir.Metadata, change: Callable[[ir.Metadata, str], ir.Metadata]
) -> ir.Metadata:
    assert metadata.abi is not None
    for item in metadata.abi.targets:
        metadata = change(metadata, item.target)
    return metadata


def found(metadata: ir.Metadata) -> list[tuple[str, str, str]]:
    return [(issue.severity, issue.code, issue.message) for issue in validate(metadata)]


UNION = ir.RecordLayout(
    "",
    "union",
    "complete",
    "x.h:1",
    4,
    1,
    (ir.FieldLayout("a", "unsigned char[4]", 4, 1, offset=0),),
)


def test_the_fixture_and_its_layouts_check_out(widgets: ir.Metadata) -> None:
    assert widgets.abi is not None
    assert [item.target for item in widgets.abi.targets] == ["windows-x86_64", LINUX]
    assert validate(widgets) == []


@pytest.mark.parametrize(
    ("change", "code", "message"),
    [
        (
            lambda m: replace(m, abi=replace(m.abi, clang=" ")),
            "invalid-layout",
            "no libclang version",
        ),
        (
            lambda m: with_targets(m, *m.abi.targets, target(m)),
            "duplicate-layout",
            "linux-x86_64: laid out 2 times",
        ),
        (
            lambda m: change_target(m, target="vax-pdp11"),
            "unknown-target",
            "vax-pdp11: not a target codegen.abi knows",
        ),
        (
            lambda m: change_target(m, triple="x86_64-unknown-linux-musl"),
            "unknown-target",
            "compiled for x86_64-unknown-linux-musl, not x86_64-pc-linux-gnu",
        ),
        (
            lambda m: change_target(m, records=(*target(m).records, record(m, "WidgetPos_t"))),
            "duplicate-layout",
            "record WidgetPos_t is laid out 2 times",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", name=""),
            "invalid-layout",
            "a record laid out on its own needs a name",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", size=13),
            "invalid-layout",
            "size 13 is not a multiple",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", align=3),
            "invalid-layout",
            "alignment 3 is not a power of two",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", size=None),
            "invalid-layout",
            "a complete record needs a size and an alignment",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", reason="why"),
            "invalid-layout",
            "a complete record gives no reason",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", status="opaque"),
            "invalid-layout",
            "an opaque record has no size, alignment, members or reason",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", status="unsupported"),
            "invalid-layout",
            "an unsupported record must give a reason",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", status="unsupported", reason="why"),
            "invalid-layout",
            "an unsupported record lists no members",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", kind="union"),
            "invalid-layout",
            "m_flY: a union member starts at the union's beginning",
        ),
        (
            lambda m: change_field(m, 1, offset=2),
            "invalid-layout",
            "m_flY: overlaps the member before it",
        ),
        (
            lambda m: change_field(m, 2, offset=10),
            "invalid-layout",
            "bytes 10 to 14 lie outside the record",
        ),
        (lambda m: change_field(m, 2, offset=None), "invalid-layout", "needs a byte offset"),
        (lambda m: change_field(m, 0, align=3), "invalid-layout", "alignment 3 is impossible"),
        (
            lambda m: change_field(m, 2, type="uint8[4]"),
            "invalid-layout",
            "'uint8[4]' is not a canonical layout type",
        ),
        (
            lambda m: change_field(m, 0, type="struct Missing_t"),
            "invalid-layout",
            "Missing_t is used by value but has no layout",
        ),
        (
            lambda m: change_field(m, 0, type="enum EMissing"),
            "invalid-layout",
            "enum EMissing has no layout",
        ),
        (
            lambda m: change_field(m, 0, type="class WidgetReady_t"),
            "invalid-layout",
            "WidgetReady_t is a struct, not a class",
        ),
        (
            lambda m: change_field(m, 1, bit_offset=32, bit_width=3),
            "invalid-layout",
            "a bit-field has a bit offset instead of an offset",
        ),
        (
            lambda m: change_field(m, 1, offset=None, bit_offset=32, bit_width=40),
            "invalid-layout",
            "40 bits do not fit a 4-byte type",
        ),
        (
            lambda m: change_field(m, 1, offset=None, bit_offset=120, bit_width=30),
            "invalid-layout",
            "m_flY: lies outside the record",
        ),
        (
            lambda m: change_field(m, 1, offset=None, bit_offset=32, bit_width=3),
            "invalid-layout",
            "a bit-field cannot be float",
        ),
        (
            lambda m: change_field(m, 2, type="union"),
            "invalid-layout",
            "an anonymous record type needs its layout described in place",
        ),
        (
            lambda m: change_field(m, 2, type="union", record=replace(UNION, size=8)),
            "invalid-layout",
            "its size or alignment differs from the record described in place",
        ),
        (
            lambda m: change_field(m, 2, type="union", record=replace(UNION, name="U")),
            "invalid-layout",
            "the record described in place must be an unnamed union",
        ),
        (
            lambda m: change_field(m, 2, name=""),
            "invalid-layout",
            "only an anonymous record or a bit-field can have no name",
        ),
        (
            lambda m: change_target(m, enums=(replace(target(m).enums[0], underlying="float"),)),
            "invalid-layout",
            "enum EWidgetResult cannot be stored as a 4-byte float",
        ),
        (
            lambda m: change_target(m, enums=(*target(m).enums, *target(m).enums)),
            "duplicate-layout",
            "enum EWidgetResult is laid out 2 times",
        ),
        (
            lambda m: change_target(
                m, typedefs=(replace(target(m).typedefs[0], type="struct Missing_t"),)
            ),
            "invalid-layout",
            "typedef CallID_t: Missing_t is used by value but has no layout",
        ),
        (
            lambda m: change_target(m, typedefs=(replace(target(m).typedefs[0], align=None),)),
            "invalid-layout",
            "typedef CallID_t: needs both a size and an alignment, or neither",
        ),
        (
            lambda m: change_target(m, typedefs=(replace(target(m).typedefs[0], size=12),)),
            "invalid-layout",
            "typedef CallID_t: size 12 is not a multiple of the alignment",
        ),
        (
            lambda m: change_record(m, "WidgetPos_t", size=0),
            "invalid-layout",
            "WidgetPos_t: size 0 is impossible",
        ),
        (
            lambda m: change_field(
                m, 1, offset=None, type="unsigned int", bit_offset=32, bit_width=0
            ),
            "invalid-layout",
            "m_flY: a zero-width bit-field has no name",
        ),
        # Every use of a type agrees on its size and alignment.
        (
            lambda m: change_field(m, 0, size=8),
            "invalid-layout",
            "WidgetPos_t.m_flY: float is 4 bytes aligned to 4 here but 8 bytes aligned to 4 in "
            "WidgetPos_t.m_flX",
        ),
        (
            lambda m: change_field(m, 2, size=6),
            "invalid-layout",
            "WidgetPos_t.m_rgubColor: 6 bytes cannot hold 4 equal elements",
        ),
        (
            lambda m: change_field(
                change_target(
                    m,
                    typedefs=(
                        replace(target(m).typedefs[0], type="function*"),
                        *target(m).typedefs[1:],
                    ),
                ),
                0,
                type="void*",
            ),
            "invalid-layout",
            "WidgetPos_t.m_flX: a pointer is 4 bytes aligned to 4 here but 8 bytes aligned to 8 "
            "in typedef CallID_t",
        ),
        (
            lambda m: change_field(m, 2, type="struct WidgetReady_t", align=4),
            "invalid-layout",
            "WidgetPos_t.m_rgubColor: struct WidgetReady_t is 8 bytes, not 4",
        ),
        (
            lambda m: change_field(m, 2, type="struct WidgetReady_t[1]", size=8),
            "invalid-layout",
            "WidgetPos_t.m_rgubColor: struct WidgetReady_t is aligned to 4, not 1",
        ),
        (
            lambda m: change_field(m, 0, type="enum EWidgetResult", size=2, align=2),
            "invalid-layout",
            "WidgetPos_t.m_flX: enum EWidgetResult is 4 bytes, not 2",
        ),
    ],
)
def test_malformed_layouts_are_errors(
    widgets: ir.Metadata, change: Change, code: str, message: str
) -> None:
    errors = [(c, m) for severity, c, m in found(change(widgets)) if severity == "error"]
    assert any(c == code and message in m for c, m in errors), errors


def test_a_well_formed_union_and_bit_fields_check_out(widgets: ir.Metadata) -> None:
    changed = change_field(widgets, 2, type="union", record=UNION)
    changed = change_field(changed, 1, offset=None, type="unsigned int", bit_offset=32, bit_width=3)
    assert [code for _, code, _ in found(changed) if code != "json-fields-differ"] == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda m: change_target(m, records=target(m).records[1:]),
            "WidgetPos_t has no layout on linux-x86_64",
        ),
        (
            lambda m: everywhere(
                m, lambda m, t: change_target(m, t, records=target(m, t).records[1:])
            ),
            "WidgetPos_t has no layout on every target",
        ),
        (
            lambda m: everywhere(
                m,
                lambda m, t: change_record(
                    m, "WidgetPos_t", t, status="opaque", size=None, align=None, fields=()
                ),
            ),
            "WidgetPos_t is opaque on every target",
        ),
        (
            lambda m: change_record(
                m, "WidgetPos_t", status="unsupported", reason="it has base classes", fields=()
            ),
            "WidgetPos_t is unsupported: it has base classes on linux-x86_64",
        ),
        (lambda m: change_target(m, enums=()), "EWidgetResult has no layout on linux-x86_64"),
        (
            lambda m: change_target(m, typedefs=target(m).typedefs[1:]),
            "CallID_t has no layout on linux-x86_64",
        ),
    ],
)
def test_declarations_without_a_complete_layout_are_warnings(
    widgets: ir.Metadata, change: Change, message: str
) -> None:
    assert ("warning", "missing-layout", message) in found(change(widgets))


def test_layouts_must_agree_with_the_external_types_overrides_locate(widgets: ir.Metadata) -> None:
    uses = ir.Struct(
        "Holder_t",
        (ir.Field("m_p", parse_type("Handle_t *")), ir.Field("m_h", parse_type("Hook_t"))),
        "test",
    )
    externals = (
        ir.ExternalType("Handle_t", "incomplete", "h.h", "overrides.toml#/external_type/0"),
        ir.ExternalType("Hook_t", "typedef", "h.h", "overrides.toml#/external_type/1"),
    )
    handle = ir.RecordLayout("Handle_t", "struct", "complete", "h.h:1", 4, 4)
    changed = replace(widgets, structs=(*widgets.structs, uses), external_types=externals)
    changed = everywhere(
        changed, lambda m, t: change_target(m, t, records=(handle, *target(m, t).records))
    )
    errors = [(code, message) for severity, code, message in found(changed) if severity == "error"]
    assert errors == [
        ("external-type-mismatch", "Handle_t is complete, not opaque on every target"),
        ("external-type-mismatch", "Hook_t has no layout on every target"),
    ]


def test_a_steam_api_json_field_list_the_headers_contradict_is_a_warning(
    widgets: ir.Metadata,
) -> None:
    changed = change_field(widgets, 2, name="", type="union", record=UNION)
    assert ("warning", "json-fields-differ") in [(s, c) for s, c, _ in found(changed)]
    (message,) = [m for _, code, m in found(changed) if code == "json-fields-differ"]
    assert message == (
        "steam_api.json lists WidgetPos_t as m_flX, m_flY, m_rgubColor, but the headers lay it "
        "out as m_flX, m_flY, (anonymous union) on linux-x86_64"
    )


def returning_pos(metadata: ir.Metadata, spelling: str = "WidgetPos_t") -> ir.Metadata:
    (interface,) = metadata.interfaces
    methods = (replace(interface.methods[0], returns=parse_type(spelling)), *interface.methods[1:])
    return replace(metadata, interfaces=(replace(interface, methods=methods),))


def by_value(metadata: ir.Metadata) -> list[str]:
    return [message for _, code, message in found(metadata) if code == "by-value-record"]


def test_a_plain_record_passed_by_value_raises_nothing(widgets: ir.Metadata) -> None:
    assert by_value(returning_pos(widgets)) == []


@pytest.mark.parametrize(
    ("change", "hazards"),
    [
        (lambda m: change_record(m, "WidgetPos_t", align=1), "packing"),
        (lambda m: change_field(m, 2, type="union", record=UNION), "a union"),
        (
            lambda m: change_field(
                m, 1, offset=None, type="unsigned int", bit_offset=32, bit_width=3
            ),
            "bit-fields",
        ),
    ],
)
def test_records_ctypes_may_mishandle_by_value_are_warnings(
    widgets: ir.Metadata, change: Change, hazards: str
) -> None:
    assert by_value(change(returning_pos(widgets))) == [
        f"WidgetAPI_IWidgets_GetWidgetCount returns WidgetPos_t by value, whose layout has "
        f"{hazards} on linux-x86_64; ctypes may not pass such a record correctly, so check it "
        "before binding the function"
    ]


def test_by_value_records_are_found_through_typedefs_and_without_layouts(
    widgets: ir.Metadata,
) -> None:
    alias = ir.Typedef("Pos_t", parse_type("WidgetPos_t"), "test")
    changed = returning_pos(replace(widgets, typedefs=(*widgets.typedefs, alias)), "Pos_t")
    changed = everywhere(changed, lambda m, t: change_record(m, "WidgetPos_t", t, align=1))
    assert by_value(changed) == [
        "WidgetAPI_IWidgets_GetWidgetCount returns WidgetPos_t by value, whose layout has packing "
        "on every target; ctypes may not pass such a record correctly, so check it before "
        "binding the function"
    ]
    gone = change_target(returning_pos(widgets), records=target(widgets).records[1:])
    assert "which has no complete layout on linux-x86_64" in by_value(gone)[0]


@pytest.mark.parametrize(
    ("underlying", "size", "stored"), [("unsigned int", 4, 4294967295), ("unsigned char", 1, 255)]
)
def test_enum_values_a_target_stores_wrapped_are_notes(
    widgets: ir.Metadata, underlying: str, size: int, stored: int
) -> None:
    enum = replace(target(widgets).enums[0], underlying=underlying, size=size)
    notes = [m for s, c, m in found(change_target(widgets, enums=(enum,))) if s == "note"]
    assert notes == [
        f"EWidgetResult::k_EWidgetResultFail is -1, which {underlying} stores as {stored} "
        "on linux-x86_64"
    ]


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("int", LayoutType("int", 0, 1)),
        ("unsigned long long", LayoutType("unsigned long long", 0, 1)),
        ("const char*", LayoutType("char", 1, 1)),
        ("void**", LayoutType("void", 2, 1)),
        ("function*", LayoutType("function", 1, 1)),
        ("char*[4]", LayoutType("char", 1, 4)),
        ("struct A::B[2][3]", LayoutType("struct A::B", 0, 6)),
        ("enum E", LayoutType("enum E", 0, 1)),
        ("union", LayoutType("union", 0, 1)),
    ],
)
def test_canonical_layout_types_parse(text: str, parsed: LayoutType) -> None:
    assert parse(text) == parsed


@pytest.mark.parametrize(
    "text",
    [
        "",
        "uint32",
        "void",
        "function",
        "const int",
        "const function*",
        "union*",
        "struct 1x",
        "int[",
    ],
)
def test_other_spellings_are_not_canonical_layout_types(text: str) -> None:
    assert parse(text) is None


REVIEW = ir.ByValue(
    "WidgetPos_t", ("windows-x86_64",), ("linux-x86_64",), "overrides.toml#/by_value/0"
)


def reviewed(metadata: ir.Metadata, *entries: ir.ByValue) -> ir.Metadata:
    return replace(returning_pos(metadata), by_value=entries)


def review_errors(metadata: ir.Metadata) -> list[str]:
    return [message for _, code, message in found(metadata) if code == "invalid-by-value"]


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ((replace(REVIEW, direct=("vax",)),), "WidgetPos_t: vax are not targets codegen.abi knows"),
        (
            (replace(REVIEW, direct=("linux-x86_64",)),),
            "linux-x86_64 cannot be both direct and shim",
        ),
        (
            (replace(REVIEW, record="WidgetReady_t"),),
            "WidgetReady_t: no flat function takes or returns it by value",
        ),
        ((REVIEW, REVIEW), "WidgetPos_t: it is reviewed 2 times"),
    ],
)
def test_contradictory_by_value_reviews_are_errors(
    widgets: ir.Metadata, entries: tuple[ir.ByValue, ...], message: str
) -> None:
    assert any(message in error for error in review_errors(reviewed(widgets, *entries)))


def test_a_direct_review_the_layouts_contradict_is_an_error(widgets: ir.Metadata) -> None:
    assert review_errors(reviewed(widgets, REVIEW)) == []
    # Packed to 4, a 64-bit member at offset 4 is passed in memory by SysV but not by ctypes.
    fields = (
        ir.FieldLayout("m_n", "int", 4, 4, offset=0),
        ir.FieldLayout("m_ul", "unsigned long long", 8, 8, offset=4),
    )
    packed = change_record(widgets, "WidgetPos_t", fields=fields)
    direct = replace(REVIEW, direct=("windows-x86_64", "linux-x86_64"), shim=())
    assert review_errors(reviewed(packed, direct)) == [
        "by_value WidgetPos_t: it is direct on linux-x86_64, but a misaligned member makes SysV "
        "pass it in memory, where ctypes uses registers"
    ]
    opaque = change_record(
        widgets, "WidgetPos_t", "windows-x86_64", status="opaque", size=None, align=None, fields=()
    )
    assert review_errors(reviewed(opaque, REVIEW)) == [
        "by_value WidgetPos_t: it is direct on windows-x86_64, where it has no complete layout"
    ]


def test_records_passed_by_value_are_never_direct_without_review(widgets: ir.Metadata) -> None:
    unreviewed = returning_pos(widgets)
    assert (
        "warning",
        "unreviewed-by-value",
        "WidgetPos_t is passed by value, but no by_value override says how ctypes may pass it, "
        "so it is unverified on every target",
    ) in found(unreviewed)
    assert {by_value_status(unreviewed, "WidgetPos_t", t) for t in abi.TARGETS} == {"unverified"}
    metadata = reviewed(widgets, REVIEW)
    assert [by_value_status(metadata, "WidgetPos_t", t) for t in abi.TARGETS] == [
        "direct",
        "shim",
        "unverified",
        "unverified",
    ]
    assert "unreviewed-by-value" not in [code for _, code, _ in found(metadata)]
