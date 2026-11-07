"""Platform ABI layouts of the SDK's records, enums and typedefs, computed by libclang.

Layouts come from compiling the SDK headers for each target, never from steam_api.json, which
leaves out unions, packing and every size. codegen.abi.clang does the compiling and needs the
libclang package from the "codegen" dependency group; this module and codegen.abi.checks only
describe the targets and check layouts, so reading metadata never needs libclang.
codegen.abi.probe checks layouts against a native C++ compiler instead, and needs only that.
"""

from dataclasses import dataclass

from codegen import ir

# The fundamental types a canonical layout type may name, spelled as in ir.FieldLayout.
FUNDAMENTAL = frozenset(
    {
        "bool",
        "char",
        "signed char",
        "unsigned char",
        "short",
        "unsigned short",
        "int",
        "unsigned int",
        "long",
        "unsigned long",
        "long long",
        "unsigned long long",
        "float",
        "double",
        "long double",
        "wchar_t",
        "char16_t",
        "char32_t",
    }
)


@dataclass(frozen=True)
class Target:
    triple: str
    arguments: tuple[str, ...] = ()


# The platforms Valve ships the SDK's native library for. The MSVC version only fixes _MSC_VER,
# so the result does not depend on whichever Visual Studio the host has installed.
TARGETS = {
    "windows-x86_64": Target(
        "x86_64-pc-windows-msvc",
        ("-fms-extensions", "-fms-compatibility", "-fms-compatibility-version=19.40"),
    ),
    "linux-x86_64": Target("x86_64-pc-linux-gnu"),
    "macos-x86_64": Target("x86_64-apple-macosx10.15"),
    "macos-arm64": Target("arm64-apple-macosx11.0"),
}


@dataclass(frozen=True)
class Roots:
    """Qualified names to lay out. Records and enums that their fields use are added."""

    records: tuple[str, ...] = ()
    enums: tuple[str, ...] = ()
    typedefs: tuple[str, ...] = ()


def roots(metadata: ir.Metadata) -> Roots:
    """Everything the metadata declares that has a layout; interfaces are only used by pointer."""
    external = metadata.external_types
    return Roots(
        records=tuple(
            sorted(
                {item.name for item in metadata.structs}
                | {item.name for item in external if item.declaration != "typedef"}
            )
        ),
        enums=tuple(sorted({item.qualified_name for item in metadata.enums})),
        typedefs=tuple(
            sorted(
                {item.name for item in metadata.typedefs}
                | {item.name for item in external if item.declaration == "typedef"}
            )
        ),
    )
