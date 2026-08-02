"""Generate a results PDF and three deliberately divergent company models.

The point of three models is that none of FinScan's behaviour may come from
knowing what a model looks like. They disagree on everything that companies
actually disagree on:

  northwind  labels in A, headers row 4, plain RGB blue inputs, units in crores,
             three tabs of which one is a cover sheet
  acme       note numbers in A and labels in B, headers row 7, *theme*-coloured
             blue inputs, two statement tabs kept in different units, and a
             pre-formatted empty column reserved for the next quarter
  zenith     no colour convention at all — everything black, no formulas

    python samples/make_samples.py
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.styles.colors import Color
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).parent

BLUE = Font(color="0000FF")                       # classic hardcode blue
BLACK = Font(color="000000")                      # calculated
THEME_BLUE = Font(color=Color(theme=8, tint=-0.2))  # accent5, as a corporate template does it

# (caption, Q1FY27, Q4FY26, Q1FY26, FY26) — Rs. lakhs, as printed in the filing
ROWS_CONSOL = [
    ("1. Revenue from operations", 128_450.00, 121_300.00, 108_920.00, 462_180.00),
    ("2. Other income", 3_120.00, 2_880.00, 2_640.00, 11_050.00),
    ("3. Total income (1+2)", 131_570.00, 124_180.00, 111_560.00, 473_230.00),
    ("4. Expenses", None, None, None, None),
    ("   (a) Cost of materials consumed", 52_310.00, 49_870.00, 45_100.00, 189_420.00),
    ("   (b) Purchases of stock-in-trade", 6_240.00, 5_910.00, 5_330.00, 22_480.00),
    ("   (c) Changes in inventories of finished goods", -1_180.00, 940.00, -620.00, 1_310.00),
    ("   (d) Employee benefits expense", 21_760.00, 20_940.00, 19_210.00, 81_530.00),
    ("   (e) Finance costs", 4_310.00, 4_180.00, 4_020.00, 16_640.00),
    ("   (f) Depreciation and amortisation expense", 8_920.00, 8_640.00, 8_110.00, 33_480.00),
    ("   (g) Other expenses", 18_470.00, 17_920.00, 16_540.00, 69_870.00),
    ("   Total expenses", 110_830.00, 108_400.00, 97_690.00, 414_730.00),
    ("5. Profit before exceptional items and tax", 20_740.00, 15_780.00, 13_870.00, 58_500.00),
    ("6. Exceptional items", 0.00, -1_200.00, 0.00, -1_200.00),
    ("7. Profit before tax (5+6)", 20_740.00, 14_580.00, 13_870.00, 57_300.00),
    ("8. Tax expense", None, None, None, None),
    ("   Current tax", 5_420.00, 3_910.00, 3_640.00, 15_180.00),
    ("   Deferred tax", -210.00, 240.00, 110.00, 460.00),
    ("   Total tax expense", 5_210.00, 4_150.00, 3_750.00, 15_640.00),
    ("9. Profit for the period (7-8)", 15_530.00, 10_430.00, 10_120.00, 41_660.00),
    ("10. Other comprehensive income", 180.00, -90.00, 140.00, 320.00),
    ("11. Total comprehensive income", 15_710.00, 10_340.00, 10_260.00, 41_980.00),
    ("12. Paid-up equity share capital (FV Rs.10)", 4_960.00, 4_960.00, 4_960.00, 4_960.00),
    ("13. Earnings per share (Rs.) - Basic", 31.31, 21.03, 20.40, 83.99),
    ("        - Diluted", 31.14, 20.92, 20.29, 83.54),
]


def _fmt(v: float | None) -> str:
    if v is None:
        return ""
    s = f"{abs(v):,.2f}"
    return f"({s})" if v < 0 else s


def make_pdf(path: Path) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=landscape(A4),
                            topMargin=24, bottomMargin=24, leftMargin=24, rightMargin=24)
    story = [
        Paragraph("<b>NORTHWIND INDUSTRIES LIMITED</b>", styles["Title"]),
        Paragraph("CIN: L27100MH1994PLC123456 &nbsp;|&nbsp; Regd. Office: Mumbai 400001",
                  styles["Normal"]),
        Spacer(1, 8),
        Paragraph("<b>Statement of Unaudited Consolidated Financial Results for the "
                  "Quarter ended 30 June 2026</b>", styles["Heading3"]),
        Paragraph("(Rs. in Lakhs, except per share data)", styles["Normal"]),
        Spacer(1, 6),
    ]
    header = [
        ["Particulars", "Quarter ended", "", "", "Year ended"],
        ["", "30.06.2026", "31.03.2026", "30.06.2025", "31.03.2026"],
        ["", "(Unaudited)", "(Audited)", "(Unaudited)", "(Audited)"],
    ]
    body = [[c, _fmt(a), _fmt(b), _fmt(d), _fmt(e)] for c, a, b, d, e in ROWS_CONSOL]
    tbl = Table(header + body, colWidths=[280, 90, 90, 90, 90], repeatRows=3)
    tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("SPAN", (1, 0), (3, 0)),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 2), colors.HexColor("#EEEEEE")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Notes: 1. The above results were reviewed by the Audit Committee and approved by the "
        "Board of Directors at its meeting held on 24 July 2026.", styles["Normal"]))
    doc.build(story)


# --------------------------------------------------------------------------- #
# A P&L block, expressed once and rendered into whatever geometry a model uses.
# (caption, kind, quarterly values in crores | formula template)
#   kind "in" = hardcode input, "fx" = formula (col/row placeholders filled in)
# --------------------------------------------------------------------------- #
PL_BLOCK = [
    ("Net Sales", "in", [1089.20, 1132.40, 1178.60, 1213.00]),
    ("Other Income", "in", [26.40, 27.10, 28.30, 28.80]),
    ("Total Income", "fx", "={c}{r0}+{c}{r1}"),
    ("Raw Material Consumed", "in", [451.00, 468.20, 481.60, 498.70]),
    ("Purchase of Traded Goods", "in", [53.30, 55.10, 56.90, 59.10]),
    ("(Increase)/Decrease in Stock", "in", [-6.20, 3.10, -4.40, 9.40]),
    ("Staff Cost", "in", [192.10, 198.40, 204.20, 209.40]),
    ("Interest Cost", "in", [40.20, 41.10, 41.60, 41.80]),
    ("Depreciation & Amortization", "in", [81.10, 82.90, 84.20, 86.40]),
    ("Other Expenditure", "in", [165.40, 169.80, 173.10, 179.20]),
    ("Total Expenditure", "fx", "=SUM({c}{r3}:{c}{r9})"),
    ("Operating Profit (EBITDA)", "fx", "={c}{r2}-{c}{r10}+{c}{r7}+{c}{r8}-{c}{r1}"),
    ("Exceptional Items", "in", [0.00, 0.00, 0.00, -12.00]),
    ("PBT", "fx", "={c}{r2}-{c}{r10}+{c}{r12}"),
    ("Total Tax", "in", [37.50, 38.20, 45.10, 41.50]),
    ("Net Profit", "fx", "={c}{r13}-{c}{r14}"),
    ("EPS - Basic (Rs.)", "in", [20.40, 20.70, 25.12, 21.03]),
    ("EPS - Diluted (Rs.)", "in", [20.29, 20.58, 24.98, 20.92]),
]


def _render_pl(ws, *, label_col: int, header_row: int, first_col: int,
               headers: list[str], input_font: Font, scale: float = 1.0,
               with_formulas: bool = True, blank_columns: int = 0) -> None:
    from openpyxl.utils import get_column_letter

    first_row = header_row + 1
    rows = {f"r{i}": first_row + i for i in range(len(PL_BLOCK))}

    for i, h in enumerate(headers):
        cell = ws.cell(header_row, first_col + i, h)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="right")
    ws.cell(header_row, label_col, "Particulars").font = Font(bold=True)

    for i, (label, kind, payload) in enumerate(PL_BLOCK):
        r = first_row + i
        ws.cell(r, label_col, label)
        for j in range(len(headers)):
            c = first_col + j
            cell = ws.cell(r, c)
            cell.number_format = "#,##0.00"
            if kind == "in":
                cell.value = round(payload[j] * scale, 2)
                cell.font = Font(color=input_font.color)
            elif with_formulas:
                cell.value = payload.format(c=get_column_letter(c), **rows)
                cell.font = BLACK
            else:
                # no-formula model: the subtotal is typed in as a hardcode too
                cell.value = None
                cell.font = BLACK

        # a colourless model still needs subtotal values in it
        if kind == "fx" and not with_formulas:
            for j in range(len(headers)):
                ws.cell(r, first_col + j).value = round(_static_subtotal(label, j) * scale, 2)

    # optional pre-formatted but empty column(s) reserved for coming quarters
    for k in range(blank_columns):
        c = first_col + len(headers) + k
        for i, (_, kind, _) in enumerate(PL_BLOCK):
            cell = ws.cell(first_row + i, c)
            cell.number_format = "#,##0.00"
            cell.font = Font(color=input_font.color) if kind == "in" else BLACK

    ws.column_dimensions[get_column_letter(label_col)].width = 34
    for j in range(len(headers) + blank_columns):
        ws.column_dimensions[get_column_letter(first_col + j)].width = 13


_STATIC = {
    "Total Income": [1115.60, 1159.50, 1206.90, 1241.80],
    "Total Expenditure": [976.90, 1018.60, 1037.20, 1084.00],
    "Operating Profit (EBITDA)": [234.20, 240.30, 250.20, 258.60],
    "PBT": [138.70, 140.90, 169.70, 145.80],
    "Net Profit": [101.20, 102.70, 124.60, 104.30],
}


def _static_subtotal(label: str, j: int) -> float:
    return _STATIC[label][j]


# --------------------------------------------------------------------------- #
def make_northwind(path: Path) -> None:
    """Labels in A, headers row 4, plain blue inputs, crores, plus two off-topic tabs."""
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["A1"] = "Northwind Industries Ltd — Quarterly Model"
    cover["A2"] = "Prepared by Investment Research"
    cover["A3"] = "Last updated 31 March 2026"

    ws = wb.create_sheet("P&L Summary")
    ws["A1"] = "Northwind Industries Ltd — Quarterly P&L"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = "(Rs. in Crores)"
    ws["A2"].font = Font(italic=True, size=9)
    _render_pl(ws, label_col=1, header_row=4, first_col=2, input_font=BLUE,
               headers=["Q1 FY2026", "Q2 FY2026", "Q3 FY2026", "Q4 FY2026"])

    ass = wb.create_sheet("Assumptions")
    ass["A1"] = "Discount rate"
    ass["B1"] = 0.11
    ass["A2"] = "Terminal growth"
    ass["B2"] = 0.03
    wb.save(path)


def make_acme(path: Path) -> None:
    """Note numbers in A, labels in B, headers row 7, theme-coloured inputs,
    two statement tabs in different units, and an empty reserved column."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Consolidated P&L"
    ws["B1"] = "Acme Manufacturing Limited"
    ws["B1"].font = Font(bold=True, size=13)
    ws["B2"] = "Consolidated statement of profit and loss"
    ws["B3"] = "All figures in Rs. Crores unless stated"
    ws["A7"] = "Note"
    _render_pl(ws, label_col=2, header_row=7, first_col=4, input_font=THEME_BLUE,
               headers=["Q1 FY2026", "Q2 FY2026", "Q3 FY2026", "Q4 FY2026"],
               blank_columns=1)
    for i in range(len(PL_BLOCK)):
        ws.cell(8 + i, 1, f"{i + 1}")

    st = wb.create_sheet("Standalone P&L")
    st["B1"] = "Acme Manufacturing Limited — Standalone"
    st["B1"].font = Font(bold=True, size=13)
    st["B3"] = "All figures in Rs. Lakhs"
    _render_pl(st, label_col=2, header_row=7, first_col=4, input_font=THEME_BLUE,
               headers=["Q1 FY2026", "Q2 FY2026", "Q3 FY2026", "Q4 FY2026"],
               scale=100.0)          # same business, kept in lakhs on this tab

    seg = wb.create_sheet("Segments")
    seg["A1"] = "Segment"
    seg["B1"] = "Q4 FY2026"
    for i, (name, val) in enumerate(
        [("North America", 402.1), ("Europe", 318.4), ("Asia", 492.5)], start=2
    ):
        seg.cell(i, 1, name)
        seg.cell(i, 2, val)
    wb.save(path)


