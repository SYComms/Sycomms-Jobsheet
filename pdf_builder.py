"""
Builds the A4 job sheet PDF (customer copy and office copy).

The office copy never shows Wi-Fi passwords. The customer copy shows them
only when the engineer ticks "Include Wi-Fi passwords on the customer copy".
"""
from __future__ import annotations

import base64
import io
from html import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

BRAND = colors.HexColor("#1f1450")
BRAND_2 = colors.HexColor("#2d1f6e")
ACCENT = colors.HexColor("#00b5a3")
INK = colors.HexColor("#1d2333")
MUTED = colors.HexColor("#6b7389")
LINE = colors.HexColor("#d9dce6")
SOFT = colors.HexColor("#f4f5fa")

S_LABEL = ParagraphStyle("lab", fontName="Helvetica-Bold", fontSize=7.4, textColor=MUTED, leading=9)
S_VAL = ParagraphStyle("val", fontName="Helvetica", fontSize=9, textColor=INK, leading=11.5)
S_SEC = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=10, textColor=colors.white, leading=12)
S_SMALL = ParagraphStyle("sm", fontName="Helvetica", fontSize=7.5, textColor=MUTED, leading=9.5)
S_H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=17, textColor=colors.white, leading=20)
S_H2 = ParagraphStyle("h2", fontName="Helvetica", fontSize=8.5, textColor=colors.HexColor("#c9c4ee"), leading=11)
S_HR = ParagraphStyle("hr", parent=S_H2, alignment=TA_RIGHT, textColor=colors.white, fontSize=9)

W = A4[0] - 30 * mm  # usable width


def _p(text, style=S_VAL):
    t = escape(str(text if text not in (None, "") else "—")).replace("\n", "<br/>")
    return Paragraph(t, style)


def _join(v):
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x) or "—"
    return v if v not in (None, "") else "—"


def _cell(label, value):
    return [_p(label.upper(), S_LABEL), _p(_join(value))]


def _grid(pairs, cols=2):
    """pairs: list of (label, value). Lays them out cols-wide."""
    rows, row = [], []
    for lab, val in pairs:
        row.append(_cell(lab, val))
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        row += [[Paragraph("", S_LABEL), Paragraph("", S_SMALL)]] * (cols - len(row))
        rows.append(row)
    t = Table(rows, colWidths=[W / cols] * cols)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _section(num, title):
    t = Table([[_p(f"{num}.  {title.upper()}", S_SEC)]], colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_2),
        ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _sig_image(data_url, w=62 * mm, h=22 * mm):
    if not data_url or "," not in data_url:
        return _p("Not signed", S_SMALL)
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
        ir = ImageReader(io.BytesIO(raw))
        iw, ih = ir.getSize()
        scale = min(w / iw, h / ih)
        return Image(io.BytesIO(raw), width=iw * scale, height=ih * scale)
    except Exception:
        return _p("Signature unreadable", S_SMALL)


def _thanks_card(brand):
    """Branded thank-you panel: support contact + Google review link and QR code."""
    support = brand.get("support_email") or "support@sycomms.co.uk"
    review = brand.get("review_url") or ""
    h = ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=13, textColor=colors.white, leading=16)
    b = ParagraphStyle("tb", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#d9d5f5"), leading=13)
    lk = ParagraphStyle("tl", parent=b, textColor=ACCENT, fontName="Helvetica-Bold")
    sup = escape(support)
    text = [Paragraph(f"Thank you for choosing {escape(brand['name'])}", h), Spacer(1, 5),
            Paragraph("We really appreciate your business. For anything you need from here on — questions, "
                      "changes or support — please get in touch with our support team:", b), Spacer(1, 4),
            Paragraph(f'<link href="mailto:{sup}">{sup}</link>' + (
                f'&nbsp;&nbsp;<font color="#d9d5f5">|</font>&nbsp;&nbsp;'
                f'<link href="tel:{escape(brand["phone"].replace(" ", ""))}">{escape(brand["phone"])}</link>'
                if brand.get("phone") else ""), lk)]
    if review:
        text += [Spacer(1, 8), Paragraph("Happy with the service? A quick Google review really helps us:", b), Spacer(1, 3),
                 Paragraph(f'<link href="{escape(review)}">Leave us a Google review &#187;</link>', lk)]
    cells, widths = [text], [W]
    if review:
        try:
            from reportlab.graphics.barcode import qr
            from reportlab.graphics.shapes import Drawing
            q = qr.QrCodeWidget(review, barFillColor=BRAND)
            x1, y1, x2, y2 = q.getBounds()
            size = 26 * mm
            dr = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
            dr.add(q)
            qcell = Table([[dr], [Paragraph("Scan to review", ParagraphStyle(
                "qc", fontName="Helvetica", fontSize=7, textColor=MUTED, alignment=1))]], colWidths=[30 * mm])
            qcell.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.white), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                       ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
            cells, widths = [text, qcell], [W - 36 * mm, 36 * mm]
        except Exception:
            pass
    t = Table([cells], colWidths=widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND),
        ("LINEBEFORE", (0, 0), (0, -1), 4, ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (-1, 0), (-1, 0), "CENTER" if len(cells) > 1 else "LEFT"),
        ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (0, -1), 16), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    return KeepTogether([t])


