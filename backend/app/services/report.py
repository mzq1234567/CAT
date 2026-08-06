"""
Client-facing cost-assessment report (PDF).

`generate_pdf` renders the branded TechPlus Talent template. The document's *copy and layout* live
in `assets/report_template.yml`; this module only supplies the *data* — every figure, table row and
chart series is derived from the assessment and its findings, so nothing in the report is
hardcoded. Sections whose findings are absent are skipped rather than printed empty.
"""
from __future__ import annotations

import io
import os
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

import yaml
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend

from PIL import Image as PILImage

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
_TEMPLATE_PATH = os.path.join(_ASSET_DIR, "report_template.yml")

# Cover artwork (see assets/README.md). Everything but the backdrop is optional — a missing file is
# simply not drawn, so the cover degrades gracefully rather than failing the whole report.
_COVER_BG = "bg.png"                        # full-bleed backdrop, authored at the A4 aspect ratio
_COVER_LOGO = "Picture1.png"                # TPT wordmark lockup
_COVER_ART = "cover_illustration.png.png"   # isometric assessment illustration
_COVER_COIN = "Picture5.png"                # green "$" hexagon
_COVER_AZURE = "azure_logo.png"             # Azure "A" (rasterised from Picture4.svg)

# Helvetica-safe currency symbols (₹/native glyphs don't render in the base PDF font, so INR → "Rs ";
# the $-currencies are disambiguated with a country prefix).
_CURRENCY_SYMBOLS = {"USD": "$", "CAD": "CA$", "AUD": "A$", "GBP": "£", "EUR": "€", "INR": "Rs "}
_CUR = {"code": "USD", "symbol": "$"}


def _set_currency(code: Optional[str]) -> None:
    code = (code or "USD").upper()
    _CUR["code"] = code
    _CUR["symbol"] = _CURRENCY_SYMBOLS.get(code, code + " ")


def _usd(v: Optional[float], dec: int = 2) -> str:
    return f"{_CUR['symbol']}{(v or 0):,.{dec}f}"


def _money0(v: Optional[float]) -> str:
    """Whole-currency, for chart labels and axis ticks."""
    return f"{_CUR['symbol']}{(v or 0):,.0f}"


# ── Template ────────────────────────────────────────────────────────────────────────

_TEMPLATE_CACHE: Dict[str, object] = {}


def _template() -> Dict:
    """Parsed report_template.yml, reloaded whenever the file changes on disk."""
    try:
        stamp = os.path.getmtime(_TEMPLATE_PATH)
    except OSError:
        return {}
    if _TEMPLATE_CACHE.get("stamp") != stamp:
        with open(_TEMPLATE_PATH, encoding="utf-8") as fh:
            _TEMPLATE_CACHE["data"] = yaml.safe_load(fh) or {}
        _TEMPLATE_CACHE["stamp"] = stamp
    return _TEMPLATE_CACHE["data"]  # type: ignore[return-value]


# ── Fonts ───────────────────────────────────────────────────────────────────────────

_FONTS_READY: Dict[str, str] = {}


def _fonts(cfg: Dict) -> str:
    """Register the bundled font family and return its base name.

    Falls back to Helvetica when the TTFs are missing so the report still renders; only the
    typeface differs.
    """
    family = cfg.get("family") or "Carlito"
    if _FONTS_READY.get("family"):
        return _FONTS_READY["family"]
    directory = os.path.join(_ASSET_DIR, cfg.get("dir") or "fonts")
    faces = {
        family: cfg.get("regular"),
        f"{family}-Bold": cfg.get("bold"),
        f"{family}-Italic": cfg.get("italic"),
        f"{family}-BoldItalic": cfg.get("bold_italic"),
    }
    try:
        for name, filename in faces.items():
            pdfmetrics.registerFont(TTFont(name, os.path.join(directory, filename)))
        pdfmetrics.registerFontFamily(
            family, normal=family, bold=f"{family}-Bold",
            italic=f"{family}-Italic", boldItalic=f"{family}-BoldItalic")
    except Exception:  # noqa: BLE001 — missing/unreadable TTFs
        family = "Helvetica"
    _FONTS_READY["family"] = family
    return family


# ── Assets ──────────────────────────────────────────────────────────────────────────

_ASSET_CACHE: Dict[str, Optional[ImageReader]] = {}


def _asset(name: str) -> Optional[ImageReader]:
    """Cached ImageReader for a cover asset, trimmed to its visible (non-transparent) bounds.

    The source art carries wide transparent margins that differ per file, so trimming lets the
    layout position the *artwork* rather than each file's incidental padding. Returns None when
    the file is missing or unreadable — callers just skip it.
    """
    if name not in _ASSET_CACHE:
        path = os.path.join(_ASSET_DIR, name)
        reader = None
        try:
            im = PILImage.open(path)
            im.load()
            if im.mode in ("RGBA", "LA"):
                box = im.getchannel("A").getbbox()
                if box:
                    im = im.crop(box)
            reader = ImageReader(im)
        except Exception:  # noqa: BLE001
            reader = None
        _ASSET_CACHE[name] = reader
    return _ASSET_CACHE[name]


