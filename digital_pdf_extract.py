"""Exact table extraction from DIGITAL (text-layer) PDFs.

Financial statements are usually generated PDFs, not scans. Their numbers are
already in the file - running OCR over them renders exact text to pixels and
then guesses, which introduces errors. This reads the text layer directly.

Rows come from y-clustering; columns from the right-edge alignment of numeric
tokens (financial tables are right-aligned).
"""
import re, sys
import pdfplumber
import pandas as pd

NUM = re.compile(r"^\(?-?[\d,]+\.?\d*\)?%?$")


def is_num(t):
    t = t.strip()
    return bool(t) and bool(NUM.match(t)) and any(c.isdigit() for c in t)


def to_float(t):
    """'(1,234.56)' -> -1234.56 ; '1,234.56' -> 1234.56 ; else None."""
    t = t.strip().replace(",", "").replace("%", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def has_text_layer(path, min_chars=200):
    with pdfplumber.open(path) as pdf:
        return sum(len(p.extract_text() or "") for p in pdf.pages) >= min_chars


def cluster_columns(rights, tol=12):
    """Group right-edge x positions into column anchors."""
    anchors = []
    for x in sorted(rights):
        if anchors and x - anchors[-1][-1] <= tol:
            anchors[-1].append(x)
        else:
            anchors.append([x])
    return [sum(a) / len(a) for a in anchors]


def extract_page(page, row_tol=4, col_tol=30):
    words = page.extract_words(keep_blank_chars=False)
    if not words:
        return None
    rows = {}
    for w in words:
        rows.setdefault(round(w["top"] / row_tol) * row_tol, []).append(w)

    # column anchors from every numeric token on the page
    rights = [w["x1"] for ws in rows.values() for w in ws if is_num(w["text"])]
    if not rights:
        return None
    cols = cluster_columns(rights, col_tol)

    out = []
    for k in sorted(rows):
        line = sorted(rows[k], key=lambda x: x["x0"])
        label = " ".join(w["text"] for w in line if not is_num(w["text"])).strip()
        cells = [None] * len(cols)
        for w in line:
            if not is_num(w["text"]):
                continue
            j = min(range(len(cols)), key=lambda i: abs(cols[i] - w["x1"]))
            cells[j] = to_float(w["text"])
        if label or any(c is not None for c in cells):
            out.append([label] + cells)

    df = pd.DataFrame(out, columns=["Line Item"] + ["C%d" % (i + 1) for i in range(len(cols))])
    return df.dropna(axis=1, how="all")


def name_columns(df, page):
    """Try to label numeric columns from the header row (e.g. 'Jan 2025')."""
    txt = page.extract_text() or ""
    hdr = None
    for ln in txt.splitlines()[:12]:
        months = re.findall(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*\d{4}", ln)
        if len(months) >= 3:
            hdr = months
            break
    n = len(df.columns) - 1
    if hdr:
        names = list(hdr[:n])
        while len(names) < n:
            names.append("Total" if len(names) == n - 1 else "C%d" % (len(names) + 1))
        df.columns = ["Line Item"] + names[:n]
    return df


def extract(path, out_xlsx=None):
    dfs = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            df = extract_page(page)
            if df is None or df.empty:
                continue
            df = name_columns(df, page)
            dfs.append(("P%d" % (i + 1), df))
    if out_xlsx and dfs:
        with pd.ExcelWriter(out_xlsx, engine="openpyxl") as xw:
            for sn, df in dfs:
                df.to_excel(xw, sheet_name=sn[:31], index=False)
    return dfs


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else "extracted.xlsx"
    print("text layer:", has_text_layer(src))
    res = extract(src, dst)
    for sn, df in res:
        print("\n=== %s : %d rows x %d cols ===" % (sn, len(df), len(df.columns)))
        print(df.head(14).to_string(max_colwidth=26))
    print("\nwrote:", dst)
