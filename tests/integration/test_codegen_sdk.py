"""Extract the developer's own Steamworks SDK. Opt in with STEAMWORKS_SDK; CI never sets it."""

import os
from collections import Counter

import pytest

from codegen import ir
from codegen.abi import TARGETS
from codegen.extract import Extraction, extract
from codegen.extract.overrides import DEFAULT, load
from codegen.inventory import counts, layout_counts
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


@pytest.fixture(scope="module")
def extraction() -> Extraction:
    result = extract(SDK, load(DEFAULT.read_text(encoding="utf-8")), tuple(TARGETS))
    if result.metadata.sdk_version != "1.65":
        pytest.skip(f"expects SDK 1.65, found {result.metadata.sdk_version}")
    return result


def test_sdk_165_inventory(extraction: Extraction) -> None:
    assert counts(extraction) == SDK_165
    assert layout_counts(extraction.metadata) == LAYOUTS_165


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
        ("warning", "unresolved-type"): 8,
    }


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