def _asset_image(name: str, width: float):
    """Trimmed asset as an Image *flowable* (for use inside tables), or None if unavailable.

    Goes through an in-memory PNG because the Image flowable takes a path or file-like, not the
    ImageReader that `_asset` caches.
    """
    if _asset(name) is None:
        return None
    try:
        from reportlab.platypus import Image as _Image
        im = PILImage.open(os.path.join(_ASSET_DIR, name)).convert("RGBA")
        box = im.getchannel("A").getbbox()
        if box:
            im = im.crop(box)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        buf.seek(0)
        return _Image(buf, width=width, height=width * im.height / im.width)
    except Exception:  # noqa: BLE001
        return None


def _place(canvas, name: str, x: float, top: float, width: float = None,
           height: float = None) -> None:
    """Draw cover asset `name` with its top-left at (x, top), `top` measured down from the page top.

    Give width or height; the other follows from the artwork's aspect ratio.
    """
    reader = _asset(name)
    if reader is None:
        return
    iw, ih = reader.getSize()
    if width is None:
        width = height * iw / ih
    if height is None:
        height = width * ih / iw
    canvas.drawImage(reader, x, A4[1] - top - height, width=width, height=height, mask="auto")


# ── Findings organisation ───────────────────────────────────────────────────────────

# category → pillar, matching the 3-pillar client template (Compute / Storage / Network).
# Database-style commitments fold into Compute.
_PILLAR = {
    "ri_vm": "Compute", "windows_ahb": "Compute",
    "sql_ahb": "Compute",
    "oversized_vms": "Compute", "idle_vms": "Compute", "deallocated_vms": "Compute",
    "vm_rightsizing": "Compute", "idle_app_service_plans": "Compute",
    "app_service_plan_rightsizing": "Compute", "app_service_reserved_capacity": "Compute",
    "paused_sql_databases": "Compute", "stopped_sql_managed_instances": "Compute",
    "sql_db_rightsizing": "Compute", "sql_mi_rightsizing": "Compute",
    "sql_db_reserved_capacity": "Compute", "sql_mi_reserved_capacity": "Compute",
    "cosmos_reserved_capacity": "Compute", "mysql_reserved_capacity": "Compute",
    "unattached_managed_disks": "Storage", "orphaned_snapshots": "Storage",
    "managed_disk_reserved_capacity": "Storage", "disk_rightsizing": "Storage",
    "backup_redundancy": "Storage", "azure_files_reserved_capacity": "Storage",
    "orphaned_public_ips": "Network", "empty_load_balancers": "Network",
    "idle_nat_gateways": "Network", "bastion_hosts": "Network",
}
# Categories rendered by their own dedicated table, so the pillar's catch-all table skips them.
_DEDICATED = {"ri_vm", "windows_ahb", "disk_rightsizing"}
_PILLARS = ("Compute", "Storage", "Network")


def _pillar_of(finding) -> str:
    return _PILLAR.get(finding.category, "Other")


def _annual(f) -> float:
    return float(getattr(f, "estimated_savings_annual", 0.0) or 0.0)


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


# ── Data context ────────────────────────────────────────────────────────────────────

def _ri_items(findings: List) -> List[Dict]:
    """Per-VM Reserved Instance rows from the aggregated ri_vm finding's reservation items."""
    out: List[Dict] = []
    for f in findings:
        if f.category != "ri_vm":
            continue
        for item in (f.details or {}).get("reservation_items", []) or []:
            out.append({
                "name": item.get("name") or item.get("sku") or "—",
                "sku": item.get("sku") or "—",
                "region": item.get("region") or "—",
                "quantity": item.get("quantity") or "",
                "annual_ondemand": (item.get("monthly_ondemand") or 0) * 12,
                "annual_savings": (item.get("monthly_savings") or 0) * 12,
                "annual_savings_3yr": (item.get("monthly_savings_3yr") or 0) * 12,
            })
    return out


def _ahb_items(findings: List) -> List[Dict]:
    """Per-VM Azure Hybrid Benefit rows.

    Current annual cost prefers the VM's measured cost; when Cost Management data is absent it
    falls back to the Windows-inclusive list price used to derive the saving, so the column is
    always grounded in the same figure the saving came from.
    """
    out: List[Dict] = []
    for f in findings:
        if f.category != "windows_ahb":
            continue
        for vm in (f.details or {}).get("eligible_vms", []) or []:
            # None (not 0) when neither figure is present, so the cell reads "—" rather than
            # asserting the VM costs nothing.
            monthly_cost = vm.get("actual_monthly_cost") or vm.get("windows_price")
            out.append({
                "name": vm.get("name") or "—",
                "sku": vm.get("sku") or "—",
                "annual_cost": monthly_cost * 12 if monthly_cost else None,
                "annual_savings": (vm.get("monthly_savings") or 0) * 12,
            })
    return out


