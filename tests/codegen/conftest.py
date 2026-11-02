"""A tiny synthetic SDK in the shape of Valve's metadata. Every name and value is invented.

The files are written to a temporary folder by each test, so no file in the repository looks
like an SDK file.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

API: dict[str, Any] = {
    "callback_structs": [
        {
            "callback_id": 701,
            "struct": "WidgetReady_t",
            "fields": [
                {"fieldname": "m_eResult", "fieldtype": "EWidgetResult"},
                {"fieldname": "m_nCount", "fieldtype": "uint32"},
            ],
        }
    ],
    "consts": [
        {"constname": "k_nWidgetMax", "consttype": "int", "constval": "16"},
        {"constname": "k_unWidgetMask", "consttype": "uint32", "constval": "0xFF"},
        {"constname": "k_nWidgetCombo", "consttype": "int", "constval": "k_nWidgetMax | 1"},
    ],
    "enums": [
        {
            "enumname": "EWidgetResult",
            "values": [
                {"name": "k_EWidgetOK", "value": "1"},
                {"name": "k_EWidgetFail", "value": "-2"},
            ],
        }
    ],
    "interfaces": [
        {
            "classname": "IWidgets",
            "version_string": "Widgets007",
            "accessors": [
                {"kind": "user", "name": "Widgets", "name_flat": "WidgetAPI_Widgets_v007"}
            ],
            "fields": [],
            "enums": [
                {
                    "enumname": "EShape",
                    "fqname": "IWidgets::EShape",
                    "values": [{"name": "eShape_Round", "value": "0"}],
                }
            ],
            "methods": [
                {
                    "methodname": "GetOwner",
                    "methodname_flat": "WidgetAPI_IWidgets_GetOwner",
                    "params": [],
                    "returntype": "WidgetOwner_t",
                    "returntype_flat": "uint64_owner",
                },
                {
                    "methodname": "Fetch",
                    "methodname_flat": "WidgetAPI_IWidgets_Fetch",
                    "callresult": "WidgetReady_t",
                    "params": [
                        {
                            "paramname": "owner",
                            "paramtype": "WidgetOwner_t",
                            "paramtype_flat": "uint64_owner",
                        },
                        {
                            "paramname": "pch",
                            "paramtype": "char *",
                            "out_string_count": "cch",
                            "desc": "invented description",
                        },
                        {"paramname": "cch", "paramtype": "int"},
                    ],
                    "returntype": "CallID_t",
                },
                {
                    "methodname": "SetShape",
                    "methodname_flat": "WidgetAPI_IWidgets_SetShape",
                    "params": [{"paramname": "eShape", "paramtype": "IWidgets::EShape"}],
                    "returntype": "void",
                },
                {
                    "methodname": "SetHook",
                    "methodname_flat": "WidgetAPI_IWidgets_SetHook",
                    "params": [{"paramname": "pfn", "paramtype": "WidgetHook_t"}],
                    "returntype": "void",
                },
            ],
        }
    ],
    "structs": [
        {
            "struct": "WidgetOwner_t",
            "fields": [{"fieldname": "m_unValue", "fieldtype": "uint64"}],
        },
        {
            "struct": "WidgetPos_t",
            "fields": [
                {"fieldname": "m_rgflCoords", "fieldtype": "float [3]"},
                {"fieldname": "m_nSecret", "fieldtype": "int", "private": True},
            ],
            "methods": [
                {
                    "methodname": "IsSet",
                    "methodname_flat": "WidgetAPI_WidgetPos_t_IsSet",
                    "params": [],
                    "returntype": "bool",
                }
            ],
        },
    ],
    "typedefs": [
        {"typedef": "uint32", "type": "unsigned int"},
        {"typedef": "uint64", "type": "unsigned long long"},
        {"typedef": "CallID_t", "type": "uint64"},
        {"typedef": "WidgetHook_t", "type": "void (*)(WidgetReady_t *)"},
    ],
}

FLAT_HEADER = """\
// Invented flat header for tests.
#ifndef WIDGETFLAT_H
#define WIDGETFLAT_H

typedef uint64 uint64_owner; // passed instead of WidgetOwner_t

// IWidgets
S_API IWidgets *WidgetAPI_Widgets_v007();
inline IWidgets *WidgetAPI_Widgets() { return WidgetAPI_Widgets_v007(); }
S_API uint64_owner WidgetAPI_IWidgets_GetOwner( IWidgets* self );
S_API CallID_t WidgetAPI_IWidgets_Fetch( IWidgets* self, uint64_owner owner, char * pch, int cch );
S_API void WidgetAPI_IWidgets_SetShape( IWidgets* self, IWidgets::EShape eShape );
S_API void WidgetAPI_IWidgets_SetHook( IWidgets* self, WidgetHook_t pfn );

// WidgetPos_t
S_API bool WidgetAPI_WidgetPos_t_IsSet( WidgetPos_t* self );
#endif
"""

HEADERS = {
    "iwidgets.h": '#define WIDGETS_INTERFACE_VERSION "Widgets007"\n',
    "widget_api.h": "S_API bool S_CALLTYPE WidgetAPI_Init();\nS_API void WidgetAPI_Shutdown();\n",
}

README = "Widget SDK\n\nv9.99 1st January 2000\nNotes\n"


@pytest.fixture
def api() -> dict[str, Any]:
    return copy.deepcopy(API)


@pytest.fixture
def flat_header() -> str:
    return FLAT_HEADER


@pytest.fixture
def make_sdk(tmp_path: Path) -> Callable[..., Path]:
    """Write an SDK folder; keyword arguments replace the defaults above."""

    def make(
        api: dict[str, Any] | None = None,
        *,
        json_text: str | None = None,
        flat: str = FLAT_HEADER,
        headers: dict[str, str] | None = None,
        readme: str | None = README,
        newline: str = "\n",
        name: str = "sdk",
    ) -> Path:
        root = tmp_path / name
        folder = root / "public" / "steam"
        folder.mkdir(parents=True)
        text = json_text if json_text is not None else json.dumps(api or API, indent=1)
        files = {"steam_api.json": text, "steam_api_flat.h": flat, **(headers or HEADERS)}
        for file_name, content in files.items():
            (folder / file_name).write_bytes(content.replace("\n", newline).encode())
        if readme is not None:
            (root / "Readme.txt").write_bytes(readme.replace("\n", newline).encode())
        return root

    return make
