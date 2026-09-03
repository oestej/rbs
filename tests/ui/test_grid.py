from datetime import date

from rbs.catalog import sample_instance
from rbs.ui.grid import (
    format_weeks,
    four_week_block_groups,
    group_label,
    monday_header,
    parse_weeks,
    render_grid_html,
    rotation_color_class,
    visible_week_numbers,
    week_groups,
)
from rbs.ui.residents.ops import resident_schedule_report_rows


def test_parse_and_format_weeks() -> None:
    assert parse_weeks("12, 13, 28, 41") == [12, 13, 28, 41]
    assert parse_weeks("") == []
    assert format_weeks([12, 13]) == "12, 13"


def test_rotation_color_class_is_stable() -> None:
    assert rotation_color_class("fmed") == rotation_color_class("fmed")
    assert rotation_color_class("fmed") != rotation_color_class("clinic")


def test_grid_html_includes_residents_and_time_off_without_locks() -> None:
    instance = sample_instance()
    markup = render_grid_html(instance, schedule=None)
    assert "Avery Chen" in markup
    assert "Taylor Kim" in markup
    assert "vac" in markup
    assert " lock" not in markup
    assert "locked week" not in markup
    assert '<span class="rbs-block-week-number">Week 1</span>' in markup
    assert '<span class="rbs-block-week-date">(Jun 29)</span>' in markup
    assert '<span class="rbs-block-week-number">Week 2</span>' in markup
    assert '<span class="rbs-block-week-date">(Jul 06)</span>' in markup
    assert (
        '<th class="rbs-block-week-header rbs-block-start" scope="col" '
        'title="Week 1 · Monday Jun 29">'
    ) in markup
    assert "rbs-block-barrier" not in markup
    assert ">1</th>" not in markup
    assert 'id="rbs-block-grid" class="rbs-grid-wrap" tabindex="0"' in markup


def test_grid_groups_resident_swimlanes_by_pgy() -> None:
    markup = render_grid_html(sample_instance(), schedule=None)

    assert markup.count('class="rbs-pgy-label"') == 3
    assert ('<th class="rbs-pgy-label" scope="rowgroup" rowspan="8"><span>PGY1</span>') in markup
    assert ('<th class="rbs-pgy-label" scope="rowgroup" rowspan="8"><span>PGY2</span>') in markup
    assert ('<th class="rbs-pgy-label" scope="rowgroup" rowspan="8"><span>PGY3</span>') in markup
    assert markup.count('class="rbs-resident-row"') == 24
    assert markup.count('class="rbs-pgy-spacer"') == 2
    assert '<td colspan="54"></td>' in markup
    assert markup.index("PGY1") < markup.index("Avery Chen")
    assert markup.index("Avery Chen") < markup.index("PGY2")
    assert markup.index("PGY2") < markup.index("Taylor Kim")


def test_grid_resident_names_link_to_the_resident_editor() -> None:
    markup = render_grid_html(
        sample_instance(),
        schedule=None,
        resident_edit_url="/",
    )

    assert (
        '<a class="rbs-resident-link" href="/?resident=resident-001" '
        'title="Edit resident Avery Chen" aria-label="Edit resident Avery Chen">'
        "Avery Chen</a>"
    ) in markup


def test_monday_header_is_mmm_dd() -> None:
    start = date(2026, 6, 29)
    assert monday_header(start, 1) == "Jun 29"
    assert monday_header(start, 2) == "Jul 06"


def test_four_week_block_groups_cover_blocks_a_through_m() -> None:
    groups = four_week_block_groups(list(range(1, 53)))

    assert len(groups) == 13
    assert groups[0] == ("A", [1, 2, 3, 4])
    assert groups[-1] == ("M", [49, 50, 51, 52])


def test_visible_weeks_hide_only_completed_monday_through_sunday_weeks() -> None:
    start = date(2026, 6, 29)

    assert visible_week_numbers(
        start,
        52,
        show_past_weeks=False,
        today=date(2026, 8, 22),
    ) == list(range(8, 53))
    assert visible_week_numbers(
        start,
        52,
        show_past_weeks=True,
        today=date(2026, 8, 22),
    ) == list(range(1, 53))


