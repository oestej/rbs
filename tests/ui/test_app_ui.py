import asyncio
from datetime import date
from types import SimpleNamespace

import pytest


def _created_elements(before: set[int]) -> list:
    from nicegui import ui

    return [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]


def test_workspace_navigation_uses_requested_order_and_labels() -> None:
    from nicegui import ui

    from rbs.ui.app_shell import _workspace_navigation

    before = set(ui.context.client.elements)
    _workspace_navigation()
    created = [
        element
        for element_id, element in ui.context.client.elements.items()
        if element_id not in before
    ]
    tabs = [element for element in created if element.__class__.__name__ == "Tab"]

    assert [tab._props.get("label") for tab in tabs] == [
        "Block Schedule",
        "Clinic Schedule",
        "Residents",
        "Rotations",
        "Clinic",
        "Configuration",
    ]


def test_schedule_pages_share_the_canvas_header_and_toolbar_order() -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.ui.app_shell import _render_block_schedule, _render_clinic_schedule

    workspace = SimpleNamespace(instance=sample_instance(), latest_schedule=None)

    before = set(ui.context.client.elements)
    _render_block_schedule(
        SimpleNamespace(show_past_block_weeks=False),
        workspace,
    )
    block_elements = _created_elements(before)
    block_labels = {getattr(element, "_text", None) for element in block_elements}
    assert "Block schedule" in block_labels
    assert "Review weekly block assignments across the academic year." in block_labels
    assert any(
        "rbs-schedule-canvas" in getattr(element, "_classes", [])
        for element in block_elements
    )
    assert any(
        element.__class__.__name__ == "Checkbox"
        and element._text == "Show past weeks"
        for element in block_elements
    )
    block_controls = next(
        element
        for element in block_elements
        if "rbs-schedule-heading-actions" in getattr(element, "_classes", [])
    )
    assert [
        child.__class__.__name__ for child in block_controls.default_slot.children
    ] == ["Checkbox", "Button"]
    assert block_controls.default_slot.children[-1]._props.get("label") == "Export to PDF"
    assert "rbs-schedule-heading-row" in block_controls.parent_slot.parent._classes
    assert not any(
        "rbs-page-toolbar" in getattr(element, "_classes", []) for element in block_elements
    )

    before = set(ui.context.client.elements)
    _render_clinic_schedule(
        SimpleNamespace(show_past_clinic_weeks=False, clinic_site="all"),
        workspace,
    )
    clinic_elements = _created_elements(before)
    clinic_labels = {getattr(element, "_text", None) for element in clinic_elements}
    assert "Clinic schedule" in clinic_labels
    assert "Review clinic staffing by week, site, and half-day." in clinic_labels
    controls = next(
        element
        for element in clinic_elements
        if "rbs-page-toolbar-actions" in getattr(element, "_classes", [])
    )
    control_kinds = [child.__class__.__name__ for child in controls.default_slot.children]
    assert control_kinds[:2] == ["Checkbox", "Select"]
    assert control_kinds[2:] == ["Button", "Button"]


def test_csv_export_uses_native_document_file_picker(monkeypatch, tmp_path) -> None:
    from nicegui import ui

    from rbs.ui.app_shell import _present_csv_export

    calls: list[tuple[str, str]] = []

    class Documents:
        async def save_csv_export(self, content: str, suggested_name: str):
            calls.append((content, suggested_name))
            return tmp_path / "clinic.csv"

    downloads: list[tuple] = []
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(ui.download, "content", lambda *args: downloads.append(args))
    monkeypatch.setattr(
        ui,
        "notify",
        lambda message, *, type: notifications.append((message, type)),
    )
    session = SimpleNamespace(
        workspace_host=SimpleNamespace(document_io=Documents()),
    )

    assert asyncio.run(_present_csv_export(session, "Week\n1\n", "schedule.csv"))
    assert calls == [("Week\n1\n", "schedule.csv")]
    assert downloads == []
    assert notifications == [("CSV saved to clinic.csv", "positive")]


def test_csv_export_keeps_browser_download_behavior(monkeypatch) -> None:
    from nicegui import ui

    from rbs.ui.app_shell import _present_csv_export

    downloads: list[tuple] = []
    monkeypatch.setattr(ui.download, "content", lambda *args: downloads.append(args))
    session = SimpleNamespace(
        workspace_host=SimpleNamespace(document_io=None),
    )

    assert asyncio.run(_present_csv_export(session, "Week\n1\n", "schedule.csv"))
    assert downloads == [("Week\n1\n", "schedule.csv", "text/csv")]


def test_cancelled_native_csv_export_reports_that_nothing_was_written(
    monkeypatch,
) -> None:
    from nicegui import ui

    from rbs.ui.app_shell import _present_csv_export

    class Documents:
        async def save_csv_export(self, _content: str, _suggested_name: str):
            return None

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ui,
        "notify",
        lambda message, *, type: notifications.append((message, type)),
    )
    session = SimpleNamespace(
        workspace_host=SimpleNamespace(document_io=Documents()),
    )

    assert not asyncio.run(_present_csv_export(session, "Week\n", "schedule.csv"))
    assert notifications == [
        ("CSV export cancelled - nothing was written", "info")
    ]


def test_loading_screen_is_accessible_and_covers_startup() -> None:
    from rbs.ui.app_branding import (
        DISMISS_LOADING_SCREEN_SCRIPT,
        LOADING_SCREEN_HTML,
        SPINNER_ELAPSED_SCRIPT,
        WORDMARK_URL,
    )

    assert 'id="rbs-loading-screen"' in LOADING_SCREEN_HTML
    assert 'role="status"' in LOADING_SCREEN_HTML
    assert 'aria-live="polite"' in LOADING_SCREEN_HTML
    assert "Preparing your workspace…" in LOADING_SCREEN_HTML
    assert ">0:00.0</div>" in LOADING_SCREEN_HTML
    assert "Elapsed:" not in LOADING_SCREEN_HTML
    assert 'class="rbs-spinner-status rbs-loading-status"' in LOADING_SCREEN_HTML
    assert 'class="rbs-elapsed-time rbs-loading-elapsed"' in LOADING_SCREEN_HTML
    assert "window.setInterval(update, 100)" in SPINNER_ELAPSED_SCRIPT
    assert ".rbs-reconnect-elapsed" in SPINNER_ELAPSED_SCRIPT
    assert "rbs-reconnect-spinner" in SPINNER_ELAPSED_SCRIPT
    assert "Elapsed:" not in SPINNER_ELAPSED_SCRIPT
    assert WORDMARK_URL in LOADING_SCREEN_HTML
    assert "rbs-dialog-wordmark" in LOADING_SCREEN_HTML
    assert "rbs-branded-dialog" in LOADING_SCREEN_HTML
    assert 'class="rbs-loading-title"' not in LOADING_SCREEN_HTML
    assert "classList.add('is-ready')" in DISMISS_LOADING_SCREEN_SCRIPT


