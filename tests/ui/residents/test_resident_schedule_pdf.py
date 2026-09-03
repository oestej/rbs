from datetime import date
from io import BytesIO

from pypdf import PdfReader

from rbs.models.resident import Resident
from rbs.ui.residents.schedule_pdf import (
    _block_schedule_table,
    _styles,
    build_resident_schedule_pdf,
    resident_schedule_pdf_filename,
)
from rbs.ui.schedule_styles import (
    CONFERENCE_COLOR,
    CONFERENCE_TINT,
    VACATION_COLOR,
    VACATION_TINT,
)


def test_resident_schedule_pdf_contains_both_reports() -> None:
    resident = Resident(id="resident-001", name="Avery Chen", pgy=1)
    pdf = build_resident_schedule_pdf(
        resident=resident,
        academic_year="2026-2027",
        block_rows=[
            {
                "period": "Week 11 (Sep 7–13, 2026)",
                "rotation": "CLINIC · Clinic\nContinuity Clinic",
                "kind": "rotation",
            },
            {
                "period": "Week 12 (Sep 14–20, 2026)",
                "rotation": "Vacation",
                "kind": "vacation",
            },
            {
                "period": "Week 13 (Sep 21–27, 2026)",
                "rotation": "CLINIC · Clinic (Cont.)",
                "kind": "rotation",
            },
        ],
        clinic_rows=[
            {
                "dates": "Sep 7–13, 2026",
                "week": "11",
                "rotation": "CLINIC · Clinic",
                "monday_date": "Sep 7",
                "monday_morning": "Maple",
                "monday_morning_kind": "site",
                "monday_morning_color": "#3971B8",
                "monday_morning_tint": "#EBF1F8",
                "monday_afternoon": "",
                "monday_afternoon_kind": "",
                "tuesday_date": "Sep 8",
                "tuesday_morning": "",
                "tuesday_morning_kind": "",
                "tuesday_afternoon": "Admin",
                "tuesday_afternoon_kind": "admin",
                "sessions": "Mon AM · Maple\nTue PM · Admin",
            }
        ],
    )

    assert pdf.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert len(reader.pages) == 2
    assert reader.metadata.title == "Avery Chen Schedule"
    assert "Avery Chen" in text
    assert "Block Schedule" in text
    assert "Clinic Schedule" in text
    assert "Vacation" in text
    assert "Clinic (Cont.)" in text
    assert "Continuity Clinic" in text
    assert "Locked weeks" not in text
    assert "alongside each assignment" not in text
    assert "WEEK 11 | Sep 7-13, 2026" in text
    assert "MON" in text
    assert "TUE" in text
    assert "AM" in text
    assert "PM" in text
    assert "Maple" in text
    assert "Admin" in text
    assert "Page 1" in text
    assert "Page 2" in text


def test_resident_schedule_pdf_filename_is_safe_and_descriptive() -> None:
    resident = Resident(id="resident-001", name="Avery Chen, MD", pgy=1)

    assert (
        resident_schedule_pdf_filename(
            resident,
            "2026-2027",
            exported_on=date(2026, 8, 23),
        )
        == "avery-chen-md-schedule-2026-2027-exported-2026-08-23.pdf"
    )


def test_resident_pdf_uses_fixed_gray_vacation_and_conference_colors() -> None:
    from reportlab.lib import colors

    table = _block_schedule_table(
        [
            {"period": "Week 1", "rotation": "Vacation", "kind": "vacation"},
            {"period": "Week 2", "rotation": "Conference", "kind": "special"},
        ],
        _styles(),
    )

    backgrounds = [command[3] for command in table._bkgrndcmds if command[0] == "BACKGROUND"]
    accents = [command[4] for command in table._linecmds if command[0] == "LINEBEFORE"]
    assert colors.HexColor(VACATION_TINT) in backgrounds
    assert colors.HexColor(CONFERENCE_TINT) in backgrounds
    assert colors.HexColor(VACATION_COLOR) in accents
    assert colors.HexColor(CONFERENCE_COLOR) in accents


def test_resident_schedule_pdf_uses_the_configured_rotation_color(monkeypatch) -> None:
    import rbs.ui.residents.schedule_pdf as pdf_module

    mixed: list[tuple[str, float]] = []

    def record_tint(color: str, *, white_mix: float = 0.9) -> str:
        mixed.append((color, white_mix))
        return "#FFFFFF"

    monkeypatch.setattr(pdf_module, "lighten_hex_color", record_tint)
    build_resident_schedule_pdf(
        resident=Resident(id="resident-001", name="Avery Chen", pgy=1),
        academic_year="2026-2027",
        block_rows=[
            {
                "period": "Weeks 1–4",
                "rotation": "FMED · Family Med Education Service",
                "kind": "rotation",
                "color": "#28735C",
            }
        ],
        clinic_rows=[],
    )

    assert mixed == [("#28735C", 0.88)]
