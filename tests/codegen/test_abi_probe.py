"""The native compiler check of layouts.

The tests that build a probe need libclang and a C++ compiler for the host, found as
codegen.abi.probe.find_compiler finds one. They skip without them, unless PYSTEAMSDK_REQUIRE_CXX
is 1, as in CI, where a missing compiler is a failure.
"""

import shutil
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from codegen import abi, ir
from codegen.__main__ import main
from codegen.abi import probe
from codegen.abi.probe import Compiler, Context, Failure, Gap, Node, ProbeError

FIXTURE = Path(__file__).parent / "fixtures" / "layouts.h"
INCLUDE = '#include "layouts.h"\n'
RECORDS = (
    *("Aligned", "Bits", "Derived", "Event", "Extended", "Flags", "Forward", "Handle", "Mixed"),
    *("Named", "Native", "Notice", "Outer", "Packed1", "Packed4", "Plain", "Pointers"),
    *("Polymorphic", "Shown", "Tagged", "Totals", "WithLong"),
)
ROOTS = abi.Roots(
    records=RECORDS,
    enums=("Fixed", "Scoped", "Small", "Wide"),
    typedefs=(
        *("Count", "Forward_t", "ForwardRef", "Total", "callback_t", "handle_t"),
        *("platform_long", "size_t", "uint64_t"),
    ),
)
MakeSdk = Callable[..., Path]
MISSING = "hold a value that no extracted member covers, so a member is missing"


def field_layout(name: str, ctype: str, size: int, offset: int) -> ir.FieldLayout:
    return ir.FieldLayout(name, ctype, size, size, offset=offset)


PAIR = ir.RecordLayout(
    "Pair",
    "struct",
    "complete",
    "x.h:1",
    8,
    4,
    (field_layout("a", "int", 4, 0), field_layout("b", "unsigned int", 4, 4)),
)
LAYOUT = ir.TargetLayout(
    "linux-x86_64",
    "x86_64-pc-linux-gnu",
    (PAIR,),
    (ir.EnumLayout("Mode", "unsigned int", 4),),
    (ir.TypedefLayout("pair_t", "struct Pair", 8, 4),),
)
IDENTITY = "pysteamsdk-probe 1\ncompiler GCC 15.2.0\nos linux\narch x86_64\npointer 8\n"


def with_records(*records: ir.RecordLayout) -> ir.TargetLayout:
    return replace(LAYOUT, records=records)


def output(*rows: str) -> str:
    return IDENTITY + "".join(f"{row}\n" for row in rows) + "end\n"


# The rows a correct probe prints for LAYOUT. Row 3 is Pair's mask: all 64 bits hold its value.
ROWS = (
    "0 8 4 0 255",
    "1 0 4 4 1 0 0 0 0 0",
    "2 4 4 4 1 0 0 0 0 0",
    "3 1 1 0 64",
    "4 1 4 1",
    "5 1 1 8 4",
)


def compared(layout: ir.TargetLayout, *rows: str) -> tuple[list[Failure], list[Gap], list[Gap]]:
    plan = probe.plan(layout)
    return probe.compare(
        plan.checks, probe.parse_output(output(*rows), plan.checks)[1], plan.contexts
    )


def test_the_probe_holds_names_and_types_but_never_the_values_it_checks() -> None:
    checks = probe.plan(LAYOUT).checks
    text, owners = probe.source(INCLUDE, checks)
    assert [(check.kind, check.label) for check in checks] == [
        ("record", "Pair"),
        ("field", "Pair.a"),
        ("field", "Pair.b"),
        ("mask", "Pair value bits"),
        ("enum", "Mode"),
        ("typedef", "pair_t"),
    ]
    assert "template struct pysteamsdk_probe::record<0, ::Pair>;" in text
    assert (
        "template struct pysteamsdk_probe::field<2, offsetof(::Pair, b), decltype(::Pair::b), "
        "unsigned int>;"
    ) in text
    assert "template struct pysteamsdk_probe::mask<3, ::Pair>;" in text
    assert "template struct pysteamsdk_probe::alias<5, ::pair_t, ::Pair>;" in text
    assert sorted(set(owners.values())) == [0, 1, 2, 3, 4, 5]
    # Wrong sizes and offsets make the same program, so the compiler cannot echo them back.
    first = replace(PAIR.fields[0], offset=7, size=2, align=2)
    wrong = replace(PAIR, size=99, align=64, fields=(first, PAIR.fields[1]))
    assert probe.source(INCLUDE, probe.plan(with_records(wrong)).checks)[0] == text


