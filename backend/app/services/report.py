import io
from typing import List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, numbers

BRAND_BLUE = colors.HexColor("#1565C0")
BRAND_LIGHT = colors.HexColor("#E3F2FD")
ROW_ALT = colors.HexColor("#F5F5F5")

SEVERITY_COLORS = {"high": "#FFCDD2", "medium": "#FFF9C4", "low": "#E8F5E9"}


def _severity_fill(sev: str) -> colors.Color:
    return colors.HexColor(SEVERITY_COLORS.get(sev.lower(), "#FFFFFF"))


def generate_pdf(assessment, findings: List) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], textColor=BRAND_BLUE, spaceAfter=4)
    normal = styles["Normal"]
    small = ParagraphStyle("Small", parent=normal, fontSize=8)

    story = []

    # Header
    story.append(Paragraph("Azure Cost Assessment Report", h1))
    story.append(HRFlowable(width="100%", thickness=2, color=BRAND_BLUE))
    story.append(Spacer(1, 0.15 * inch))

    meta = [
        ["Assessment ID", str(assessment.id), "User", assessment.user_email],
        ["Date", assessment.created_at.strftime("%Y-%m-%d %H:%M UTC"), "Status", assessment.status.upper()],
        ["Subscriptions", str(len(assessment.subscription_ids)), "Total Findings", str(assessment.findings_count)],
    ]
    meta_table = Table(meta, colWidths=[1.5 * inch, 2.5 * inch, 1.5 * inch, 2.5 * inch])
    meta_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("BACKGROUND", (0, 0), (0, -1), BRAND_LIGHT),
        ("BACKGROUND", (2, 0), (2, -1), BRAND_LIGHT),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.2 * inch))

    # Savings summary
    summary_data = [
        ["Monthly Savings", "Annual Savings"],
        [
            f"${assessment.total_savings_monthly:,.2f}",
            f"${assessment.total_savings_annual:,.2f}",
        ],
    ]
    sum_table = Table(summary_data, colWidths=[3.5 * inch, 3.5 * inch])
    sum_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("FONTSIZE", (0, 1), (-1, 1), 16),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWHEIGHT", (0, 1), (-1, 1), 30),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    story.append(sum_table)
    story.append(Spacer(1, 0.25 * inch))

    # Findings table
    story.append(Paragraph("Findings", h1))
    story.append(Spacer(1, 0.1 * inch))

    headers = ["#", "Category", "Resource", "Resource Group", "Monthly ($)", "Annual ($)", "Severity"]
    col_widths = [0.3 * inch, 2.4 * inch, 2.2 * inch, 1.8 * inch, 0.9 * inch, 0.9 * inch, 0.8 * inch]
    rows = [headers]
    for i, f in enumerate(findings, 1):
        rows.append([
            str(i),
            f.display_name,
            Paragraph(f.resource_name or "—", small),
            f.resource_group or "—",
            f"${f.estimated_savings_monthly:,.2f}",
            f"${f.estimated_savings_annual:,.2f}",
            f.severity.upper(),
        ])

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("ALIGN", (4, 0), (5, -1), "RIGHT"),
        ("ALIGN", (6, 0), (6, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for i, f in enumerate(findings, 1):
        bg = _severity_fill(f.severity)
        style.append(("BACKGROUND", (6, i), (6, i), bg))
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (5, i), ROW_ALT))

    tbl.setStyle(TableStyle(style))
    story.append(tbl)

    doc.build(story)
    return buf.getvalue()


def generate_excel(assessment, findings: List) -> bytes:
    wb = openpyxl.Workbook()

    # ── Summary sheet ──────────────────────────────────────────────────────
    ws_sum = wb.active
    ws_sum.title = "Summary"
    hdr_fill = PatternFill("solid", fgColor="1565C0")
    hdr_font = Font(color="FFFFFF", bold=True, size=11)

    summary_rows = [
        ("Assessment ID", assessment.id),
        ("User", assessment.user_email),
        ("Date", assessment.created_at.strftime("%Y-%m-%d %H:%M UTC")),
        ("Status", assessment.status.upper()),
        ("Subscriptions", len(assessment.subscription_ids)),
        ("Total Findings", assessment.findings_count),
        ("Monthly Savings (USD)", assessment.total_savings_monthly),
        ("Annual Savings (USD)", assessment.total_savings_annual),
    ]
    for r, (label, value) in enumerate(summary_rows, 1):
        ws_sum.cell(r, 1, label).font = Font(bold=True)
        ws_sum.cell(r, 2, value)
    ws_sum.column_dimensions["A"].width = 30
    ws_sum.column_dimensions["B"].width = 40

    # ── Findings sheet ─────────────────────────────────────────────────────
    ws = wb.create_sheet("Findings")
    headers = [
        "Category", "Resource Name", "Resource Group", "Subscription ID",
        "Resource Type", "Monthly Savings (USD)", "Annual Savings (USD)",
        "Severity", "Description", "Recommendation",
    ]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col, h)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    sev_fills = {
        "high": PatternFill("solid", fgColor="FFCDD2"),
        "medium": PatternFill("solid", fgColor="FFF9C4"),
        "low": PatternFill("solid", fgColor="E8F5E9"),
    }

    for row, f in enumerate(findings, 2):
        sev = f.severity.lower()
        values = [
            f.display_name, f.resource_name or "", f.resource_group or "",
            f.subscription_id or "", f.resource_type or "",
            round(f.estimated_savings_monthly, 2),
            round(f.estimated_savings_annual, 2),
            f.severity.upper(), f.description or "", f.recommendation or "",
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row, col, val)
            if col in (6, 7):
                cell.number_format = '#,##0.00'
            if col == 8:
                cell.fill = sev_fills.get(sev, PatternFill())

    col_widths = [30, 25, 20, 38, 35, 22, 22, 12, 60, 60]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{openpyxl.utils.get_column_letter(len(headers))}1"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