def test_about_dialog_shows_wordmark_version_and_legal_notices() -> None:
    from nicegui import ui

    from rbs import __version__
    from rbs.product import LOCAL_PRODUCT
    from rbs.ui.app_branding import (
        ABOUT_COPYRIGHT_NOTICE,
        ABOUT_LICENSE_NOTICE,
    )
    from rbs.ui.app_documents import (
        _open_about_dialog,
    )

    before = set(ui.context.client.elements)
    _open_about_dialog(LOCAL_PRODUCT)

    created = _created_elements(before)
    dialog = next(element for element in created if element.__class__.__name__ == "Dialog")
    text = {getattr(element, "_text", None) for element in created}
    images = [element for element in created if element.__class__.__name__ == "Image"]

    assert dialog.value is True
    assert {
        None,
        f"Version {__version__}",
        ABOUT_COPYRIGHT_NOTICE,
        "Licensing & notices",
        ABOUT_LICENSE_NOTICE,
        "Third-party components",
    } <= text
    assert "RBS" not in text
    assert "RBS Desktop" not in text
    assert len(images) == 1
    assert "wordmark.svg?v=" in images[0]._props["src"]
    assert "rbs-dialog-wordmark" in images[0]._classes
    assert any("rbs-branded-dialog" in element._classes for element in created)
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "View license"
        for element in created
    )
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "View third-party licenses"
        for element in created
    )
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("label") == "View release notes"
        for element in created
    )
    close = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("icon") == "close"
    )
    assert "Close about dialog" in close._props["aria-label"]


def test_cloud_about_dialog_omits_desktop_dependency_notices() -> None:
    from nicegui import ui

    from rbs.product import CLOUD_PRODUCT
    from rbs.ui.app_documents import _open_about_dialog

    before = set(ui.context.client.elements)
    _open_about_dialog(CLOUD_PRODUCT)

    created = _created_elements(before)
    text = {getattr(element, "_text", None) for element in created}
    button_labels = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Button"
    }

    assert "Licensing & notices" in text
    assert "Third-party components" not in text
    assert "View third-party licenses" not in button_labels
    assert "View license" in button_labels
    assert "View release notes" in button_labels


def test_application_license_dialog_shows_the_bundled_license(monkeypatch) -> None:
    from nicegui import ui

    import rbs.ui.app as app

    monkeypatch.setattr(app, "load_application_license", lambda: "Demo OSL text")

    from rbs.ui.app_documents import _open_application_license_dialog

    before = set(ui.context.client.elements)
    _open_application_license_dialog()

    created = _created_elements(before)
    dialog = next(element for element in created if element.__class__.__name__ == "Dialog")
    text = {getattr(element, "_text", None) for element in created}
    scroll_area = next(
        element for element in created if element.__class__.__name__ == "ScrollArea"
    )

    assert dialog.value is True
    assert {"Open Software License 3.0", "License terms governing RBS.", "Demo OSL text"} <= text
    assert scroll_area._props["role"] == "region"
    assert scroll_area._props["aria-label"] == "RBS license text"
    close = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("icon") == "close"
    )
    assert close._props["aria-label"] == "Close RBS license"


def test_release_notes_dialog_shows_sanitized_bundled_markdown(monkeypatch) -> None:
    from nicegui import ui

    import rbs.ui.app as app

    monkeypatch.setattr(app, "load_release_notes", lambda: "# Changelog\n\n- Demo change")

    from rbs.ui.app_documents import _open_release_notes_dialog

    before = set(ui.context.client.elements)
    _open_release_notes_dialog()

    created = _created_elements(before)
    dialog = next(element for element in created if element.__class__.__name__ == "Dialog")
    markdown = next(element for element in created if element.__class__.__name__ == "Markdown")
    scroll_area = next(
        element for element in created if element.__class__.__name__ == "ScrollArea"
    )

    assert dialog.value is True
    assert markdown.content == "# Changelog\n\n- Demo change"
    assert markdown._props["sanitize"] is True
    assert scroll_area._props["role"] == "region"
    assert scroll_area._props["aria-label"] == "RBS release notes"
    close = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("icon") == "close"
    )
    assert close._props["aria-label"] == "Close release notes"


def test_third_party_license_dialog_shows_the_bundled_notices(monkeypatch) -> None:
    from nicegui import ui

    import rbs.ui.app as app

    monkeypatch.setattr(app, "load_third_party_licenses", lambda: "Demo license text")

    from rbs.ui.app_documents import _open_third_party_licenses_dialog

    before = set(ui.context.client.elements)
    _open_third_party_licenses_dialog()

    created = _created_elements(before)
    dialog = next(element for element in created if element.__class__.__name__ == "Dialog")
    text = {getattr(element, "_text", None) for element in created}

    assert dialog.value is True
    assert {
        "Third-party licenses",
        "License terms and attributions for components bundled with RBS Desktop.",
        "Demo license text",
    } <= text
    scroll_area = next(
        element for element in created if element.__class__.__name__ == "ScrollArea"
    )
    assert scroll_area._props["role"] == "region"
    assert scroll_area._props["aria-label"] == "Third-party license notices"
    close = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("icon") == "close"
    )
    assert close._props["aria-label"] == "Close third-party licenses"


def test_solver_progress_uses_a_persistent_branded_overlay() -> None:
    from nicegui import ui

    from rbs.ui.app_solve import _open_solver_progress

    before = set(ui.context.client.elements)
    overlay = _open_solver_progress()

    created = _created_elements(before)
    assert overlay.dialog.value is True
    assert overlay.dialog._props["persistent"] is True
    assert "rbs-overlay-dialog" in overlay.dialog._classes
    text = {getattr(element, "_text", None) for element in created}
    assert "Running Solve... Please be patient, this process can take a few minutes." in text
    assert "0:00.0" in text
    assert "Elapsed: 0:00.0" not in text
    wordmark = next(element for element in created if element.__class__.__name__ == "Image")
    assert "wordmark.svg?v=" in wordmark._props["src"]
    assert "rbs-dialog-wordmark" in wordmark._classes
    assert "rbs-overlay-wordmark" in wordmark._classes
    assert any("rbs-branded-dialog" in element._classes for element in created)
    assert any("rbs-solver-progress-status" in element._classes for element in created)
    assert any("rbs-overlay-spinner" in element._classes for element in created)
    overlay.close()


def test_solver_elapsed_time_uses_minutes_seconds_and_tenths() -> None:
    from rbs.ui.app_solve import _format_elapsed

    assert _format_elapsed(0) == "0:00.0"
    assert _format_elapsed(0.1) == "0:00.1"
    assert _format_elapsed(59.9) == "0:59.9"
    assert _format_elapsed(60) == "1:00.0"
    assert _format_elapsed(121.27) == "2:01.2"
    assert _format_elapsed(-1) == "0:00.0"