def test_planning_is_deterministic_and_can_be_limited() -> None:
    assert probe.plan(LAYOUT) == probe.plan(LAYOUT)
    only = probe.plan(LAYOUT, only=["Mode", "pair_t"])
    assert [check.label for check in only.checks] == ["Mode", "pair_t"]


UNION = ir.RecordLayout("", "union", "complete", "", 4, 4, (field_layout("i", "int", 4, 0),))
ODD = ir.RecordLayout(
    "Odd",
    "struct",
    "complete",
    "x.h:1",
    12,
    4,
    (
        ir.FieldLayout("", "union", 4, 4, offset=0, record=UNION),
        ir.FieldLayout("", "unsigned int", 4, 4, bit_offset=32, bit_width=0),
        ir.FieldLayout("e", "enum Mode", 4, 4, bit_offset=32, bit_width=3),
        field_layout("f", "int", 4, 8),
    ),
)


def test_anonymous_members_unnamed_and_enum_bit_fields_are_planned() -> None:
    unsupported = replace(PAIR, status="unsupported", fields=(), reason="it has base classes")
    plan = probe.plan(with_records(ODD, unsupported))
    assert plan.gaps == (
        Gap("record", "Pair", "Pair", "its members are not laid out: it has base classes"),
    )
    # The anonymous union's member is checked at its offset in Odd; the unnamed bit-field is
    # padding, so only the mask sees it.
    (member,) = [check for check in plan.checks if check.label == "Odd.(anonymous union).i"]
    assert "offsetof(::Odd, i)" in member.code
    assert member.expected[0] == 0
    (bits,) = [check for check in plan.checks if check.kind == "bits"]
    assert (bits.label, bits.expected) == ("Odd.e", (1, 32, 1, 1))
    assert "static_cast<F>(1)" in bits.code
    (mask,) = [check for check in plan.checks if check.kind == "mask"]
    assert mask.spans == ((0, 32), (32, 35), (64, 96))
    # The enum bit-field's value bits must end where f begins, or at padding.
    assert mask.widths == (("Odd.e", 32, 64, 3),)
    assert plan.contexts == (
        Context(
            "Odd",
            "Odd",
            (
                Node(
                    "Odd.(anonymous union)",
                    "union",
                    (0, 4, 4),
                    (Node("Odd.(anonymous union).i", "field"),),
                ),
                Node("Odd.(anonymous unsigned int)", "bits"),
                Node("Odd.e", "bits"),
                Node("Odd.f", "field"),
            ),
            False,
        ),
    )


@pytest.mark.parametrize(
    ("layout", "text"),
    [
        (with_records(replace(PAIR, name="Pair; int x")), "Pair; int x"),
        (with_records(replace(PAIR, fields=(field_layout("a) + (b", "int", 4, 0),))), "a) + (b"),
        (with_records(replace(PAIR, fields=(field_layout("a", "int>; x<int", 4, 0),))), "x<int"),
        (with_records(replace(PAIR, fields=(replace(PAIR.fields[0], offset=None),))), "::Pair::a"),
        (replace(LAYOUT, records=(), enums=(ir.EnumLayout("Mode", "int); x(", 4),)), "x("),
        (replace(LAYOUT, records=(), typedefs=(ir.TypedefLayout("t", "uint32", 4, 4),)), "uint32"),
    ],
)
def test_layouts_that_are_not_cpp_fail_without_reaching_the_compiler(
    layout: ir.TargetLayout, text: str
) -> None:
    plan = probe.plan(layout)
    assert len(plan.invalid) == 1
    assert plan.invalid[0].message.endswith(("spell", "is not a canonical type", "no offset"))
    assert text not in probe.source(INCLUDE, plan.checks)[0]


def test_a_correct_probe_report_passes() -> None:
    checks = probe.plan(LAYOUT).checks
    identity, rows = probe.parse_output(output(*ROWS), checks)
    assert identity == probe.Identity("GCC 15.2.0", "linux", "x86_64", 8)
    assert identity.target == "linux-x86_64"
    assert probe.compare(checks, rows) == ([], [], [])


