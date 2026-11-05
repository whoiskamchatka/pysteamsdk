"""Inspect a local Steamworks SDK, write its normalized metadata, and check written metadata.

    python -m codegen inventory SDK
    python -m codegen extract SDK --output FILE
    python -m codegen check FILE

SDK is the folder that contains public/steam. inventory and extract apply the reviewed
corrections in codegen/overrides.toml unless told otherwise. Exit status: 0 when no issue is an
error, 1 when some are, 2 when the input cannot be read.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from codegen import ir
from codegen.extract import MetadataError, Overrides, extract, overrides
from codegen.inventory import plural, render
from codegen.validate import validate

REPOSITORY = Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m codegen", description="Read Steamworks SDK metadata into pysteamsdk's IR."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="report what the SDK metadata contains")
    write = commands.add_parser("extract", help="write the normalized metadata as JSON")
    for command in (inventory, write):
        command.add_argument("sdk", type=Path, help="the SDK folder that contains public/steam")
        choice = command.add_mutually_exclusive_group()
        choice.add_argument(
            "--overrides",
            type=Path,
            default=overrides.DEFAULT,
            metavar="FILE",
            help="reviewed corrections to apply (default: codegen/overrides.toml)",
        )
        choice.add_argument(
            "--no-overrides", dest="overrides", action="store_const", const=None, help="apply none"
        )
    write.add_argument(
        "-o", "--output", type=Path, required=True, help="file to write, outside this repository"
    )
    check = commands.add_parser("check", help="validate metadata that extract wrote")
    check.add_argument("metadata", type=Path, help="the metadata file to check")
    args = parser.parse_args(argv)

    if args.command == "check":
        return _check(args.metadata)
    if args.command == "extract" and args.output.resolve().is_relative_to(REPOSITORY):
        # The IR is derived from Valve's SDK; it stays out of the repository until the
        # redistribution review decides otherwise.
        print(f"Refusing to write {args.output} inside the repository.", file=sys.stderr)
        return 2
    try:
        extraction = extract(args.sdk, _read_overrides(args.overrides))
    except MetadataError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "inventory":
        sys.stdout.write(render(extraction))
    else:
        args.output.write_text(ir.dumps(extraction.metadata), encoding="utf-8", newline="\n")
        print(f"Wrote {args.output}")
    return 1 if any(issue.severity == "error" for issue in extraction.metadata.issues) else 0


def _read_overrides(path: Path | None) -> Overrides | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MetadataError([f"{path}: cannot read overrides: {exc}"]) from None
    return overrides.load(text, path.name)


def _check(path: Path) -> int:
    try:
        metadata = ir.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ir.ReadError) as exc:
        print(f"{path}: cannot read metadata:\n{exc}", file=sys.stderr)
        return 2
    issues = ir.sort_issues(validate(metadata))
    for issue in issues:
        print(f"{issue.severity}: {issue.code}: {issue.message}  [{issue.source}]")
    errors = sum(issue.severity == "error" for issue in issues)
    print(f"{path}: {plural(errors, 'error')}, {plural(len(issues) - errors, 'warning')}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
