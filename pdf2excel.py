"""pdf2excel - batch PDF table extraction, no per-file tuning.

Handles a whole folder of mixed PDFs:
  * digital PDFs  -> exact text-layer extraction (fast, no GPU, no OCR error)
  * scanned PDFs  -> PaddleOCR-VL via the vLLM server (only when necessary)

Column detection auto-tunes per page by finding the STABILITY PLATEAU: the
column count that persists across the widest range of clustering tolerances.
A layout that really has N columns stays at N over many tolerances; a wrong
split collapses as soon as tolerance grows.

Every table gets a confidence report, and financial tables are checked
arithmetically (do the rows sum to their own Total column?).

Usage
-----
  python pdf2excel.py INPUT [-o OUTDIR] [--ocr auto|never|always] [--jobs N]

  INPUT may be a single PDF, a folder, or a glob.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import sys
import traceback
from collections import Counter
from pathlib import Path

import pandas as pd
import pdfplumber

NUM_RE = re.compile(r"^\(?[-+]?[\d,]*\.?\d+\)?%?$")
MONTH_RE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*'?\d{2,4}", re.I)

VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1")
MIN_TEXT_CHARS = 120          # below this a page is treated as scanned


# --------------------------------------------------------------------------
# numeric helpers
# --------------------------------------------------------------------------
def is_num(tok: str) -> bool:
    tok = tok.strip()
    return bool(tok) and bool(NUM_RE.match(tok)) and any(c.isdigit() for c in tok)


def to_num(tok: str):
    """'(1,234.56)' -> -1234.56   '45%' -> 45.0   else None"""
    t = tok.strip().replace(",", "").replace("%", "").replace("$", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


# --------------------------------------------------------------------------
# column detection - auto-tuned, no magic constant
# --------------------------------------------------------------------------
def _cluster(xs, tol):
    out = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [sum(g) / len(g) for g in out]


def auto_columns(rights, page_width):
    """Pick the column layout that is stable over the widest tolerance band.

    Returns (columns, confidence 0..1). Confidence is the fraction of the
    scanned tolerance range that agreed on this column count.
    """
    if len(rights) < 4:
        return [], 0.0
    lo, hi = 4, max(8, int(page_width * 0.08))
    tols = list(range(lo, hi, 2)) or [lo]
    counts, layouts = [], {}
    for t in tols:
        cols = _cluster(rights, t)
        n = len(cols)
        counts.append(n)
        layouts.setdefault(n, cols)

    # ignore degenerate 0/1-column answers when richer ones exist
    tally = Counter(c for c in counts if c >= 2) or Counter(counts)
    best_n, hits = tally.most_common(1)[0]
    return layouts[best_n], hits / len(tols)


# --------------------------------------------------------------------------
# digital extraction
# --------------------------------------------------------------------------
def page_rows(page, row_tol=4):
    words = page.extract_words(keep_blank_chars=False)
    rows = {}
    for w in words:
        rows.setdefault(round(w["top"] / row_tol) * row_tol, []).append(w)
    return rows


def extract_digital(page):
    rows = page_rows(page)
    if not rows:
        return None, 0.0
    rights = [w["x1"] for ws in rows.values() for w in ws if is_num(w["text"])]
    cols, conf = auto_columns(rights, page.width)
    if not cols:
        return None, 0.0

    data = []
    for key in sorted(rows):
        line = sorted(rows[key], key=lambda w: w["x0"])
        label = " ".join(w["text"] for w in line if not is_num(w["text"])).strip()
        cells = [None] * len(cols)
        for w in line:
            if not is_num(w["text"]):
                continue
            j = min(range(len(cols)), key=lambda i: abs(cols[i] - w["x1"]))
            cells[j] = to_num(w["text"])
        if label or any(c is not None for c in cells):
            data.append([label] + cells)

    df = pd.DataFrame(data, columns=["Line Item"] + [f"C{i+1}" for i in range(len(cols))])
    df = df.dropna(axis=1, how="all")
    return name_columns(df, page), conf


def name_columns(df, page):
    """Name numeric columns from a period header row when one exists."""
    n = len(df.columns) - 1
    if n <= 0:
        return df
    for line in (page.extract_text() or "").splitlines()[:15]:
        months = MONTH_RE.findall(line)
        if len(months) >= max(2, n - 2):
            names = list(months[:n])
            while len(names) < n:
                names.append("Total" if len(names) == n - 1 else f"C{len(names)+1}")
            df.columns = ["Line Item"] + names[:n]
            return df
    return df


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
def reconcile(df):
    """If a Total-like column exists, check rows sum to it. -> (ok, total, pct)"""
    if df is None or df.empty:
        return 0, 0, None
    tcol = next((c for c in df.columns
                 if isinstance(c, str) and c.strip().lower() in ("total", "ytd", "sum")), None)
    if tcol is None:
        num = [c for c in df.columns if c != "Line Item"]
        if len(num) < 3:
            return 0, 0, None
        tcol = num[-1]
    parts = [c for c in df.columns if c not in ("Line Item", tcol)]
    if not parts:
        return 0, 0, None
    sub = df.dropna(subset=[tcol])
    sub = sub[sub[parts].notna().sum(axis=1) >= 2]
    if sub.empty:
        return 0, 0, None
    diff = (sub[parts].sum(axis=1) - sub[tcol]).abs()
    ok = int((diff < 0.02).sum())
    return ok, len(sub), round(100 * ok / len(sub), 1)


# --------------------------------------------------------------------------
# OCR fallback (scanned pages only)
# --------------------------------------------------------------------------
_PIPE = None


def ocr_pages(pdf_path, page_numbers, dpi=150):
    """Return {page_no: html} using PaddleOCR-VL. Imported lazily."""
    global _PIPE
    import pypdfium2 as pdfium
    from paddleocr import PaddleOCRVL
    if _PIPE is None:
        _PIPE = PaddleOCRVL(vl_rec_backend="vllm-server",
                            vl_rec_server_url=VLLM_URL,
                            vl_rec_max_concurrency=32,
                            vl_rec_api_model_name="PaddlePaddle/PaddleOCR-VL-1.6")
    doc = pdfium.PdfDocument(pdf_path)
    tmp, order = [], []
    out_dir = Path(os.environ.get("TEMP", ".")) / "pdf2excel_pages"
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in page_numbers:
        f = out_dir / f"{Path(pdf_path).stem[:30]}_{p}.png"
        doc[p].render(scale=dpi / 72.0).to_pil().save(f)
        tmp.append(str(f)); order.append(p)
    res = list(_PIPE.predict(tmp))
    out = {}
    for p, r in zip(order, res):
        out[p] = r.markdown.get("markdown_texts", "") if hasattr(r, "markdown") else ""
    return out


def html_to_frames(html):
    from io import StringIO
    if not html or "<table" not in html:
        return []
    try:
        return [d for d in pd.read_html(StringIO(html)) if not d.empty]
    except Exception:
        return []


# --------------------------------------------------------------------------
# per-document driver
# --------------------------------------------------------------------------
def process(pdf_path, out_dir, ocr_mode="auto", dpi=150):
    pdf_path = str(pdf_path)
    rec = {"file": Path(pdf_path).name, "pages": 0, "digital_pages": 0,
           "ocr_pages": 0, "tables": 0, "reconciled": "", "confidence": "",
           "output": "", "status": "ok", "note": ""}
    sheets, confs, rec_ok, rec_tot, scanned = [], [], 0, 0, []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            rec["pages"] = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                txt = page.extract_text() or ""
                digital = len(txt) >= MIN_TEXT_CHARS and ocr_mode != "always"
                if not digital:
                    if ocr_mode != "never":
                        scanned.append(i)
                    continue
                df, conf = extract_digital(page)
                if df is None or df.empty:
                    if ocr_mode != "never":
                        scanned.append(i)
                    continue
                rec["digital_pages"] += 1
                confs.append(conf)
                o, t, _ = reconcile(df)
                rec_ok += o; rec_tot += t
                sheets.append((f"P{i+1}", df))

        if scanned:
            pages = ocr_pages(pdf_path, scanned, dpi)
            for p, html in pages.items():
                for j, df in enumerate(html_to_frames(html)):
                    rec["ocr_pages"] += 1
                    sheets.append((f"P{p+1}_ocr{j+1}", df))

        if not sheets:
            rec["status"] = "no tables found"
            return rec

        out = Path(out_dir) / (Path(pdf_path).stem[:60] + ".xlsx")
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            used = set()
            for name, df in sheets:
                nm = name[:31]
                k = 1
                while nm in used:
                    nm = f"{name[:27]}_{k}"; k += 1
                used.add(nm)
                df.to_excel(xw, sheet_name=nm, index=False)
        rec["tables"] = len(sheets)
        rec["output"] = out.name
        rec["confidence"] = round(sum(confs) / len(confs), 2) if confs else ""
        rec["reconciled"] = (f"{rec_ok}/{rec_tot} ({round(100*rec_ok/rec_tot,1)}%)"
                             if rec_tot else "n/a")
        if rec_tot and rec_ok / rec_tot < 0.95:
            rec["note"] = "CHECK: rows do not reconcile"
        elif confs and sum(confs) / len(confs) < 0.4:
            rec["note"] = "CHECK: low column confidence"
    except Exception as e:
        rec["status"] = f"{type(e).__name__}: {e}"
        rec["note"] = traceback.format_exc()[-200:]
    return rec


def collect(target):
    p = Path(target)
    if p.is_dir():
        return sorted(p.rglob("*.pdf"))
    if any(ch in str(target) for ch in "*?"):
        return sorted(Path().glob(str(target)))
    return [p]


def main():
    ap = argparse.ArgumentParser(description="Batch PDF -> Excel table extraction")
    ap.add_argument("input", help="PDF file, folder, or glob")
    ap.add_argument("-o", "--outdir", default="extracted")
    ap.add_argument("--ocr", choices=["auto", "never", "always"], default="auto",
                    help="auto = OCR only pages without a text layer")
    ap.add_argument("--jobs", type=int, default=4, help="parallel workers (digital only)")
    ap.add_argument("--dpi", type=int, default=150)
    a = ap.parse_args()

    files = collect(a.input)
    if not files:
        print("no PDFs found"); return 1
    Path(a.outdir).mkdir(parents=True, exist_ok=True)
    print(f"{len(files)} PDF(s) -> {a.outdir}/   (ocr={a.ocr})\n")

    rows = []
    if a.ocr == "never" and a.jobs > 1:
        with cf.ThreadPoolExecutor(a.jobs) as ex:
            futs = {ex.submit(process, f, a.outdir, a.ocr, a.dpi): f for f in files}
            for fut in cf.as_completed(futs):
                r = fut.result(); rows.append(r)
                print(f"  {r['status']:<12} {r['file'][:52]:<54} "
                      f"tables={r['tables']:<3} recon={r['reconciled']}")
    else:
        for f in files:
            r = process(f, a.outdir, a.ocr, a.dpi); rows.append(r)
            print(f"  {r['status']:<12} {r['file'][:52]:<54} "
                  f"tables={r['tables']:<3} recon={r['reconciled']} {r['note'][:40]}")

    rep = pd.DataFrame(rows)
    rp = Path(a.outdir) / "_report.xlsx"
    rep.to_excel(rp, index=False)
    ok = (rep["status"] == "ok").sum()
    flagged = rep["note"].astype(str).str.startswith("CHECK").sum()
    print(f"\ndone: {ok}/{len(rep)} ok, {flagged} flagged for review")
    print(f"report: {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