@pytest.mark.parametrize(
    ("rows", "messages"),
    [
        (
            {0: "0 16 8 1 255"},
            [
                "size: extracted 8, compiler 16",
                "alignment: extracted 4, compiler 8",
                "the compiler says it is a union",
            ],
        ),
        ({1: "1 4 4 4 1 0 0 0 0 0"}, ["offset: extracted 0, compiler 4"]),
        (
            {2: "2 4 8 8 0 0 0 0 0 0"},
            [
                "size: extracted 4, compiler 8",
                "alignment: extracted 4, compiler 8",
                "the compiler's type is not unsigned int",
            ],
        ),
        ({2: "2 4 4 4 1 1 3 0 0 0"}, ["array dimensions: extracted none, compiler [3]"]),
        # A value the extracted members do not cover means the extractor dropped a member.
        (
            {3: "3 1 2 0 64 72 96"},
            ["bytes 9 to 12 hold a value that no extracted member covers, so a member is missing"],
        ),
        (
            {4: "4 1 1 0"},
            ["size: extracted 4, compiler 1", "the compiler's underlying type is not unsigned int"],
        ),
        ({4: "4 0 4 0"}, ["the compiler says it is not an enum"]),
        ({5: "5 0 0 -1 -1"}, ["the compiler's type is not struct Pair", "it is incomplete"]),
    ],
)
def test_each_difference_is_reported_with_both_values(
    rows: dict[int, str], messages: list[str]
) -> None:
    printed = [rows.get(index, row) for index, row in enumerate(ROWS)]
    failures, gaps, _ = compared(LAYOUT, *printed)
    assert [item.message for item in failures] == messages
    assert gaps == []


def test_bit_fields_and_opaque_records_are_compared() -> None:
    record = ir.RecordLayout(
        "B",
        "struct",
        "complete",
        "",
        4,
        4,
        (ir.FieldLayout("x", "unsigned int", 4, 4, bit_offset=3, bit_width=5),),
    )
    layout = replace(
        LAYOUT,
        records=(record, ir.RecordLayout("F", "struct", "opaque", "")),
        enums=(),
        typedefs=(),
    )
    assert compared(layout, "0 4 4 0 255", "1 1 3 5 1", "2 1 1 3 8", "3 0") == ([], [], [])
    failures, _, _ = compared(layout, "0 4 4 0 255", "1 1 4 6 0", "2 1 1 3 8", "3 1")
    assert [item.message for item in failures] == [
        "bit offset: extracted 3, compiler 4",
        "width: extracted 5, compiler 6",
        "the compiler stores it in bits that are not contiguous",
        "the compiler sees it defined, so it is not opaque",
    ]
    failures, gaps, _ = compared(layout, "0 4 4 0 0", "1 0 -1 0 0", "2 0 0", "3 0")
    assert failures == []
    assert [(gap.label, gap.reason) for gap in gaps] == [
        ("B.x", "its record is not trivially copyable and default-constructible"),
        (
            "B value bits",
            "no missing member is ruled out: it is not trivially copyable and "
            "default-constructible, so setting its bytes is undefined",
        ),
    ]


def test_enum_bit_field_widths_come_from_the_value_bits() -> None:
    layout = with_records(ODD)
    plan = probe.plan(layout)
    labels = [check.label for check in plan.checks]
    assert labels == [
        *("Odd", "Odd.(anonymous union).i", "Odd.e", "Odd.f", "Odd value bits", "Mode", "pair_t"),
    ]

    def printed(mask: str) -> list[str]:
        return [
            *("0 12 4 0 255", "1 0 4 4 1 0 0 0 0 0", "2 1 32 1 1", "3 8 4 4 1 0 0 0 0 0"),
            *(f"4 {mask}", "5 1 4 1", "6 1 1 8 4"),
        ]

    good = printed("1 2 0 35 64 96")
    failures, gaps, derived = compared(layout, *good)
    assert (failures, gaps) == ([], [])
    assert derived == [Gap("record", "Odd", "Odd.(anonymous union)", "size 4, alignment 4")]
    # Value bits that run on past the extracted width: the extractor got the width wrong.
    failures, _, _ = compared(layout, *printed("1 2 0 40 64 96"))
    assert [(item.label, item.message) for item in failures] == [
        (
            "Odd value bits",
            "bits 35 to 40 hold a value that no extracted member covers, so a member is missing",
        ),
        ("Odd.e", "width: extracted 3, compiler 8"),
    ]
    # Clang cannot report padding, so neither the width nor missing members can be checked.
    _, gaps, _ = compared(layout, *printed("2 0"))
    assert [gap.label for gap in gaps] == ["Odd value bits", "Odd.e width"]
    assert all("Clang has none" in gap.reason for gap in gaps)