def _disk_sku_items(findings: List) -> List[Dict]:
    """Managed-disk SKU right-sizing rows (premium → standard SSD), when the engine emits them."""
    out: List[Dict] = []
    for f in findings:
        if f.category != "disk_rightsizing":
            continue
        d = f.details or {}
        monthly_cost = getattr(f, "actual_monthly_cost", None) or d.get("monthly_cost")
        out.append({
            "name": f.resource_name or "—",
            "current_sku": d.get("current_sku") or "—",
            "recommended_sku": d.get("recommended_sku") or "—",
            "quantity": d.get("quantity") or 1,
            "annual_cost": monthly_cost * 12 if monthly_cost else None,
            "annual_savings": _annual(f),
        })
    return out


def _pillar_rows(findings: List, pillar: str) -> List[Dict]:
    """Catch-all rows for a pillar: every finding without a dedicated table of its own."""
    out: List[Dict] = []
    for f in findings:
        if _pillar_of(f) != pillar or f.category in _DEDICATED:
            continue
        monthly_cost = getattr(f, "actual_monthly_cost", None)
        out.append({
            "name": f.resource_name or f.display_name or "—",
            "opportunity": f.display_name or f.category,
            "annual_cost": (monthly_cost * 12) if monthly_cost else None,
            "annual_savings": _annual(f),
        })
    out.sort(key=lambda r: r["annual_savings"], reverse=True)
    return out


def _pillar_options(findings: List, pillar: str) -> List[Dict]:
    """Chart series for a pillar: annual savings grouped by opportunity type."""
    totals: "OrderedDict[str, float]" = OrderedDict()
    for f in findings:
        if _pillar_of(f) != pillar:
            continue
        label = f.display_name or f.category
        totals[label] = totals.get(label, 0.0) + _annual(f)
    rows = [{"label": k, "value": v} for k, v in totals.items() if v > 0]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def _environment_rows(assessment, findings: List) -> List[List[str]]:
    """Left/right rows for the Environment Details table, from the assessment record."""
    tenant = assessment.tenant_display_name or "—"
    rows = [["Tenant Details", f"{tenant}<br/>({assessment.tenant_id or '—'})"]]

    subs = assessment.subscription_names or {}
    if subs:
        detail = "<br/>".join(f"{name or sid}<br/>({sid})" for sid, name in subs.items())
    else:
        detail = "<br/>".join(assessment.subscription_ids or []) or "—"
    rows.append(["Subscription Details", detail])

    rows.append(["Total No. of resources", str(assessment.total_resources or "—")])

    # Top 3 only: the column stores a longer breakdown for the live discovery metrics, but this row
    # reads as prose and turns into a wall of text if every type is joined in.
    types = [t.get("type", "") for t in (assessment.major_resource_types or [])[:3] if t.get("type")]
    rows.append(["Major Resource Type", " and ".join(types) if types else "—"])

    if assessment.current_monthly_spend is not None:
        when = assessment.created_at.strftime("%b %Y") if assessment.created_at else ""
        label = f"Last Month Consumption ({when})" if when else "Last Month Consumption"
        rows.append([label, _usd(assessment.current_monthly_spend, 0)])
    return rows


def _context(assessment, findings: List) -> Dict:
    """Everything the template can reference — all of it derived from the assessment."""
    annual = assessment.total_savings_annual or 0.0
    monthly = assessment.total_savings_monthly or 0.0
    spend = assessment.current_annual_spend if assessment.cost_data_available else None

    # `tenant_display_name` doubles as both today; a distinct engaging-client field would slot in
    # here without touching the template.
    name = assessment.tenant_display_name or "Your Organisation"

    ctx: Dict = {
        "client": name,
        "tenant": name,
        "tenant_id": assessment.tenant_id or "—",
        "savings_3year": _usd(annual * 3),
        "savings_annual": _usd(annual),
        "savings_monthly": _usd(monthly),
        "annual_spend": _usd(spend) if spend else "",
        "has_spend": bool(spend),
        "no_spend": not spend,
        "_spend": spend or 0.0,
        "_savings": annual,
        "_growth": getattr(assessment, "observed_annual_growth", None),
        "environment_rows": _environment_rows(assessment, findings),
        "ri_items": _ri_items(findings),
        "ahb_items": _ahb_items(findings),
        "disk_sku_items": _disk_sku_items(findings),
        "_year": (assessment.created_at or datetime.utcnow()).year,
    }

    for pillar in _PILLARS:
        key = pillar.lower()
        total = sum(_annual(f) for f in findings if _pillar_of(f) == pillar)
        ctx[f"{key}_savings"] = _usd(total)
        ctx[f"{key}_any"] = total > 0 or any(_pillar_of(f) == pillar for f in findings)
        ctx[f"{key}_options"] = _pillar_options(findings, pillar)
        ctx[f"{key}_other"] = _pillar_rows(findings, pillar)

    ctx["vm_any"] = bool(ctx["ri_items"] or ctx["ahb_items"])
    ctx["compute_other"] = _pillar_rows(findings, "Compute")
    ctx["no_findings"] = not findings
    return ctx


