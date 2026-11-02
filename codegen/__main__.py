"""Inspect a local Steamworks SDK and write its normalized metadata.

    python -m codegen inventory SDK
    python -m codegen extract SDK --output FILE

SDK is the folder that contains public/steam. Exit status: 0 when no issue is an error, 1 when
some are, 2 when the metadata cannot be read.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from codegen import ir
from codegen.extract import MetadataError, extract
from codegen.inventory import render

REPOSITORY = Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m codegen", description="Read Steamworks SDK metadata into pysteamsdk's IR."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="report what the SDK metadata contains")
    inventory.add_argument("sdk", type=Path, help="the SDK folder that contains public/steam")
    write = commands.add_parser("extract", help="write the normalized metadata as JSON")
    write.add_argument("sdk", type=Path, help="the SDK folder that contains public/steam")
    write.add_argument(
        "-o", "--output", type=Path, required=True, help="file to write, outside this repository"
    )
    args = parser.parse_args(argv)

    if args.command == "extract" and args.output.resolve().is_relative_to(REPOSITORY):
        # The IR is derived from Valve's SDK; it stays out of the repository until the
        # redistribution review decides otherwise.
        print(f"Refusing to write {args.output} inside the repository.", file=sys.stderr)
        return 2
    try:
        extraction = extract(args.sdk)
    except MetadataError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.command == "inventory":
        sys.stdout.write(render(extraction))
    else:
        args.output.write_text(ir.dumps(extraction.metadata), encoding="utf-8", newline="\n")
        print(f"Wrote {args.output}")
    return 1 if any(issue.severity == "error" for issue in extraction.metadata.issues) else 0


if __name__ == "__main__":
    sys.exit(main())
