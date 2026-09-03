from __future__ import annotations

import json

from rbs.cloud import main as cloud_main
from rbs.cloud.config import ConfigurationError


def test_cloud_configuration_failure_is_structured_and_does_not_echo_details(
    monkeypatch,
    capsys,
) -> None:
    def fail():
        raise ConfigurationError("secret alice@example.org")

    monkeypatch.setattr(cloud_main.CloudConfig, "from_env", fail)

    assert cloud_main.main([]) == 2

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert [record["event"] for record in records] == [
        "application.started",
        "configuration.invalid",
        "application.stopped",
    ]
    assert all(record["runtime"] == "cloud" for record in records)
    assert captured.err == ""
    assert "alice@example.org" not in captured.out
    assert "secret" not in captured.out


def test_cloud_sweep_reports_the_result_as_json_not_console_prose(
    monkeypatch,
    capsys,
) -> None:
    host = type("Host", (), {"sweep_once": lambda self: 3})()
    monkeypatch.setattr(cloud_main.CloudConfig, "from_env", lambda: object())
    monkeypatch.setattr(cloud_main, "build_host", lambda _config: host)

    assert cloud_main.main(["--sweep-only"]) == 0

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    sweep = next(record for record in records if record["event"] == "retention.sweep_completed")
    assert sweep["evicted_count"] == 3
    assert "evicted 3" not in captured.out
    assert captured.err == ""
