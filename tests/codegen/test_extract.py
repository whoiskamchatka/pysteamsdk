import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from codegen import ir
from codegen.extract import MetadataError, extract

MakeSdk = Callable[..., Path]


def codes(metadata: ir.Metadata) -> Counter[tuple[str, str]]:
    return Counter((issue.severity, issue.code) for issue in metadata.issues)


def test_a_consistent_sdk_extracts_without_errors_or_warnings(make_sdk: MakeSdk) -> None:
    metadata = extract(make_sdk()).metadata
    assert {severity for severity, _ in codes(metadata)} == {"note"}
    assert metadata.sdk_version == "9.99"
    assert [item.path for item in metadata.inputs] == [
        "public/steam/steam_api.json",
        "public/steam/iwidgets.h",
        "public/steam/steam_api_flat.h",
        "public/steam/widget_api.h",
        "public/steam/widget_types.h",
        "Readme.txt",
    ]
    (flat_typedef,) = [t for t in metadata.typedefs if t.source.startswith("steam_api_flat.h")]
    assert (flat_typedef.name, flat_typedef.type.base) == ("uint64_owner", "uint64")
    assert flat_typedef.source == "steam_api_flat.h:5"
    assert [(e.name, e.source) for e in metadata.exports] == [
        ("WidgetAPI_Init", "widget_api.h:1"),
        ("WidgetAPI_Shutdown", "widget_api.h:2"),
    ]


def test_constants_are_evaluated_with_their_provenance(
    make_sdk: MakeSdk, api: dict[str, Any]
) -> None:
    api["consts"].append({"constname": "k_nWidgetHalf", "consttype": "int", "constval": "1 / 2"})
    metadata = extract(make_sdk(api)).metadata
    found = {c.name: (c.value, c.expression, c.source) for c in metadata.constants}
    assert found == {
        "k_nWidgetCombo": (17, "k_nWidgetMax | 1", "steam_api.json#/consts/2"),
        "k_nWidgetHalf": (None, "1 / 2", "steam_api.json#/consts/3"),
        "k_nWidgetMax": (16, "16", "steam_api.json#/consts/0"),
        "k_unWidgetMask": (255, "0xFF", "steam_api.json#/consts/1"),
    }
    (issue,) = [i for i in metadata.issues if i.severity != "note"]
    assert (issue.code, issue.source) == ("unevaluated-constant", "steam_api.json#/consts/3")
    assert issue.message == "k_nWidgetHalf = '1 / 2': '/' is not supported here"


@pytest.mark.parametrize(
    ("old", "new", "issue"),
    [
        (
            "S_API bool WidgetAPI_WidgetPos_t_IsSet( WidgetPos_t* self );\n",
            "",
            "flat-function-missing",
        ),
        ("#endif", "S_API void WidgetAPI_Extra();\n#endif", "flat-function-undescribed"),
        ("int cch );", "unsigned int cch );", "flat-signature-mismatch"),
        (
            "S_API bool WidgetAPI_WidgetPos",
            "S_API int WidgetAPI_WidgetPos",
            "flat-signature-mismatch",
        ),
        ("( IWidgets* self, WidgetHook_t pfn )", "( IWidgets* self )", "flat-signature-mismatch"),
        ("uint64_owner owner,", "WidgetOwner_t owner,", "flat-signature-mismatch"),
        (
            "#endif",
            "S_API bool WidgetAPI_WidgetPos_t_IsSet( WidgetPos_t* self );\n#endif",
            "duplicate-flat-function",
        ),
        ("#endif", "extern int g_nWidgets;\n#endif", "unparsed-flat-line"),
        ("#endif", "S_API void WidgetAPI_Bad( int const * p );\n#endif", "unparsed-flat-line"),
        (
            "{ return WidgetAPI_Widgets_v007(); }",
            "{ return WidgetAPI_Widgets_v008(); }",
            "inline-accessor-target",
        ),
    ],
)
def test_flat_header_disagreements_are_errors(
    make_sdk: MakeSdk, flat_header: str, old: str, new: str, issue: str
) -> None:
    assert flat_header.count(old) == 1
    metadata = extract(make_sdk(flat=flat_header.replace(old, new))).metadata
    assert ("error", issue) in codes(metadata)


