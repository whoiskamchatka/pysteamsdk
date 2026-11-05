"""Extract the developer's own Steamworks SDK. Opt in with STEAMWORKS_SDK; CI never sets it."""

import os
from collections import Counter

import pytest

from codegen import ir
from codegen.extract import Extraction, extract
from codegen.extract.overrides import DEFAULT, load
from codegen.inventory import counts
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


@pytest.fixture(scope="module")
def extraction() -> Extraction:
    result = extract(SDK, load(DEFAULT.read_text(encoding="utf-8")))
    if result.metadata.sdk_version != "1.65":
        pytest.skip(f"expects SDK 1.65, found {result.metadata.sdk_version}")
    return result


def test_sdk_165_inventory(extraction: Extraction) -> None:
    assert counts(extraction) == SDK_165


def test_sdk_165_has_only_notes_with_the_reviewed_overrides(extraction: Extraction) -> None:
    issues = Counter((i.severity, i.code) for i in extraction.metadata.issues)
    assert issues == {
        ("note", "documentation-omitted"): 17,
        ("note", "interface-without-version"): 6,
        ("note", "version-only-in-headers"): 2,
        ("note", "wrapped-constant"): 1,
    }


def test_sdk_165_without_overrides_has_only_the_warnings_they_resolve(
    extraction: Extraction,
) -> None:
    issues = Counter((i.severity, i.code) for i in extract(SDK).metadata.issues)
    assert {key: count for key, count in issues.items() if key[0] != "note"} == {
        ("warning", "shared-callback-id"): 1,
        ("warning", "unresolved-type"): 8,
    }


def test_sdk_extraction_is_deterministic(extraction: Extraction) -> None:
    again = extract(SDK, load(DEFAULT.read_text(encoding="utf-8")))
    assert ir.dumps(again.metadata) == ir.dumps(extraction.metadata)


def test_sdk_metadata_round_trips_and_validates(extraction: Extraction) -> None:
    text = ir.dumps(extraction.metadata)
    metadata = ir.loads(text)
    assert metadata == extraction.metadata
    assert ir.dumps(metadata) == text
    assert validate(metadata) == []
