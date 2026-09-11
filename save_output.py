"""Write recognised output to files the user can download.

The pipeline emits markdown with embedded HTML tables, one block per page,
separated by `<!-- ===== page N ===== -->` markers. That is fine to read but
awkward to use, so it is also written as a browser-openable HTML page and as a
workbook with one sheet per page.
"""
import io
import re
import time
from pathlib import Path

PAGE_RE = re.compile(r"<!--\s*=+\s*page\s+(\d+)\s*=+\s*-->", re.I)


def split_pages(text):
    """[(page_no, content)] -- one entry per page marker, or a single page."""
    parts = PAGE_RE.split(text)
    if len(parts) < 3:
        return [(1, text)]
    out = []
    for i in range(1, len(parts) - 1, 2):
        out.append((int(parts[i]), parts[i + 1]))
    return out


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "output"


def save_all(text, base_name="output", out_dir="outputs"):
    """Write .md/.html/.xlsx. Returns the list of paths actually written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_safe(Path(base_name).stem)}_{time.strftime('%Y%m%d_%H%M%S')}"
    written = []

    md = out_dir / f"{stem}.md"
    md.write_text(text, encoding="utf-8")
    written.append(str(md.resolve()))

    pages = split_pages(text)

    html = out_dir / f"{stem}.html"
    body = []
    for pno, content in pages:
        body.append(f"<h2>Page {pno}</h2>\n{content}")
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
        with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
            for pno, content in pages:
                if "<table" not in content:
                    continue
                try:
                    tables = pd.read_html(io.StringIO(content))
                except Exception:
                    continue
                for j, df in enumerate(tables):
                    sheet = f"p{pno}" if j == 0 else f"p{pno}_{j + 1}"
                    df.to_excel(xw, sheet_name=sheet[:31], index=False,
                                header=False)
                    wrote_sheet = True
            if not wrote_sheet:
                # ExcelWriter refuses to save a workbook with no sheets.
                pd.DataFrame({"note": ["no tables detected"]}).to_excel(
                    xw, sheet_name="empty", index=False)
        written.append(str(xlsx.resolve()))
    except Exception as e:
        print(f"xlsx export skipped: {type(e).__name__}: {e}", flush=True)

    return written