def derivation(record: ir.RecordLayout, *rows: str) -> tuple[list[Failure], list[Gap], list[Gap]]:
    plan = probe.plan(with_records(record))
    assert len(plan.contexts) == 1
    return compared(with_records(record), *rows)


# A packed record holding an anonymous union of a 4-byte int and 6 chars at offset 2.
PACKED = ir.RecordLayout(
    "Packed",
    "struct",
    "complete",
    "",
    8,
    1,
    (
        ir.FieldLayout("tag", "unsigned short", 2, 2, offset=0),
        ir.FieldLayout(
            "",
            "union",
            6,
            1,
            offset=2,
            record=ir.RecordLayout(
                "",
                "union",
                "complete",
                "",
                6,
                1,
                (
                    ir.FieldLayout("i", "int", 4, 4, offset=0),
                    ir.FieldLayout("c", "char[6]", 6, 1, offset=0),
                ),
            ),
        ),
    ),
)
PACKED_ROWS = (
    "0 8 1 0 255",
    "1 0 2 2 1 0 0 0 0 0",
    "2 2 4 4 1 0 0 0 0 0",
    "3 2 6 1 1 1 6 0 0 0",
    "4 1 1 0 64",
    "5 1 4 1",
    "6 1 1 8 4",
)


def test_an_anonymous_union_size_and_alignment_are_derived() -> None:
    # Aligned to 1, the record caps the union at 1: its 6-byte char array sets its size.
    assert derivation(PACKED, *PACKED_ROWS) == (
        [],
        [],
        [Gap("record", "Packed", "Packed.(anonymous union)", "size 6, alignment 1")],
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda u: replace(u, size=8), "size: extracted 8, derived 6"),
        (lambda u: replace(u, align=4), "alignment: extracted 4, derived 1"),
    ],
)
def test_a_wrong_anonymous_union_layout_fails(
    change: Callable[[ir.FieldLayout], ir.FieldLayout], message: str
) -> None:
    union = PACKED.fields[1]
    assert union.record is not None
    changed = change(union)
    changed = replace(changed, record=replace(union.record, size=changed.size, align=changed.align))
    record = replace(PACKED, fields=(PACKED.fields[0], changed))
    failures, _, derived = derivation(record, *PACKED_ROWS)
    assert [(item.label, item.message) for item in failures] == [
        ("Packed.(anonymous union)", message)
    ]
    assert derived == []


def test_an_anonymous_union_that_overruns_the_record_fails() -> None:
    # The compiler measures the char array at 8 bytes, which no longer fits after tag.
    rows = (*PACKED_ROWS[:3], "3 2 8 1 1 1 8 0 0 0", *PACKED_ROWS[4:])
    failures, _, _ = derivation(PACKED, *rows)
    assert ("Packed.(anonymous union)", "it would run 2 bytes into what follows") in [
        (item.label, item.message) for item in failures
    ]


def test_an_alignment_beyond_every_member_stops_derivation() -> None:
    rows = ("0 8 8 0 255", *PACKED_ROWS[1:])
    _, gaps, derived = derivation(PACKED, *rows)
    assert derived == []
    assert gaps == [
        Gap(
            "record",
            "Packed",
            "Packed.(anonymous union)",
            "the record is more aligned than any member, so an alignment attribute is in play",
        )
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "not a probe report"),
        ("hello\n", "not a probe report"),
        (IDENTITY.replace("os linux", "system linux"), "should give its os"),
        (IDENTITY.replace("pointer 8", "pointer eight"), "no pointer size"),
        (IDENTITY + "0 8 4 0 255\n", "or stopped early"),
        (output(*ROWS[:-1]), "or stopped early"),
        (output(*ROWS, "6 1"), "or stopped early"),
        (output("0 8 4 0 x", *ROWS[1:]), "row 0 of the probe's output is '0 8 4 0 x'"),
        (output("1 8 4 0 255", *ROWS[1:]), "row 0 of the probe's output is"),
        (output("0", *ROWS[1:]), "check 0 (Pair) never ran"),
        (output("0 8 4 0", *ROWS[1:]), "has 3 values for Pair, not 4"),
        (output(*ROWS[:-1], "5 1 1 8 4 9"), "has 5 values for pair_t, not 4"),
        # A mask row must hold exactly the runs it counts.
        (output(*ROWS[:3], "3 1 2 0 64", *ROWS[4:]), "has 4 values for Pair value bits"),
        (output(*ROWS[:3], "3 1 99 0 64", *ROWS[4:]), "has 4 values for Pair value bits"),
        (output(*ROWS[:3], "3 1", *ROWS[4:]), "has 1 values for Pair value bits"),
    ],
)
def test_unexpected_probe_output_is_refused(text: str, message: str) -> None:
    with pytest.raises(ProbeError) as raised:
        probe.parse_output(text, probe.plan(LAYOUT).checks)
    assert message in str(raised.value)


