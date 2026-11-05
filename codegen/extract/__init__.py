"""Build IR metadata from a Steamworks SDK folder: steam_api.json, checked against the headers."""

import dataclasses
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from codegen import ir
from codegen.cexpr import evaluate_constants
from codegen.extract import api_json, checks, headers
from codegen.extract.api_json import MetadataError
from codegen.extract.overrides import Overrides
from codegen.extract.overrides import apply as apply_overrides
from codegen.validate import validate

__all__ = ["Extraction", "MetadataError", "Overrides", "extract"]

HEADER_FOLDER = Path("public", "steam")


@dataclass(frozen=True)
class Extraction:
    """The metadata plus the header facts the inventory reports but the IR does not keep."""

    metadata: ir.Metadata
    flat: headers.FlatHeader
    scan: headers.HeaderScan
    files: dict[str, Path]


def extract(sdk: str | os.PathLike[str], overrides: Overrides | None = None) -> Extraction:
    """Read the SDK whose root folder, the one containing public/steam, is ``sdk``.

    Raises MetadataError when steam_api.json or steam_api_flat.h is missing or unreadable.
    Anything else that looks wrong is reported in the metadata's issues instead, including
    overrides whose targets no longer match.
    """
    root = Path(sdk)
    folder = root / HEADER_FOLDER
    json_path = folder / api_json.SOURCE
    flat_path = folder / headers.FLAT_HEADER
    missing = [path for path in (json_path, flat_path) if not path.is_file()]
    if missing:
        raise MetadataError(
            [
                f"{path} not found; pass the SDK folder that contains public/steam"
                for path in missing
            ]
        )
    header_paths = sorted(folder.glob("*.h"), key=lambda path: path.name)
    readme = root / "Readme.txt"
    paths = [json_path, *header_paths, *([readme] if readme.is_file() else [])]
    raw = {path: path.read_bytes() for path in paths}

    try:
        json_text = raw[json_path].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MetadataError([f"{json_path}: not UTF-8: {exc}"]) from None
    declarations = api_json.load(json_text)
    # Header comments may hold any byte; only declarations, which are ASCII, are matched.
    texts = {path.name: raw[path].decode("utf-8", "replace") for path in header_paths}
    flat = headers.parse_flat_header(texts[headers.FLAT_HEADER])
    scan = headers.scan_headers(texts)

    flat_names = {function.name for function in flat.functions}
    metadata = ir.Metadata(
        format=ir.FORMAT,
        sdk_version=_readme_version(raw.get(readme, b"")),
        inputs=tuple(
            ir.Input(path.relative_to(root).as_posix(), hashlib.sha256(raw[path]).hexdigest())
            for path in paths
        ),
        interfaces=declarations.interfaces,
        structs=declarations.structs,
        enums=declarations.enums,
        constants=declarations.constants,
        typedefs=tuple(
            sorted((*declarations.typedefs, *flat.typedefs), key=lambda item: item.name)
        ),
        external_types=(),
        shared_callbacks=(),
        exports=tuple(export for export in scan.exports if export.name not in flat_names),
        issues=(),
    )
    issues = [*declarations.issues, *flat.issues]
    if overrides is not None:
        metadata, found = apply_overrides(metadata, overrides, texts, scan)
        issues += found
    constants, found = evaluate_constants(metadata.constants, metadata.typedefs)
    metadata = dataclasses.replace(metadata, constants=constants)
    issues += [
        *found,
        *checks.check_flat_header(metadata, flat),
        *checks.check_versions(metadata, scan),
        *validate(metadata),
    ]
    if metadata.sdk_version is None:
        issues.append(ir.Issue("note", "sdk-version-unknown", "no version in Readme.txt", "SDK"))
    metadata = dataclasses.replace(metadata, issues=ir.sort_issues(issues))
    files = {"sdk": root, "json": json_path, "flat": flat_path}
    return Extraction(metadata, flat, scan, files)


def _readme_version(readme: bytes) -> str | None:
    # The newest release heads the release notes as a line such as "v1.65 23th July 2026".
    match = re.search(rb"^v(\d+\.\d+)\b", readme, re.MULTILINE)
    return match.group(1).decode() if match else None