def test_infeasible_solver_diagnostic_stays_open_with_resolution_options() -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.models.schedule import SolverDiagnostic
    from rbs.ui.app_solve import _open_solver_diagnostics

    instance = sample_instance()
    before = set(ui.context.client.elements)
    diagnostic = SolverDiagnostic(
        code="resident_vacation_coverage",
        message="A resident cannot cover the year around weeks 9–10.",
        resident_ids=["resident-001"],
        special_rotation_ids=["conference"],
        weeks=[9, 10],
        suggestions=["Move a vacation week.", "Shorten the conference."],
    )

    _open_solver_diagnostics(
        [diagnostic],
        draft_kept=True,
        session=SimpleNamespace(workspace=lambda: SimpleNamespace(instance=instance)),
    )

    created = _created_elements(before)
    text = {getattr(element, "_text", None) for element in created}
    dialog = next(element for element in created if element.__class__.__name__ == "Dialog")
    assert dialog.value is True
    assert "No feasible block schedule" in text
    kept_message = (
        "Your current draft was kept. Resolve one of the conflicts below and solve again."
    )
    assert kept_message in text
    assert diagnostic.message in text
    assert "Ways to resolve it" in text
    assert "• Move a vacation week." in text
    assert "• Shorten the conference." in text
    button_labels = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Button"
    }
    assert f"Open {instance.residents_by_id['resident-001'].name}" in button_labels
    assert "Open Special events" in button_labels
    wordmark = next(element for element in created if element.__class__.__name__ == "Image")
    assert "wordmark.svg?v=" in wordmark._props["src"]
    assert "rbs-dialog-wordmark" in wordmark._classes
    assert any("rbs-branded-dialog" in element._classes for element in created)


def test_readiness_dialog_names_each_configuration_conflict_and_links_its_editor() -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.solver.readiness import ReadinessIssue, ReadinessResult
    from rbs.ui.app_solve import _open_readiness_diagnostics

    instance = sample_instance()
    session = SimpleNamespace(workspace=lambda: SimpleNamespace(instance=instance))
    issues = (
        ReadinessIssue(
            code="missing_elective_fallback",
            message=(
                "Clinic · PGY1: a 2-week Elective requires a compatible Clinic fallback, "
                "but none is configured."
            ),
            suggestions=("Add a 2-week Clinic block configuration for PGY1.",),
            pgy=1,
        ),
        ReadinessIssue(
            code="rotation_capacity_conflict",
            message="ICU: a weekly minimum cannot be met.",
            rotation_id="icu",
        ),
    )
    readiness = ReadinessResult(
        errors=tuple(issue.message for issue in issues),
        issues=issues,
    )
    before = set(ui.context.client.elements)

    _open_readiness_diagnostics(session, readiness)

    created = _created_elements(before)
    text = {getattr(element, "_text", None) for element in created}
    buttons = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Button"
    }
    dialog = next(element for element in created if element.__class__.__name__ == "Dialog")
    assert dialog.value is True
    assert "Cannot solve · 2 configuration conflicts" in text
    assert "Solve did not start. Fix these settings, then run Solve again." in text
    assert {issue.message for issue in issues} <= text
    assert {"Open Clinic block rules", "Open ICU"} <= buttons


def test_solve_does_not_start_search_when_configuration_is_provably_blocked(
    monkeypatch,
) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.ui import app_solve

    instance = sample_instance()
    rotations = [
        rotation.model_copy(update={"max_consecutive_weeks": 2})
        if rotation.id == "icu"
        else rotation
        for rotation in instance.rotations
    ]
    blocked = instance.revised(rotations=rotations)
    # Deliberately omit workspace_host: reaching search would fail this test.
    session = SimpleNamespace(
        solving=False,
        workspace=lambda: SimpleNamespace(instance=blocked),
    )
    opened: list = []
    notifications: list[str] = []
    monkeypatch.setattr(
        app_solve,
        "_open_readiness_diagnostics",
        lambda _session, readiness: opened.extend(readiness.issues),
    )
    monkeypatch.setattr(
        ui,
        "notify",
        lambda message, **_kwargs: notifications.append(message),
    )

    asyncio.run(app_solve._solve(session))

    assert session.solving is False
    assert [issue.code for issue in opened] == ["block_exceeds_consecutive_limit"]
    assert "ICU · PGY1" in opened[0].message
    assert notifications == ["Solve did not start · 1 configuration conflict"]


def test_readiness_action_opens_the_exact_configuration_surface() -> None:
    from rbs.catalog import sample_instance
    from rbs.solver.readiness import ReadinessIssue
    from rbs.ui.app_solve import _navigate_to_readiness_issue

    instance = sample_instance()
    rebuilt: list[bool] = []
    closed: list[bool] = []
    session = SimpleNamespace(
        active_tab="block_schedule",
        clinic_section="clinic_sites",
        rotation_section="rotation_summary",
        rotation_id=None,
        workspace=lambda: SimpleNamespace(instance=instance),
        rebuild=lambda: rebuilt.append(True),
    )
    dialog = SimpleNamespace(close=lambda: closed.append(True))

    _navigate_to_readiness_issue(
        session,
        ReadinessIssue(
            code="block_exceeds_consecutive_limit",
            message="ICU block is too long.",
            rotation_id="icu",
        ),
        dialog,
    )

    assert closed == [True]
    assert rebuilt == [True]
    assert session.active_tab == "rotations"
    assert session.rotation_section == "standard_rotations"
    assert session.rotation_id == "icu"

    _navigate_to_readiness_issue(
        session,
        ReadinessIssue(
            code="missing_elective_fallback",
            message="Clinic fallback is missing.",
        ),
        dialog,
    )

    assert session.active_tab == "clinic"
    assert session.clinic_section == "clinic_block_rules"
    assert session.rotation_id is None


def test_solver_outcomes_distinguish_timeout_infeasibility_and_clinic_failure() -> None:
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta, SolverDiagnostic
    from rbs.ui.app_solve import _solver_outcome

    def schedule(
        status: SolverStatus,
        *,
        solver_status: SolverStatus | None = None,
        notes: list[str] | None = None,
        validation_errors: list[str] | None = None,
        diagnostics: list[SolverDiagnostic] | None = None,
    ) -> Schedule:
        return Schedule(
            meta=ScheduleMeta(
                academic_year="2026-2027",
                engine=SolverEngineName.CP_SAT,
                status=status,
                solver_status=solver_status,
                notes=notes or [],
                validation_errors=validation_errors or [],
                diagnostics=diagnostics or [],
            )
        )

    infeasible = _solver_outcome(schedule(SolverStatus.INFEASIBLE))
    timeout = _solver_outcome(
        schedule(
            SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
            notes=["no feasible schedule within 60s"],
        )
    )
    clinic = _solver_outcome(
        schedule(
            SolverStatus.UNKNOWN,
            solver_status=SolverStatus.FEASIBLE,
            validation_errors=["Maple capacity exceeded"],
            diagnostics=[
                SolverDiagnostic(
                    code="clinic_allocation_capacity",
                    message="Maple: 3 clinic half-days exceed capacity.",
                )
            ],
        )
    )
    model_build = _solver_outcome(
        schedule(
            SolverStatus.INFEASIBLE,
            diagnostics=[
                SolverDiagnostic(
                    code="model_build_error",
                    message="Clinic · PGY1 has no compatible 2-week fallback.",
                )
            ],
        )
    )

    assert infeasible.title == "No feasible block schedule"
    assert timeout.title == "No schedule found in time"
    assert "does not prove" in timeout.detail
    assert clinic.title == "Block schedule found, but clinic placement failed"
    assert "not accepted" in clinic.detail
    assert model_build.title == "Cannot build schedule model"