# ── Styles ──────────────────────────────────────────────────────────────────────────

_ALIGN = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT, "justify": TA_JUSTIFY}


def _styles(tpl: Dict) -> Dict[str, ParagraphStyle]:
    """Build ParagraphStyles from the template's `styles` + `palette` blocks."""
    family = _fonts(tpl.get("fonts") or {})
    palette = tpl.get("palette") or {}

    def colour(value, default="#000000"):
        raw = palette.get(value, value if isinstance(value, str) else default) or default
        return colors.HexColor(raw)

    out: Dict[str, ParagraphStyle] = {}
    for name, spec in (tpl.get("styles") or {}).items():
        spec = spec or {}
        size = float(spec.get("size", 11))
        out[name] = ParagraphStyle(
            name,
            fontName=f"{family}-Bold" if spec.get("bold") else family,
            fontSize=size,
            leading=float(spec.get("leading", size * 1.32)),
            textColor=colour(spec.get("colour", "ink")),
            alignment=_ALIGN.get(spec.get("align", "left"), TA_LEFT),
            spaceBefore=float(spec.get("space_before", 0)),
            spaceAfter=float(spec.get("space_after", 0)),
            leftIndent=float(spec.get("left_indent", 0)),
            bulletIndent=float(spec.get("bullet_indent", 0)),
        )
    # Heading levels double as TOC anchors; ReportLab keys the notification off the style name.
    for level, key in ((0, "h1"), (1, "h2"), (2, "h3")):
        if key in out:
            out[key].name = f"TOCH{level + 1}"
    return out


# ── Charts ──────────────────────────────────────────────────────────────────────────

def _projection(spend: float, savings: float, growth: float):
    """Cumulative 3-year (ACR, ACR-after-savings, savings) at annual growth rate `growth`.

    Growth is LINEAR (simple), not compounded — spend rises by the same amount each year, matching
    the "Linear Growth" scenario name: year N spend = spend x (1 + growth*N).
    """
    rate = (savings / spend) if spend else 0.0
    acr, after, saved = [], [], []
    c_acr = c_after = c_saved = 0.0
    for year in range(3):
        annual = spend * (1 + growth * year)
        s = annual * rate
        c_acr += annual
        c_saved += s
        c_after += annual - s
        acr.append(round(c_acr))
        after.append(round(c_after))
        saved.append(round(c_saved))
    return acr, after, saved


def _framed(drawing: Drawing, palette: Dict) -> Drawing:
    """Wrap a chart in the reference's thin outer frame."""
    frame = Rect(0, 0, drawing.width, drawing.height, fillColor=None,
                 strokeColor=colors.HexColor(palette.get("panel_border", "#9DC3E6")),
                 strokeWidth=0.75)
    drawing.insert(0, frame)
    return drawing


def _projection_chart(title: str, series, labels, tpl: Dict, family: str, width: float):
    """Grouped 3-series bar chart matching the reference: value labels, bottom legend, frame."""
    palette = tpl.get("palette") or {}
    cols = [colors.HexColor(palette.get(k, d)) for k, d in
            (("spend", "#2E74B5"), ("after", "#ED7D31"), ("savings", "#1E6C34"))]
    names = (tpl.get("projections") or {}).get("series") or ["ACR", "ACR after Savings", "Savings"]

    height = 232.0
    d = Drawing(width, height)
    d.add(String(width / 2, height - 22, title, fontName=family, fontSize=12,
                 fillColor=colors.HexColor(palette.get("ink", "#000000")), textAnchor="middle"))

    bc = VerticalBarChart()
    bc.x, bc.y = 56, 52
    bc.width, bc.height = width - 78, height - 104
    bc.data = series
    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontName = family
    bc.categoryAxis.labels.fontSize = 8.5
    bc.categoryAxis.strokeColor = colors.HexColor("#BFBFBF")
    bc.valueAxis.valueMin = 0
    bc.valueAxis.labels.fontName = family
    bc.valueAxis.labels.fontSize = 8.5
    bc.valueAxis.labelTextFormat = lambda v: _money0(v)
    bc.valueAxis.strokeColor = colors.HexColor("#BFBFBF")
    bc.valueAxis.gridStrokeColor = colors.HexColor("#D9D9D9")
    bc.valueAxis.gridStrokeWidth = 0.4
    bc.valueAxis.visibleGrid = True
    # Gap between adjacent bars matters: ACR and ACR-after-savings sit close in value, so their
    # labels collide if the bars touch.
    bc.barSpacing = 4
    bc.groupSpacing = 26
    for i, col in enumerate(cols):
        bc.bars[i].fillColor = col
        bc.bars[i].strokeColor = None
    bc.barLabelFormat = lambda v: _money0(v)
    bc.barLabels.fontName = family
    bc.barLabels.fontSize = 6.5
    bc.barLabels.dy = 5
    bc.barLabels.fillColor = colors.HexColor("#404040")
    d.add(bc)

    legend = Legend()
    legend.x, legend.y = width / 2 - 96, 22
    legend.deltax = 74
    legend.dxTextSpace = 4
    legend.columnMaximum = 1
    legend.alignment = "right"
    legend.fontName = family
    legend.fontSize = 8.5
    legend.colorNamePairs = list(zip(cols, names))
    d.add(legend)
    return _framed(d, palette)


