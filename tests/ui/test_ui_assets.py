import re
from importlib.resources import files
from pathlib import Path

from rbs.ui.app_branding import (
        BLOCK_LABEL_FIT_SCRIPT,
        FAVICON_URL,
        RECONNECT_BRANDING_HTML,
        SPINNER_ELAPSED_SCRIPT,
        STYLESHEET_URLS,
        WORDMARK_URL,
    )
from rbs.ui.buttons import (
    DESTRUCTIVE_BUTTON_PROPS,
    ICON_BUTTON_PROPS,
    PRIMARY_BUTTON_PROPS,
    SECONDARY_BUTTON_PROPS,
)
from rbs.ui.visual_tokens import (
    ACADEMIC_TINT,
    ADMIN,
    BORDER_STRONG,
    CLOSURE_TINT,
    CONFERENCE,
    DANGER,
    INK,
    PARTIAL_CLOSURE_TINT,
    SPECIAL_EVENT,
    SUCCESS,
    SURFACE_MUTED,
    TEXT_MUTED,
    VACATION,
    WARNING,
)


def _css_rule(stylesheet: str, selector: str) -> str:
    start = list(re.finditer(rf"^{re.escape(selector)} \{{", stylesheet, re.MULTILINE))[-1].start()
    return stylesheet[start : stylesheet.index("}", start) + 1]


def test_shared_button_variants_define_the_application_hierarchy() -> None:
    assert PRIMARY_BUTTON_PROPS == "unelevated no-caps"
    assert SECONDARY_BUTTON_PROPS == "outline no-caps"
    assert DESTRUCTIVE_BUTTON_PROPS == "unelevated no-caps color=negative"
    assert ICON_BUTTON_PROPS == "flat round dense"


