"""Cross-check steam_api.json against the headers."""

from collections.abc import Iterator
from dataclasses import dataclass

from codegen import ir
from codegen.extract.headers import FlatFunction, FlatHeader, HeaderScan


@dataclass(frozen=True)
class FlatSignature:
    """The flat C function steam_api.json implies: the method plus a self pointer."""

    name: str
    returns: ir.CType
    params: tuple[tuple[str, ir.CType], ...]
    source: str


def expected_flat_signatures(metadata: ir.Metadata) -> Iterator[FlatSignature]:
    owners: tuple[ir.Interface | ir.Struct, ...] = (*metadata.interfaces, *metadata.structs)
    for owner in owners:
        self_param = ("self", ir.CType(f"{owner.name} *", base=owner.name, pointers=1))
        for method in owner.methods:
            params = tuple((p.name, p.flat_type or p.type) for p in method.params)
            returns = method.flat_returns or method.returns
            yield FlatSignature(method.flat_name, returns, (self_param, *params), method.source)
    for interface in metadata.interfaces:
        returns = ir.CType(f"{interface.name} *", base=interface.name, pointers=1)
        for accessor in interface.accessors:
            yield FlatSignature(accessor.flat_name, returns, (), accessor.source)


def check_flat_header(metadata: ir.Metadata, flat: FlatHeader) -> list[ir.Issue]:
    issues: list[ir.Issue] = []
    header: dict[str, FlatFunction] = {}
    for function in flat.functions:
        if function.name in header:
            message = f"{function.name} is declared again"
            issues.append(ir.Issue("error", "duplicate-flat-function", message, function.source))
        else:
            header[function.name] = function
    expected = {signature.name: signature for signature in expected_flat_signatures(metadata)}

    for name in sorted(expected.keys() - header.keys()):
        message = f"{name} is in steam_api.json but not in steam_api_flat.h"
        issues.append(ir.Issue("error", "flat-function-missing", message, expected[name].source))
    for name in sorted(header.keys() - expected.keys()):
        message = f"{name} is in steam_api_flat.h but not in steam_api.json"
        issues.append(ir.Issue("error", "flat-function-undescribed", message, header[name].source))
    for name in sorted(expected.keys() & header.keys()):
        issues.extend(_compare(expected[name], header[name]))

    for accessor in flat.inline_accessors:
        if accessor.target not in header:
            message = f"{accessor.name} forwards to {accessor.target}, which is not declared"
            issues.append(ir.Issue("error", "inline-accessor-target", message, accessor.source))
    return issues


def _compare(expected: FlatSignature, declared: FlatFunction) -> list[ir.Issue]:
    issues = []
    problems = []
    if expected.returns.shape() != declared.returns.shape():
        problems.append(f"returns {declared.returns.spelling}, JSON {expected.returns.spelling}")
    if len(expected.params) != len(declared.params):
        problems.append(f"takes {len(declared.params)} parameters, JSON {len(expected.params)}")
    else:
        for (name, ctype), param in zip(expected.params, declared.params, strict=True):
            if ctype.shape() != param.type.shape():
                problems.append(f"{param.name} is {param.type.spelling}, JSON {ctype.spelling}")
            elif name != param.name:
                message = f"{declared.name}: parameter {param.name} is {name} in steam_api.json"
                issues.append(ir.Issue("warning", "flat-parameter-name", message, declared.source))
    if problems:
        message = f"{declared.name}: {'; '.join(problems)}"
        issues.append(ir.Issue("error", "flat-signature-mismatch", message, declared.source))
    return issues


def check_versions(metadata: ir.Metadata, scan: HeaderScan) -> list[ir.Issue]:
    """Match interface versions, including those from overrides, with header version macros."""
    issues: list[ir.Issue] = []
    in_headers = {macro.version for macro in scan.versions}
    known = {i.version for i in metadata.interfaces if i.version is not None}
    for interface in metadata.interfaces:
        if interface.version is None:
            message = f"{interface.name} has no version string"
            issues.append(ir.Issue("note", "interface-without-version", message, interface.source))
        elif interface.version not in in_headers:
            message = f"{interface.name} version {interface.version} has no header macro"
            issues.append(ir.Issue("warning", "version-not-in-headers", message, interface.source))
    for macro in scan.versions:
        if macro.version not in known:
            message = f"{macro.macro} = {macro.version!r} is not in steam_api.json"
            issues.append(ir.Issue("note", "version-only-in-headers", message, macro.source))
    return issues
