"""Turn rows of tokens into labelled records with numeric columns.

Two ideas carry most of the work on system-generated reports:

* Numbers are right-aligned, so they are binned by their RIGHT edge (x1).
  Their left edge moves with the magnitude of the figure -- 1,234.56 and
  17,976,882.02 start in different places and end in the same one -- so
  binning by x0 scatters one column across several.

* Indentation carries meaning. A label at x0=53 is a section, at 59 a line
  item, at 68 a total. Reading the level off the geometry is more reliable
  than matching the word "total", which also appears inside labels.

Nothing here interprets a figure. It reports what was printed and where; the
validator decides whether the reading can be trusted.
"""
from __future__ import annotations

import re

from . import geometry as G

# 1,234.56  (1,234.56)  -1,234.56  1234  .55  1,234.56-
MONEY = re.compile(r"^\(?-?[\d,]*\.?\d+\)?-?$")


def looks_numeric(text):
    t = text.strip()
    if not t or not any(ch.isdigit() for ch in t):
        return False
    return bool(MONEY.match(t.replace("$", "")))


def parse_number(text):
    """Printed figure -> float, or None. Parentheses and trailing minus are
    both negatives in accounting output."""
    t = str(text).strip().replace("$", "").replace(",", "")
    if not t:
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.endswith("-"):
        neg, t = True, t[:-1]
    if t.startswith("-"):
        neg, t = True, t[1:]
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def discover_columns(rows, tol=6.0, min_rows=3):
    """Right edges where numbers repeatedly line up -> numeric columns.

    Found from the document rather than configured, so a new report does not
    need measuring by hand before it can be read at all. A config can still
    pin them later when a template is known.
    """
    edges = []
    for row in rows:
        for t in row:
            if looks_numeric(t["text"]):
                edges.append(t["x1"])
    edges.sort()
    clusters, cur = [], []
    for e in edges:
        if cur and e - cur[-1] > tol:
            clusters.append(cur)
            cur = []
        cur.append(e)
    if cur:
        clusters.append(cur)
    cols = []
    for c in clusters:
        if len(c) >= min_rows:
            cols.append({"x1": round(sum(c) / len(c), 1), "n": len(c),
                         "lo": c[0], "hi": c[-1]})
    return sorted(cols, key=lambda c: c["x1"])


def indent_levels(rows, tol=3.0):
    """Distinct left edges of the label part of each row, ascending."""
    lefts = []
    for row in rows:
        label = [t for t in row if not looks_numeric(t["text"])]
        if label:
            lefts.append(label[0]["x0"])
    lefts.sort()
    levels, cur = [], None
    for x in lefts:
        if cur is None or x - cur > tol:
            levels.append(x)
            cur = x
    return levels


def read_rows(tokens, page=None, row_tol=2.0, col_tol=6.0, columns=None):
    """[{page, y, level, x0, label, values[], unbinned[]}] for the tokens given.

    `unbinned` holds numeric tokens that matched no column. The pack is blunt
    about this and it is right: a number that falls outside every bin is a bug
    until it is explained, so it is carried forward rather than dropped.
    """
    toks = [t for t in tokens if page is None or t["page"] == page]
    rows = G.group_rows(toks, tol=row_tol)
    cols = columns or discover_columns(rows, tol=col_tol)
    levels = indent_levels(rows)

    # Where the numeric columns begin. Digits to the left of this belong to a
    # label -- a period line ("Oct 2024-Dec") or an entity code ("225bush") --
    # and counting them as stray values would raise a false alarm on every
    # header row.
    value_zone = min((c["lo"] for c in cols), default=0.0) - 5.0

    out = []
    for row in rows:
        labels = [t for t in row
                  if not looks_numeric(t["text"]) or t["x1"] < value_zone]
        nums = [t for t in row
                if looks_numeric(t["text"]) and t["x1"] >= value_zone]
        if not labels and not nums:
            continue

        values = [None] * len(cols)
        unbinned = []
        for t in nums:
            hit = None
            for i, c in enumerate(cols):
                if abs(t["x1"] - c["x1"]) <= col_tol:
                    hit = i
                    break
            if hit is None:
                unbinned.append(t["text"])
            elif values[hit] is None:
                values[hit] = t["text"]
            else:
                unbinned.append(t["text"])       # two numbers in one bin

        x0 = labels[0]["x0"] if labels else None
        level = None
        if x0 is not None and levels:
            level = min(range(len(levels)), key=lambda i: abs(levels[i] - x0))

        out.append({
            "page": row[0]["page"],
            "y": row[0]["top"],
            "x0": x0,
            "level": level,
            "label": G.row_text(labels).strip(),
            "values": values,
            "unbinned": unbinned,
        })
    return out, cols, levels
