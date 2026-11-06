"""Local launches choose a free port and honor explicit port requests."""
import socket
from types import SimpleNamespace

import pytest

from rbs.cli import build_parser
from rbs.ui import app as ui_app


def test_cli_defaults_to_automatic_port():
    assert build_parser().parse_args(["ui"]).port is None
    assert build_parser().parse_args(["ui", "--port", "8123"]).port == 8123


def test_available_port_does_not_choose_an_occupied_port():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        selected = ui_app._available_port("127.0.0.1")
        assert selected != occupied.getsockname()[1]
        with socket.socket() as available:
            available.bind(("127.0.0.1", selected))


@pytest.mark.parametrize("requested", [None, 0, 8123])
def test_launch_prints_and_opens_the_selected_port(monkeypatch, capsys, requested):
    launched = []
    probes = []
    host = SimpleNamespace(bootstrap=lambda: None)
    monkeypatch.setattr(ui_app, "build_host", lambda *_args: host)
    monkeypatch.setattr(ui_app, "_available_port", lambda host: probes.append(host) or 49175)
    monkeypatch.setattr(ui_app, "serve", lambda actual_host, **kwargs: launched.append(kwargs))
    ui_app.run_app(object(), port=requested, show=False)
    expected = 8123 if requested == 8123 else 49175
    assert launched[0]["port"] == expected
    assert launched[0]["show"] is False
    assert probes == ([] if requested == 8123 else ["127.0.0.1"])
    assert f"RBS workspace: http://127.0.0.1:{expected}/" in capsys.readouterr().err


@pytest.mark.parametrize("port", [-1, 65536])
def test_invalid_port_is_rejected(port):
    with pytest.raises(ValueError, match="port must be between"):
        ui_app._resolve_port("127.0.0.1", port)
