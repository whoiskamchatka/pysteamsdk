import json
from collections import Counter
from typing import Any

import pytest

from codegen.extract.api_json import MetadataError, load
from codegen.ir import Issue


def codes(issues: tuple[Issue, ...]) -> Counter[tuple[str, str]]:
    return Counter((issue.severity, issue.code) for issue in issues)


def test_valid_metadata_is_normalized(api: dict[str, Any]) -> None:
    found = load(json.dumps(api))
    (widgets,) = found.interfaces
    assert (widgets.name, widgets.version) == ("IWidgets", "Widgets007")
    (accessor,) = widgets.accessors
    assert (accessor.kind, accessor.flat_name) == ("user", "WidgetAPI_Widgets_v007")
    owner, fetch, _, _ = widgets.methods
    assert owner.returns.spelling == "WidgetOwner_t"
    assert owner.flat_returns is not None
    assert owner.flat_returns.base == "uint64_owner"
    assert fetch.flat_name == "WidgetAPI_IWidgets_Fetch"
    assert fetch.callresult == "WidgetReady_t"
    assert fetch.source == "steam_api.json#/interfaces/0/methods/1"
    assert [p.name for p in fetch.params] == ["owner", "pch", "cch"]
    assert fetch.params[0].flat_type is not None
    assert fetch.params[1].annotations == {"out_string_count": "cch"}
    assert fetch.params[2].flat_type is None

    structs = {struct.name: struct for struct in found.structs}
    assert structs["WidgetReady_t"].callback_id == 701
    assert structs["WidgetPos_t"].callback_id is None
    assert [(f.name, f.type.array, f.private) for f in structs["WidgetPos_t"].fields] == [
        ("m_rgflCoords", 3, False),
        ("m_nSecret", None, True),
    ]
    assert structs["WidgetPos_t"].methods[0].flat_name == "WidgetAPI_WidgetPos_t_IsSet"

    enums = {enum.qualified_name: enum for enum in found.enums}
    assert enums["IWidgets::EShape"].scope == "IWidgets"
    assert [(v.name, v.value) for v in enums["EWidgetResult"].values] == [
        ("k_EWidgetOK", 1),
        ("k_EWidgetFail", -2),
    ]
    constants = {constant.name: constant for constant in found.constants}
    assert constants["k_nWidgetCombo"].expression == "k_nWidgetMax | 1"
    # Values need every constant and typedef, so extract evaluates them afterwards.
    assert {constant.value for constant in found.constants} == {None}
    typedefs = {typedef.name: typedef for typedef in found.typedefs}
    assert typedefs["WidgetHook_t"].type.kind == "function_pointer"
    assert codes(found.issues) == {("note", "documentation-omitted"): 1}


def test_valve_prose_never_reaches_the_ir(api: dict[str, Any]) -> None:
    found = load(json.dumps(api))
    assert "invented description" not in repr(found)


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(MetadataError, match="not valid JSON"):
        load('{"interfaces": [')


def test_every_missing_required_field_is_listed(api: dict[str, Any]) -> None:
    del api["typedefs"]
    del api["interfaces"][0]["methods"][1]["methodname_flat"]
    del api["callback_structs"][0]["callback_id"]
    del api["enums"][0]["values"][1]["value"]
    with pytest.raises(MetadataError) as raised:
        load(json.dumps(api))
    assert sorted(raised.value.problems) == [
        "steam_api.json#/callback_structs/0: missing required key 'callback_id'",
        "steam_api.json#/enums/0/values/1: missing required key 'value'",
        "steam_api.json#/interfaces/0/methods/1: missing required key 'methodname_flat'",
        "steam_api.json#: missing required key 'typedefs'",
    ]


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (("callback_structs", 0, "callback_id"), "701", "expected an integer, found a string"),
        (("callback_structs", 0, "callback_id"), True, "expected an integer, found a boolean"),
        (("structs", 1, "fields", 1, "private"), "yes", "expected a boolean, found a string"),
        (("interfaces", 0, "methods", 2, "params", 0, "paramtype"), 5, "expected a string"),
        (("interfaces", 0, "methods"), {}, "expected a list, found an object"),
        (("enums", 0), [], "expected an object, found a list"),
    ],
)
def test_values_of_the_wrong_type_are_rejected(
    api: dict[str, Any], path: tuple[str | int, ...], value: object, expected: str
) -> None:
    node: Any = api
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(MetadataError, match=expected):
        load(json.dumps(api))


def test_unknown_keys_are_reported_with_their_location(api: dict[str, Any]) -> None:
    api["future_section"] = []
    api["interfaces"][0]["methods"][0]["noexcept"] = True
    found = load(json.dumps(api))
    unknown = [(i.severity, i.source) for i in found.issues if i.code == "unknown-key"]
    assert unknown == [
        ("warning", "steam_api.json#/future_section"),
        ("warning", "steam_api.json#/interfaces/0/methods/0/noexcept"),
    ]


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(MetadataError, match="duplicate key"):
        load('{"typedefs": [], "typedefs": []}')


def test_unsupported_type_syntax_is_kept_and_reported(api: dict[str, Any]) -> None:
    api["structs"][1]["fields"][0]["fieldtype"] = "float const [3]"
    found = load(json.dumps(api))
    (pos,) = [s for s in found.structs if s.name == "WidgetPos_t"]
    assert pos.fields[0].type.kind == "unparsed"
    assert pos.fields[0].type.spelling == "float const [3]"
    assert ("warning", "unsupported-type-syntax") in codes(found.issues)


def test_a_missing_version_is_kept_as_none(api: dict[str, Any]) -> None:
    del api["interfaces"][0]["version_string"]
    assert load(json.dumps(api)).interfaces[0].version is None


@pytest.mark.parametrize(
    ("spelling", "value"),
    [
        ("0", 0),
        ("-2", -2),
        ("- 1", -1),
        ("0x1F", 31),
        ("0xffffffffffffffffull", 2**64 - 1),
        ("010", 8),
        ("1 << 4", 16),
        ("600.f", None),
        ("k_EOther", None),
    ],
)
def test_enum_values_are_evaluated_when_they_name_nothing(
    api: dict[str, Any], spelling: str, value: int | None
) -> None:
    api["enums"][0]["values"][0]["value"] = spelling
    found = load(json.dumps(api))
    (enum, _) = found.enums
    assert (enum.values[0].spelling, enum.values[0].value) == (spelling, value)
    unevaluated = ("warning", "unevaluated-enum-value") in codes(found.issues)
    assert unevaluated == (value is None)