def test_schedule_styles_live_in_packaged_css_assets() -> None:
    static = files("rbs.ui").joinpath("static")
    tokens_css = static.joinpath("tokens.css").read_text(encoding="utf-8")
    app_css = static.joinpath("app.css").read_text(encoding="utf-8")
    grid_css = static.joinpath("grid.css").read_text(encoding="utf-8")
    clinic_css = static.joinpath("clinic.css").read_text(encoding="utf-8")
    favicon = static.joinpath("favicon.svg").read_text(encoding="utf-8")
    wordmark = static.joinpath("wordmark.svg").read_text(encoding="utf-8")

    assert ".rbs-grid-wrap" in grid_css
    assert ".rbs-rotation-color-23" in grid_css
    assert ".rbs-pgy-label" in grid_css
    pgy_label = _css_rule(grid_css, ".rbs-pgy-label span")
    assert "left: 50%" in pgy_label
    assert "translate(-50%, -50%) rotate(-90deg)" in pgy_label
    pgy_spacer = _css_rule(grid_css, ".rbs-grid .rbs-pgy-spacer td")
    assert "height: 12px" in pgy_spacer
    assert "border: 0" in pgy_spacer
    assert ".rbs-resident-row" in grid_css
    assert ".rbs-resident-link" in grid_css
    assert ".rbs-block-state" in grid_css
    assert ".rbs-block-schedule-grid" in grid_css
    week_column = _css_rule(
        grid_css,
        ".rbs-block-schedule-grid col.rbs-block-week-column",
    )
    assert "width: 56px" in week_column
    assert ".rbs-block-name-code" in grid_css
    assert ".rbs-block-name.is-code .rbs-block-name-code" in grid_css
    assert "scrollWidth > label.clientWidth" in BLOCK_LABEL_FIT_SCRIPT
    assert "copyRect.right + 16" in BLOCK_LABEL_FIT_SCRIPT
    assert "controlsRect.bottom + 12" in BLOCK_LABEL_FIT_SCRIPT
    assert "horizontalRoom * 0.36" in BLOCK_LABEL_FIT_SCRIPT
    bands = _css_rule(grid_css, ".rbs-block-run,\n.rbs-block-state")
    assert "height: 42px" in bands
    resident_cells = _css_rule(
        grid_css,
        ".rbs-grid .rbs-resident-row > th,\n.rbs-grid .rbs-resident-row > td",
    )
    assert "border-bottom: 0" in resident_cells
    schedule_cells = _css_rule(grid_css, ".rbs-grid .rbs-resident-row > td")
    assert "border-right: 0" in schedule_cells
    assert "border-left: 0" in schedule_cells
    boundary = _css_rule(
        grid_css,
        ".rbs-grid .rbs-resident-row > td.rbs-four-week-boundary",
    )
    assert "border-left-width: 4px" in boundary
    vacation = _css_rule(grid_css, ".rbs-grid td.vac")
    assert "outline" not in vacation
    conference = _css_rule(grid_css, ".rbs-grid td.special")
    assert "outline" not in conference
    state_cell = _css_rule(grid_css, ".rbs-grid td.rbs-state-cell")
    assert "border-top-color: var(--rbs-white)" in state_cell
    assert ".rbs-block-weeks" not in grid_css
    assert "max-height: 70vh" not in grid_css
    assert ".rbs-clinic-wrap" in clinic_css
    assert ".rbs-clinic-person.site" in clinic_css
    assert ".rbs-clinic-swatch.site" in clinic_css
    assert ".rbs-clinic-toolbar" in clinic_css
    assert ".rbs-clinic-toolbar-controls" in clinic_css
    assert ".rbs-clinic-session-attending" in clinic_css
    assert ".rbs-clinic-session-att + .rbs-clinic-session-att::before" in clinic_css
    assert "justify-content: flex-end" in clinic_css
    assert "font-size: var(--rbs-type-caption-size)" in clinic_css
    assert "border-color: var(--rbs-clinic-site-color)" in clinic_css
    assert "background: var(--rbs-clinic-site-tint)" in clinic_css
    assert "#3971b8" not in clinic_css
    assert "border: 3px solid transparent" in clinic_css
    assert "repeating-linear-gradient" in clinic_css
    assert ".rbs-clinic-last-name" in clinic_css
    assert "@media (max-width: 760px)" in clinic_css
    assert ".rbs-clinic-training-level {\n    display: none;" in clinic_css
    clinic_person = _css_rule(clinic_css, ".rbs-clinic-person")
    assert "overflow: hidden" in clinic_person
    assert "text-overflow: ellipsis" in clinic_person
    assert "font-size: var(--rbs-type-secondary-size)" in clinic_css
    assert "text-align: center" in clinic_css
    assert all("?v=" in url for url in STYLESHEET_URLS)
    assert FAVICON_URL.startswith("/rbs-static/favicon.svg?v=")
    assert WORDMARK_URL.startswith("/rbs-static/wordmark.svg?v=")
    assert "RBS schedule mark" in favicon
    assert {"#174A7E", "#C58A17", "#102F4E", "#FFFFFF"} <= {
        token.upper() for token in favicon.split('"') if token.startswith("#")
    }
    assert "<text" not in wordmark
    assert "font-family" not in wordmark
    assert wordmark.count("<path") >= 3
    assert 'viewBox="0 0 180 64"' in wordmark
    assert "--rbs-primary: var(--q-primary, #174a7e)" in tokens_css
    assert "--rbs-secondary: var(--q-secondary, #c58a17)" in tokens_css
    assert "--rbs-accent: var(--q-accent, #52606d)" in tokens_css
    assert "--rbs-ink: #262626" in tokens_css
    assert "--rbs-primary-soft: color-mix" in tokens_css
    assert "--rbs-secondary-strong: color-mix" in tokens_css
    assert "--rbs-admin-grey: #5b6570" in tokens_css
    assert "--rbs-vacation-grey: #6b7580" in tokens_css
    assert "--rbs-conference-grey: #4b5967" in tokens_css
    assert "--rbs-special-event-grey: #607080" in tokens_css
    loading_message = _css_rule(app_css, ".rbs-loading-message")
    assert "color: var(--rbs-text-muted)" in loading_message
    assert "#popup.nicegui-error-popup" in app_css
    assert '#popup.nicegui-error-popup[aria-hidden="false"]' in app_css
    assert "pointer-events: auto" in app_css
    assert "var(--rbs-reconnect-mark)" in app_css
    assert 'url("/rbs-static/favicon.svg")' not in app_css
    assert WORDMARK_URL in RECONNECT_BRANDING_HTML
    assert "window.setInterval(update, 100)" in SPINNER_ELAPSED_SCRIPT
    assert "Elapsed:" not in SPINNER_ELAPSED_SCRIPT
    assert "transform: translateY(38px)" in app_css
    assert ".rbs-overlay-card" in app_css
    assert ".rbs-branded-dialog" in app_css
    assert ".rbs-dialog-wordmark" in app_css
    assert ".rbs-overlay-logo" not in app_css
    assert ".rbs-overlay-spinner" in app_css
    assert ".rbs-spinner-status" in app_css
    assert ".rbs-reconnect-spinner" in app_css
    assert ".rbs-solver-elapsed" in app_css
    assert ".rbs-loading-elapsed" in app_css
    assert ".rbs-reconnect-elapsed" in app_css
    assert "font-variant-numeric: tabular-nums" in app_css
    assert ".rbs-about-close" in app_css
    assert ".rbs-popout-dialog" in app_css
    assert ".rbs-workspace-settings-fields" in app_css
    assert ".rbs-about-release-notes-button" in app_css
    assert ".rbs-release-notes-dialog" in app_css
    assert ".rbs-release-notes-markdown" in app_css
    assert ".rbs-about-notices-button" in app_css
    assert ".rbs-about-license-button" in app_css
    assert ".rbs-third-party-scroll" in app_css
    assert ".rbs-third-party-text" in app_css
    assert "width: min(96vw, 1080px)" in app_css
    assert "max-width: min(96vw, 1080px)" in app_css
    assert ".q-dialog__inner--minimized > .rbs-third-party-dialog" in app_css
    assert "height: min(90vh, 820px)" in app_css
    assert ".rbs-solver-state" not in app_css
    assert ".rbs-empty-workspace" in app_css
    assert ".rbs-empty-workspace-arrow" in app_css
    assert ".rbs-empty-workspace-menu" in app_css
    assert "white-space: nowrap" in _css_rule(app_css, ".rbs-empty-workspace-menu .q-item__section")
    assert ".rbs-rbsc-upload" in app_css
    assert ".rbs-vacation-calendar" in app_css
    assert ".rbs-vacation-calendar-grid" in app_css
    assert ".is-selected-week" in app_css
    assert ".rbs-day-off-chip" in app_css
    assert ".is-selected-day" in app_css
    assert ".rbs-academic-override-row" in app_css
    assert ".rbs-academic-override-empty" in app_css
    assert ".rbs-resident-summary" in app_css
    assert ".rbs-resident-schedule-dialog" not in app_css
    assert ".rbs-resident-schedule-header" in app_css
    assert ".rbs-resident-schedule-actions" in app_css
    assert ".rbs-resident-schedule-tabs" in app_css
    assert ".rbs-resident-schedule-tabs .q-tab" in app_css
    assert "min-height: 52px" in app_css
    assert ".rbs-resident-schedule-panels" in app_css
    assert ".rbs-resident-schedule-content" in app_css
    assert ".rbs-resident-block-timeline" in app_css
    assert ".rbs-resident-block-lane" in app_css
    assert ".rbs-resident-block-band.is-vacation" in app_css
    assert ".rbs-resident-schedule-report" not in app_css
    assert ".rbs-resident-clinic-calendar-list" in app_css
    assert ".rbs-resident-clinic-week-grid" in app_css
    assert ".rbs-resident-clinic-event.admin" in app_css
    assert ".rbs-resident-clinic-event.academic" in app_css
    assert ".rbs-resident-clinic-event.manual-override" in app_css
    assert ".rbs-resident-clinic-override-badge" in app_css
    assert ".rbs-resident-clinic-week-override" in app_css
    assert ".rbs-resident-clinic-context-menu" in app_css
    assert "background: var(--rbs-academic-tint) !important" in app_css
    assert ".rbs-resident-clinic-event-lock" in app_css
    assert ".rbs-resident-clinic-conflict-icon" in app_css
    assert ".rbs-rotation-code-avatar" in app_css
    assert "background: var(--rbs-rotation-code-color, var(--rbs-primary))" in app_css
    assert ".rbs-rotation-overview-grid" in app_css
    assert ".rbs-rotation-editor-actions" in app_css
    elective_scroll = _css_rule(
        app_css,
        ".rbs-elective-editor-scroll .q-scrollarea__content",
    )
    assert "align-items: stretch" in elective_scroll
    assert "width: 100%" in elective_scroll
    assert "min-width: 0" in elective_scroll
    clinic_week = _css_rule(app_css, ".rbs-clinic-week-grid")
    assert "max-width: 100%" in clinic_week
    assert "repeat(7, minmax(7rem, 1fr))" in clinic_week
    assert "overflow-x: auto" in clinic_week
    assert ".rbs-master-directory-body" in app_css
    assert ".rbs-master-detail-panel > :only-child" in app_css
    assert ".rbs-master-detail-panel > :only-child > .rbs-master-detail:last-child" in app_css
    assert "flex: 1 1 auto" in app_css
    assert ".rbs-master-split.rbs-master-has-selection" in app_css
    button_rule = _css_rule(app_css, ".q-btn")
    assert "text-transform: none" in button_rule
    assert ".rbs-page-shell" in app_css
    assert ".rbs-page-toolbar" in app_css
    assert ".rbs-configuration-tabs" in app_css
    assert ".rbs-rotation-code-input" in app_css
    assert "text-transform: uppercase" in app_css
    assert ".rbs-color-scheme-swatch" in app_css
    assert "background: var(--rbs-color-scheme-value)" in app_css
    assert ".rbs-rotation-color-palette" in app_css
    assert ".q-btn.rbs-rotation-color-choice" in app_css
    assert ".rbs-rotation-summary-table col.time" in app_css
    summary_table = _css_rule(app_css, ".rbs-rotation-summary-table")
    assert "min-width: 880px" in summary_table
    summary_time = _css_rule(app_css, ".rbs-rotation-summary-table col.time")
    assert "width: 92px" in summary_time
    assert ".rbs-clinic-advanced-limits" in app_css
    assert "@media (max-width: 760px)" in app_css
    assert "grid-template-columns: auto minmax(0, 1fr)" in app_css
    assert ".rbs-resident-schedule-header" in app_css
    assert ".rbs-clinic-editor-header" in app_css
    assert ".rbs-save-button .q-btn__content > .block" in app_css