def test_invalid_clinic_placement_is_explained_without_saving_it(
    monkeypatch,
    tmp_path,
) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta, SolverDiagnostic
    from rbs.store import Store
    from rbs.ui import app_solve
    from rbs.ui.session import WorkspaceSession

    invalid = Schedule(
        meta=ScheduleMeta(
            academic_year="2026-2027",
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.FEASIBLE,
            validation_errors=["Maple capacity exceeded"],
            diagnostics=[
                SolverDiagnostic(
                    code="clinic_allocation_capacity",
                    message="Maple: 3 clinic half-days exceed capacity.",
                )
            ],
        )
    )

    class Host:
        document_io = None

        def principal(self, _request):
            return "local"

        async def solve(self, _principal, _instance, *, reference_schedule=None):
            assert reference_schedule is None
            return invalid

    store = Store(tmp_path / "invalid-clinic.sqlite")
    store.init()
    workspace = store.create("Clinic test", sample_instance())
    session = WorkspaceSession(
        store=store,
        workspace_host=Host(),
        workspace_id=workspace.id,
    )
    closed: list[bool] = []
    opened: list[dict] = []
    notifications: list[str] = []
    monkeypatch.setattr(
        app_solve,
        "_open_solver_progress",
        lambda: SimpleNamespace(close=lambda: closed.append(True)),
    )
    monkeypatch.setattr(
        app_solve,
        "_open_solver_diagnostics",
        lambda _diagnostics, **kwargs: opened.append(kwargs),
    )
    monkeypatch.setattr(
        ui,
        "notify",
        lambda message, **_kwargs: notifications.append(message),
    )

    asyncio.run(app_solve._solve(session))

    assert store.get(workspace.id).schedule is None
    assert closed == [True]
    assert opened[0]["title"] == "Block schedule found, but clinic placement failed"
    assert notifications == [
        "Clinic placement failed · result not saved · explanation opened"
    ]


def test_settings_keeps_scheduling_behaviour_and_gives_up_the_workspace(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import _settings_tab

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Managed workspace", sample_instance())
    before = set(ui.context.client.elements)
    _settings_tab(
        store,
        workspace,
        {"id": workspace.id},
        persist_instance=lambda *_args, **_kwargs: None,
        redraw=lambda: None,
    )
    created = _created_elements(before)
    checkbox_labels = {
        getattr(element, "_text", None)
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }
    labels = {getattr(element, "_text", None) for element in created}
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    inputs = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Input"
    }
    selects = {
        element._props.get("label"): element
        for element in created
        if element.__class__.__name__ == "Select"
    }

    # Naming, creating and closing a workspace live on the Workspace tab now.
    assert "Workspace" not in labels
    assert "Workspace name" not in inputs
    assert "Academic year" not in selects
    assert not {"Save workspace", "New workspace", "Close workspace"} & buttons
    # What shapes the schedule stays here.
    assert "Automatically lock blocks and clinic sessions through today" in checkbox_labels
    assert "Institutional color scheme" not in labels
    assert not {"Save settings", "Load settings"} & buttons
    assert not any(
        "Manual lock choices are preserved separately" in str(label) for label in labels
    )
    assert "Manual block locks are managed from Residents → Manual Override Blocks." not in labels
    assert "Workspace management and scheduling behavior." not in labels
    assert (
        "Changing the academic year updates calendar dates and requires a new solve."
        not in labels
    )


def test_changing_academic_year_permanently_discards_the_schedule(tmp_path) -> None:
    from rbs.catalog import sample_instance
    from rbs.solver.core import get_engine
    from rbs.store import Store
    from rbs.ui.settings.view import save_general_workspace_settings

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    instance = sample_instance()
    workspace = store.create(
        "Reversible year",
        instance,
        get_engine("stub").solve(instance, options=instance.solver),
    )

    changed, did_change = save_general_workspace_settings(
        store,
        workspace,
        name=workspace.name,
        academic_year="2027-2028",
    )

    assert did_change
    assert changed.schedule is None
    assert changed.stale_schedule is None
    assert changed.latest_schedule is None
    assert not changed.solution_is_out_of_date
    assert changed.schedule_revision is None

    restored, did_restore = save_general_workspace_settings(
        store,
        changed,
        name=changed.name,
        academic_year="2026-2027",
    )

    assert did_restore
    assert restored.schedule is None
    assert restored.stale_schedule is None
    assert not restored.solution_is_out_of_date


def test_moving_the_annual_start_date_persists_and_revives_a_compatible_solve(
    tmp_path,
) -> None:
    from datetime import timedelta

    from rbs.catalog import sample_instance
    from rbs.solver.core import get_engine
    from rbs.store import Store
    from rbs.ui.settings.view import save_general_workspace_settings

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    instance = sample_instance()
    workspace = store.create(
        "Calendar workspace",
        instance,
        get_engine("stub").solve(instance, options=instance.solver),
    )
    new_start = instance.calendar.first_week_start + timedelta(weeks=1)

    changed, did_change = save_general_workspace_settings(
        store,
        workspace,
        name=workspace.name,
        academic_year=workspace.instance.academic_year,
        first_week_start=new_start,
    )

    assert did_change
    assert changed.instance.calendar.first_week_start == new_start
    assert changed.schedule is not None
    assert not changed.solution_is_out_of_date

    _repeat, did_repeat = save_general_workspace_settings(
        store,
        changed,
        name=changed.name,
        academic_year=changed.instance.academic_year,
        first_week_start=new_start,
    )

    assert not did_repeat


def test_moving_the_annual_start_date_rejects_dates_outside_the_year(
    tmp_path,
) -> None:
    from datetime import timedelta

    import pytest

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import save_general_workspace_settings

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    instance = sample_instance()
    first_day = instance.calendar.first_week_start
    resident = instance.residents[0].model_copy(update={"days_off": [first_day]})
    workspace = store.create(
        "Boundary workspace",
        instance.revised(residents=[resident, *instance.residents[1:]]),
    )

    with pytest.raises(ValueError, match="outside academic year"):
        save_general_workspace_settings(
            store,
            workspace,
            name=workspace.name,
            academic_year=workspace.instance.academic_year,
            first_week_start=first_day + timedelta(weeks=1),
        )


def test_renaming_a_workspace_ignores_a_stale_snapshot(tmp_path) -> None:
    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import save_general_workspace_settings
    from rbs.workspaces import WorkspaceController

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Workplace", sample_instance())

    # An interleaved save (week-start change, lock toggle, another tab, or a
    # double submit's first write) moves the stored revision past the snapshot
    # a rename dialog holds. Renaming only writes the name column, so it must
    # not fail with a revision conflict.
    WorkspaceController(store).rename(store.get(workspace.id), "Touched elsewhere")

    renamed, did_change = save_general_workspace_settings(
        store,
        workspace,
        name="Renamed",
        academic_year=workspace.academic_year,
    )

    assert did_change
    assert renamed.name == "Renamed"
    assert renamed.instance == workspace.instance
    assert store.get(workspace.id).name == "Renamed"

    # A repeat submit after the rename is a no-op returning stored state.
    again, did_again = save_general_workspace_settings(
        store,
        workspace,
        name="Renamed",
        academic_year=workspace.academic_year,
    )

    assert not did_again
    assert again.name == "Renamed"


