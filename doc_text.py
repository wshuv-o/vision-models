"""Turn any supported document into labelled chunks of text.

Everything downstream works on the same shape -- a list of

    (label, text)

where the label says where the text came from ("p14", "Sheet1", "para 30-45")
so an extracted figure can always be traced back to a place in a file.

OCR is deliberately not attempted here. A file whose words are only pixels
returns nothing and says so; turning those pixels into text is a separate,
explicit step, because it is slow and it guesses.
"""
import io
import os
import re

import field_retrieve as F
import pdf_pages

TEXT_EXTS = {".txt", ".md", ".markdown", ".html", ".htm", ".json", ".xml",
             ".csv", ".tsv"}
DOC_EXTS = {".docx", ".pptx"}
SHEET_EXTS = {".xlsx", ".xls", ".xlsm"}
PIXEL_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}

# Roughly a page of prose. Word files have no pages until they are rendered,
# so they get cut into comparable pieces to keep scoring and budgeting even.
DOC_CHUNK_CHARS = 2500


class NeedsOCR(Exception):
    """The words in this file are pixels. Reading it requires an explicit OCR run."""


def _chunk(paragraphs, size=DOC_CHUNK_CHARS, prefix="part"):
    out, buf, start = [], [], 1
    for i, para in enumerate(paragraphs, 1):
        buf.append(para)
        if sum(len(x) for x in buf) >= size:
            out.append((f"{prefix} {start}-{i}", "\n".join(buf)))
            buf, start = [], i + 1
    if buf:
        out.append((f"{prefix} {start}-{len(paragraphs)}", "\n".join(buf)))
    return out


def read_pdf(path):
    pages = F.page_texts(path)
    if not F.text_layer_ok(pages):
        raise NeedsOCR(f"{os.path.basename(path)}: no usable text layer")
    return [(f"p{n}", t) for n, t in pages if t.strip()]


def read_docx(path):
    import docx

    d = docx.Document(str(path))
    parts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    # Tables carry the facts in loan and finance documents far more often than
    # the prose does, so they are kept rather than skipped.
    for ti, table in enumerate(d.tables, 1):
        rows = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            # a cell spanning columns repeats, same as read_html
            dedup = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
            if any(dedup):
                rows.append(" | ".join(dedup))
        if rows:
            parts.append(f"[table {ti}]\n" + "\n".join(rows))
    if not parts:
        raise NeedsOCR(f"{os.path.basename(path)}: no text in document")
    return _chunk(parts)


def read_pptx(path):
    from pptx import Presentation

    prs = Presentation(str(path))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        bits = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                bits.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    bits.append(" | ".join(c.text.strip() for c in row.cells))
        if bits:
            out.append((f"slide {i}", "\n".join(bits)))
    if not out:
        raise NeedsOCR(f"{os.path.basename(path)}: no text in slides")
    return out


def read_sheet(path):
    import pandas as pd

    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
    out = []
    for name, df in sheets.items():
        df = df.fillna("")
        rows = []
        for row in df.itertuples(index=False):
            cells = [str(c).strip() for c in row]
            dedup = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
            line = "\t".join(dedup).rstrip()
            if line.strip():
                rows.append(line)
        if rows:
            out.append((str(name), "\n".join(rows)))
    return out


def read_plain(path):
    raw = io.open(path, encoding="utf-8", errors="replace").read()
    if path.lower().endswith((".html", ".htm")):
        raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw,
                     flags=re.S | re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
    paras = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    return _chunk(paras) if paras else []


def read(path):
    """[(label, text)] for one file, or raise NeedsOCR.

    Raises NeedsOCR rather than returning empty so a caller cannot mistake
    "this file needs a different tool" for "this file has nothing in it".
    """
    ext = os.path.splitext(str(path))[1].lower()
    if ext == ".pdf":
        return read_pdf(path)
    if ext == ".docx":
        return read_docx(path)
    if ext == ".pptx":
        return read_pptx(path)
    if ext in SHEET_EXTS:
        return read_sheet(path)
    if ext in TEXT_EXTS:
        return read_plain(path)
    if ext in PIXEL_EXTS:
        raise NeedsOCR(f"{os.path.basename(path)}: image, words are pixels")
    raise ValueError(f"no reader for {ext or 'file with no extension'}")


def can_read(path):
    ext = os.path.splitext(str(path))[1].lower()
    return ext == ".pdf" or ext in DOC_EXTS or ext in SHEET_EXTS or ext in TEXT_EXTS