def test_feature_stylesheets_only_consume_visual_tokens() -> None:
    """Prevent a gradual return to one-off visual values in feature CSS."""
    static = files("rbs.ui").joinpath("static")
    for filename in ("app.css", "grid.css", "clinic.css"):
        stylesheet = static.joinpath(filename).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", stylesheet), filename
        assert not re.search(r"\b(?:rgb|rgba|hsl|hsla|color-mix)\(", stylesheet), filename

        for value in re.findall(r"font-size:\s*([^;]+);", stylesheet):
            normalized = " ".join(value.split())
            assert normalized == "0 !important" or normalized.startswith(("var(", "clamp(")), (
                filename,
                normalized,
            )
        assert not re.search(r"font-weight:\s*\d+", stylesheet), filename

        for value in re.findall(r"border-radius:\s*([^;]+);", stylesheet):
            assert value.strip() == "0" or value.strip().startswith("var("), (
                filename,
                value,
            )
        for value in re.findall(r"box-shadow:\s*([^;]+);", stylesheet):
            assert value.strip() == "none" or value.strip().startswith("var("), (
                filename,
                value,
            )
        assert not re.search(r"(?:transition|animation)[^;]*\b\d+(?:\.\d+)?m?s\b", stylesheet), (
            filename
        )


def test_python_ui_uses_semantic_typography_and_neutral_classes() -> None:
    ui_root = Path(__file__).parents[2] / "src" / "rbs" / "ui"
    forbidden = re.compile(
        r"(?<!rbs-)text-(?:h[1-6]|body[12]|caption|subtitle[12]|grey-\d+)\b"
        r"|(?<!rbs-)font-(?:medium|bold)\b"
        r"|color=[\"']grey-\d+"
    )
    offenders = [
        str(path.relative_to(ui_root))
        for path in ui_root.rglob("*.py")
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_css_and_python_fixed_color_tokens_stay_in_sync() -> None:
    tokens_css = files("rbs.ui").joinpath("static", "tokens.css").read_text(encoding="utf-8")
    expected = {
        "--rbs-ink": INK,
        "--rbs-text-muted": TEXT_MUTED,
        "--rbs-surface-muted": SURFACE_MUTED,
        "--rbs-border-strong": BORDER_STRONG,
        "--rbs-success": SUCCESS,
        "--rbs-danger": DANGER,
        "--rbs-warning": WARNING,
        "--rbs-admin-grey": ADMIN,
        "--rbs-academic-tint": ACADEMIC_TINT,
        "--rbs-vacation-grey": VACATION,
        "--rbs-conference-grey": CONFERENCE,
        "--rbs-special-event-grey": SPECIAL_EVENT,
        "--rbs-closure-tint": CLOSURE_TINT,
        "--rbs-partial-closure-tint": PARTIAL_CLOSURE_TINT,
    }
    declarations = dict(
        re.findall(r"^\s*(--rbs-[\w-]+):\s*(#[0-9a-fA-F]{3,8});", tokens_css, re.MULTILINE)
    )
    assert {name: declarations[name].upper() for name in expected} == expected


def test_system_schedule_states_use_fixed_distinct_gray_patterns() -> None:
    static = files("rbs.ui").joinpath("static")
    app_css = static.joinpath("app.css").read_text(encoding="utf-8")
    grid_css = static.joinpath("grid.css").read_text(encoding="utf-8")
    clinic_css = static.joinpath("clinic.css").read_text(encoding="utf-8")

    clinic_admin = _css_rule(clinic_css, ".rbs-clinic-person.admin")
    clinic_event = _css_rule(clinic_css, ".rbs-clinic-special-event")
    resident_admin = _css_rule(app_css, ".rbs-resident-clinic-event.admin")
    resident_event = _css_rule(app_css, ".rbs-resident-clinic-event.special-event")
    vacation = _css_rule(grid_css, ".rbs-grid td.vac")
    conference = _css_rule(grid_css, ".rbs-grid td.special")
    resident_vacation = _css_rule(
        app_css,
        ".rbs-resident-block-band.is-vacation",
    )
    resident_conference = _css_rule(
        app_css,
        ".rbs-resident-block-band.is-special",
    )

    assert "var(--rbs-admin-grey)" in clinic_admin
    assert "135deg" in clinic_admin
    assert "var(--rbs-admin-grey)" in resident_admin
    assert "135deg" in resident_admin

    assert "var(--rbs-special-event-grey)" in clinic_event
    assert "radial-gradient" in clinic_event
    assert "135deg" not in clinic_event
    assert "var(--rbs-special-event-grey)" in resident_event
    assert "radial-gradient" in resident_event

    assert "var(--rbs-vacation-grey)" in vacation
    assert "0deg" in vacation
    assert "var(--rbs-conference-grey)" in conference
    assert "90deg" in conference
    assert "0deg" in resident_vacation
    assert "90deg" in resident_conference


def test_static_schedule_palette_matches_generated_defaults() -> None:
    from rbs.models.color_scheme import DEFAULT_COLOR_SCHEME

    tokens_css = files("rbs.ui").joinpath("static", "tokens.css").read_text(encoding="utf-8")
    declarations = dict(
        re.findall(r"^\s*(--rbs-palette-[0-9]+):\s*(#[0-9a-fA-F]{6});", tokens_css, re.MULTILINE)
    )

    assert tuple(
        declarations[f"--rbs-palette-{index}"].upper()
        for index in range(len(DEFAULT_COLOR_SCHEME.palette))
    ) == DEFAULT_COLOR_SCHEME.palette