def _options_chart(title: str, options: List[Dict], tpl: Dict, family: str, width: float):
    """Single-series bar chart of annual savings per opportunity, one colour per bar."""
    palette = tpl.get("palette") or {}
    cycle = [colors.HexColor(palette.get(k, d)) for k, d in
             (("savings", "#1E6C34"), ("after", "#ED7D31"), ("spend", "#2E74B5"))]
    values = [round(o["value"]) for o in options]
    names = [o["label"] for o in options]

    height = 236.0
    d = Drawing(width, height)
    d.add(String(width / 2, height - 24, title, fontName=family, fontSize=12,
                 fillColor=colors.HexColor(palette.get("ink", "#000000")), textAnchor="middle"))

    bc = VerticalBarChart()
    bc.x, bc.y = 58, 54
    bc.width, bc.height = width - 82, height - 106
    bc.data = [values]
    bc.categoryAxis.categoryNames = [""] * len(values)   # identified by the legend, as in the reference
    bc.categoryAxis.strokeColor = colors.HexColor("#BFBFBF")
    bc.valueAxis.valueMin = 0
    bc.valueAxis.labels.fontName = family
    bc.valueAxis.labels.fontSize = 8.5
    bc.valueAxis.labelTextFormat = lambda v: _money0(v)
    bc.valueAxis.strokeColor = colors.HexColor("#BFBFBF")
    bc.valueAxis.gridStrokeColor = colors.HexColor("#D9D9D9")
    bc.valueAxis.gridStrokeWidth = 0.4
    bc.valueAxis.visibleGrid = True
    bc.groupSpacing = 26
    bc.barWidth = 26
    for i in range(len(values)):
        bc.bars[(0, i)].fillColor = cycle[i % len(cycle)]
        bc.bars[(0, i)].strokeColor = None
    bc.barLabelFormat = lambda v: _money0(v)
    bc.barLabels.fontName = f"{family}-Bold"
    bc.barLabels.fontSize = 8
    bc.barLabels.dy = 6
    bc.barLabels.fillColor = colors.HexColor("#262626")
    d.add(bc)

    legend = Legend()
    legend.x, legend.y = 58, 22
    legend.deltax = min(150, max(90, (width - 90) / max(1, len(names))))
    legend.dxTextSpace = 4
    legend.columnMaximum = 1
    legend.alignment = "right"
    legend.fontName = family
    legend.fontSize = 8.5
    legend.colorNamePairs = [(cycle[i % len(cycle)], n) for i, n in enumerate(names)]
    d.add(legend)
    return _framed(d, palette)


# ── Document shell ──────────────────────────────────────────────────────────────────

class _TPTDoc(SimpleDocTemplate):
    """SimpleDocTemplate that notifies the TableOfContents of H1/H2/H3 headings."""

    def afterFlowable(self, flowable):
        if not isinstance(flowable, Paragraph):
            return
        name = flowable.style.name
        if name in ("TOCH1", "TOCH2", "TOCH3") and getattr(flowable, "_toc", False):
            level = int(name[-1]) - 1
            self.notify("TOCEntry", (level, flowable.getPlainText(), self.page))


