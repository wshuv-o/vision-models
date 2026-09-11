"""Write recognised output to files the user can download.

The pipeline emits markdown with embedded HTML tables, one block per page,
separated by `<!-- ===== page N ===== -->` markers. That is fine to read but
awkward to use, so it is also written as a browser-openable HTML page and as a
workbook with one sheet per page.
"""
import io
import os
import re
import time
from pathlib import Path

KNOWN_EXTS = {".pdf", ".md", ".markdown", ".html", ".htm", ".xlsx", ".xls",
              ".xlsm", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}

# A batch run covers several PDFs, so a page marker carries the file it came
# from as well as the page number. The plain `page N` form is still accepted so
# older single-file output keeps working.
PAGE_RE = re.compile(
    r"<!--\s*=+\s*(?:file:\s*(?P<file>[^|]*?)\s*\|\s*)?"
    r"page\s+(?P<page>\d+)\s*=+\s*-->",
    re.I,
)


def page_label(file_name, page_no):
    """'invoice.pdf p3', or just 'page 3' when there is only one document."""
    file_name = (file_name or "").strip()
    return f"{file_name} p{page_no}" if file_name else f"page {page_no}"


def page_marker(file_name, page_no):
    if (file_name or "").strip():
        return f"<!-- ===== file: {file_name} | page {page_no} ===== -->"
    return f"<!-- ===== page {page_no} ===== -->"


def split_pages(text):
    """[(label, content)] -- one entry per page marker, or a single page."""
    marks = list(PAGE_RE.finditer(text))
    if not marks:
        return [("page 1", text)]
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((page_label(m.group("file"), m.group("page")),
                    text[m.end():end]))
    return out


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "output"


def _sheet_name(label, used):
    """Excel sheet names: <=31 chars, none of []:*?/\\, and unique.

    The page number lives at the end of the label, so a plain truncation turns
    every page of a long-named PDF into the same string. Keep the ' pN' tail
    and cut the file name instead.
    """
    name = re.sub(r"[\[\]:*?/\\]+", "_", str(label)).strip() or "page"
    m = re.match(r"^(?P<head>.*?)(?P<tail>\s*p\d+(?:_\d+)?)$", name)
    head, tail = (m.group("head"), m.group("tail")) if m else (name, "")

    def build(extra=""):
        room = 31 - len(tail) - len(extra)
        return (head[:max(room, 0)].rstrip() + extra + tail)[:31]

    cand = build()
    if cand not in used:
        used.add(cand)
        return cand
    for n in range(2, 1000):
        cand = build(f"~{n}")
        if cand not in used:
            used.add(cand)
            return cand
    raise ValueError(f"cannot make a unique sheet name for {label!r}")


def save_all(text, base_name="output", out_dir="outputs"):
    """Write .md/.html/.xlsx. Returns the list of paths actually written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Path().stem cuts at the LAST dot, which mangles names that contain dots
    # ("2026.06_Sheraton_..." became "2026"). Only drop a real extension.
    raw = Path(base_name).name
    root, ext = os.path.splitext(raw)
    if ext.lower() in KNOWN_EXTS:
        raw = root
    stem = f"{_safe(raw)}_{time.strftime('%Y%m%d_%H%M%S')}"
    written = []

    md = out_dir / f"{stem}.md"
    md.write_text(text, encoding="utf-8")
    written.append(str(md.resolve()))

    pages = split_pages(text)

    html = out_dir / f"{stem}.html"
    body = []
    for label, content in pages:
        # data-page is what stage 2 reads back; the heading text is for humans.
        esc = label.replace('"', "&quot;")
        body.append(f'<h2 data-page="{esc}">{label}</h2>\n{content}')
    html.write_text(
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{stem}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;}"
        "table{border-collapse:collapse;margin:1rem 0;font-size:12px;}"
        "td,th{border:1px solid #999;padding:2px 4px;}"
        "h2{margin-top:2rem;border-top:2px solid #ccc;padding-top:1rem;}</style>\n"
        + "\n".join(body),
        encoding="utf-8",
    )
    written.append(str(html.resolve()))

    xlsx = out_dir / f"{stem}.xlsx"
    try:
        import pandas as pd

        wrote_sheet = False
        used = set()
        with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
            for label, content in pages:
                if "<table" not in content:
                    continue
                try:
                    tables = pd.read_html(io.StringIO(content))
                except Exception:
                    continue
                for j, df in enumerate(tables):
                    base = label if j == 0 else f"{label}_{j + 1}"
                    df.to_excel(xw, sheet_name=_sheet_name(base, used),
                                index=False, header=False)
                    wrote_sheet = True
            if not wrote_sheet:
                # ExcelWriter refuses to save a workbook with no sheets.
                pd.DataFrame({"note": ["no tables detected"]}).to_excel(
                    xw, sheet_name="empty", index=False)
        written.append(str(xlsx.resolve()))
    except Exception as e:
        print(f"xlsx export skipped: {type(e).__name__}: {e}", flush=True)

    return written
