"""
export.py
=========

Builds a printable "weak spots" review PDF from a list of tracked
questions (normally your L2/L3 ones) -- grouped by subject, worst-first,
with the actual question snapshot (cropped straight from your source
PDF, formulas and all), your own notes, and a link back to the source
question.
"""

from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    HRFlowable,
    Image,
    KeepTogether,
    Table,
    TableStyle,
)

import fitz

LEVEL_INFO = {
    "L3": ("Didn't understand", "#c62828"),
    "L2": ("Forgot something", "#e65100"),
    "L1": ("Easy", "#2e7d32"),
}
LEVEL_SORT_ORDER = {"L3": 0, "L2": 1, "L1": 2, None: 3}

MAX_IMAGE_HEIGHT_MM = 200


class _SnapshotSource:
    """Keeps at most one fitz.Document open per source PDF while an
    export is running, instead of reopening a file for every question."""

    def __init__(self):
        self._docs = {}

    def get(self, filepath):
        if filepath not in self._docs:
            try:
                self._docs[filepath] = fitz.open(filepath)
            except Exception:
                self._docs[filepath] = None
        return self._docs[filepath]

    def render(self, filepath, page_num, y_top, y_bottom, dpi=200, pad=4):
        if not filepath or y_top is None or y_bottom is None:
            return None
        doc = self.get(filepath)
        if doc is None:
            return None
        try:
            page = doc[page_num - 1]
            rect = page.rect
            clip = fitz.Rect(
                rect.x0 + 2,
                max(rect.y0, y_top - pad),
                rect.x1 - 2,
                min(rect.y1, y_bottom + pad),
            )
            if clip.height <= 2 or clip.width <= 2:
                return None
            pix = page.get_pixmap(dpi=dpi, clip=clip)
            return pix.tobytes("png")
        except Exception:
            return None

    def close_all(self):
        for doc in self._docs.values():
            if doc is not None:
                doc.close()
        self._docs.clear()


def _image_flowable(png_bytes, max_width_pt):
    try:
        reader = ImageReader(BytesIO(png_bytes))
        iw, ih = reader.getSize()
    except Exception:
        return None
    if not iw or not ih:
        return None
    width = max_width_pt
    height = width * (ih / iw)
    max_height_pt = MAX_IMAGE_HEIGHT_MM * mm
    if height > max_height_pt:
        height = max_height_pt
        width = height * (iw / ih)
    return Image(BytesIO(png_bytes), width=width, height=height)


def _styles():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "SubjectHeading",
            parent=styles["Heading2"],
            spaceBefore=18,
            spaceAfter=6,
            textColor=colors.HexColor("#1a1a1a"),
        )
    )
    styles.add(
        ParagraphStyle(
            "QTitle",
            parent=styles["Normal"],
            fontSize=10.5,
            leading=14,
            spaceBefore=2,
        )
    )
    styles.add(
        ParagraphStyle(
            "QMeta",
            parent=styles["Normal"],
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#666666"),
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            "QNotes",
            parent=styles["Normal"],
            fontSize=9.5,
            leading=13,
            leftIndent=10,
            textColor=colors.HexColor("#333333"),
            spaceBefore=3,
            spaceAfter=2,
        )
    )
    return styles