def _cover(client: str, when: str):
    """Build the page-1 painter for the branded cover.

    The whole cover is drawn on the canvas rather than flowed as platypus content: it is a fixed
    composition of overlapping artwork (Azure mark bleeding off the bottom edge, the "$" hexagon
    sitting on top of it), which flowables cannot express. Page 1 therefore carries no story
    content. Positions are fractions of the page so the layout holds at any page size.
    """

    def draw(canvas, doc):
        canvas.saveState()
        w, h = A4

        # Backdrop: full-bleed artwork (authored at the A4 aspect, so no distortion), else flat dark.
        if _asset(_COVER_BG) is not None:
            canvas.drawImage(_asset(_COVER_BG), 0, 0, width=w, height=h,
                             preserveAspectRatio=False, mask="auto")
        else:
            canvas.setFillColor(colors.HexColor("#0B1622"))
            canvas.rect(0, 0, w, h, fill=1, stroke=0)
            canvas.setFillColor(colors.HexColor("#123047"))
            canvas.rect(0, 0, w, 2.4 * inch, fill=1, stroke=0)

        # Decorative art, back to front — the hexagon overlaps the Azure mark, which bleeds off-page.
        _place(canvas, _COVER_AZURE, x=0.055 * w, top=0.750 * h, width=0.385 * w)
        _place(canvas, _COVER_COIN, x=0.255 * w, top=0.700 * h, width=0.180 * w)
        _place(canvas, _COVER_ART, x=0.595 * w, top=0.440 * h, width=0.325 * w)

        # TPT lockup, top-left.
        _place(canvas, _COVER_LOGO, x=0.075 * w, top=0.070 * h, width=0.560 * w)

        family = _fonts((_template().get("fonts") or {}))
        canvas.setFillColor(colors.white)
        canvas.setFont(f"{family}-Bold", 32)
        canvas.drawString(0.150 * w, h - 0.256 * h, _template().get("meta", {}).get(
            "title", "Cost Assessment"))

        canvas.setStrokeColor(colors.white)
        canvas.setLineWidth(1.6)
        canvas.line(0.110 * w, h - 0.350 * h, 0.310 * w, h - 0.350 * h)

        canvas.setFont(f"{family}-Bold", 16)
        if when:
            canvas.drawString(0.110 * w, h - 0.397 * h, when)
        canvas.drawString(0.110 * w, h - 0.437 * h, f"Prepared for: {client}")

        canvas.restoreState()

    return draw


def _decorate(tpl: Dict):
    """Content-page painter: page border, header logo, footer rule + page number."""
    page_cfg = tpl.get("page") or {}
    border = page_cfg.get("border") or {}
    logo_cfg = page_cfg.get("header_logo") or {}
    footer = page_cfg.get("footer") or {}
    family = _fonts(tpl.get("fonts") or {})

    def draw(canvas, doc):
        canvas.saveState()
        w, h = A4

        inset = float(border.get("inset", 24))
        canvas.setStrokeColor(colors.HexColor(border.get("colour", "#000000")))
        canvas.setLineWidth(float(border.get("width", 1.0)))
        canvas.rect(inset, inset, w - 2 * inset, h - 2 * inset, fill=0, stroke=1)

        if logo_cfg.get("asset"):
            lw = float(logo_cfg.get("width", 122))
            _place(canvas, logo_cfg["asset"],
                   x=w - inset - float(logo_cfg.get("right", 30)) - lw,
                   top=inset + float(logo_cfg.get("top", 30)), width=lw)

        rule = footer.get("rule") or {}
        baseline = float(footer.get("baseline", 40))
        if rule:
            canvas.setStrokeColor(colors.HexColor(rule.get("colour", "#BFBFBF")))
            canvas.setLineWidth(float(rule.get("width", 0.6)))
            y = baseline + float(rule.get("above", 14))
            canvas.line(inset + 26, y, w - inset - 26, y)

        canvas.setFont(family, float(footer.get("size", 10.5)))
        canvas.setFillColor(colors.HexColor(footer.get("colour", "#595959")))
        label = str(footer.get("text", "{page} | P a g e")).format(page=doc.page)
        canvas.drawRightString(w - inset - 26, baseline, label)
        canvas.restoreState()

    return draw


# ── Block renderers ─────────────────────────────────────────────────────────────────

def _fmt(value, kind: Optional[str]) -> str:
    if value is None or value == "":
        return "—"
    if kind == "currency":
        return _usd(value)
    if kind == "currency0":
        return _usd(value, 0)
    return str(value)


def _text(raw: str, ctx: Dict) -> str:
    """Fill {placeholders} from the context, leaving unknown ones untouched."""
    class _Safe(dict):
        def __missing__(self, key):  # noqa: D105
            return "{" + key + "}"
    return str(raw).format_map(_Safe(ctx))


