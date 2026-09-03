import json
from pathlib import Path

import pytest

from rbs.cli import build_parser, main


def test_validate_ok(sample_input_path, capsys) -> None:
    assert main(["validate", str(sample_input_path)]) == 0
    out = capsys.readouterr().out
    assert "PGY1=8" in out
    assert "PGY2=8" in out
    assert "PGY3=8" in out
    assert "ok" in out


def test_validate_missing_file(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope.json"
    assert main(["validate", str(missing)]) == 1
    assert "error:" in capsys.readouterr().err


def test_schedule_writes_json(sample_input_path, tmp_path: Path, capsys) -> None:
    output = tmp_path / "schedule.json"
    assert main(["schedule", str(sample_input_path), "-o", str(output), "--engine", "stub"]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["meta"]["engine"] == "stub"
    assert payload["meta"]["status"] == "not_implemented"
    assert payload["assignments"] == []
    assert len(payload["unassigned"]) == 24
    assert "wrote" in capsys.readouterr().out


def test_schema_input() -> None:
    assert main(["schema", "input"]) == 0


def test_ui_subcommand_parses() -> None:
    args = build_parser().parse_args(["ui", "--port", "9999", "--no-browser", "--db", "tmp.sqlite"])
    assert args.command == "ui"
    assert args.port == 9999
    assert args.no_browser is True
    assert args.db == "tmp.sqlite"


def test_ui_preview_flags_parse() -> None:
    args = build_parser().parse_args(["ui"])
    assert args.desktop is False
    assert args.cloud is False
    assert build_parser().parse_args(["ui", "--desktop"]).desktop is True
    assert build_parser().parse_args(["ui", "--cloud"]).cloud is True


def test_ui_preview_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ui", "--desktop", "--cloud"])


def test_dump_sample(tmp_path: Path) -> None:
    output = tmp_path / "sample.json"
    assert main(["dump-sample", "-o", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["residents"]) == 24
    assert payload["residents"][0]["name"]
    assert "rotations" not in payload
    assert "requirements" not in payload


def test_dump_catalog(tmp_path: Path) -> None:
    output = tmp_path / "catalog.json"
    assert main(["dump-catalog", "-o", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 5
    assert payload["rotation_groups"]
    assert payload["rotations"]
    assert all("weekend" not in rotation for rotation in payload["rotations"])
