"""Inspect a local Steamworks SDK, write its normalized metadata, and check written metadata.

    python -m codegen inventory SDK
    python -m codegen extract SDK --output FILE
    python -m codegen check FILE
    python -m codegen verify SDK [--metadata FILE]

SDK is the folder that contains public/steam. inventory and extract apply the reviewed
corrections in codegen/overrides.toml and compute layouts for every target with libclang unless
told otherwise. verify checks one target's layouts against a native C++ compiler. Exit status:
0 when no issue is an error and no layout differs, 1 when some do, 2 when the input cannot be
read or the compiler cannot be run.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from codegen import ir
from codegen.abi import TARGETS, probe
from codegen.abi.checks import check as check_layouts
from codegen.extract import (
    HEADER_FOLDER,
    MetadataError,
    Overrides,
    extract,
    layout_source,
    overrides,
)
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
    verify = commands.add_parser("verify", help="check layouts against a native C++ compiler")
    for command in (inventory, write, verify):
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
        if command is verify:
            continue
        layouts = command.add_mutually_exclusive_group()
        layouts.add_argument(
            "--targets",
            type=_targets,
            default=tuple(TARGETS),
            metavar="LIST",
            help=f"comma-separated targets to lay out (default: {','.join(TARGETS)})",
        )
        layouts.add_argument(
            "--no-layouts",
            dest="targets",
            action="store_const",
            const=(),
            help="compute no layouts, so libclang is not needed",
        )
    write.add_argument(
        "-o", "--output", type=Path, required=True, help="file to write, outside this repository"
    )
    verify.add_argument(
        "--target",
        choices=TARGETS,
        help="the target the compiler builds for (default: this machine's)",
    )
    verify.add_argument(
        "--metadata",
        type=Path,
        metavar="FILE",
        help="check the layouts in a file extract wrote instead of computing them",
    )
    verify.add_argument(
        "--cxx", metavar="COMPILER", help="default: $CXX, else cl on Windows and c++ elsewhere"
    )
    verify.add_argument(
        "--cxx-flag",
        dest="cxx_flags",
        action="append",
        default=[],
        metavar="FLAG",
        help="pass FLAG to the compiler, as in --cxx-flag=-arch --cxx-flag=x86_64",
    )
    verify.add_argument(
        "--only",
        type=_names,
        metavar="NAMES",
        help="comma-separated records, enums and typedefs to check (default: all)",
    )
    verify.add_argument(
        "--timeout",
        type=float,
        default=probe.COMPILE_TIMEOUT,
        metavar="SECONDS",
        help=f"how long one compile may take (default: {probe.COMPILE_TIMEOUT:g})",
    )
    check = commands.add_parser("check", help="validate metadata that extract wrote")
    check.add_argument("metadata", type=Path, help="the metadata file to check")
    args = parser.parse_args(argv)

    if args.command == "check":
        return _check(args.metadata)
    if args.command == "verify":
        return _verify(args)
    if args.command == "extract" and args.output.resolve().is_relative_to(REPOSITORY):
        # The IR is derived from Valve's SDK; it stays out of the repository until the
        # redistribution review decides otherwise.
        print(f"Refusing to write {args.output} inside the repository.", file=sys.stderr)
        return 2
    try:
        extraction = extract(args.sdk, _read_overrides(args.overrides), args.targets)
    except MetadataError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "inventory":
        sys.stdout.write(render(extraction))
    else:
        args.output.write_text(ir.dumps(extraction.metadata), encoding="utf-8", newline="\n")
        print(f"Wrote {args.output}")
    return 1 if any(issue.severity == "error" for issue in extraction.metadata.issues) else 0


def _targets(text: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.strip() for name in text.split(",")))
    unknown = [name for name in names if name not in TARGETS]
    if unknown or not names:
        known = ", ".join(TARGETS)
        raise argparse.ArgumentTypeError(f"unknown target {', '.join(unknown)!r}; use {known}")
    return names


def _names(text: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.strip() for name in text.split(",") if name.strip()))
    if not names:
        raise argparse.ArgumentTypeError("name at least one record, enum or typedef")
    return names


def _verify(args: argparse.Namespace) -> int:
    target = args.target or probe.host_target()
    if target is None:
        print("This machine is not one of the targets; pass --target.", file=sys.stderr)
        return 2
    if not (args.sdk / HEADER_FOLDER).is_dir():
        print(f"{args.sdk / HEADER_FOLDER} not found; pass the SDK folder.", file=sys.stderr)
        return 2
    try:
        compiler = probe.find_compiler(args.cxx, args.cxx_flags)
        if args.metadata is None:
            overrides = _read_overrides(args.overrides)
            metadata = extract(args.sdk, overrides, (target,)).metadata
        else:
            metadata = ir.loads(args.metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ir.ReadError, MetadataError, probe.ProbeError) as exc:
        print(f"Cannot verify: {exc}", file=sys.stderr)
        return 2
    errors = [
        issue
        for issue in dict.fromkeys([*metadata.issues, *check_layouts(metadata)])
        if issue.severity == "error"
    ]
    for issue in ir.sort_issues(errors):
        print(f"error: {issue.code}: {issue.message}  [{issue.source}]")
    layouts = (
        [item for item in metadata.abi.targets if item.target == target] if metadata.abi else []
    )
    if not layouts:
        print(f"The metadata has no layouts for {target}.", file=sys.stderr)
        return 2
    include, folders = layout_source(args.sdk)
    try:
        report = probe.verify(layouts[0], include, folders, compiler, args.only, args.timeout)
    except probe.ProbeError as exc:
        print(f"Cannot verify {target}: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(probe.render(report, layouts[0], metadata))
    return 1 if report.failures or errors else 0


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
    counts = Counter(issue.severity for issue in issues)
    print(f"{path}: " + ", ".join(plural(counts[severity], severity) for severity in ir.SEVERITIES))
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