@pytest.mark.parametrize(
    ("compiler_output", "found"),
    [
        # MSVC reports the error, then the line that instantiated the template.
        (
            "probe.cpp\n"
            "C:\\t\\probe.cpp(81): error C2039: 'q': is not a member of 'Pair'\n"
            "C:\\sdk\\layouts.h(8): note: see declaration of 'Pair'\n",
            {1: "'q': is not a member of 'Pair'"},
        ),
        # GCC names the instantiating line first.
        (
            "/t/probe.cpp: In instantiation of 'const bool pysteamsdk_probe::field<...>::done':\n"
            "/t/probe.cpp:82:8:   required from here\n"
            "/t/probe.cpp:40:3: error: 'q' is not a member of 'Pair'\n",
            {2: "'q' is not a member of 'Pair'"},
        ),
        (
            "/t/probe.cpp:81:52: error: no member named 'q' in 'Pair'\n"
            "/t/probe.cpp:82:52: error: no member named 'r' in 'Pair'\n",
            {1: "no member named 'q' in 'Pair'", 2: "no member named 'r' in 'Pair'"},
        ),
        # clang-cl warns about offsetof on non-standard-layout types right beside an error.
        (
            "C:\\t\\probe.cpp(80,1): warning: offset of on non-standard-layout type 'S'\n"
            "C:\\t\\probe.cpp(81,52): error: no member named 'q' in 'Pair'\n"
            "C:\\t\\probe.cpp(82,1): warning: offset of on non-standard-layout type 'S'\n",
            {1: "no member named 'q' in 'Pair'"},
        ),
        # An error in a header points at no check.
        ("/sdk/x.h:3:1: error: unknown type name 'uint99'\n", {}),
        ("/t/probe.cpp:81:1: warning: unused\n", {}),
    ],
)
def test_compiler_errors_are_traced_to_the_checks_they_reject(
    compiler_output: str, found: dict[int, str]
) -> None:
    owners = {80: 0, 81: 1, 82: 2}
    assert probe.rejections(compiler_output, owners) == found


def test_a_missing_compiler_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CXX", raising=False)
    with pytest.raises(ProbeError, match="'no-such-cxx' not found; pass --cxx or set CXX"):
        probe.find_compiler("no-such-cxx")
    with pytest.raises(ProbeError, match="developer prompt"):
        probe.find_compiler("cl" if shutil.which("cl") is None else "no-such/cl")
    monkeypatch.setenv("CXX", sys.executable)
    assert probe.find_compiler(flags=["-x"]).command == (shutil.which(sys.executable), "-x")
    assert not Compiler(("/usr/bin/g++",)).msvc_style
    assert Compiler((str(Path("VS", "bin", "cl.EXE")),)).msvc_style
    assert Compiler((str(Path("llvm", "clang-cl")),)).msvc_style


def fake_compiler(tmp_path: Path, script: str) -> Compiler:
    path = tmp_path / "fake compiler.py"
    path.write_text(script, encoding="utf-8")
    return Compiler((sys.executable, str(path)))


def test_a_compiler_that_fails_is_an_error_with_its_output(tmp_path: Path) -> None:
    compiler = fake_compiler(
        tmp_path, "import sys\nprint('probe.cpp:3:1: error: boom')\nsys.exit(1)\n"
    )
    with pytest.raises(ProbeError, match=r"does not compile:\nprobe\.cpp:3:1: error: boom"):
        probe.verify(LAYOUT, INCLUDE, [tmp_path], compiler)


def test_a_compiler_that_hangs_times_out(tmp_path: Path) -> None:
    compiler = fake_compiler(tmp_path, "import time\ntime.sleep(60)\n")
    with pytest.raises(ProbeError, match="compiling the probe timed out after 1 seconds"):
        probe.verify(LAYOUT, INCLUDE, [tmp_path], compiler, timeout=1)


def test_a_compiler_that_cannot_start_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="compiling the probe failed"):
        probe.verify(LAYOUT, INCLUDE, [tmp_path], Compiler((str(tmp_path / "missing"),)))


