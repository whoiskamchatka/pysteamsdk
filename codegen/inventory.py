from collections import Counter

from codegen import ir
from codegen.extract import Extraction
from codegen.extract.checks import expected_flat_signatures


def counts(extraction: Extraction) -> dict[str, int]:
    metadata, flat = extraction.metadata, extraction.flat
    interface_methods = sum(len(item.methods) for item in metadata.interfaces)
    struct_methods = sum(len(item.methods) for item in metadata.structs)
    accessors = sum(len(item.accessors) for item in metadata.interfaces)
    callbacks = sum(item.callback_id is not None for item in metadata.structs)
    json_typedefs = sum(item.source.startswith("steam_api.json") for item in metadata.typedefs)
    return {
        "interfaces": len(metadata.interfaces),
        "versioned_interfaces": sum(item.version is not None for item in metadata.interfaces),
        "accessors": accessors,
        "interface_methods": interface_methods,
        "struct_methods": struct_methods,
        "flat_functions_json": interface_methods + struct_methods + accessors,
        "flat_functions_header": len(flat.functions),
        "inline_accessors": len(flat.inline_accessors),
        "structs": len(metadata.structs) - callbacks,
        "callback_structs": callbacks,
        "enums": len(metadata.enums),
        "nested_enums": sum(item.scope is not None for item in metadata.enums),
        "enum_values": sum(len(item.values) for item in metadata.enums),
        "constants": len(metadata.constants),
        "constant_expressions": sum(item.value is None for item in metadata.constants),
        "typedefs_json": json_typedefs,
        "typedefs_flat": len(metadata.typedefs) - json_typedefs,
        "other_exports": len(metadata.exports),
        "version_macros": len(extraction.scan.versions),
    }


def flat_api_cases(metadata: ir.Metadata) -> dict[str, int]:
    """Signature features a binding generator must handle specially, in the flat C API."""
    structs = {item.name for item in metadata.structs}
    interfaces = {item.name for item in metadata.interfaces}
    functions = metadata.functions()
    flat = list(expected_flat_signatures(metadata))
    flat_params = [ctype for signature in flat for _, ctype in signature.params]
    typedefs = {item.name: item.type for item in metadata.typedefs}

    def by_value_struct(ctype: ir.CType) -> bool:
        plain = ctype.pointers == 0 and not ctype.reference and ctype.kind == "named"
        return plain and ctype.base in structs

    def function_pointer(ctype: ir.CType) -> bool:
        # The SDK names most function pointer types with a typedef, so follow one level.
        if ctype.kind == "function_pointer":
            return True
        target = typedefs.get(ctype.base)
        plain = ctype.pointers == 0 and not ctype.reference
        return plain and target is not None and target.kind == "function_pointer"

    fields = [item.type for struct in metadata.structs for item in struct.fields]
    annotated = Counter(key for f in functions for p in f.params for key in p.annotations)
    return {
        "params_with_flat_type": sum(p.flat_type is not None for f in functions for p in f.params),
        "returns_with_flat_type": sum(f.flat_returns is not None for f in functions),
        "reference_params": sum(ctype.reference for ctype in flat_params),
        "function_pointer_params": sum(map(function_pointer, flat_params)),
        "function_pointer_fields": sum(map(function_pointer, fields)),
        "struct_params_by_value": sum(map(by_value_struct, flat_params)),
        "struct_returns_by_value": sum(by_value_struct(s.returns) for s in flat),
        "interface_pointer_returns": sum(
            f.returns.base in interfaces and f.returns.pointers == 1 for f in functions
        ),
        "string_returns": sum(
            f.returns.base == "char" and f.returns.const and f.returns.pointers == 1
            for f in functions
        ),
        "call_result_methods": sum(f.callresult is not None for f in functions),
        "callback_methods": sum(f.callback is not None for f in functions),
        **{f"annotation_{key}": count for key, count in sorted(annotated.items())},
    }


def render(extraction: Extraction) -> str:
    metadata, files = extraction.metadata, extraction.files
    found = counts(extraction)
    cases = flat_api_cases(metadata)
    severities = Counter(issue.severity for issue in metadata.issues)
    lines = [
        "Steamworks SDK metadata",
        f"  SDK folder        {files['sdk']}",
        f"  SDK version       {metadata.sdk_version or 'unknown'} (from Readme.txt)",
        f"  steam_api.json    {files['json']}",
        f"  steam_api_flat.h  {files['flat']}",
        "",
        "Declarations",
        _row("interfaces", found["interfaces"], f"{found['versioned_interfaces']} with a version"),
        _row("accessors", found["accessors"], "exported, versioned"),
        _row("interface methods", found["interface_methods"]),
        _row("struct methods", found["struct_methods"]),
        _row("flat functions", found["flat_functions_json"], "in steam_api.json"),
        _row("", found["flat_functions_header"], "in steam_api_flat.h"),
        _row("structs", found["structs"]),
        _row("callback structs", found["callback_structs"]),
        _row("enums", found["enums"], f"{found['nested_enums']} nested"),
        _row("enum values", found["enum_values"]),
        _row("constants", found["constants"], f"{found['constant_expressions']} not evaluated"),
        _row("typedefs", found["typedefs_json"], "in steam_api.json"),
        _row("", found["typedefs_flat"], "in steam_api_flat.h"),
        _row("other exports", found["other_exports"], "S_API functions not in steam_api.json"),
        "",
        "Flat API special cases",
        *(_row(name.replace("_", " "), count) for name, count in cases.items()),
        "",
        "Issues: " + ", ".join(f"{severities[s]} {s}s" for s in ir.SEVERITIES),
    ]
    notes: Counter[str] = Counter()
    for issue in metadata.issues:
        if issue.severity == "note":
            notes[issue.code] += 1
        else:
            lines.append(f"  {issue.severity}: {issue.code}: {issue.message}  [{issue.source}]")
    lines += [f"  notes: {code} x{count}" for code, count in sorted(notes.items())]
    return "\n".join(lines) + "\n"


def _row(label: str, count: int, detail: str = "") -> str:
    return f"  {label:28} {count:5}  {detail}".rstrip()
