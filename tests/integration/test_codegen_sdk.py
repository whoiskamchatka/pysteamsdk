"""Extract the developer's own Steamworks SDK. Opt in with STEAMWORKS_SDK; CI never sets it."""

import os
from collections import Counter
from pathlib import Path

import pytest

from codegen import ir
from codegen.abi import TARGETS, Roots, probe
from codegen.abi.checks import by_value_records, parse
from codegen.extract import Extraction, extract, layout_source
from codegen.extract.overrides import DEFAULT, load
from codegen.inventory import by_value_counts, counts, layout_counts
from codegen.validate import validate

SDK = os.environ.get("STEAMWORKS_SDK", "")

pytestmark = [
    pytest.mark.sdk,
    pytest.mark.skipif(not SDK, reason="set STEAMWORKS_SDK to a Steamworks SDK folder"),
]

# Counts only, so nothing of Valve's is copied into the repository.
SDK_165 = {
    "interfaces": 34,
    "versioned_interfaces": 28,
    "accessors": 34,
    "interface_methods": 921,
    "struct_methods": 66,
    "flat_functions_json": 1021,
    "flat_functions_header": 1021,
    "inline_accessors": 34,
    "structs": 29,
    "callback_structs": 196,
    "enums": 116,
    "nested_enums": 4,
    "enum_values": 2574,
    "constants": 104,
    "unevaluated_constants": 0,
    "typedefs_json": 69,
    "typedefs_flat": 2,
    "external_types": 8,
    "shared_callbacks": 1,
    "other_exports": 48,
    "version_macros": 30,
}
LAYOUTS_165 = {
    **{
        f"{target}_{kind}": count
        for target in TARGETS
        for kind, count in (
            ("records", 246),
            ("complete", 241),
            ("opaque", 5),
            ("unsupported", 0),
            ("enums", 117),
            ("typedefs", 72),
        )
    },
    # Callback structs are packed to 8 bytes on Windows and to 4 elsewhere, and enums without a
    # fixed type are int on Windows and unsigned int elsewhere unless a value is negative.
    "records_differing": 97,
    "enums_differing": 110,
}
# As codegen/overrides.toml classifies the records passed by value; macOS is unverified.
BY_VALUE_165 = {
    "by_value_records": 5,
    **{f"{target}_{kind}": 0 for target in TARGETS for kind in ("direct", "shim", "unverified")},
    "windows-x86_64_direct": 5,
    "linux-x86_64_direct": 4,
    "linux-x86_64_shim": 1,
    "macos-x86_64_unverified": 5,
    "macos-arm64_unverified": 5,
}
# The records in tests/codegen/fixtures/calls.h whose calls stand for the SDK's by-value ones.
LOOK_ALIKES = {
    "Address": "SteamIPAddress_t",
    "Button": "InputDigitalActionData_t",
    "Location": "SteamPartyBeaconLocation_t",
    "Motion": "InputMotionData_t",
    "Stick": "InputAnalogActionData_t",
}


@pytest.fixture(scope="module")
def extraction() -> Extraction:
    result = extract(SDK, load(DEFAULT.read_text(encoding="utf-8")), tuple(TARGETS))
    if result.metadata.sdk_version != "1.65":
        pytest.skip(f"expects SDK 1.65, found {result.metadata.sdk_version}")
    return result


def test_sdk_165_inventory(extraction: Extraction) -> None:
    assert counts(extraction) == SDK_165
    assert layout_counts(extraction.metadata) == LAYOUTS_165
    assert by_value_counts(extraction.metadata) == BY_VALUE_165


def test_sdk_165_with_the_reviewed_overrides_has_only_layout_warnings(
    extraction: Extraction,
) -> None:
    # The warnings are findings for the binding generator, not problems in the metadata:
    # two structs whose definitions Valve does not ship, six field lists steam_api.json
    # simplifies, and eight functions that pass packed or union records by value.
    issues = Counter((i.severity, i.code) for i in extraction.metadata.issues)
    assert issues == {
        ("warning", "by-value-record"): 8,
        ("warning", "json-fields-differ"): 6,
        ("warning", "missing-layout"): 2,
        ("note", "documentation-omitted"): 17,
        ("note", "interface-without-version"): 6,
        ("note", "version-only-in-headers"): 2,
        ("note", "wrapped-constant"): 1,
        ("note", "wrapped-enum-value"): 1,
    }


def test_sdk_165_layouts_differ_only_between_windows_and_the_rest(
    extraction: Extraction,
) -> None:
    assert extraction.metadata.abi is not None
    linux, *others = [t for t in extraction.metadata.abi.targets if t.target != "windows-x86_64"]
    for other in others:
        assert [(r.name, r.size, r.align) for r in other.records] == [
            (r.name, r.size, r.align) for r in linux.records
        ]
        assert other.enums == linux.enums