def make_dated(path: Path) -> None:
    """Headers are real dates, as an analyst model keeps them. This is what lets
    FinScan tell whether a filing actually belongs in the next column."""
    from datetime import datetime

    wb = Workbook()
    ws = wb.active
    ws.title = "P&L Summary"
    ws["A1"] = "Dated Co — Quarterly P&L"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = "(Rs. in Crores)"
    _render_pl(
        ws, label_col=1, header_row=4, first_col=2, input_font=BLUE,
        headers=[datetime(2025, 3, 31), datetime(2025, 6, 30),
                 datetime(2025, 9, 30), datetime(2025, 12, 31)],
    )
    for c in range(2, 6):
        ws.cell(4, c).number_format = "yyyy-mm-dd"
    wb.save(path)


def make_zenith(path: Path) -> None:
    """No colour convention and no formulas — the fallback case."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Quarterly"
    ws["A1"] = "Zenith Chemicals Ltd"
    ws["A1"].font = Font(bold=True)
    ws["A2"] = "Rs. in Crores"
    _render_pl(ws, label_col=1, header_row=3, first_col=2, input_font=BLACK,
               headers=["Q1 FY2026", "Q2 FY2026", "Q3 FY2026", "Q4 FY2026"],
               with_formulas=False)
    wb.save(path)


if __name__ == "__main__":
    make_pdf(HERE / "northwind_q1_fy27_results.pdf")
    make_northwind(HERE / "northwind_model.xlsx")
    make_acme(HERE / "acme_model.xlsx")
    make_zenith(HERE / "zenith_model.xlsx")
    make_dated(HERE / "dated_model.xlsx")
    print(f"Wrote samples to {HERE}")
