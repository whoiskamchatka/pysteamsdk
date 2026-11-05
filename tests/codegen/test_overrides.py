from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from codegen import ir
from codegen.extract import MetadataError, extract
from codegen.extract.overrides import DEFAULT, load

MakeSdk = Callable[..., Path]

INTERFACE_VERSION = """
[[interface_version]]
interface = "IWidgets"
macro = "WIDGETS_INTERFACE_VERSION"
version = "Widgets007"
reason = "invented"
"""
SHARED_CALLBACK = """
[[shared_callback]]
callback_id = 701
structs = ["WidgetGone_t", "WidgetReady_t"]
reason = "invented"
"""
EXTERNAL_TYPES = """
[[external_type]]
name = "CWidgetID"
declaration = "class"
header = "widgetid.h"
reason = "invented"

[[external_type]]
name = "WidgetHandle_t"
declaration = "incomplete"
header = "widgetid.h"
reason = "invented"
"""
WIDGETID_H = """\
// A CWidgetID is passed around by value.
class CWidgetID
{
public:
	unsigned long long m_ulID;
};
struct WidgetHandle_t;
"""
HEADERS = {
    "iwidgets.h": '#define WIDGETS_INTERFACE_VERSION "Widgets007"\n',
    "widgetid.h": WIDGETID_H,
}


def codes(metadata: ir.Metadata) -> Counter[tuple[str, str]]:
    return Counter((issue.severity, issue.code) for issue in metadata.issues)


def mismatches(metadata: ir.Metadata) -> list[str]:
    return [issue.message for issue in metadata.issues if issue.code == "override-mismatch"]


def test_the_reviewed_overrides_file_is_well_formed() -> None:
    reviewed = load(DEFAULT.read_text(encoding="utf-8"))
    sources = [
        *(entry.source for entry in reviewed.interface_versions),
        *(entry.source for entry in reviewed.shared_callbacks),
        *(entry.source for entry in reviewed.external_types),
    ]
    assert sources
    assert all(source.startswith("overrides.toml#/") for source in sources)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[[interface_version]\n", "overrides.toml: "),
        ("[[enum_value]]\nreason = 'x'\n", "unknown table 'enum_value'"),
        ("interface_version = 1\n", "interface_version must be an array of tables"),
        (INTERFACE_VERSION.replace('reason = "invented"', ""), "missing 'reason'"),
        (INTERFACE_VERSION.replace('"invented"', '"  "'), "'reason' must say why"),
        (INTERFACE_VERSION + 'flat = "x"\n', "/interface_version/0: unknown key 'flat'"),
        (INTERFACE_VERSION + INTERFACE_VERSION, "'IWidgets' is already overridden"),
        (
            (INTERFACE_VERSION * 2).replace('"IWidgets"', '["IWidgets"]'),
            "'interface' must be a string",
        ),
        (SHARED_CALLBACK.replace("701", '"701"'), "'callback_id' must be an integer"),
        (SHARED_CALLBACK.replace("701", "true"), "'callback_id' must be an integer"),
        (SHARED_CALLBACK.replace('"WidgetGone_t"', "1"), "'structs' must list struct names"),
        (EXTERNAL_TYPES.replace('"class"', '"enum"'), "'declaration' must be one of class"),
    ],
)
def test_malformed_overrides_are_rejected(text: str, problem: str) -> None:
    with pytest.raises(MetadataError) as raised:
        load(text)
    assert any(problem in message for message in raised.value.problems)


def test_an_interface_version_from_a_header_macro(make_sdk: MakeSdk, api: dict[str, Any]) -> None:
    del api["interfaces"][0]["version_string"]
    without = extract(make_sdk(api, name="without")).metadata
    assert without.interfaces[0].version is None
    assert ("note", "interface-without-version") in codes(without)
    assert ("note", "version-only-in-headers") in codes(without)

    metadata = extract(make_sdk(api, name="with"), load(INTERFACE_VERSION)).metadata
    (interface,) = metadata.interfaces
    assert interface.version == "Widgets007"
    assert interface.version_source == "overrides.toml#/interface_version/0"
    assert set(codes(metadata)) == {("note", "documentation-omitted")}


@pytest.mark.parametrize(
    ("keep_json_version", "headers", "problem"),
    [
        (True, HEADERS, "steam_api.json now gives IWidgets version 'Widgets007'"),
        (
            False,
            {"iwidgets.h": '#define WIDGETS_INTERFACE_VERSION "Widgets008"\n'},
            "WIDGETS_INTERFACE_VERSION is 'Widgets008' in the headers, not 'Widgets007'",
        ),
        (False, {"iwidgets.h": "\n"}, "WIDGETS_INTERFACE_VERSION is not defined in the headers"),
    ],
)
def test_an_interface_version_that_no_longer_matches_is_an_error(
    make_sdk: MakeSdk,
    api: dict[str, Any],
    keep_json_version: bool,
    headers: dict[str, str],
    problem: str,
) -> None:
    if not keep_json_version:
        del api["interfaces"][0]["version_string"]
    metadata = extract(make_sdk(api, headers=headers), load(INTERFACE_VERSION)).metadata
    assert mismatches(metadata) == [problem]
    assert metadata.interfaces[0].version_source is None


def test_an_override_for_a_missing_interface_is_an_error(make_sdk: MakeSdk) -> None:
    text = INTERFACE_VERSION.replace('"IWidgets"', '"IGadgets"')
    metadata = extract(make_sdk(), load(text)).metadata
    assert mismatches(metadata) == ["IGadgets is not in steam_api.json"]