def test_general_settings_leads_with_the_annual_calendar(tmp_path) -> None:
    from datetime import timedelta

    from nicegui import ui
    from nicegui.events import ValueChangeEventArguments

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import _settings_tab

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Managed workspace", sample_instance())
    current = workspace.instance.calendar.first_week_start
    before = set(ui.context.client.elements)
    _settings_tab(
        store,
        workspace,
        {"id": workspace.id},
        persist_instance=lambda *_args, **_kwargs: None,
        redraw=lambda: None,
    )
    created = _created_elements(before)
    calendar_title = next(
        element
        for element in created
        if getattr(element, "_text", None) == "Annual calendar"
    )
    week_start = next(
        element
        for element in created
        if element.__class__.__name__ == "Select"
        and element._props.get("label") == "Annual calendar start date"
    )
    locking = next(
        element
        for element in created
        if getattr(element, "_text", None) == "Automatic schedule locking"
    )

    assert calendar_title.id < locking.id
    assert week_start.value == current.isoformat()
    assert week_start.options
    assert all(
        date.fromisoformat(option).weekday() == 0 for option in week_start.options
    )

    new_start = current + timedelta(weeks=1)
    week_start.value = new_start.isoformat()
    event = ValueChangeEventArguments(
        sender=week_start,
        client=week_start.client,
        value=new_start.isoformat(),
        previous_value=current.isoformat(),
    )
    for handler in week_start._change_handlers:
        handler(event)

    assert store.get(workspace.id).instance.calendar.first_week_start == new_start


def test_rbsc_download_filename_uses_the_portable_extension() -> None:
    from rbs.ui.workspaces.io import _rbsc_filename

    assert _rbsc_filename(date(2026, 8, 23)) == "rbs-database-2026-08-23.rbsc"


def test_the_workspace_tab_owns_identity_and_files(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.session import WorkspaceSession
    from rbs.ui.workspaces.io import _workspace_tab

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Portable year", sample_instance())
    before = set(ui.context.client.elements)

    session = WorkspaceSession(store=store, workspace_id=workspace.id)
    _workspace_tab(store, workspace, session, lambda: None)

    created = _created_elements(before)
    text = {getattr(element, "_text", None) for element in created}
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    uploads = [element for element in created if element.__class__.__name__ == "Upload"]
    inputs = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Input"
    }

    # Opening a file merges onto the desk and leads; replacing the whole
    # database is a support tool and is kept separate.
    assert any(upload._props.get("label") == "Open workspace file" for upload in uploads)
    assert "Download whole database" in buttons
    assert any(upload._props.get("label") == "Replace database" for upload in uploads)
    assert any(".rbsc" in str(upload._props.get("accept", "")) for upload in uploads)
    assert "RBSC database file" not in text
    # Identity moved here from Settings.
    assert "Workspace name" in inputs
    assert "This workspace" in text
    assert {"Save workspace", "New workspace", "Close workspace"} <= buttons
    assert not any(isinstance(item, str) and item.startswith("Current database ·") for item in text)
    assert not any(
        isinstance(item, str) and item.startswith("Restoring validates") for item in text
    )
    assert (
        not {
            "Active constraint catalog",
            "Constraint catalog",
            "Instance summary",
        }
        & text
    )
    assert (
        not {
            "Delete workspace",
            "Import JSON",
            "Export instance",
            "Export schedule",
            "Export bundle",
            "Import constraint JSON",
            "Export constraints",
        }
        & buttons
    )


def test_empty_workspace_replaces_the_selector_with_new_and_open() -> None:
    from nicegui import ui

    from rbs.store import Store
    from rbs.ui.app_shell import _empty_header_actions, _empty_workspace_page
    from rbs.ui.session import WorkspaceSession

    store = Store(":memory:")
    session = WorkspaceSession(store=store)
    before = set(ui.context.client.elements)
    _empty_header_actions(session)
    _empty_workspace_page()

    created = _created_elements(before)
    text = {getattr(element, "_text", None) for element in created}
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    uploads = [element for element in created if element.__class__.__name__ == "Upload"]

    assert "New" in buttons
    assert any(upload._props.get("label") == "Open" for upload in uploads)
    assert "Open Sample Data" in text
    assert any(
        element.__class__.__name__ == "Button"
        and element._props.get("icon") == "more_vert"
        and element._props.get("aria-label") == "More workspace actions"
        for element in created
    )
    assert "Create or import a workspace to get started" in text
    assert any("rbs-empty-workspace-arrow" in element._classes for element in created)


def test_rbsc_restore_is_staged_behind_replacement_confirmation(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.workspaces.io import _open_rbsc_restore_dialog

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Portable year", sample_instance())
    payload = store.export_rbsc()
    rbsc = store.inspect_rbsc(payload)
    before = set(ui.context.client.elements)

    _open_rbsc_restore_dialog(
        store,
        {"id": workspace.id},
        lambda: None,
        payload=payload,
        filename="complete-state.rbsc",
        rbsc=rbsc,
    )

    created = _created_elements(before)
    text = {getattr(element, "_text", None) for element in created}
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }

    assert "Restore RBSC database" in text
    assert "complete-state.rbsc" in text
    assert "Cancel" in buttons
    assert "Replace database" in buttons
    assert store.get(workspace.id).name == "Portable year"


