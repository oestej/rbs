import sys
import time

from rbs.ui.pdf_export import (
    _pending_exports,
    open_with_system_viewer,
    present_pdf_export,
    stage_pdf_export,
    system_viewer_command,
    take_staged_pdf_export,
    write_export_tempfile,
)


def test_staged_pdf_export_is_single_use() -> None:
    path = stage_pdf_export(b"%PDF-1.4", "quinn-schedule.pdf")

    assert path.startswith("/_exports/pdf/")
    token = path.rsplit("/", 1)[1]
    assert take_staged_pdf_export(token) == (b"%PDF-1.4", "quinn-schedule.pdf")
    assert take_staged_pdf_export(token) is None
    assert take_staged_pdf_export("missing") is None


def test_staged_pdf_export_expires() -> None:
    _pending_exports["stale"] = (b"%PDF-1.4", "old.pdf", time.monotonic() - 3601)

    assert take_staged_pdf_export("stale") is None
    assert "stale" not in _pending_exports


def test_system_viewer_command_per_platform(monkeypatch) -> None:
    from pathlib import Path

    path = Path("/tmp/schedule.pdf")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert system_viewer_command(path) == ["open", str(path)]
    monkeypatch.setattr(sys, "platform", "win32")
    assert system_viewer_command(path) is None
    monkeypatch.setattr(sys, "platform", "linux")
    assert system_viewer_command(path) == ["xdg-open", str(path)]


def test_open_with_system_viewer_writes_and_launches(monkeypatch, tmp_path) -> None:
    import rbs.ui.pdf_export as pdf_export

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        pdf_export.tempfile, "tempdir", str(tmp_path)
    )
    launched: list = []
    monkeypatch.setattr(
        pdf_export.subprocess, "Popen", lambda command: launched.append(command)
    )

    result = open_with_system_viewer(b"%PDF-1.4", "quinn-schedule.pdf")

    assert launched == [["open", result]]
    assert result.startswith(str(tmp_path))
    assert result.endswith(".pdf")
    assert open(result, "rb").read() == b"%PDF-1.4"


def test_present_pdf_export_opens_new_tab_in_browsers(monkeypatch) -> None:
    from nicegui import ui

    import rbs.ui.pdf_export as pdf_export

    opened: list = []
    monkeypatch.setattr(
        ui.navigate, "to", lambda target, new_tab=False: opened.append((target, new_tab))
    )

    pdf_export.present_pdf_export(b"%PDF-1.4", "quinn-schedule.pdf", native=False)

    assert len(opened) == 1
    target, new_tab = opened[0]
    assert new_tab is True
    staged = take_staged_pdf_export(target.rsplit("/", 1)[1])
    assert staged == (b"%PDF-1.4", "quinn-schedule.pdf")


def test_present_pdf_export_uses_system_viewer_on_desktop(monkeypatch, tmp_path) -> None:
    import rbs.ui.pdf_export as pdf_export

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pdf_export.tempfile, "tempdir", str(tmp_path))
    launched: list = []
    monkeypatch.setattr(
        pdf_export.subprocess, "Popen", lambda command: launched.append(command)
    )

    result = present_pdf_export(b"%PDF-1.4", "quinn-schedule.pdf", native=True)

    assert launched == [["open", result]]


def test_write_export_tempfile_keeps_recognisable_pdf_name(monkeypatch, tmp_path) -> None:
    import rbs.ui.pdf_export as pdf_export

    monkeypatch.setattr(pdf_export.tempfile, "tempdir", str(tmp_path))
    path = write_export_tempfile(b"%PDF-1.4", "quinn-schedule.pdf")

    assert path.suffix == ".pdf"
    assert path.name.startswith("quinn-schedule-")
    assert path.read_bytes() == b"%PDF-1.4"


def test_pdf_export_response_serves_inline_pdf() -> None:
    from rbs.ui.app import pdf_export_response

    class Host:
        def principal(self, _request):
            return object()

    token = stage_pdf_export(b"%PDF-1.4", "quinn-schedule.pdf").rsplit("/", 1)[1]
    response = pdf_export_response(Host(), token, object())

    assert response.status_code == 200
    assert response.media_type == "application/pdf"
    assert response.headers["Content-Disposition"] == 'inline; filename="quinn-schedule.pdf"'
    assert response.body == b"%PDF-1.4"
    # Single use: a second fetch finds nothing.
    assert pdf_export_response(Host(), token, object()).status_code == 404
    assert pdf_export_response(Host(), "missing", object()).status_code == 404


def test_pdf_export_response_rejects_anonymous_requests() -> None:
    from rbs.ui.app import pdf_export_response

    class Host:
        def principal(self, _request):
            return None

    token = stage_pdf_export(b"%PDF-1.4", "quinn-schedule.pdf").rsplit("/", 1)[1]
    try:
        assert pdf_export_response(Host(), token, object()).status_code == 403
    finally:
        take_staged_pdf_export(token)


def test_resident_export_to_pdf_uses_open_callback() -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.ui.residents.schedule import _resident_schedule_workspace

    instance = sample_instance()
    resident = instance.residents[0]
    opened: list = []
    before = set(ui.context.client.elements)
    _resident_schedule_workspace(
        instance,
        None,
        resident,
        on_pdf_open=lambda content, filename: opened.append((content, filename)),
    )
    export = next(
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
        and element.__class__.__name__ == "Button"
        and element._props.get("label") == "Export to PDF"
    )
    next(iter(export._event_listeners.values())).handler(None)

    assert len(opened) == 1
    content, filename = opened[0]
    assert content.startswith(b"%PDF")
    assert filename.endswith(".pdf")
