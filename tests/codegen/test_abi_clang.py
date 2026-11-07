"""Layouts libclang computes for fixtures/layouts.h.

The expected values for windows-x86_64 and linux-x86_64 are what MSVC and GCC report for the same
header. The macOS values come from libclang alone.
"""

from pathlib import Path

import pytest

from codegen import abi, ir
from codegen.abi.checks import check

pytest.importorskip("clang.cindex", reason="needs the codegen dependency group")
from codegen.abi.clang import LayoutError, lay_out, version

FIXTURE = Path(__file__).parent / "fixtures" / "layouts.h"
RECORDS = (
    "Aligned",
    "Bits",
    "Derived",
    "Forward",
    "Named",
    "Outer",
    "Packed1",
    "Packed4",
    "Plain",
    "Pointers",
    "Polymorphic",
    "Tagged",
    "WithLong",
)
ROOTS = abi.Roots(
    records=RECORDS,
    enums=("Fixed", "Scoped", "Small", "Wide"),
    typedefs=("callback_t", "handle_t", "platform_long", "size_t", "uint64_t"),
)
WINDOWS, ITANIUM = ("windows-x86_64",), ("linux-x86_64", "macos-x86_64", "macos-arm64")


def compile_layouts(
    source: str, folder: Path, targets: tuple[str, ...] = tuple(abi.TARGETS)
) -> tuple[ir.Abi, list[ir.Issue]]:
    return lay_out(source, [folder], ROOTS, targets)


@pytest.fixture(scope="module")
def layouts() -> dict[str, ir.TargetLayout]:
    found, issues = compile_layouts('#include "layouts.h"\n', FIXTURE.parent)
    assert issues == []
    return {target.target: target for target in found.targets}


def record(target: ir.TargetLayout, name: str) -> ir.RecordLayout:
    (found,) = [item for item in target.records if item.name == name]
    return found


def places(layout: ir.RecordLayout) -> list[tuple[str, int | None]]:
    return [(item.name, item.offset) for item in layout.fields]


def test_every_target_is_laid_out_in_order(layouts: dict[str, ir.TargetLayout]) -> None:
    assert list(layouts) == list(abi.TARGETS)
    for name, target in layouts.items():
        assert target.triple == abi.TARGETS[name].triple


@pytest.mark.parametrize("target", abi.TARGETS)
def test_portable_records_lay_out_alike_everywhere(
    layouts: dict[str, ir.TargetLayout], target: str
) -> None:
    found = layouts[target]
    plain = record(found, "Plain")
    assert (plain.kind, plain.status, plain.size, plain.align) == ("struct", "complete", 16, 8)
    assert places(plain) == [("c", 0), ("i", 4), ("d", 8)]
    aligned = record(found, "Aligned")
    assert (aligned.size, aligned.align, places(aligned)) == (32, 16, [("v", 0), ("tag", 16)])
    # A member's own alignment comes from its type; alignas only shows in the offsets.
    assert aligned.fields[0].align == 4
    value = record(found, "Value")
    assert (value.kind, value.size, value.align) == ("union", 8, 8)
    assert {item.offset for item in value.fields} == {0}


@pytest.mark.parametrize("target", abi.TARGETS)
def test_pragma_pack_lowers_alignment(layouts: dict[str, ir.TargetLayout], target: str) -> None:
    packed4, packed1 = record(layouts[target], "Packed4"), record(layouts[target], "Packed1")
    assert (packed4.size, packed4.align, places(packed4)) == (12, 4, [("a", 0), ("b", 4)])
    assert packed4.fields[1].align == 8
    assert (packed1.size, packed1.align, places(packed1)) == (7, 1, [("a", 0), ("b", 1), ("c", 5)])


@pytest.mark.parametrize("target", abi.TARGETS)
def test_anonymous_and_unnamed_records_are_described_in_place(
    layouts: dict[str, ir.TargetLayout], target: str
) -> None:
    tagged = record(layouts[target], "Tagged")
    _, member = tagged.fields
    assert (member.name, member.type, member.offset, member.size) == ("", "union", 8, 8)
    assert member.record is not None
    assert (member.record.name, member.record.kind, member.record.size) == ("", "union", 8)
    assert [(item.name, item.type) for item in member.record.fields] == [
        ("i", "int"),
        ("d", "double"),
    ]
    named = record(layouts[target], "Named")
    point = named.fields[0]
    assert (point.name, point.type, point.offset) == ("point", "struct", 0)
    assert point.record is not None
    assert places(point.record) == [("x", 0), ("y", 2)]


