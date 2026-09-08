from datetime import date
from io import BytesIO

from pypdf import PdfReader

from rbs.catalog import sample_instance
from rbs.models.enums import SolverEngineName, SolverStatus
from rbs.models.schedule import Assignment, Schedule, ScheduleMeta
from rbs.ui.block_schedule_pdf import (
    block_schedule_page_weeks,
    block_schedule_pdf_filename,
    build_block_schedule_pdf,
)


def _schedule_for_export():
    instance = sample_instance()
    resident = instance.residents[0]
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
                end_week=4,
                weeks=list(range(1, 5)),
            ),
            Assignment(
                resident_id=resident.id,
                rotation_id="clinic",
                elective=True,
                start_week=5,
                end_week=52,
                weeks=list(range(5, 53)),
            ),
        ],
    )
    return instance, schedule


def test_block_schedule_pdf_covers_the_full_year_at_block_boundaries() -> None:
    instance, schedule = _schedule_for_export()

    pdf = build_block_schedule_pdf(instance, schedule)

    assert pdf.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 4
    assert reader.metadata.title == "Block Schedule - 2026-2027"
    assert float(reader.pages[0].mediabox.width) > float(reader.pages[0].mediabox.height)

    page_text = [page.extract_text() or "" for page in reader.pages]
    expected_ranges = [
        ("Blocks A-D", "Block A/1", "Block D/4", "Week 1", "Week 16"),
        ("Blocks E-G", "Block E/5", "Block G/7", "Week 17", "Week 28"),
        ("Blocks H-J", "Block H/8", "Block J/10", "Week 29", "Week 40"),
        ("Blocks K-M", "Block K/11", "Block M/13", "Week 41", "Week 52"),
    ]
    for page_number, (text, expected) in enumerate(
        zip(page_text, expected_ranges, strict=True),
        start=1,
    ):
        for label in expected:
            assert label in text
        assert instance.residents[0].name in text
        assert instance.residents[-1].name in text
        assert f"Page {page_number}" in text

    combined = "\n".join(page_text)
    assert "FMED" in combined
    assert "CLINIC (E)" in combined
    assert "VAC" in combined
    assert "CONF" in combined
    assert "PGY1" in combined
    assert "Blank = Not scheduled" in combined


def test_block_schedule_pages_balance_the_last_block_without_splitting_blocks() -> None:
    pages = block_schedule_page_weeks(52)

    assert pages == [
        list(range(1, 17)),
        list(range(17, 29)),
        list(range(29, 41)),
        list(range(41, 53)),
    ]
    assert all((page[0] - 1) % 4 == 0 for page in pages)
    assert all(page[-1] % 4 == 0 for page in pages)


def test_block_schedule_pdf_keeps_configured_time_away_without_a_solved_schedule() -> None:
    instance = sample_instance()

    reader = PdfReader(BytesIO(build_block_schedule_pdf(instance, None)))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert len(reader.pages) == 4
    assert instance.residents[0].name in text
    assert "VAC" in text
    assert "CONF" in text


def test_block_schedule_pdf_filename_is_safe_and_descriptive() -> None:
    assert (
        block_schedule_pdf_filename(
            "2026-2027",
            exported_on=date(2026, 8, 23),
        )
        == "block-schedule-2026-2027-exported-2026-08-23.pdf"
    )
