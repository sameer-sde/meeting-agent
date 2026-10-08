"""A Word (.docx) copy of the Minutes of Meeting, built by hand so no extra package is needed.

A .docx file is a zip of a few XML files. Only what the minutes use is written here: headings,
paragraphs with bold parts, two levels of bullet points and tables with borders.
"""
import io
import re
import zipfile
from xml.sax.saxutils import escape

INK, MUTED, ACCENT, HEAD_FILL = "1C1B18", "6B6760", "B5562A", "F5F2EC"
PAGE_WIDTH = 11906 - 2 * 1134       # A4 width minus the side margins, in Word's units


def _runs(text, bold=False, size=None, color=None, italic=False):
    """Text with **bold** parts, as Word runs."""
    out = []
    for i, part in enumerate(re.split(r"\*\*", text or "")):
        if not part:
            continue
        props = []
        if bold or i % 2:
            props.append("<w:b/>")
        if italic:
            props.append("<w:i/>")
        if color:
            props.append(f'<w:color w:val="{color}"/>')
        if size:
            props.append(f'<w:sz w:val="{size}"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def _p(inner, before=0, after=120, left=0, hanging=0, border=False):
    ind = f'<w:ind w:left="{left}" w:hanging="{hanging}"/>' if left else ""
    line = ('<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="4" w:color="E2DED6"/></w:pBdr>'
            if border else "")
    return f'<w:p><w:pPr>{line}<w:spacing w:before="{before}" w:after="{after}"/>{ind}</w:pPr>{inner}</w:p>'


def _heading(text):
    return _p(_runs(text, size=30, color=INK), before=320, after=120, border=True)


def _bullet(text, level):
    left = 360 if level == 1 else 900
    mark = "•" if level == 1 else "◦"
    return _p(_runs(f"{mark}\t") + _runs(text), before=80 if level == 1 else 0, after=60,
              left=left, hanging=280)


def _table(rows):
    """rows: list of lists of cell text; the first row is the header."""
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    # columns as wide as their longest text, so short columns don't squeeze long ones
    need = [max(min(len(re.sub(r"\*\*", "", r[c] if c < len(r) else "")), 60) for r in rows) + 4 for c in range(cols)]
    widths = [int(PAGE_WIDTH * n / sum(need)) for n in need]
    grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>"
    borders = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BFB9AE"/>'
                      for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="{PAGE_WIDTH}" w:type="dxa"/><w:tblLayout w:type="fixed"/><w:tblBorders>{borders}</w:tblBorders>'
           '<w:tblCellMar><w:top w:w="60" w:type="dxa"/><w:left w:w="100" w:type="dxa"/>'
           '<w:bottom w:w="60" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr>' + grid]
    for n, row in enumerate(rows):
        cells = []
        for c in range(cols):
            text = row[c] if c < len(row) else ""
            shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{HEAD_FILL}"/>' if n == 0 else ""
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[c]}" w:type="dxa"/>{shade}</w:tcPr>'
                         f'<w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>'
                         f"{_runs(text, bold=(n == 0), size=20)}</w:p></w:tc>")
        header = "<w:trPr><w:tblHeader/></w:trPr>" if n == 0 else ""
        out.append(f"<w:tr>{header}{''.join(cells)}</w:tr>")
    out.append("</w:tbl>")
    return "".join(out) + _p("", after=0)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _markdown(md):
    """The minutes' markdown as Word paragraphs."""
    out, table = [], []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("|"):
            if not re.fullmatch(r"\|?[\s:|-]+\|?", line.strip()):      # skip the |---|---| line
                table.append(_cells(line))
            continue
        if table:
            out.append(_table(table))
            table = []
        if not line.strip():
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            out.append(_heading(m[2]))
            continue
        m = re.match(r"^(\s*)(?:[-*•]|\d+[.)])\s+(.*)", line)
        if m:
            out.append(_bullet(m[2], 2 if len(m[1].expandtabs(4)) >= 2 else 1))
            continue
        out.append(_p(_runs(line.strip())))
    if table:
        out.append(_table(table))
    return "".join(out)


def make_docx(title, facts, people, mom_md):
    """facts: [(label, value)]; people: attendance rows with name, designation, email, status, pct."""
    body = [_p(_runs("Minutes of Meeting", bold=True, size=20, color=ACCENT), after=40),
            _p(_runs(title, size=44, color=INK), after=200, border=True)]
    body.append(_table([[label for label, _ in facts], [value for _, value in facts]]) if facts else "")
    body.append(_heading("Attendees"))
    if people:
        body.append(_table([["Name", "Designation", "Email", "Status", "Participation"]] +
                           [[p["name"], p.get("designation") or "—", p.get("email") or "—", p["status"],
                             f"{p.get('pct', 0)}%"] for p in people]))
    else:
        body.append(_p(_runs("Attendance was not recorded.", color=MUTED)))
    body.append(_p(_runs("Generated by AI. Be sure to check for accuracy.", italic=True, size=20, color=MUTED),
                   before=240))
    body.append(_markdown(mom_md))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(body)}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" '
                'w:footer="567" w:gutter="0"/></w:sectPr></w:body></w:document>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" '
              f'w:cs="Calibri" w:eastAsia="Calibri"/><w:color w:val="{INK}"/><w:sz w:val="22"/>'
              '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:line="276" w:lineRule="auto"/>'
              '</w:pPr></w:pPrDefault></w:docDefaults></w:styles>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
             '<Override PartName="/word/styles.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/styles" Target="styles.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles)
    return buf.getvalue()