def test_sdk_165_without_overrides_has_only_the_warnings_they_resolve(
    extraction: Extraction,
) -> None:
    issues = Counter((i.severity, i.code) for i in extract(SDK).metadata.issues)
    assert {key: count for key, count in issues.items() if key[0] != "note"} == {
        ("warning", "shared-callback-id"): 1,
        ("warning", "unreviewed-by-value"): 5,
        ("warning", "unresolved-type"): 8,
    }


def test_sdk_165_layouts_match_the_native_compiler(extraction: Extraction) -> None:
    # Checked natively with MSVC 19.44 on Windows and GCC 15.2 on Linux. Six anonymous unions are
    # derived from what the compiler measured. Three records are not trivially copyable and keep
    # padding, so a member hidden in it cannot be ruled out without undefined behavior.
    target = probe.host_target()
    if target is None:
        pytest.skip("this machine is not one of the targets")
    try:
        compiler = probe.find_compiler()
    except probe.ProbeError as exc:
        pytest.skip(str(exc))
    assert extraction.metadata.abi is not None
    (layout,) = [item for item in extraction.metadata.abi.targets if item.target == target]
    report = probe.verify(layout, *layout_source(SDK), compiler)
    assert report.failures == ()
    found = {
        group: Counter(items.values()) for group, items in probe.statuses(report, layout).items()
    }
    if "clang" in report.identity.compiler:
        # Clang reads no value bits, so far more records are only partly verified.
        assert "mismatched" not in found["record"]
    else:
        assert found == {
            "record": {"verified": 232, "derived": 6, "partly verified": 3, "opaque": 5},
            "enum": {"verified": 117},
            "typedef": {"verified": 72},
        }
        assert len(report.gaps) == 3
    # Five records are passed by value. C++ passes none differently from C, but off Windows a
    # callback packed to 4 leaves a 64-bit member misaligned, which ctypes passes wrongly.
    records = {item.name: item for item in layout.records}
    uses: dict[str, set[str]] = {}
    for _, verb, name in by_value_records(extraction.metadata):
        uses.setdefault(name, set()).add(verb)
    assert len(uses) == 5
    risky = [
        name
        for name, verbs in uses.items()
        if probe.risks(target, records[name], report.facts[name], verbs)
    ]
    assert len(risky) == {"linux-x86_64": 1, "macos-x86_64": 1}.get(target, 0)


def test_the_call_tests_stand_for_every_record_passed_by_value(extraction: Extraction) -> None:
    clang = pytest.importorskip("codegen.abi.clang", reason="needs the codegen dependency group")
    fixtures = Path(__file__).parents[1] / "codegen" / "fixtures"
    roots = Roots(records=tuple(LOOK_ALIKES))
    found, issues = clang.lay_out('#include "calls.h"\n', [fixtures], roots, TARGETS)
    assert issues == []
    assert extraction.metadata.abi is not None
    assert {name for _, _, name in by_value_records(extraction.metadata)} == set(
        LOOK_ALIKES.values()
    )
    for ours, theirs in zip(found.targets, extraction.metadata.abi.targets, strict=True):
        assert ours.target == theirs.target
        mine = {item.name: item for item in ours.records}
        sdk = {item.name: item for item in theirs.records}
        for name, record in LOOK_ALIKES.items():
            assert shape(mine[name], mine) == shape(sdk[record], sdk), (ours.target, name)


def shape(record: ir.RecordLayout, records: dict[str, ir.RecordLayout]) -> object:
    """What passing a record by value depends on: its size and alignment, and where each scalar
    sits and whether it is an integer, a pointer or which floating-point type."""
    return record.size, record.align, sorted(_scalars(record, records, 0))


def _scalars(
    record: ir.RecordLayout, records: dict[str, ir.RecordLayout], start: int
) -> list[tuple[int, int, str]]:
    found = []
    for item in record.fields:
        offset = start + (item.offset or 0)
        if item.record is not None:
            found += _scalars(item.record, records, offset)
            continue
        parsed = parse(item.type)
        assert parsed is not None
        assert item.bit_width is None
        element = item.size // parsed.count
        reference = parsed.by_value()
        for index in range(parsed.count):
            at = offset + index * element
            if reference is not None and reference[0] != "enum":
                found += _scalars(records[reference[1]], records, at)
            elif parsed.pointers:
                found.append((at, element, "pointer"))
            elif parsed.base in ("float", "double", "long double"):
                found.append((at, element, parsed.base))
            else:
                found.append((at, element, "integer"))
    return found


def test_sdk_extraction_is_deterministic(extraction: Extraction) -> None:
    again = extract(SDK, load(DEFAULT.read_text(encoding="utf-8")), tuple(reversed(TARGETS)))
    assert ir.dumps(again.metadata) == ir.dumps(extraction.metadata)


def test_sdk_metadata_round_trips_and_validates(extraction: Extraction) -> None:
    text = ir.dumps(extraction.metadata)
    metadata = ir.loads(text)
    assert metadata == extraction.metadata
    assert ir.dumps(metadata) == text
    found = validate(metadata)
    assert set(found) <= set(metadata.issues)
    assert not [issue for issue in found if issue.severity == "error"]