@pytest.mark.parametrize("target", abi.TARGETS)
def test_named_records_used_by_value_are_laid_out_too(
    layouts: dict[str, ir.TargetLayout], target: str
) -> None:
    outer = record(layouts[target], "Outer")
    assert [(item.name, item.type, item.offset, item.size) for item in outer.fields] == [
        ("inner", "struct Outer::Inner[2]", 0, 6),
        ("grid", "float[2][3]", 8, 24),
    ]
    inner = record(layouts[target], "Outer::Inner")
    assert (inner.size, inner.align, places(inner)) == (3, 1, [("a", 0)])
    # Pointers stores Value by value; Forward only through a pointer, so it stays opaque.
    assert record(layouts[target], "Value").status == "complete"
    assert record(layouts[target], "Forward").status == "opaque"


@pytest.mark.parametrize(
    ("targets", "size", "align", "offset", "size_type"),
    [(WINDOWS, 8, 4, 4, "unsigned long long"), (ITANIUM, 16, 8, 8, "unsigned long")],
)
def test_long_is_4_bytes_only_on_windows(
    layouts: dict[str, ir.TargetLayout],
    targets: tuple[str, ...],
    size: int,
    align: int,
    offset: int,
    size_type: str,
) -> None:
    for target in targets:
        with_long = record(layouts[target], "WithLong")
        assert (with_long.size, with_long.align) == (size, align)
        assert places(with_long)[1] == ("l", offset)
        pointers = record(layouts[target], "Pointers")
        assert pointers.fields[-1].type == size_type


@pytest.mark.parametrize(
    ("targets", "size", "bits"),
    [
        # MSVC opens a new storage unit when the declared type's size changes, and after ":0".
        (WINDOWS, 16, [("a", 0, 3), ("b", 32, 30), ("c", 64, 2), ("", 96, 0), ("d", 96, 1)]),
        (ITANIUM, 12, [("a", 0, 3), ("b", 32, 30), ("c", 62, 2), ("", 64, 0), ("d", 64, 1)]),
    ],
)
def test_bit_fields_follow_each_compiler(
    layouts: dict[str, ir.TargetLayout],
    targets: tuple[str, ...],
    size: int,
    bits: list[tuple[str, int, int]],
) -> None:
    for target in targets:
        found = record(layouts[target], "Bits")
        assert (found.size, found.align) == (size, 4)
        assert [(item.name, item.bit_offset, item.bit_width) for item in found.fields] == bits
        assert all(item.offset is None for item in found.fields)


@pytest.mark.parametrize(
    ("targets", "storage"),
    [
        (WINDOWS, {"Fixed": "unsigned char", "Scoped": "long long", "Small": "int", "Wide": "int"}),
        (
            ("linux-x86_64",),
            {
                "Fixed": "unsigned char",
                "Scoped": "long",
                "Small": "unsigned int",
                "Wide": "unsigned int",
            },
        ),
        (
            ("macos-x86_64", "macos-arm64"),
            {
                "Fixed": "unsigned char",
                "Scoped": "long long",
                "Small": "unsigned int",
                "Wide": "unsigned int",
            },
        ),
    ],
)
def test_enum_storage_follows_the_target(
    layouts: dict[str, ir.TargetLayout], targets: tuple[str, ...], storage: dict[str, str]
) -> None:
    for target in targets:
        assert {item.name: item.underlying for item in layouts[target].enums} == storage
        assert {item.name: item.size for item in layouts[target].enums} == {
            "Fixed": 1,
            "Scoped": 8,
            "Small": 4,
            "Wide": 4,
        }


@pytest.mark.parametrize("target", abi.TARGETS)
def test_pointers_arrays_and_typedefs_have_canonical_types(
    layouts: dict[str, ir.TargetLayout], target: str
) -> None:
    pointers = record(layouts[target], "Pointers")
    assert [(item.type, item.offset) for item in pointers.fields[:5]] == [
        ("const char*", 0),
        ("void*", 8),
        ("function*", 16),
        ("struct Forward*", 24),
        ("union Value[2]", 32),
    ]
    typedefs = {item.name: item for item in layouts[target].typedefs}
    assert (typedefs["callback_t"].type, typedefs["callback_t"].size) == ("function*", 8)
    assert (typedefs["handle_t"].type, typedefs["handle_t"].size) == ("struct handle_t", 4)
    assert record(layouts[target], "handle_t").status == "complete"
    long_size = 4 if target in WINDOWS else 8
    assert (typedefs["platform_long"].type, typedefs["platform_long"].size) == ("long", long_size)