def _data_table(block: Dict, ctx: Dict, st: Dict, tpl: Dict, avail: float) -> Optional[Table]:
    rows = ctx.get(block.get("source")) or []
    if not rows:
        return None
    palette = tpl.get("palette") or {}
    tcfg = tpl.get("table") or {}
    cols = block.get("columns") or []
    fractions = block.get("widths") or [1.0 / len(cols)] * len(cols)
    widths = [avail * f for f in fractions]

    data = [[Paragraph(c["header"], st["cell_h"]) for c in cols]]
    for row in rows:
        data.append([Paragraph(_fmt(row.get(c["field"]), c.get("format")), st["cell"])
                     for c in cols])

    table = Table(data, colWidths=widths, repeatRows=1)
    pad = float(tcfg.get("row_padding", 6))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(palette.get("table_header_bg", "#2E74B5"))),
        ("GRID", (0, 0), (-1, -1), float(tcfg.get("grid_width", 0.75)),
         colors.HexColor(palette.get("table_grid", "#000000"))),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _environment_table(block: Dict, ctx: Dict, st: Dict, tpl: Dict, avail: float) -> Table:
    palette = tpl.get("palette") or {}
    rows = ctx.get("environment_rows") or []
    header = Paragraph(_text(block.get("title", "Environment Details"), ctx), st["cell_h"])
    centred = ParagraphStyle("env_value", parent=st["cell"], alignment=TA_CENTER)
    data = [[header, ""]]
    data += [[Paragraph(label, st["cell"]), Paragraph(value, centred)] for label, value in rows]

    table = Table(data, colWidths=[avail * 0.36, avail * 0.64])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(palette.get("table_header_bg", "#2E74B5"))),
        ("SPAN", (0, 0), (-1, 0)),
        ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor(palette.get("table_grid", "#000000"))),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _savings_panel(block: Dict, ctx: Dict, st: Dict, tpl: Dict, avail: float) -> Table:
    """Executive-summary hero: bordered panel, coin mark, label/value pairs."""
    palette = tpl.get("palette") or {}
    family = _fonts(tpl.get("fonts") or {})
    label_style = ParagraphStyle("panel_label", fontName=f"{family}-Bold", fontSize=16,
                                 textColor=colors.HexColor(palette.get("label", "#1F3864")),
                                 leading=20, spaceAfter=1)
    value_style = ParagraphStyle("panel_value", fontName=f"{family}-Bold", fontSize=27,
                                 textColor=colors.HexColor(palette.get("savings", "#1E6C34")),
                                 leading=32, spaceAfter=8)

    stack: List = []
    for row in block.get("rows") or []:
        stack.append(Paragraph(_text(row["label"], ctx), label_style))
        stack.append(Paragraph(_text(row["value"], ctx), value_style))

    mark = _asset_image(_COVER_COIN, width=78.0) or ""

    panel = Table([[mark, stack]], colWidths=[avail * 0.30, avail * 0.70])
    panel.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.9, colors.HexColor(palette.get("panel_border", "#9DC3E6"))),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(palette.get("panel_bg", "#FBFCFE"))),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 20),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 20),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
    ]))
    return panel


def _scenario_growth(scenario: Dict, measured: Optional[float]) -> Optional[float]:
    """Resolve a scenario's annual growth rate. `measured` is the environment's own trend (or None).

    A scenario tagged `growth_source: measured` uses that trend directly; `half_measured` uses half
    of it (the Conservative view). Returns None to SKIP the scenario — used when a trend-based chart
    has no measured growth to draw on (too little billing history), so we hide it rather than guess.
    Untagged scenarios keep their fixed `growth` (e.g. Fixed 0 %, the Civo 25 % hypothetical).
    """
    source = scenario.get("growth_source")
    if source == "measured":
        return measured
    if source == "half_measured":
        return round(measured / 2, 4) if measured is not None else None
    return float(scenario.get("growth", 0))


def _projection_blocks(block: Dict, ctx: Dict, st: Dict, tpl: Dict, avail: float) -> List:
    """One framed chart + caption per scenario, page-broken every `per_page`.

    Linear/Conservative growth come from the environment's actual spend trend
    (`ctx["_growth"]`); scenarios that need a trend but have none are skipped.
    """
    cfg = tpl.get("projections") or {}
    family = _fonts(tpl.get("fonts") or {})
    spend, savings = ctx["_spend"], ctx["_savings"]
    measured = ctx.get("_growth")

    # Spend-based projections are only meaningful when the numbers reconcile (savings ≤ measured spend).
    # On a partial/young billing window (new or recently-migrated subscription) the measured spend is a
    # fragment far below the run-rate savings, which would drive "ACR after savings" deeply negative — a
    # broken chart. Show an honest note instead of nonsensical bars (mirrors the dashboard's behaviour).
    if not spend or spend <= 0 or savings > spend:
        return [Paragraph(
            "Three-year spend projections aren't shown for this assessment. The identified savings exceed "
            "the measured spend for this scope, which means the billing window is partial — typically a "
            "new or recently-migrated subscription without a complete billing month. Re-run after a full "
            "billing month has elapsed for reliable spend and projection figures.",
            st["caption"])]

    base = ctx["_year"] + int(cfg.get("base_year_offset", 0))
    pattern = cfg.get("year_label", "{ordinal} Year ({start}-{end})")
    labels = [pattern.format(ordinal=_ordinal(i + 1), start=base + i, end=str(base + i + 1)[-2:])
              for i in range(3)]

    blocks: List = []
    for scenario in cfg.get("scenarios") or []:
        growth = _scenario_growth(scenario, measured)
        if growth is None:
            continue  # trend-based scenario with no measured growth → hide rather than guess
        acr, after, saved = _projection(spend, savings, growth)
        chart = _projection_chart(_text(scenario.get("title", ""), ctx), [acr, after, saved],
                                  labels, tpl, family, avail)
        caption = Paragraph(_text(scenario.get("caption", ""), ctx), st["caption"])
        blocks.append(KeepTogether([chart, Spacer(1, 8), caption]))

    per_page = int(block.get("per_page", 2))
    out: List = []
    for index, chart_block in enumerate(blocks):
        out.append(chart_block)
        if per_page and (index + 1) % per_page == 0 and index + 1 < len(blocks):
            out.append(PageBreak())
    return out