def test_a_renamed_flat_parameter_is_only_a_warning(make_sdk: MakeSdk, flat_header: str) -> None:
    metadata = extract(make_sdk(flat=flat_header.replace("eShape )", "eNewShape )"))).metadata
    assert codes(metadata)[("warning", "flat-parameter-name")] == 1
    assert not [issue for issue in metadata.issues if issue.severity == "error"]


def test_types_defined_nowhere_are_reported_once_each(
    make_sdk: MakeSdk, api: dict[str, Any], flat_header: str
) -> None:
    api["structs"][0]["fields"].append({"fieldname": "m_pOpaque", "fieldtype": "Opaque_t *"})
    api["structs"][0]["fields"].append({"fieldname": "m_pOther", "fieldtype": "Opaque_t *"})
    api["typedefs"].append({"typedef": "Width_t", "type": "size_t"})
    metadata = extract(make_sdk(api)).metadata
    unresolved = [i.message for i in metadata.issues if i.code == "unresolved-type"]
    assert unresolved == ["Opaque_t is used 2 times but defined nowhere in the metadata"]


def test_versions_are_matched_with_header_macros(make_sdk: MakeSdk, api: dict[str, Any]) -> None:
    api["interfaces"][0]["version_string"] = "Widgets008"
    headers = {"iwidgets.h": '#define WIDGETS_INTERFACE_VERSION "Widgets007"\n'}
    metadata = extract(make_sdk(api, headers=headers)).metadata
    assert metadata.interfaces[0].version == "Widgets008"
    assert ("warning", "version-not-in-headers") in codes(metadata)
    assert ("note", "version-only-in-headers") in codes(metadata)


def test_missing_files_and_bad_encoding_are_fatal(tmp_path: Path, make_sdk: MakeSdk) -> None:
    with pytest.raises(MetadataError, match=r"steam_api\.json not found"):
        extract(tmp_path)
    sdk = make_sdk()
    (sdk / "public" / "steam" / "steam_api.json").write_bytes(b'{"x": "\xff"}')
    with pytest.raises(MetadataError, match="not UTF-8"):
        extract(sdk)


def test_output_is_deterministic(make_sdk: MakeSdk) -> None:
    sdk = make_sdk()
    first = ir.dumps(extract(sdk).metadata)
    assert first == ir.dumps(extract(str(sdk)).metadata)
    assert first.endswith("}\n")
    assert "\r" not in first
    assert str(sdk) not in first
    assert json.loads(first)["format"] == ir.FORMAT


def test_line_endings_and_location_do_not_change_the_declarations(make_sdk: MakeSdk) -> None:
    unix = extract(make_sdk(name="unix")).metadata
    windows = extract(make_sdk(name="windows", newline="\r\n")).metadata
    assert unix.inputs != windows.inputs  # the files differ byte for byte
    assert ir.dumps(unix).split('"interfaces"')[1] == ir.dumps(windows).split('"interfaces"')[1]


def test_issues_are_sorted_by_severity_then_code(make_sdk: MakeSdk, flat_header: str) -> None:
    metadata = extract(make_sdk(flat=flat_header.replace("int cch );", "long cch );"))).metadata
    order = [(ir.SEVERITIES.index(i.severity), i.code, i.source) for i in metadata.issues]
    assert order == sorted(order)


def test_layouts_add_to_the_issues_without_losing_any(
    make_sdk: MakeSdk, api: dict[str, Any]
) -> None:
    pytest.importorskip("clang.cindex", reason="needs the codegen dependency group")
    api["consts"].append({"constname": "k_flWidgetHalf", "consttype": "float", "constval": "1.f/2"})
    sdk = make_sdk(api)
    plain = extract(sdk).metadata
    laid_out = extract(sdk, targets=("linux-x86_64",)).metadata
    assert ("warning", "unevaluated-constant") in codes(plain)
    assert laid_out.issues == plain.issues
    assert plain.abi is None
    assert laid_out.abi is not None
    assert [item.target for item in laid_out.abi.targets] == ["linux-x86_64"]


def test_unknown_layout_targets_are_refused(make_sdk: MakeSdk) -> None:
    pytest.importorskip("clang.cindex", reason="needs the codegen dependency group")
    with pytest.raises(MetadataError, match="unknown targets: vax"):
        extract(make_sdk(), targets=("vax",))
