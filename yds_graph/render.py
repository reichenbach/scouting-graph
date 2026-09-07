"""One page per pitcher: the facts table, the approved notes, the footer.

The footer is not decoration. A page without a version stamp and a data
source line cannot be checked later, and a report nobody can check is a
report nobody should act on.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .facts import facts_for


def _footer_text(facts_sheet: dict) -> str:
    prov = facts_sheet["provenance"]
    date_range = " to ".join(prov.get("date_range", [])) or "unknown dates"
    return (
        f"{facts_sheet['version']} | facts {facts_sheet['schema']} | gates {facts_sheet['rules_version']} | "
        f"source {prov['source_file']} sha256 {prov['sha256'][:12]} | "
        f"{prov['rows']} pitches over {prov.get('games', 0)} games, {date_range} | "
        "numbers computed in code, prose reviewed by a person before delivery"
    )


def render_report(facts_sheet: dict, notes: dict[str, str], out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("YdsTitle", parent=styles["Heading1"], fontSize=16, spaceAfter=6)
    sub_style = ParagraphStyle("YdsSub", parent=styles["Normal"], fontSize=9, textColor=colors.grey)
    body_style = ParagraphStyle("YdsBody", parent=styles["Normal"], fontSize=10, leading=14)
    head_style = ParagraphStyle("YdsHead", parent=styles["Heading2"], fontSize=12, spaceBefore=10)

    footer = _footer_text(facts_sheet)

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(colors.grey)
        text = footer
        max_chars = 165
        chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
        y = 0.45 * inch
        for chunk in reversed(chunks):
            canvas.drawString(0.75 * inch, y, chunk)
            y += 8
        canvas.restoreState()

    doc = BaseDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.9 * inch,
        title="Pitch tracking report",
        # Uncompressed on purpose: the version stamp and provenance line stay
        # greppable inside the delivered file, so an old report can be checked
        # without this program.
        pageCompression=0,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=draw_footer)])

    story = []
    pitchers = sorted(facts_sheet["pitchers"])
    for index, pitcher in enumerate(pitchers):
        if index:
            story.append(PageBreak())
        meta = facts_sheet["pitchers"][pitcher]
        story.append(Paragraph(f"Pitcher report: {pitcher}", title_style))
        story.append(
            Paragraph(
                f"{meta['total_pitches']} pitches in this sample. "
                f"Throws {meta.get('hand') or 'unrecorded'}.",
                sub_style,
            )
        )
        story.append(Spacer(1, 10))

        story.append(Paragraph("Notes", head_style))
        story.append(Paragraph(notes.get(pitcher, ""), body_style))
        story.append(Spacer(1, 10))

        story.append(Paragraph("Facts this page is built from", head_style))
        data = [["id", "measure", "value", "n"]]
        for fact in facts_for(facts_sheet, pitcher):
            unit = "%" if fact["unit"] == "percent" else f" {fact['unit']}"
            data.append([fact["id"], fact["label"], f"{fact['value']}{unit}", str(fact["n"])])
        table = Table(data, colWidths=[0.5 * inch, 3.7 * inch, 1.1 * inch, 0.6 * inch], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("LEADING", (0, 0), (-1, -1), 9),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (2, 1), (3, -1), "RIGHT"),
                ]
            )
        )
        story.append(table)

    doc.build(story)
    return out_path