def add_gone(api: dict[str, Any], fields: int = 2) -> None:
    ready = api["callback_structs"][0]
    gone = {"callback_id": 701, "struct": "WidgetGone_t", "fields": ready["fields"][:fields]}
    api["callback_structs"].append(gone)


def test_a_shared_callback_id_accepted_by_review(make_sdk: MakeSdk, api: dict[str, Any]) -> None:
    add_gone(api)
    assert ("warning", "shared-callback-id") in codes(extract(make_sdk(api, name="a")).metadata)
    metadata = extract(make_sdk(api, name="b"), load(SHARED_CALLBACK)).metadata
    assert set(codes(metadata)) == {("note", "documentation-omitted")}
    assert metadata.shared_callbacks == (
        ir.SharedCallback(
            701, ("WidgetGone_t", "WidgetReady_t"), "overrides.toml#/shared_callback/0"
        ),
    )


def test_a_shared_callback_review_fails_once_the_fields_differ(
    make_sdk: MakeSdk, api: dict[str, Any]
) -> None:
    add_gone(api, fields=1)
    metadata = extract(make_sdk(api), load(SHARED_CALLBACK)).metadata
    (issue,) = [i for i in metadata.issues if i.severity == "error"]
    assert (issue.code, issue.source) == (
        "shared-callback-mismatch",
        "overrides.toml#/shared_callback/0",
    )


def use_external_types(api: dict[str, Any]) -> None:
    fields = api["structs"][0]["fields"]
    fields.append({"fieldname": "m_id", "fieldtype": "CWidgetID"})
    fields.append({"fieldname": "m_pHandle", "fieldtype": "WidgetHandle_t *"})


def test_external_types_are_located_in_the_headers(make_sdk: MakeSdk, api: dict[str, Any]) -> None:
    use_external_types(api)
    without = extract(make_sdk(api, headers=HEADERS, name="a")).metadata
    assert codes(without)[("warning", "unresolved-type")] == 2

    metadata = extract(make_sdk(api, headers=HEADERS, name="b"), load(EXTERNAL_TYPES)).metadata
    assert set(codes(metadata)) == {("note", "documentation-omitted")}
    assert [(e.name, e.declaration, e.header) for e in metadata.external_types] == [
        ("CWidgetID", "class", "widgetid.h"),
        ("WidgetHandle_t", "incomplete", "widgetid.h"),
    ]


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ('header = "widgetid.h"', 'header = "gadget.h"', "gadget.h is not an SDK header"),
        ('"class"', '"struct"', "widgetid.h does not define struct CWidgetID"),
        ('"class"', '"typedef"', "widgetid.h declares no typedef CWidgetID"),
        ('"incomplete"', '"class"', "widgetid.h does not define class WidgetHandle_t"),
    ],
)
def test_external_types_must_match_their_headers(
    make_sdk: MakeSdk, api: dict[str, Any], old: str, new: str, problem: str
) -> None:
    use_external_types(api)
    text = EXTERNAL_TYPES.replace(old, new, 1)
    metadata = extract(make_sdk(api, headers=HEADERS), load(text)).metadata
    assert problem in mismatches(metadata)


@pytest.mark.parametrize(
    ("header", "problem"),
    [
        (
            WIDGETID_H + "struct WidgetHandle_t { int m_n; };\n",
            "WidgetHandle_t is defined in widgetid.h, so it is not incomplete",
        ),
        (
            WIDGETID_H.replace("class CWidgetID\n{", "// class CWidgetID {\n{"),
            "widgetid.h does not define class CWidgetID",
        ),
        (
            WIDGETID_H.replace("struct WidgetHandle_t;", "/* struct WidgetHandle_t; */"),
            "widgetid.h does not declare WidgetHandle_t",
        ),
    ],
)
def test_header_checks_ignore_comments_and_notice_definitions(
    make_sdk: MakeSdk, api: dict[str, Any], header: str, problem: str
) -> None:
    use_external_types(api)
    headers = {**HEADERS, "widgetid.h": header}
    metadata = extract(make_sdk(api, headers=headers), load(EXTERNAL_TYPES)).metadata
    assert problem in mismatches(metadata)


def test_an_incomplete_type_used_by_value_is_an_error(
    make_sdk: MakeSdk, api: dict[str, Any]
) -> None:
    use_external_types(api)
    api["structs"][0]["fields"].append({"fieldname": "m_handle", "fieldtype": "WidgetHandle_t"})
    metadata = extract(make_sdk(api, headers=HEADERS), load(EXTERNAL_TYPES)).metadata
    assert ("error", "incomplete-type-by-value") in codes(metadata)


def test_an_external_type_nothing_uses_is_an_error(make_sdk: MakeSdk) -> None:
    metadata = extract(make_sdk(headers=HEADERS), load(EXTERNAL_TYPES)).metadata
    assert codes(metadata)[("error", "unused-external-type")] == 2


def test_an_external_type_steam_api_json_now_describes_is_an_error(
    make_sdk: MakeSdk, api: dict[str, Any]
) -> None:
    use_external_types(api)
    api["structs"].append({"struct": "CWidgetID", "fields": []})
    metadata = extract(make_sdk(api, headers=HEADERS), load(EXTERNAL_TYPES)).metadata
    (issue,) = [i for i in metadata.issues if i.code == "duplicate-declaration"]
    assert "overrides.toml#/external_type/0" in issue.message