def test_configuration_splits_scheduling_sections(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import _settings_tab

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Managed workspace", sample_instance())
    before = set(ui.context.client.elements)
    _settings_tab(
        store,
        workspace,
        {"id": workspace.id},
        persist_instance=lambda *_args, **_kwargs: None,
        redraw=lambda: None,
    )
    created = _created_elements(before)
    tabs = [
        element._props.get("label") for element in created if element.__class__.__name__ == "Tab"
    ]
    labels = {getattr(element, "_text", None) for element in created}

    assert tabs == ["General", "Training levels", "Advanced"]
    assert "Configuration" in labels
    assert "Configure scheduling behavior, training levels, and solver options." in labels


def test_desktop_settings_actions_appear_above_automatic_locking(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import _settings_tab

    async def save_settings():
        return tmp_path / "settings.json"

    async def load_settings():
        return None

    documents = SimpleNamespace(
        supports_application_settings=True,
        save_settings=save_settings,
        load_settings=load_settings,
    )
    state = SimpleNamespace(
        workspace_host=SimpleNamespace(document_io=documents),
        solving=False,
    )
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Managed workspace", sample_instance())
    before = set(ui.context.client.elements)

    _settings_tab(
        store,
        workspace,
        state,
        persist_instance=lambda *_args, **_kwargs: None,
        redraw=lambda: None,
    )

    created = _created_elements(before)
    save = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Save settings"
    )
    load = next(
        element
        for element in created
        if element.__class__.__name__ == "Button" and element._props.get("label") == "Load settings"
    )
    numbers = {
        element._props.get("label")
        for element in created
        if element.__class__.__name__ == "Number"
    }
    labels = {
        getattr(element, "_text", None)
        for element in created
        if element.__class__.__name__ == "Label"
    }
    automatic_locking = next(
        element
        for element in created
        if getattr(element, "_text", None) == "Automatic schedule locking"
    )

    assert save.id < automatic_locking.id
    assert load.id < automatic_locking.id
    assert "CPU workers" not in numbers
    assert not any(
        text and "Attempts race independent seeds" in text
        for text in labels
    )
    assert not any(
        text and "Extra time buys very little" in text
        for text in labels
    )


def test_colors_settings_defines_the_institutional_palette_not_assignments(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import _colors_settings

    instance = sample_instance()
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Color workspace", instance)
    before = set(ui.context.client.elements)

    _colors_settings(
        workspace,
        lambda *_args, **_kwargs: None,
        lambda: None,
    )

    created = _created_elements(before)
    token_names = [
        element
        for element in created
        if element.__class__.__name__ == "Input"
        and "rbs-color-token-name-input" in element._classes
    ]
    token_colors = [
        element
        for element in created
        if element.__class__.__name__ == "ColorInput"
        and "rbs-color-token-value-input" in element._classes
    ]
    swatches = [
        element
        for element in created
        if "rbs-color-scheme-swatch" in getattr(element, "_classes", [])
    ]
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    labels = {
        getattr(element, "_text", None)
        for element in created
        if element.__class__.__name__ == "Label"
    }

    palette_size = len(instance.color_scheme.selectable_colors)
    assert not token_names
    assert len(token_colors) == palette_size
    assert len(swatches) == palette_size
    assert {"Generate matching accents", "Save color scheme"} <= buttons
    assert not any(element.__class__.__name__ == "Select" for element in created)
    assert not any(
        element._props.get("label") == f"{rotation.code} color"
        for element in created
        for rotation in instance.rotations
    )
    assert not any("Scheme changes update matching" in str(label) for label in labels)


def test_colors_settings_saves_scheme_and_remaps_assignments(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import _colors_settings
    from rbs.workspaces import InstanceEditImpact

    instance = sample_instance()
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Color workspace", instance)
    persisted: list[tuple[object, dict]] = []
    applied_themes: list[object] = []
    before = set(ui.context.client.elements)

    _colors_settings(
        workspace,
        lambda updated, **kwargs: persisted.append((updated, kwargs)),
        applied_themes.append,
    )

    created = _created_elements(before)
    scheme_name = next(
        element
        for element in created
        if element.__class__.__name__ == "Input"
        and "rbs-color-scheme-name-input" in element._classes
    )
    primary_color = next(
        element
        for element in created
        if element.__class__.__name__ == "ColorInput" and element.value == "#174A7E"
    )
    scheme_name.set_value("Example University")
    primary_color.set_value("#123A67")
    save = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Save color scheme"
    )
    next(iter(save._event_listeners.values())).handler(None)

    assert len(persisted) == 1
    updated, kwargs = persisted[0]
    assert updated.color_scheme.name == "Example University"
    assert updated.color_scheme.primary.color == "#123A67"
    assert updated.clinic_policy.site("cedar").color == "#123A67"
    assert kwargs == {"impact": InstanceEditImpact.PRESENTATION}
    assert applied_themes == [updated.color_scheme]


def test_colors_settings_generates_accents_from_unsaved_institutional_colors(
    tmp_path,
) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.models.color_scheme import generate_accent_colors
    from rbs.store import Store
    from rbs.ui.settings.view import _colors_settings

    instance = sample_instance()
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Color workspace", instance)
    persisted: list[object] = []
    applied_themes: list[object] = []
    before = set(ui.context.client.elements)

    _colors_settings(
        workspace,
        lambda updated, **_kwargs: persisted.append(updated),
        applied_themes.append,
    )

    created = _created_elements(before)
    color_inputs = [
        element
        for element in created
        if element.__class__.__name__ == "ColorInput"
        and "rbs-color-token-value-input" in element._classes
    ]
    primary, secondary, neutral, *accents = color_inputs
    primary.set_value("#123A67")
    secondary.set_value("#EAAA00")
    neutral.set_value("#5A5D61")
    generate = next(
        element
        for element in created
        if element.__class__.__name__ == "Button"
        and element._props.get("label") == "Generate matching accents"
    )

    next(iter(generate._event_listeners.values())).handler(None)

    expected = generate_accent_colors("#123A67", "#EAAA00", "#5A5D61")
    assert tuple(color.value for color in accents) == expected
    assert not persisted
    assert not applied_themes

    next(iter(generate._event_listeners.values())).handler(None)
    assert tuple(color.value for color in accents) == expected


def test_advanced_settings_exposes_every_objective_weight(tmp_path) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.models.instance import ObjectiveWeights
    from rbs.store import Store
    from rbs.ui.settings.view import _WEIGHT_FIELDS, _advanced_settings

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Tuned workspace", sample_instance())
    before = set(ui.context.client.elements)

    _advanced_settings(
        workspace,
        lambda *_args, **_kwargs: None,
    )

    created = _created_elements(before)
    numbers = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Number"
    }
    buttons = {
        element._props.get("label") for element in created if element.__class__.__name__ == "Button"
    }
    checkboxes = {
        getattr(element, "_text", None): element
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }

    # every weight the model defines is reachable from the tab
    assert {name for name, _label in _WEIGHT_FIELDS} == set(ObjectiveWeights.model_fields)
    assert {label for _name, label in _WEIGHT_FIELDS} <= numbers
    assert {
        "Time limit (seconds)",
        "CPU workers",
        "Parallel attempts",
        "Fewest per week",
        "Most per week",
    } <= numbers
    assert {"Save advanced settings", "Restore defaults"} <= buttons
    boundary_label = "Allow rotation blocks to span four-week boundaries"
    assert boundary_label in checkboxes
    assert checkboxes[boundary_label].value is False
    labels = {
        getattr(element, "_text", None)
        for element in created
        if element.__class__.__name__ == "Label"
    }
    assert (
        "Off keeps every rotation assignment inside Block A/1 through Block M/13. "
        "Turning it on can mean a more complex schedule, but possibly a more optimal one."
    ) in labels
    assert not any(
        text and "Attempts race independent seeds" in text
        for text in labels
    )
    assert not any(
        text and "Extra time buys very little" in text
        for text in labels
    )


def test_advanced_solver_config_reads_weights_and_clinic_bounds() -> None:
    from rbs.catalog import sample_instance
    from rbs.ui.settings.view import advanced_solver_config

    solver = sample_instance().solver
    updated = advanced_solver_config(
        solver,
        {
            "time_limit_seconds": 90,
            "allow_blocks_to_span_four_week_boundaries": True,
            "min_clinic_blocks_per_week": 3,
            "max_clinic_blocks_per_week": 4,
            "attending_sessions": 42,
            "clinic_block_week_evenness": 7,
        },
    )

    assert updated.time_limit_seconds == 90.0
    assert updated.num_workers == solver.num_workers
    assert updated.allow_blocks_to_span_four_week_boundaries is True
    assert updated.min_clinic_blocks_per_week == 3
    assert updated.max_clinic_blocks_per_week == 4
    assert updated.weights.attending_sessions == 42
    assert updated.weights.clinic_block_week_evenness == 7
    # a weight the tab did not send falls back to zero rather than silently keeping
    # the previous value, matching what an emptied field means on screen
    assert updated.weights.session_pgy_mix == 0


def test_advanced_solver_config_treats_blank_clinic_bounds_as_no_bound() -> None:
    from rbs.catalog import sample_instance
    from rbs.ui.settings.view import advanced_solver_config

    solver = sample_instance().solver.model_copy(
        update={"min_clinic_blocks_per_week": 2, "max_clinic_blocks_per_week": 5}
    )
    updated = advanced_solver_config(
        solver,
        {
            "time_limit_seconds": 60,
            "min_clinic_blocks_per_week": "",
            "max_clinic_blocks_per_week": None,
        },
    )

    assert updated.min_clinic_blocks_per_week is None
    assert updated.max_clinic_blocks_per_week is None


def test_advanced_solver_config_rejects_an_inverted_clinic_band() -> None:
    import pytest
    from pydantic import ValidationError

    from rbs.catalog import sample_instance
    from rbs.ui.settings.view import advanced_solver_config

    with pytest.raises(ValidationError):
        advanced_solver_config(
            sample_instance().solver,
            {
                "time_limit_seconds": 60,
                "min_clinic_blocks_per_week": 6,
                "max_clinic_blocks_per_week": 2,
            },
        )


def test_remember_settings_section_only_accepts_known_sections() -> None:
    from rbs.ui.app_shell import _remember_settings_section

    state = SimpleNamespace(settings_section="settings_general")
    _remember_settings_section(state, "settings_advanced")
    assert state.settings_section == "settings_advanced"

    _remember_settings_section(state, "settings_colors")
    assert state.settings_section == "settings_advanced"

    _remember_settings_section(state, "settings_training_levels")
    assert state.settings_section == "settings_training_levels"

    _remember_settings_section(state, "not_a_section")
    assert state.settings_section == "settings_training_levels"


def test_saved_scheme_roles_feed_nicegui_page_colors() -> None:
    from rbs.models.color_scheme import ColorScheme
    from rbs.ui.app_shell import _nicegui_theme_colors

    raw = ColorScheme().model_dump(mode="json")
    raw["primary"]["color"] = "#123A67"
    raw["secondary"]["color"] = "#EAAA00"
    raw["neutral"]["color"] = "#5A5D61"

    colors = _nicegui_theme_colors(ColorScheme.model_validate(raw))

    assert colors["primary"] == "#123A67"
    assert colors["secondary"] == "#EAAA00"
    assert colors["accent"] == "#5A5D61"
    assert colors["dark"] == "#262626"
    assert colors["positive"] == "#28735C"
    assert colors["negative"] == "#B42318"
    assert colors["warning"] == "#9A6700"


def test_saved_scheme_updates_the_existing_page_theme_element(monkeypatch) -> None:
    from rbs.models.color_scheme import ColorScheme
    from rbs.ui.app_shell import _set_nicegui_theme

    state = SimpleNamespace(theme=None)
    _set_nicegui_theme(state, ColorScheme())
    theme = state.theme
    assert theme._props["custom-colors"] == {
        "rbs_on_primary": "#FFFFFF",
        "rbs_on_secondary": "#262626",
        "rbs_primary_text": "#174A7E",
        "rbs_secondary_text": "#262626",
    }
    scripts: list[str] = []
    monkeypatch.setattr(theme.client, "run_javascript", scripts.append)
    raw = ColorScheme().model_dump(mode="json")
    raw["primary"]["color"] = "#123A67"
    raw["secondary"]["color"] = "#EAAA00"

    _set_nicegui_theme(state, ColorScheme.model_validate(raw))

    assert state.theme is theme
    assert theme._props["primary"] == "#123A67"
    assert theme._props["secondary"] == "#EAAA00"
    assert theme._props["positive"] == "#28735C"
    assert theme._props["negative"] == "#B42318"
    assert '"--q-primary": "#123A67"' in scripts[0]
    assert '"--q-rbs-primary-text": "#123A67"' in scripts[0]
    assert '"--q-rbs-secondary-text": "#262626"' in scripts[0]


def test_empty_chrome_uses_saved_application_color_scheme(tmp_path) -> None:
    from nicegui import ui

    from rbs.models.color_scheme import ColorScheme
    from rbs.store import Store
    from rbs.ui.app_shell import _mount_shell
    from rbs.ui.host import LocalHost
    from rbs.ui.session import WorkspaceSession

    raw = ColorScheme().model_dump(mode="json")
    raw["primary"]["color"] = "#123A67"
    raw["secondary"]["color"] = "#EAAA00"
    scheme = ColorScheme.model_validate(raw)
    store = Store(tmp_path / "empty.sqlite")
    store.init()
    session = WorkspaceSession(store=store)
    session.workspace_host = LocalHost(
        store,
        document_io=SimpleNamespace(
            application_settings=SimpleNamespace(
                settings=SimpleNamespace(colors=SimpleNamespace(scheme=scheme))
            )
        ),
    )
    session.header = ui.header()
    session.body = ui.column()

    _mount_shell(session)

    assert session.theme._props["primary"] == "#123A67"
    assert session.theme._props["secondary"] == "#EAAA00"
    assert session.chrome_scheme == scheme


def test_chrome_color_scheme_stays_consistent_without_an_open_workspace() -> None:
    from rbs.models.color_scheme import ColorScheme
    from rbs.ui.app_shell import _chrome_color_scheme

    def scheme_with(*, primary: str) -> ColorScheme:
        raw = ColorScheme().model_dump(mode="json")
        raw["primary"]["color"] = primary
        return ColorScheme.model_validate(raw)

    workspace_scheme = scheme_with(primary="#111111")
    saved_scheme = scheme_with(primary="#123A67")
    last_scheme = scheme_with(primary="#EAAA00")
    workspace = SimpleNamespace(instance=SimpleNamespace(color_scheme=workspace_scheme))
    session = SimpleNamespace(
        chrome_scheme=last_scheme,
        workspace_host=SimpleNamespace(
            document_io=SimpleNamespace(
                application_settings=SimpleNamespace(
                    settings=SimpleNamespace(colors=SimpleNamespace(scheme=saved_scheme))
                )
            )
        ),
    )

    assert _chrome_color_scheme(session, workspace) is workspace_scheme
    assert _chrome_color_scheme(session, None) is saved_scheme

    session.workspace_host.document_io.application_settings = None
    assert _chrome_color_scheme(session, None) is last_scheme

    session.chrome_scheme = None
    assert _chrome_color_scheme(session, None).primary.color == ColorScheme().primary.color


def test_remember_clinic_section_only_accepts_known_sections() -> None:
    from rbs.ui.app_shell import _remember_clinic_section

    state = SimpleNamespace(clinic_section="clinic_sites")
    _remember_clinic_section(state, "clinic_block_rules")
    assert state.clinic_section == "clinic_block_rules"

    _remember_clinic_section(state, "not_a_section")
    assert state.clinic_section == "clinic_block_rules"


def test_advanced_settings_keeps_automatic_clinic_balance_copy_dataset_neutral(
    tmp_path,
) -> None:
    from nicegui import ui

    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.settings.view import _advanced_settings

    instance = sample_instance()
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Banded workspace", instance)
    before = set(ui.context.client.elements)

    _advanced_settings(
        workspace,
        lambda *_args, **_kwargs: None,
    )

    created = _created_elements(before)
    checkboxes = {
        getattr(element, "_text", None)
        for element in created
        if element.__class__.__name__ == "Checkbox"
    }

    assert "Balance Clinic blocks automatically" in checkboxes
    assert not any("per week)" in str(label) for label in checkboxes)


class _DummyPanel:
    def clear(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_instance_save_refreshes_the_visible_tab_only(tmp_path) -> None:
    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.session import TAB_NAMES, WorkspaceSession

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Session workspace", sample_instance())
    session = WorkspaceSession(store=store, workspace_id=workspace.id, active_tab="rotations")
    rendered: list[str] = []
    session.panels = {name: _DummyPanel() for name in TAB_NAMES}
    session._render_tab = lambda _session, name: rendered.append(name)

    session.persist_instance(workspace, workspace.instance)

    assert rendered == ["rotations"]
    assert "block_schedule" in session.stale_panels
    assert "rotations" not in session.stale_panels


def test_schedule_save_can_defer_the_visible_tab_refresh(tmp_path) -> None:
    from rbs.catalog import sample_instance
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta
    from rbs.store import Store
    from rbs.ui.session import TAB_NAMES, WorkspaceSession

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Session workspace", sample_instance())
    session = WorkspaceSession(store=store, workspace_id=workspace.id, active_tab="residents")
    rendered: list[str] = []
    session.panels = {name: _DummyPanel() for name in TAB_NAMES}
    session._render_tab = lambda _session, name: rendered.append(name)
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=workspace.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        )
    )

    session.persist_schedule(workspace, schedule, refresh=False)

    assert rendered == []
    assert session.stale_panels == set(TAB_NAMES)
    assert store.get(workspace.id).schedule is not None