def export_weak_spots_pdf(
    output_path,
    rows,
    heading="GATE Weak Spots Review",
    subheading=None,
    show_source=False,
    include_snapshots=True,
):
    """
    rows: list of dicts, each with:
        question_id, chapter_name, title, level, notes, url, page,
        source_filename, source_filepath, y_top, y_bottom
        (source_filepath/y_top/y_bottom are used to render the actual
        question snapshot image; if missing, that question just falls
        back to text-only.)

    Groups by chapter_name, sorts L3 before L2 before L1 within each
    group, and writes a clean printable PDF to output_path.
    """
    styles = _styles()
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=heading,
    )
    content_width = doc.width

    story = []

    story.append(Paragraph(escape(heading), styles["Title"]))
    generated = datetime.now().strftime("%d %B %Y, %I:%M %p")
    sub_lines = [f"Generated {generated}"]
    if subheading:
        sub_lines.append(subheading)
    story.append(Paragraph(" &middot; ".join(escape(s) for s in sub_lines), styles["QMeta"]))

    counts = {"L1": 0, "L2": 0, "L3": 0, "NONE": 0}
    for r in rows:
        counts[r.get("level") or "NONE"] += 1
    summary_bits = [f"{len(rows)} questions"]
    if counts["L3"]:
        summary_bits.append(f"L3: {counts['L3']}")
    if counts["L2"]:
        summary_bits.append(f"L2: {counts['L2']}")
    if counts["L1"]:
        summary_bits.append(f"L1: {counts['L1']}")
    if counts["NONE"]:
        summary_bits.append(f"Not attempted: {counts['NONE']}")
    summary = " &middot; ".join(summary_bits)
    story.append(Paragraph(summary, styles["QMeta"]))
    story.append(Spacer(1, 10))

    if not rows:
        story.append(Paragraph("Nothing to show for this filter.", styles["Normal"]))
        doc.build(story)
        return

    snapshots = _SnapshotSource() if include_snapshots else None

    try:
        # group by subject
        by_subject = {}
        for r in rows:
            by_subject.setdefault(r.get("chapter_name") or "Uncategorised", []).append(r)

        for subject in sorted(by_subject.keys()):
            items = by_subject[subject]
            items.sort(
                key=lambda r: (
                    LEVEL_SORT_ORDER.get(r.get("level"), 3),
                    tuple(int(p) for p in r["question_id"].split(".")),
                )
            )

            story.append(Paragraph(escape(subject), styles["SubjectHeading"]))
            story.append(HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#cccccc")))
            story.append(Spacer(1, 8))

            card_padding = 10
            card_inner_width = content_width - 2 * card_padding

            for r in items:
                level = r.get("level")
                label, hexcolor = LEVEL_INFO.get(level, ("Not attempted", "#666666"))

                # Each row of this list becomes one row of a single-column
                # table -- keeping cells to one simple flowable each (never
                # an Image nested inside a KeepTogether inside a cell, which
                # reportlab's table layout can't size correctly) is what
                # makes the bordered card reliable.
                card_rows = []

                header_bits = [
                    f'<font color="{hexcolor}"><b>{escape(level or "?")} &middot; {escape(label)}</b></font>',
                    f"<b>{escape(r['question_id'])}</b>",
                ]
                card_rows.append([Paragraph(
                    "&nbsp;&nbsp;&middot;&nbsp;&nbsp;".join(header_bits), styles["QMeta"]
                )])

                title_text = escape(r.get("title") or "")
                card_rows.append([Paragraph(title_text, styles["QTitle"])])

                img_flowable = None
                if snapshots is not None:
                    png_bytes = snapshots.render(
                        r.get("source_filepath"), r.get("page"), r.get("y_top"), r.get("y_bottom")
                    )
                    if png_bytes:
                        img_flowable = _image_flowable(png_bytes, card_inner_width)
                if img_flowable is not None:
                    card_rows.append([img_flowable])

                meta_bits = []
                if r.get("page"):
                    meta_bits.append(f"PDF page {r['page']}")
                if show_source and r.get("source_filename"):
                    meta_bits.append(escape(r["source_filename"]))
                if r.get("url"):
                    meta_bits.append(f'<link href="{escape(r["url"])}" color="#1155cc">Open on GATE Overflow</link>')
                if meta_bits:
                    card_rows.append([Paragraph(" &middot; ".join(meta_bits), styles["QMeta"])])

                notes = (r.get("notes") or "").strip()
                if notes:
                    card_rows.append([Paragraph(f"<i>Notes:</i> {escape(notes)}", styles["QNotes"])])

                # Per-row top padding gives generous breathing room between
                # the image and whatever comes after it, and between the
                # meta line and the notes -- without needing Spacers (which
                # don't play well inside table cells).
                n_rows = len(card_rows)
                row_top_padding = [6] * n_rows
                row_top_padding[0] = card_padding
                if img_flowable is not None:
                    img_row = 2  # header, title, image
                    if img_row < n_rows:
                        row_top_padding[img_row] = 12
                    if img_row + 1 < n_rows:
                        row_top_padding[img_row + 1] = 14
                if notes:
                    row_top_padding[-1] = 12

                card = Table(card_rows, colWidths=[card_inner_width])
                style_cmds = [
                    ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#c9c9c9")),
                    ("LEFTPADDING", (0, 0), (-1, -1), card_padding),
                    ("RIGHTPADDING", (0, 0), (-1, -1), card_padding),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, n_rows - 1), (0, n_rows - 1), card_padding),
                ]
                for i, pad in enumerate(row_top_padding):
                    style_cmds.append(("TOPPADDING", (0, i), (0, i), pad))
                card.setStyle(TableStyle(style_cmds))

                story.append(KeepTogether([card]))
                story.append(Spacer(1, 16))

        doc.build(story)
    finally:
        if snapshots is not None:
            snapshots.close_all()