def test_grid_can_hide_past_week_columns() -> None:
    markup = render_grid_html(
        sample_instance(),
        schedule=None,
        show_past_weeks=False,
        today=date(2026, 8, 22),
    )

    assert '<span class="rbs-block-week-number">Week 8</span>' in markup
    assert '<span class="rbs-block-week-date">(Aug 17)</span>' in markup
    assert '<span class="rbs-block-week-date">(Aug 10)</span>' not in markup
    assert '<span class="rbs-block-week-date">(Jun 29)</span>' not in markup
    assert (
        '<th class="rbs-block-group" scope="colgroup" colspan="1">Block B/2</th>'
        in markup
    )
    assert "Block A/1" not in markup


def test_grid_uses_one_fixed_compact_column_per_visible_week() -> None:
    markup = render_grid_html(sample_instance(), schedule=None)

    assert (
        '<table class="rbs-grid rbs-block-schedule-grid" style="width:3126px">'
        in markup
    )
    assert markup.count("rbs-block-week-column") == 52
    assert "rbs-block-start-column" not in markup
    assert markup.count('class="rbs-block-group"') == 13
    assert (
        '<th class="rbs-block-group" scope="colgroup" colspan="4">Block A/1</th>'
        in markup
    )
    assert (
        '<th class="rbs-block-group" scope="colgroup" colspan="4">Block M/13</th>'
        in markup
    )

    current_markup = render_grid_html(
        sample_instance(),
        schedule=None,
        show_past_weeks=False,
        today=date(2026, 8, 22),
    )
    assert (
        '<table class="rbs-grid rbs-block-schedule-grid" style="width:2734px">'
        in current_markup
    )
    assert current_markup.count("rbs-block-week-column") == 45


def test_week_groups_pair_the_year() -> None:
    groups = week_groups(52, 2)
    assert len(groups) == 26
    assert groups[0] == [1, 2]
    assert groups[-1] == [51, 52]
    assert group_label([1, 2]) == "1–2"


def test_solved_grid_uses_uninterrupted_assignment_and_vacation_runs() -> None:
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident_id = instance.residents[0].id
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="fmed",
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
            ),
            Assignment(
                resident_id=resident_id,
                rotation_id="clinic",
                start_week=5,
                end_week=52,
                weeks=list(range(5, 53)),
            ),
        ],
    )
    markup = render_grid_html(instance, schedule)
    first_row_start = markup.index('<tr class="rbs-resident-row"')
    first_row_end = markup.index("</tr>", first_row_start)
    resident_markup = markup[first_row_start:first_row_end]
    assert "rbs-block-schedule-grid grouped" in markup
    assert "rbs-rotation-color-" in markup
    assert rotation_color_class(instance.rotation("fmed").color) in markup
    assert rotation_color_class(instance.rotation("clinic").color) in markup
    assert '<span class="rbs-block-name-full" aria-hidden="true">Clinic</span>' in markup
    assert '<span class="rbs-block-name-code" aria-hidden="true">CLINIC</span>' in markup
    assert '<span class="rbs-block-name-full" aria-hidden="true">clinic</span>' not in markup
    assert 'class="rbs-state-cell vac' in markup
    assert '<strong class="rbs-vacation-marker">VAC</strong>' in markup
    assert "rbs-block-weeks" not in markup
    assert f"--rbs-rotation-color:{instance.rotation('fmed').color}" in markup
    assert "--rbs-rotation-foreground:#FFFFFF" in markup
    assert 'colspan="4"' in markup
    assert 'colspan="2" class="rbs-state-cell vac"' in markup
    assert " lock" not in markup
    assert "locked week" not in markup
    assert "vacation 12, 13" in markup
    assert resident_markup.count('<strong class="rbs-vacation-marker">VAC</strong>') == 3
    assert "rbs-four-week-boundary" in resident_markup

    resident_rows = resident_schedule_report_rows(instance, schedule, resident_id)
    assert any(
        row["kind"] == "rotation"
        and row["color"] == instance.rotation("fmed").color
        and row["color_class"] == rotation_color_class(instance.rotation("fmed").color)
        for row in resident_rows
    )


def test_same_schedule_run_stays_unbroken_across_four_week_boundaries() -> None:
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Assignment, Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0].model_copy(update={"vacation_weeks": []})
    instance = instance.revised(
        residents=[
            resident,
            *instance.residents[1:],
        ],
    )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                start_week=1,
                end_week=52,
                weeks=list(range(1, 53)),
            )
        ],
    )

    markup = render_grid_html(instance, schedule)
    first_row_start = markup.index('<tr class="rbs-resident-row"')
    first_row_end = markup.index("</tr>", first_row_start)
    resident_markup = markup[first_row_start:first_row_end]

    assert 'colspan="52" class="rbs-block-cell' in resident_markup
    assert "rbs-four-week-boundary" not in resident_markup