def test_reports_count_every_outcome() -> None:
    checks = probe.plan(LAYOUT).checks
    report = probe.Report(
        "linux-x86_64",
        probe.Identity("GCC 15.2.0", "linux", "x86_64", 8),
        ("/usr/bin/g++",),
        checks,
        (Failure("record", "Pair", "Pair.b", "offset: extracted 4, compiler 8"),),
        (Gap("enum", "Mode", "Mode", "invented reason"),),
        {"Pair": 255},
    )
    assert probe.statuses(report, LAYOUT) == {
        "record": {"Pair": "mismatched"},
        "enum": {"Mode": "partly verified"},
        "typedef": {"pair_t": "verified"},
    }
    assert probe.render(report, LAYOUT).splitlines() == [
        "Layouts for linux-x86_64 checked by GCC 15.2.0",
        "  compiler        /usr/bin/g++",
        "  flags           none",
        "  target          linux x86_64, 8-byte pointers",
        "  records            1  1 mismatched",
        "  enums              1  1 partly verified",
        "  typedefs           1  1 verified",
        "  checks             6  5 passed, 1 failed, 1 not possible",
        "Not possible to check",
        "  enum Mode: invented reason",
        "Mismatches",
        "  record Pair.b: offset: extracted 4, compiler 8",
    ]
    # A record whose anonymous members' layouts were derived says so, apart from the rest.
    derived = Gap("record", "Pair", "Pair.(anonymous union)", "size 4, alignment 4")
    report = replace(report, failures=(), gaps=(), derived=(derived,))
    assert probe.statuses(report, LAYOUT)["record"] == {"Pair": "derived"}
    assert "  record Pair.(anonymous union): size 4, alignment 4" in probe.render(report, LAYOUT)


def sized(size: int) -> ir.RecordLayout:
    return replace(PAIR, size=size, align=1, fields=(field_layout("b", f"char[{size}]", size, 0),))


# Packed to 4, as SDK callbacks are off Windows: the second member sits at offset 4.
MISALIGNED = replace(
    PAIR,
    size=12,
    fields=(
        field_layout("a", "int", 4, 0),
        ir.FieldLayout("b", "unsigned long long", 8, 8, offset=4),
    ),
)


@pytest.mark.parametrize(
    ("target", "record", "facts", "uses", "found"),
    [
        ("windows-x86_64", sized(8), 255, ["returns"], []),
        # A user-provided constructor makes it no aggregate, and MSVC no longer uses RAX.
        (
            "windows-x86_64",
            sized(8),
            255 & ~(1 << 2) & ~(1 << 3),
            ["returns"],
            ["MSVC returns it through a hidden pointer, where C would use RAX"],
        ),
        ("windows-x86_64", sized(8), 255 & ~(1 << 2), ["takes"], []),
        ("windows-x86_64", sized(12), 255 & ~(1 << 2), ["returns"], []),
        ("linux-x86_64", sized(8), 255 & ~(1 << 2) & ~(1 << 3), ["returns"], []),
        (
            "macos-arm64",
            sized(8),
            255 & ~(1 << 7),
            ["takes"],
            ["not trivial for calls, so C++ passes and returns it through a hidden pointer"],
        ),
        (
            "linux-x86_64",
            MISALIGNED,
            255,
            ["takes"],
            ["a misaligned member makes SysV pass it in memory, but ctypes uses registers"],
        ),
        ("macos-x86_64", replace(MISALIGNED, size=24), 255, ["takes"], []),
        ("windows-x86_64", MISALIGNED, 255, ["takes"], []),
        ("macos-arm64", MISALIGNED, 255, ["takes"], []),
    ],
)
def test_by_value_risks_follow_each_abi(
    target: str, record: ir.RecordLayout, facts: int, uses: list[str], found: list[str]
) -> None:
    assert probe.risks(target, record, facts, uses) == found


@pytest.fixture(scope="module")
def layouts() -> dict[str, ir.TargetLayout]:
    clang = pytest.importorskip("codegen.abi.clang", reason="needs the codegen dependency group")
    found, issues = clang.lay_out(INCLUDE, [FIXTURE.parent], ROOTS, abi.TARGETS)
    assert issues == []
    return {target.target: target for target in found.targets}


@pytest.fixture(scope="module")
def host(layouts: dict[str, ir.TargetLayout]) -> ir.TargetLayout:
    target = probe.host_target()
    if target is None:
        pytest.skip("this machine is not one of the targets")
    return layouts[target]


