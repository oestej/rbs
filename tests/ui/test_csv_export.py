import csv
from io import StringIO

import pytest

from rbs.ui.csv_export import build_spreadsheet_csv


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        "+1+1",
        "-1+1",
        "@SUM(1,1)",
        "＝1+1",
        "＋1+1",
        "－1+1",
        "＠SUM(1,1)",
        "  =1+1",
        "\t=1+1",
        "\r=1+1",
        "\n=1+1",
        " \t\r\n=1+1",
        "\u00a0=1+1",
        "\tName",
        "\rName",
        "\nName",
        '=1+1\";=1+1',
    ],
)
def test_csv_marks_formula_like_headers_and_cells_as_text(value: str) -> None:
    output = build_spreadsheet_csv([value, "Other"], [[value, "Next"]])

    assert list(csv.reader(StringIO(output, newline=""))) == [
        ["'" + value, "Other"],
        ["'" + value, "Next"],
    ]
    assert output.startswith('"\'')


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Normal name",
        "Children's clinic",
        "42",
        "—",
        "2026-09-18",
        "'=1+1",
        "Name,=1+1",
        'Name\",=1+1',
        "Name;=1+1",
        "Name\r=1+1",
        "Name\n=1+1",
        "Name\r\n=1+1",
    ],
)
def test_csv_preserves_other_text_and_cell_boundaries(value: str) -> None:
    output = build_spreadsheet_csv(["First", "Second"], [[value, "Next"]])

    assert list(csv.reader(StringIO(output, newline=""))) == [
        ["First", "Second"],
        [value, "Next"],
    ]
    assert output.startswith('"First","Second"\n"')