def test_standard_types_come_from_the_target_not_the_host(
    layouts: dict[str, ir.TargetLayout],
) -> None:
    def typedef(target: str, name: str) -> str:
        return next(item.type for item in layouts[target].typedefs if item.name == name)

    assert typedef("windows-x86_64", "size_t") == "unsigned long long"
    assert typedef("linux-x86_64", "uint64_t") == "unsigned long"
    assert typedef("macos-arm64", "uint64_t") == "unsigned long long"
    assert typedef("macos-arm64", "size_t") == "unsigned long"


@pytest.mark.parametrize("target", abi.TARGETS)
def test_what_cannot_be_described_is_marked_not_guessed(
    layouts: dict[str, ir.TargetLayout], target: str
) -> None:
    polymorphic, derived = (
        record(layouts[target], "Polymorphic"),
        record(layouts[target], "Derived"),
    )
    assert (polymorphic.status, polymorphic.reason, polymorphic.fields) == (
        "unsupported",
        "it has virtual functions",
        (),
    )
    assert (derived.status, derived.reason, derived.size) == (
        "unsupported",
        "it has base classes",
        24,
    )
    forward = record(layouts[target], "Forward")
    assert (forward.size, forward.align, forward.fields, forward.source) == (
        None,
        None,
        (),
        "layouts.h:53",
    )


def test_the_layouts_check_out_and_round_trip(layouts: dict[str, ir.TargetLayout]) -> None:
    metadata = ir.Metadata(
        format=ir.FORMAT,
        sdk_version=None,
        inputs=(),
        interfaces=(),
        structs=(),
        enums=(),
        constants=(),
        typedefs=(),
        external_types=(),
        shared_callbacks=(),
        by_value=(),
        exports=(),
        abi=ir.Abi(version(), tuple(layouts.values())),
        issues=(),
    )
    assert list(check(metadata)) == []
    text = ir.dumps(metadata)
    assert ir.loads(text) == metadata
    assert ir.dumps(ir.loads(text)) == text


def test_layouts_do_not_depend_on_the_order_targets_are_asked_for() -> None:
    forward, _ = compile_layouts('#include "layouts.h"\n', FIXTURE.parent, tuple(abi.TARGETS))
    backward, _ = compile_layouts(
        '#include "layouts.h"\n', FIXTURE.parent, tuple(reversed(abi.TARGETS))
    )
    assert forward == backward


def test_a_compiler_error_fails_the_target_instead_of_guessing(tmp_path: Path) -> None:
    (tmp_path / "broken.h").write_text(
        "struct Plain { int i; undeclared_t j; };\n", encoding="utf-8"
    )
    found, issues = compile_layouts('#include "broken.h"\n', tmp_path, ("linux-x86_64",))
    assert found.targets == ()
    assert [(i.severity, i.code, i.source) for i in issues] == [
        ("error", "compile-error", "broken.h:1")
    ]
    assert issues[0].message == "linux-x86_64: unknown type name 'undeclared_t'"


def test_host_headers_are_never_searched(tmp_path: Path) -> None:
    (tmp_path / "hosted.h").write_text("#include <vector>\n", encoding="utf-8")
    found, issues = compile_layouts('#include "hosted.h"\n', tmp_path, ("windows-x86_64",))
    assert found.targets == ()
    assert "'vector' file not found" in issues[0].message


def test_names_a_target_does_not_declare_are_left_out(tmp_path: Path) -> None:
    (tmp_path / "empty.h").write_text("struct Plain;\n", encoding="utf-8")
    found, issues = compile_layouts('#include "empty.h"\n', tmp_path, ("linux-x86_64",))
    assert issues == []
    (target,) = found.targets
    assert [(item.name, item.status) for item in target.records] == [("Plain", "opaque")]
    assert (target.enums, target.typedefs) == ((), ())


def test_unknown_targets_are_refused() -> None:
    with pytest.raises(LayoutError, match="unknown targets: vax"):
        compile_layouts("", FIXTURE.parent, ("vax",))