def test_the_fixture_layouts_match_the_native_compiler(
    host: ir.TargetLayout, compiler: Compiler
) -> None:
    report = probe.verify(host, INCLUDE, [FIXTURE.parent], compiler)
    assert report.failures == ()
    assert report.identity.target == host.target
    # Clang has no builtin that clears padding, so it reads no value bits at all.
    clang = "clang" in report.identity.compiler
    gaps = {gap.label for gap in report.gaps}
    assert {"Derived", "Polymorphic", "Mixed.(anonymous struct)"} <= gaps
    unread = gaps - {"Derived", "Polymorphic", "Mixed.(anonymous struct)"}
    if clang:
        assert {"Flags.fixed width", "Handle::Bits_t::Parts_t.domain width"} <= unread
        assert all(label.endswith((" value bits", " width")) for label in unread)
    else:
        assert unread == set()
    # Tagged's anonymous union and Mixed's other anonymous records are derived; Mixed's first
    # anonymous struct holds bit-fields, whose alignment C++ cannot measure.
    assert {item.label for item in report.derived} == {
        "Tagged.(anonymous union)",
        "Mixed.(anonymous union)",
        "Mixed.(anonymous union).(anonymous struct)",
    }
    found = probe.statuses(report, host)
    assert set(found["enum"].values()) == set(found["typedef"].values()) == {"verified"}
    fixed = {"Forward": "opaque", "Derived": "unsupported", "Polymorphic": "unsupported"}
    # Without value bits, only a record whose members leave no byte uncovered is fully verified:
    # Packed1 is, Plain keeps padding.
    partly = {gap.owner for gap in report.gaps} - set(fixed) if clang else {"Mixed"}
    if clang:
        assert "Plain" in partly
        assert "Packed1" not in partly
    assert found["record"] == {
        name: fixed.get(name)
        or ("partly verified" if name in partly else "derived" if name == "Tagged" else "verified")
        for name in found["record"]
    }
    # Every platform-dependent record was among those checked.
    assert {"Native", "Notice", "Extended", "WithLong", "Bits", "Handle"} <= set(found["record"])
    # Handle has a constructor and private members, so it is no aggregate.
    assert report.facts["Handle"] & 0b100 == 0
    assert report.facts["Plain"] == 255


def corrupt(layout: ir.TargetLayout) -> ir.TargetLayout:
    records = {item.name: item for item in layout.records}

    def change(record: str, index: int | None = None, /, **changes: object) -> None:
        found = records[record]
        if index is None:
            records[record] = replace(found, **changes)  # type: ignore[arg-type]
        else:
            fields = list(found.fields)
            fields[index] = replace(fields[index], **changes)  # type: ignore[arg-type]
            records[record] = replace(found, fields=tuple(fields))

    change("Plain", 2, offset=12)
    change("Packed1", size=8)
    change("Aligned", align=8)
    change("Pointers", 5, type="int")
    change("Outer", 1, type="float[3][2]")
    change("Handle::Bits_t::Parts_t", 1, bit_offset=33)
    change("Bits", 4, bit_width=2)
    change("Totals", 0, type="unsigned int")
    change("Value", kind="struct")
    change("Shown", 1, name="invented")
    change("Forward", status="complete", size=4, align=4)
    enums = tuple(
        replace(item, underlying="long long") if item.name == "Small" else item
        for item in layout.enums
    )
    typedefs = tuple(
        replace(item, size=4, align=4) if item.name == "Forward_t" else item
        for item in layout.typedefs
    )
    return replace(
        layout,
        records=tuple(records[name] for name in sorted(records)),
        enums=enums,
        typedefs=typedefs,
    )


