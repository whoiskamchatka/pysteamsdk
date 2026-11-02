import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from codegen.__main__ import REPOSITORY, main

MakeSdk = Callable[..., Path]


def test_inventory_reports_location_counts_and_issues(
    make_sdk: MakeSdk, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk = make_sdk()
    assert main(["inventory", str(sdk)]) == 0
    report = capsys.readouterr().out
    assert f"SDK folder        {sdk}" in report
    assert "SDK version       9.99 (from Readme.txt)" in report
    for line in (
        "  interfaces                       1  1 with a version",
        "  interface methods                4",
        "  struct methods                   1",
        "  flat functions                   6  in steam_api.json",
        "                                   6  in steam_api_flat.h",
        "  structs                          2",
        "  callback structs                 1",
        "  enums                            2  1 nested",
        "  typedefs                         4  in steam_api.json",
        "                                   1  in steam_api_flat.h",
        "  function pointer params          1",
        "  struct returns by value          0",
        "Issues: 0 errors, 0 warnings, 2 notes",
        "  notes: documentation-omitted x1",
    ):
        assert line in report.splitlines()


def test_errors_exit_with_status_1_and_unreadable_metadata_with_2(
    make_sdk: MakeSdk, flat_header: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = make_sdk(flat=flat_header.replace("int cch );", "long cch );"))
    assert main(["inventory", str(broken)]) == 1
    assert "error: flat-signature-mismatch" in capsys.readouterr().out
    assert main(["inventory", str(tmp_path / "nowhere")]) == 2
    assert "not found" in capsys.readouterr().err


def test_extract_writes_the_ir(make_sdk: MakeSdk, tmp_path_factory: pytest.TempPathFactory) -> None:
    output = tmp_path_factory.mktemp("out") / "metadata.json"
    assert main(["extract", str(make_sdk()), "--output", str(output)]) == 0
    data = output.read_bytes()
    assert b"\r" not in data
    assert json.loads(data)["interfaces"][0]["version"] == "Widgets007"


def test_extract_refuses_to_write_into_the_repository(make_sdk: MakeSdk) -> None:
    output = REPOSITORY / "metadata-should-not-exist.json"
    assert main(["extract", str(make_sdk()), "-o", str(output)]) == 2
    assert not output.exists()


def test_output_does_not_depend_on_hash_randomization(
    make_sdk: MakeSdk, tmp_path_factory: pytest.TempPathFactory
) -> None:
    sdk, out = make_sdk(), tmp_path_factory.mktemp("runs")
    for seed in ("1", "2"):
        subprocess.run(
            [sys.executable, "-m", "codegen", "extract", str(sdk), "-o", str(out / seed)],
            cwd=REPOSITORY,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
        )
    assert (out / "1").read_bytes() == (out / "2").read_bytes()