def test_instance_save_rejects_the_snapshot_an_editor_rendered_before_a_newer_edit(
    tmp_path,
) -> None:
    from rbs.catalog import sample_instance
    from rbs.models.workspace import WorkspaceConflictError
    from rbs.store import Store
    from rbs.ui.session import WorkspaceSession
    from rbs.workspaces import WorkspaceController

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    rendered = store.create("Session workspace", sample_instance())
    resident = rendered.instance.residents[0].revised(name="Stale resident name")
    stale_edit = rendered.instance.revised(
        residents=[resident, *rendered.instance.residents[1:]]
    )
    concurrent = rendered.instance.revised(lock_through_today=True)
    WorkspaceController(store).save_instance(rendered, concurrent)
    session = WorkspaceSession(store=store, workspace_id=rendered.id)

    with pytest.raises(WorkspaceConflictError, match="reload it before saving"):
        session.persist_instance(rendered, stale_edit)

    current = store.get(rendered.id)
    assert current.instance.lock_through_today
    assert current.instance.residents[0].name != "Stale resident name"
    assert current.workspace_revision == rendered.workspace_revision + 1


def test_schedule_save_rejects_a_snapshot_whose_inputs_changed(tmp_path) -> None:
    from rbs.catalog import sample_instance
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta
    from rbs.models.workspace import WorkspaceConflictError
    from rbs.store import Store
    from rbs.ui.session import WorkspaceSession
    from rbs.workspaces import WorkspaceController

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    rendered = store.create("Session workspace", sample_instance())
    WorkspaceController(store).save_instance(
        rendered,
        rendered.instance.revised(lock_through_today=True),
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=rendered.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        )
    )
    session = WorkspaceSession(store=store, workspace_id=rendered.id)

    with pytest.raises(WorkspaceConflictError, match="reload it before saving"):
        session.persist_schedule(rendered, schedule)

    current = store.get(rendered.id)
    assert current.schedule is None
    assert current.workspace_revision == rendered.workspace_revision + 1