def test_corrupted_layouts_are_caught_by_the_compiler(
    host: ir.TargetLayout, compiler: Compiler
) -> None:
    corrupted = corrupt(host)
    report = probe.verify(corrupted, INCLUDE, [FIXTURE.parent], compiler)
    found = {(item.label, item.message.split(":")[0]) for item in report.failures}
    assert found == {
        ("Plain.d", "offset"),
        ("Packed1", "size"),
        ("Aligned", "alignment"),
        ("Pointers.count", "the compiler's type is not int"),
        ("Outer.grid", "the compiler's type is not float[3][2]"),
        ("Outer.grid", "array dimensions"),
        ("Handle::Bits_t::Parts_t.slot", "bit offset"),
        ("Bits.d", "width"),
        ("Totals.total", "the compiler's type is not unsigned int"),
        ("Value", "the compiler says it is a union"),
        ("Shown.invented", "the compiler rejects it"),
        ("Forward", "the compiler rejects it"),
        ("Forward value bits", "the compiler rejects it"),
        ("Small", "the compiler's underlying type is not long long"),
        ("Forward_t", "it is incomplete"),
    } | (
        set()
        if "clang" in report.identity.compiler
        else {
            # The value bits catch the misplaced members a second, independent way.
            ("Plain value bits", f"bytes 8 to 12 {MISSING}"),
            ("Handle::Bits_t::Parts_t value bits", f"bits 32 to 33 {MISSING}"),
        }
    )
    messages = {(item.label, item.message) for item in report.failures}
    assert ("Bits.d", "width: extracted 2, compiler 1") in messages
    assert ("Handle::Bits_t::Parts_t.slot", "bit offset: extracted 33, compiler 32") in messages
    statuses = probe.statuses(report, corrupted)["record"]
    assert statuses["Shown"] == statuses["Plain"] == statuses["Forward"] == "mismatched"
    # Checks after a rejected one still ran; Packed4's members leave no padding, so it is fully
    # verified even where no value bits can be read.
    assert statuses["Packed4"] == "verified"


def test_a_member_the_extractor_dropped_is_found(host: ir.TargetLayout, compiler: Compiler) -> None:
    (plain,) = [item for item in host.records if item.name == "Plain"]
    dropped = replace(plain, fields=tuple(item for item in plain.fields if item.name != "i"))
    layout = replace(host, records=(dropped,), enums=(), typedefs=())
    report = probe.verify(layout, INCLUDE, [FIXTURE.parent], compiler)
    if "clang" in report.identity.compiler:
        # Without value bits nothing notices, and the report says the check was not possible.
        assert report.failures == ()
        assert [gap.label for gap in report.gaps] == ["Plain value bits"]
    else:
        assert [(item.label, item.message) for item in report.failures] == [
            (
                "Plain value bits",
                "bytes 4 to 8 hold a value that no extracted member covers, so a member is missing",
            )
        ]


def test_a_compiler_for_another_target_is_refused(
    layouts: dict[str, ir.TargetLayout], host: ir.TargetLayout, compiler: Compiler
) -> None:
    other = next(item for name, item in layouts.items() if name != host.target)
    with pytest.raises(ProbeError, match=f"builds for {host.target}, not {other.target}"):
        probe.verify(other, INCLUDE, [FIXTURE.parent], compiler, only=["Plain"])


def test_include_folders_may_contain_spaces(
    host: ir.TargetLayout, compiler: Compiler, tmp_path: Path
) -> None:
    folder = tmp_path / "with space"
    folder.mkdir()
    shutil.copy(FIXTURE, folder / FIXTURE.name)
    report = probe.verify(host, INCLUDE, [folder], compiler, only=["Plain", "Value"])
    assert report.failures == ()
    assert [check.label for check in report.checks][:1] == ["Plain"]


def test_verify_checks_an_sdk_and_fails_on_corrupted_metadata(
    make_sdk: MakeSdk,
    compiler: Compiler,
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("codegen.abi.clang", reason="needs the codegen dependency group")
    target = probe.host_target()
    if target is None:
        pytest.skip("this machine is not one of the targets")
    sdk, out = make_sdk(), tmp_path_factory.mktemp("out")
    cxx = ["--cxx", compiler.command[0]]
    assert main(["verify", str(sdk), "--no-overrides", *cxx]) == 0
    report = capsys.readouterr().out
    assert f"Layouts for {target} checked by " in report
    # WidgetPos_t has a private member, which the probe reads too.
    assert "  records            3  3 verified" in report.splitlines()
    assert (
        main(["extract", str(sdk), "--no-overrides", "--targets", target, "-o", str(out / "m")])
        == 0
    )
    data = (out / "m").read_text(encoding="utf-8").replace('"offset": 4', '"offset": 5', 1)
    (out / "bad").write_text(data, encoding="utf-8")
    capsys.readouterr()
    assert main(["verify", str(sdk), "--metadata", str(out / "bad"), *cxx]) == 1
    assert "offset: extracted 5, compiler 4" in capsys.readouterr().out
    assert main(["verify", str(sdk), "--metadata", str(out / "bad"), "--cxx", "no-such-cxx"]) == 2
    assert "Cannot verify: C++ compiler 'no-such-cxx' not found" in capsys.readouterr().err
    assert main(["verify", str(out), "--metadata", str(out / "m"), *cxx]) == 2
    assert "not found; pass the SDK folder" in capsys.readouterr().err
