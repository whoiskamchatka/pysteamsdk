"""Extract the developer's own Steamworks SDK. Opt in with STEAMWORKS_SDK; CI never sets it."""

import os
from collections import Counter

import pytest

from codegen import ir
from codegen.extract import Extraction, extract
from codegen.inventory import counts

SDK = os.environ.get("STEAMWORKS_SDK", "")

pytestmark = [
    pytest.mark.sdk,
    pytest.mark.skipif(not SDK, reason="set STEAMWORKS_SDK to a Steamworks SDK folder"),
]

# Counts only, so nothing of Valve's is copied into the repository.
SDK_165 = {
    "interfaces": 34,
    "versioned_interfaces": 27,
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
    "constant_expressions": 11,
    "typedefs_json": 69,
    "typedefs_flat": 2,
    "other_exports": 48,
    "version_macros": 30,
}


@pytest.fixture(scope="module")
def extraction() -> Extraction:
    result = extract(SDK)
    if result.metadata.sdk_version != "1.65":
        pytest.skip(f"expects SDK 1.65, found {result.metadata.sdk_version}")
    return result


def test_sdk_165_inventory(extraction: Extraction) -> None:
    assert counts(extraction) == SDK_165


def test_sdk_165_has_no_errors_and_only_known_warnings(extraction: Extraction) -> None:
    issues = Counter((i.severity, i.code) for i in extraction.metadata.issues)
    assert issues == {
        ("warning", "shared-callback-id"): 1,
        ("warning", "unresolved-type"): 8,
        ("note", "documentation-omitted"): 17,
        ("note", "interface-without-version"): 7,
        ("note", "unevaluated-constant"): 11,
        ("note", "version-only-in-headers"): 3,
    }


def test_sdk_extraction_is_deterministic(extraction: Extraction) -> None:
    assert ir.dumps(extract(SDK).metadata) == ir.dumps(extraction.metadata)
