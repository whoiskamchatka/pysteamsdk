"""A tiny synthetic SDK in the shape of Valve's metadata. Every name and value is invented.

The files are written to a temporary folder by each test, so no file in the repository looks
like an SDK file. fixtures/widgets.json is separate: invented metadata in pysteamsdk's own
format, for tests that start from the IR rather than from an SDK.
"""

import copy
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from codegen import ir
from codegen.abi import probe

WIDGETS_IR = Path(__file__).parent / "fixtures" / "widgets.json"

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
#include "widget_types.h"
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

# What the headers above leave to Valve's other headers, so that they compile for layouts.
WIDGET_TYPES = """\
#ifndef WIDGET_TYPES_H
#define WIDGET_TYPES_H
#define S_API extern "C"
#define S_CALLTYPE
typedef unsigned int uint32;
typedef unsigned long long uint64;
typedef uint64 CallID_t;
enum EWidgetResult { k_EWidgetOK = 1, k_EWidgetFail = -2 };
struct WidgetOwner_t { uint64 m_unValue; };
struct WidgetPos_t { float m_rgflCoords[3]; bool IsSet(); private: int m_nSecret; };
struct WidgetReady_t { EWidgetResult m_eResult; uint32 m_nCount; };
typedef void ( *WidgetHook_t )( WidgetReady_t * );
class IWidgets { public: enum EShape { eShape_Round = 0 }; };
#endif
"""

HEADERS = {
    "iwidgets.h": '#define WIDGETS_INTERFACE_VERSION "Widgets007"\n',
    "widget_api.h": "S_API bool S_CALLTYPE WidgetAPI_Init();\nS_API void WidgetAPI_Shutdown();\n",
    "widget_types.h": WIDGET_TYPES,
}

README = "Widget SDK\n\nv9.99 1st January 2000\nNotes\n"


@pytest.fixture(scope="session")
def compiler() -> probe.Compiler:
    """The host's C++ compiler. Tests skip without one unless PYSTEAMSDK_REQUIRE_CXX is 1."""
    try:
        return probe.find_compiler()
    except probe.ProbeError as exc:
        if os.environ.get("PYSTEAMSDK_REQUIRE_CXX") == "1":
            pytest.fail(str(exc))
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def build_library(compiler: probe.Compiler) -> Callable[[Path, Path], Path]:
    """Compiles a C++ source into a shared library in a folder; its headers sit beside it."""

    def build(source: Path, folder: Path) -> Path:
        if compiler.msvc_style:
            path = folder / f"{source.stem}.dll"
            arguments = [
                *compiler.command,
                *("/nologo", "/LD", "/EHsc", f"/I{source.parent}"),
                *(f"/Fe{path}", f"/Fo{folder / (source.stem + '.obj')}", str(source)),
            ]
        else:
            suffix = ".dylib" if sys.platform == "darwin" else ".so"
            path = folder / f"lib{source.stem}{suffix}"
            shared = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared"]
            # clang++ on Windows builds a DLL from -shared and rejects -fPIC.
            if sys.platform != "win32":
                shared.append("-fPIC")
            include = f"-I{source.parent}"
            arguments = [*compiler.command, *shared, include, "-o", str(path), str(source)]
        built = subprocess.run(
            arguments, cwd=folder, capture_output=True, text=True, errors="replace", timeout=300
        )
        assert built.returncode == 0, built.stdout + built.stderr
        return path

    return build


@pytest.fixture
def api() -> dict[str, Any]:
    return copy.deepcopy(API)


@pytest.fixture
def flat_header() -> str:
    return FLAT_HEADER


@pytest.fixture
def headers() -> dict[str, str]:
    return dict(HEADERS)


@pytest.fixture
def widgets_text() -> str:
    return WIDGETS_IR.read_text(encoding="utf-8")


@pytest.fixture
def widgets(widgets_text: str) -> ir.Metadata:
    return ir.loads(widgets_text)


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