def build_pdf(F: dict, brand: dict, copy: str = "customer", photos=None) -> bytes:
    photos = photos or []
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=16 * mm,
                            title=f"Job sheet {F.get('job_no', '')}", author=brand["name"])
    story = []
    vt = F.get("visit_type") or "Engineer visit"
    is_install = vt.lower().startswith("new install")
    heading = "Installation Job Sheet" if is_install else "Engineer Visit Report"

    # ---- header band
    left = [_p(brand["name"].upper(), ParagraphStyle("b", parent=S_H2, textColor=ACCENT,
                                                    fontName="Helvetica-Bold", fontSize=9)),
            _p(heading, S_H1),
            _p("BUSINESS PHONES  •  BROADBAND  •  ROUTER & WI-FI", S_H2)]
    right = [_p(f"Job / Order No.  {F.get('job_no') or '—'}", S_HR),
             _p(f"Visit date  {F.get('visit_date_display') or '—'}", S_HR),
             _p(f"{vt}", S_HR),
             _p("CUSTOMER COPY" if copy == "customer" else "OFFICE COPY",
                ParagraphStyle("cp", parent=S_HR, textColor=ACCENT, fontName="Helvetica-Bold"))]
    hdr = Table([[left, right]], colWidths=[W * 0.62, W * 0.38])
    hdr.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story += [hdr, Spacer(1, 8)]

    n = 1
    # ---- 1 customer
    story += [_section(n, "Customer & visit details"), _grid([
        ("Customer / business name", F.get("customer_name")),
        ("Job / order no.", F.get("job_no")),
        ("Account no.", F.get("account_number")),
        ("Site contact", F.get("contact_name")),
        ("Installation address", F.get("address")),
        ("Postcode", F.get("postcode")),
        ("Contact number", F.get("contact_phone")),
        ("Email address", F.get("contact_email")),
        ("Visit date", F.get("visit_date_display")),
        ("Engineer / installer", F.get("engineer")),
        ("Arrival / finish time", f"{F.get('arrival') or '—'}  –  {F.get('finish') or '—'}"),
    ]), Spacer(1, 8)]
    n += 1

    # ---- 2 work carried out / fault
    pairs = [("Visit type", vt)]
    if F.get("show_fault"):
        pairs += [("Fault category", F.get("fault_category")),
                  ("Cause found", F.get("root_cause")),
                  ("Outcome", F.get("fault_outcome")),
                  ("Parts / equipment used", F.get("parts_used"))]
    pairs += [("Billing", F.get("billable"))]
    block = [_section(n, "Work carried out")]
    if F.get("show_fault"):
        block += [_grid([("Fault reported", F.get("fault_reported"))], cols=1)]
    block += [_grid(pairs), _grid([("Work carried out / engineer notes", F.get("work_done"))], cols=1)]
    story += block + [Spacer(1, 8)]
    n += 1

    covered = F.get("covered") or []

    # ---- 3 phones
    if "Phone system" in covered:
        types = list(F.get("handset_types") or [])
        if F.get("handset_other"):
            types.append(F["handset_other"])
        feats = list(F.get("features") or [])
        if F.get("features_other"):
            feats.append(F["features_other"])
        story += [_section(n, "Phone system & handsets"), _grid([
            ("Phone system / platform", F.get("platform")),
            ("Handset make / model", F.get("handset_models")),
            ("Handsets required", F.get("handsets_required")),
            ("Handsets installed", F.get("handsets_installed")),
            ("Handset type(s)", types),
            ("Software included?", F.get("software_included")),
            ("Software / application", F.get("software_name") if F.get("software_included") == "Yes" else "—"),
            ("Licence / seat count", F.get("licence_count") if F.get("software_included") == "Yes" else "—"),
            ("Number of users", F.get("users_number")),
            ("Users configured", F.get("users_configured")),
            ("Extensions / DDI range", F.get("ddi_range")),
            ("Features configured", feats),
        ]), _grid([("Equipment serials / MAC addresses", F.get("serials"))], cols=1), Spacer(1, 8)]
        n += 1

    # ---- 4 broadband
    if "Broadband & network" in covered:
        def pw(v):
            if not v:
                return "—"
            if copy == "customer" and F.get("include_pw_customer"):
                return v
            return "•••••••• (not shown on this copy)"
        lines = F.get("bb_lines") or [F]
        for L in lines:
            bb = L.get("bb_type")
            if bb == "Other" and L.get("bb_type_other"):
                bb = L["bb_type_other"]
            title = "Broadband & network"
            if len(lines) > 1:
                title += f" — line {L.get('line_no')}" + (f" ({L['bb_site']})" if L.get("bb_site") else "")
            story += [_section(n, title), _grid([
                ("Broadband type", bb),
                ("Provider / package", " – ".join(x for x in [L.get("provider"), L.get("package")] if x)),
                ("Expected speed (down / up)", f"{L.get('exp_down') or '—'} / {L.get('exp_up') or '—'} Mbps"),
                ("Measured speed (down / up)", f"{L.get('meas_down') or '—'} / {L.get('meas_up') or '—'} Mbps"),
                ("Router make / model", L.get("router")),
                ("Router serial number", L.get("router_serial")),
                ("WAN / ONT details", L.get("wan_details")),
                ("Wi-Fi SSID", L.get("ssid")),
                ("Wi-Fi password", pw(L.get("wifi_pw"))),
                ("Wi-Fi band(s)", L.get("wifi_bands")),
                ("Guest Wi-Fi enabled?", L.get("guest_wifi")),
                ("Guest SSID / password",
                 f"{L.get('guest_ssid') or '—'} / {pw(L.get('guest_pw'))}" if L.get("guest_wifi") == "Yes" else "—"),
                ("Network checks", L.get("network_checks")),
                ("Coverage / signal checked", L.get("coverage_checked")),
                ("Password handed over securely", L.get("pw_handover")),
            ]), Spacer(1, 8)]
        n += 1

    # ---- testing & sign-off
    sign_rows = []
    if F.get("cust_unavailable"):
        cust_sig = _p(f"Customer not available to sign — {F.get('unavailable_reason') or 'no reason given'}", S_SMALL)
    else:
        cust_sig = _sig_image(F.get("sig_customer"))
    sign_rows.append([_p("CUSTOMER NAME", S_LABEL), _p(F.get("cust_sign_name")),
                      _p("CUSTOMER SIGNATURE", S_LABEL), cust_sig])
    sign_rows.append([_p("ENGINEER", S_LABEL), _p(F.get("engineer")),
                      _p("ENGINEER SIGNATURE", S_LABEL), _sig_image(F.get("sig_engineer"))])
    sign_rows.append([_p("DATE", S_LABEL), _p(F.get("visit_date_display")), Paragraph("", S_LABEL), Paragraph("", S_SMALL)])
    st_ = Table(sign_rows, colWidths=[W * 0.17, W * 0.28, W * 0.2, W * 0.35])
    st_.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("BACKGROUND", (3, 0), (3, 1), SOFT),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    calls = list(F.get("calls_tested") or [])
    test_pairs = []
    if "Phone system" in covered:
        test_pairs.append(("Phone calls tested", calls))
    if "Broadband & network" in covered:
        test_pairs.append(("Broadband / Wi-Fi tested", F.get("bb_tested")))
    test_pairs += [("Customer shown how to use system", F.get("shown_system")),
                   ("Follow-up visit needed?", F.get("followup_needed"))]
    story.append(KeepTogether([
        _section(n, "Testing, handover & customer sign-off"),
        _grid(test_pairs),
        _grid([("Outstanding items / issues", F.get("outstanding"))], cols=1),
        Spacer(1, 4), st_,
    ]))

    # ---- photos
    if photos:
        story += [Spacer(1, 10), _section(n + 1, "Site photos")]
        cells, row = [], []
        cw = W / 2 - 6
        for name, data in photos[:6]:
            ir = ImageReader(io.BytesIO(data))
            iw, ih = ir.getSize()
            scale = min(cw / iw, 70 * mm / ih)
            row.append([Image(io.BytesIO(data), width=iw * scale, height=ih * scale), _p(name, S_SMALL)])
            if len(row) == 2:
                cells.append(row)
                row = []
        if row:
            row.append("")
            cells.append(row)
        pt = Table(cells, colWidths=[W / 2] * 2)
        pt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6)]))
        story.append(pt)

    # ---- thank-you card (customer copy only)
    if copy == "customer":
        story += [Spacer(1, 12), _thanks_card(brand)]

    def footer(canv, _doc):
        canv.saveState()
        canv.setFont("Helvetica", 7)
        canv.setFillColor(MUTED)
        canv.drawString(15 * mm, 9 * mm,
                        f"{brand['name']}  |  {brand.get('phone', '')}  |  {brand.get('support_email') or brand.get('email', '')}  |  "
                        "Installation record — store Wi-Fi credentials securely")
        canv.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {_doc.page}")
        canv.setStrokeColor(ACCENT)
        canv.setLineWidth(1.2)
        canv.line(15 * mm, 12 * mm, A4[0] - 15 * mm, 12 * mm)
        canv.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