def _render(tpl: Dict, ctx: Dict, st: Dict, toc: TableOfContents, avail: float) -> List:
    """Walk the template's content blocks into a platypus story."""
    story: List = []
    for block in tpl.get("content") or []:
        kind = block.get("type")
        need = block.get("require")
        if need and not ctx.get(need):
            continue

        if kind == "page_break":
            story.append(PageBreak())

        elif kind == "toc":
            story.append(Paragraph(_text(block.get("title", "Table of Contents"), ctx),
                                   st["toc_h"]))
            story.append(toc)

        elif kind == "heading":
            level = int(block.get("level", 1))
            style = st.get(f"h{level}", st["h1"])
            number = block.get("number")
            label = f"{number} {block['text']}" if number else block["text"]
            para = Paragraph(_text(label, ctx), style)
            para._toc = bool(block.get("toc"))
            story.append(para)

        elif kind == "paragraph":
            style = st.get(block.get("style", "body"), st["body"])
            story.append(Paragraph(_text(block.get("text", ""), ctx), style))

        elif kind == "ordered_list":
            for i, item in enumerate(block.get("items") or [], 1):
                story.append(Paragraph(_text(item, ctx), st["listed"],
                                       bulletText=f"{i}."))

        elif kind == "bullet_list":
            for item in block.get("items") or []:
                story.append(Paragraph(_text(item, ctx), st["bullet"], bulletText="•"))

        elif kind == "savings_panel":
            story.append(_savings_panel(block, ctx, st, tpl, avail))
            story.append(Spacer(1, 12))

        elif kind == "projections":
            story.extend(_projection_blocks(block, ctx, st, tpl, avail))

        elif kind == "environment_table":
            # Kept whole — a single orphaned row reads as a rendering fault, not a page break.
            story.append(KeepTogether(_environment_table(block, ctx, st, tpl, avail)))
            story.append(Spacer(1, 10))

        elif kind == "savings_chart":
            options = ctx.get(block.get("source")) or []
            if options:
                family = _fonts(tpl.get("fonts") or {})
                story.append(_options_chart(_text(block.get("title", ""), ctx), options,
                                            tpl, family, avail))
                story.append(Spacer(1, 12))

        elif kind == "table":
            table = _data_table(block, ctx, st, tpl, avail)
            if table is not None:
                story.append(table)
                story.append(Spacer(1, 12))

        elif kind == "spacer":
            story.append(Spacer(1, float(block.get("height", 10))))

    return story


# ── PDF assembly ────────────────────────────────────────────────────────────────────

def generate_pdf(assessment, findings: List) -> bytes:
    _set_currency(getattr(assessment, "currency", None))
    tpl = _template()
    st = _styles(tpl)
    family = _fonts(tpl.get("fonts") or {})

    page_cfg = tpl.get("page") or {}
    margins = page_cfg.get("margins") or {}
    left = float(margins.get("left", 78))
    right = float(margins.get("right", 78))

    buf = io.BytesIO()
    doc = _TPTDoc(buf, pagesize=A4,
                  leftMargin=left, rightMargin=right,
                  topMargin=float(margins.get("top", 78)),
                  bottomMargin=float(margins.get("bottom", 66)),
                  title=tpl.get("meta", {}).get("title", "Cost Assessment"),
                  author=tpl.get("meta", {}).get("author", "TechPlus Talent"))
    avail = A4[0] - left - right

    active = [f for f in findings if not getattr(f, "dismissed", 0)]
    ctx = _context(assessment, active)
    when = assessment.created_at.strftime("%B %d, %Y") if assessment.created_at else ""

    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("toc1", fontName=family, fontSize=12, leading=24,
                       textColor=colors.HexColor("#000000")),
        ParagraphStyle("toc2", fontName=family, fontSize=12, leading=24, leftIndent=22,
                       textColor=colors.HexColor("#000000")),
        ParagraphStyle("toc3", fontName=family, fontSize=12, leading=24, leftIndent=44,
                       textColor=colors.HexColor("#000000")),
    ]
    toc.dotsMinLevel = 0   # dot leaders on every level, including the numbered top-level entries

    story = [Spacer(1, 1), PageBreak()]          # page 1 is painted on the canvas
    story += _render(tpl, ctx, st, toc, avail)

    doc.multiBuild(story, onFirstPage=_cover(ctx["client"], when),
                   onLaterPages=_decorate(tpl))
    return buf.getvalue()