def test_reading_a_workspace_does_not_implicitly_refresh_automatic_locks(
    tmp_path,
) -> None:
    from datetime import timedelta

    from rbs.catalog import sample_instance
    from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta
    from rbs.store import Store
    from rbs.ui.locks import THROUGH_TODAY_SOURCE
    from rbs.ui.session import WorkspaceSession

    instance = sample_instance().revised(lock_through_today=True)
    resident_id = instance.residents[0].id
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
            )
        ],
    )
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Automatic locks", instance, schedule)
    session = WorkspaceSession(store=store, workspace_id=workspace.id)

    observed = session.workspace()

    assert observed is not None
    assert observed.workspace_revision == workspace.workspace_revision
    assert not any(lock.source == THROUGH_TODAY_SOURCE for lock in observed.instance.locks)
    assert store.get(workspace.id).workspace_revision == workspace.workspace_revision

    refreshed = session.refresh_automatic_locks(
        observed,
        today=instance.calendar.first_week_start + timedelta(weeks=2),
    )

    automatic = [
        lock for lock in refreshed.instance.locks if lock.source == THROUGH_TODAY_SOURCE
    ]
    assert [(lock.rotation_id, lock.weeks) for lock in automatic] == [
        ("fmed", [1, 2, 3, 4])
    ]
    assert refreshed.workspace_revision == workspace.workspace_revision + 1
    assert refreshed.schedule is not None


def test_instance_edit_policy_preserves_names_and_stales_solver_changes(tmp_path) -> None:
    from rbs.catalog import sample_instance
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta
    from rbs.store import Store
    from rbs.ui.edit_policy import instance_edit_impact
    from rbs.ui.residents.ops import replace_resident
    from rbs.ui.session import WorkspaceSession
    from rbs.workspaces import InstanceEditImpact

    instance = sample_instance()
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        )
    )
    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Resident edits", instance, schedule)
    session = WorkspaceSession(store=store, workspace_id=workspace.id)
    previous = workspace.instance.residents[0]
    renamed = previous.revised(name="A presentation-only name")
    renamed_instance = replace_resident(workspace.instance, previous.id, renamed)

    rename_impact = instance_edit_impact(workspace.instance, renamed_instance)
    renamed_workspace = session.persist_instance(
        workspace,
        renamed_instance,
        impact=rename_impact,
    )

    assert rename_impact is InstanceEditImpact.PRESENTATION
    assert renamed_workspace.schedule is not None
    assert renamed_workspace.stale_schedule is None

    changed = renamed.revised(pgy=2 if renamed.pgy != 2 else 1)
    changed_instance = replace_resident(renamed_workspace.instance, renamed.id, changed)
    solver_impact = instance_edit_impact(
        renamed_workspace.instance,
        changed_instance,
    )
    changed_workspace = session.persist_instance(
        renamed_workspace,
        changed_instance,
        impact=solver_impact,
    )

    assert solver_impact is InstanceEditImpact.SOLVER_INPUT
    assert changed_workspace.schedule is None
    assert changed_workspace.stale_schedule is not None


def test_first_tab_visit_renders_a_stale_panel(tmp_path) -> None:
    from rbs.catalog import sample_instance
    from rbs.store import Store
    from rbs.ui.session import WorkspaceSession

    store = Store(tmp_path / "rbs.sqlite")
    store.init()
    workspace = store.create("Session workspace", sample_instance())
    session = WorkspaceSession(store=store, workspace_id=workspace.id)
    rendered: list[str] = []
    session.panels = {"clinic": _DummyPanel()}
    session._render_tab = lambda _session, name: rendered.append(name)
    session.stale_panels = {"clinic"}

    session.on_tab_change(SimpleNamespace(value="clinic"))

    assert session.active_tab == "clinic"
    assert rendered == ["clinic"]
    assert "clinic" not in session.stale_panels
