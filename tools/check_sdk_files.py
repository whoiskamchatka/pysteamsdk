"""Fail when Steamworks SDK files are tracked by git or packed into a distribution."""

import argparse
import re
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

NATIVE = re.compile(r"\.(dll|dylib|exe|lib|pdb|so)(\.\d+)*$", re.IGNORECASE)
SDK_NAME = re.compile(
    r"(isteam\w*|steam\w*|matchmakingtypes)\.h|steam_api\.json|steam_appid\.txt", re.IGNORECASE
)
# Every SDK header and the SDK readme start with this banner, so renamed copies are caught too.
VALVE_BANNER = re.compile(rb"Copyright[^\n]{0,40}Valve (Corporation|LLC)")

Entry = tuple[str, str, bytes]


def problem(name: str, data: bytes) -> str | None:
    path = PurePosixPath(name)
    if path.parts[:1] == ("sdk",):
        return "inside sdk/"
    if NATIVE.search(path.name):
        return "native binary"
    if SDK_NAME.fullmatch(path.name):
        return "Steamworks SDK file name"
    if VALVE_BANNER.search(data):
        return "Valve copyright banner"
    return None


def tracked() -> Iterator[Entry]:
    root = Path(_git("rev-parse", "--show-toplevel").decode().strip())
    for name in _git("-C", str(root), "ls-files", "-z").decode().split("\0"):
        if name:
            file = root / name
            yield name, name, file.read_bytes() if file.is_file() else b""


def scan(paths: Sequence[Path]) -> Iterator[Entry]:
    for path in paths:
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for file in files:
            label = file.as_posix()
            if file.name.endswith((".whl", ".zip")):
                with zipfile.ZipFile(file) as archive:
                    for info in archive.infolist():
                        if not info.is_dir():
                            yield f"{label}!{info.filename}", info.filename, archive.read(info)
            elif file.name.endswith((".tar.gz", ".tgz", ".tar")):
                with tarfile.open(file) as archive:
                    for member in archive.getmembers():
                        content = archive.extractfile(member) if member.isfile() else None
                        if content is not None:
                            # sdists wrap everything in a "<name>-<version>/" directory
                            inner = member.name.partition("/")[2] or member.name
                            yield f"{label}!{member.name}", inner, content.read()
            else:
                name = file.relative_to(path).as_posix() if path.is_dir() else label
                yield label, name, file.read_bytes()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files, directories or built distributions to check (default: files tracked by git)",
    )
    args = parser.parse_args(argv)
    checked = 0
    found = []
    for label, name, data in scan(args.paths) if args.paths else tracked():
        checked += 1
        reason = problem(name, data)
        if reason:
            found.append(f"{label}: {reason}")
    if found:
        print("\n".join(found), file=sys.stderr)
        print(
            f"{len(found)} Steamworks SDK file(s) found. Remove them; the SDK must stay out of "
            "the repository and its packages.",
            file=sys.stderr,
        )
        return 1
    if not checked:
        print("No files to check.", file=sys.stderr)
        return 1
    print(f"{checked} files checked, no Steamworks SDK files.")
    return 0


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


if __name__ == "__main__":
    sys.exit(main())
