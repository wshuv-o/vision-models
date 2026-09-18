"""One token format for every document, however the words were obtained.

    {page, text, x0, x1, top, bottom, source: "text"|"ocr", pass: int}

Everything downstream -- row grouping, column binning, parsing, validation --
works on this and nothing else. A born-digital PDF and a scanned one that went
through OCR become the same shape here, so a parser written for one works on
the other without knowing which it got.

Nothing in this module reads a value for meaning. It reports where the marks
are on the page; deciding what they mean is the parser's job, and checking it
is the validator's.
"""
from __future__ import annotations

import os
import re


def from_pdf(path, pages=None):
    """Tokens from a PDF's own text layer, via pdfplumber.

    Returns [] when the file has no text layer -- that is a fact about the
    file, not an error, and the caller decides whether to OCR it.
    """
    import pdfplumber

    out = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            if pages and pno not in pages:
                continue
            try:
                words = page.extract_words(use_text_flow=False,
                                           keep_blank_chars=False)
            except Exception:
                continue
            for w in words:
                out.append({
                    "page": pno,
                    "text": w["text"],
                    "x0": round(float(w["x0"]), 1),
                    "x1": round(float(w["x1"]), 1),
                    "top": round(float(w["top"]), 1),
                    "bottom": round(float(w["bottom"]), 1),
                    "source": "text",
                    "pass": 0,
                })
    return out


def from_ocr_json(words, page=1, pass_no=1, scale=1.0):
    """Tokens from the OCR word list ({t,x,y,w,h}) used by the OCR scripts.

    `scale` converts render pixels back to PDF points, so OCR coordinates and
    text-layer coordinates are comparable on the same page.
    """
    out = []
    for w in words:
        x, y = float(w["x"]) / scale, float(w["y"]) / scale
        ww, hh = float(w["w"]) / scale, float(w["h"]) / scale
        out.append({
            "page": page,
            "text": w["t"],
            "x0": round(x, 1),
            "x1": round(x + ww, 1),
            "top": round(y, 1),
            "bottom": round(y + hh, 1),
            "source": "ocr",
            "pass": pass_no,
        })
    return out


def has_text_layer(path, sample_pages=6, min_chars=200, min_legible=0.75):
    """(bool, note). False when the words are pixels, or are mojibake.

    A broken ToUnicode CMap yields plenty of characters that are not words --
    one tenancy schedule here extracts as a run of punctuation and quote marks
    with the odd letter in it. That passes a naive length check and then
    poisons everything downstream, so legibility is measured, not assumed.
    """
    try:
        toks = from_pdf(path, pages=set(range(1, sample_pages + 1)))
    except Exception as e:
        return False, f"unreadable: {type(e).__name__}: {e}"
    if not toks:
        return False, "no text layer - the words are pixels"

    joined = " ".join(t["text"] for t in toks)
    if len(joined) < min_chars:
        return False, f"only {len(joined)} characters of text - likely scanned"

    # Proportion of characters that belong in a business document.
    good = sum(ch.isalnum() or ch.isspace() or ch in ".,$%()-/:;&'#" for ch in joined)
    ratio = good / max(len(joined), 1)
    if ratio < min_legible:
        return False, (f"text layer is mojibake ({ratio:.0%} legible) - the font "
                       f"has no usable character map, so OCR is needed")
    return True, f"{len(toks)} tokens, {ratio:.0%} legible"


# --------------------------------------------------------------- masked dumps
_DIGIT = re.compile(r"\d")


def mask(text):
    """Digits to #, so a layout can be studied without exposing figures."""
    return _DIGIT.sub("#", text)


def dump_page(tokens, page, masked=True, max_rows=120, tol=2.0):
    """A readable coordinate dump of one page, for a person or a model.

    Masked by default. The point of the dump is the shape of the page, and a
    layout is just as legible with the digits replaced -- which means it can be
    shown to a model without handing over the client's numbers.
    """
    rows = group_rows([t for t in tokens if t["page"] == page], tol=tol)
    lines = []
    for r in rows[:max_rows]:
        parts = []
        for t in r:
            s = mask(t["text"]) if masked else t["text"]
            parts.append(f"{s}@{t['x0']:.0f}-{t['x1']:.0f}")
        lines.append(f"y={r[0]['top']:7.1f}  " + "  ".join(parts))
    return "\n".join(lines)


def group_rows(tokens, tol=2.0):
    """Tokens grouped into visual rows by vertical tolerance.

    By tolerance, never by rounding `top` into buckets: two words on the same
    printed line routinely differ by a fraction of a point, and a bucket edge
    falling between them splits the row in half.
    """
    rows, cur = [], []
    for t in sorted(tokens, key=lambda t: (t["top"], t["x0"])):
        if cur and abs(t["top"] - cur[0]["top"]) > tol:
            rows.append(sorted(cur, key=lambda t: t["x0"]))
            cur = []
        cur.append(t)
    if cur:
        rows.append(sorted(cur, key=lambda t: t["x0"]))
    return rows


def row_text(row, sep=" "):
    return sep.join(t["text"] for t in row)
