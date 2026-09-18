"""Walk a folder tree and work out what each file is and how to read it.

Point this at a root like

    Properties/
      123 Main St/
        appraisal/ APR_123_Main.pdf
        tax/       2025_tax_bill.pdf
        rent roll/ rentroll.xlsx

and it produces a manifest: one entry per file, saying which property folder it
belongs to and which extractor should handle it. Nothing here calls a model or
touches the GPU -- it is the deterministic half of the job, and it has to be
right every time, because an orchestrator that misroutes one file in twenty
produces a spreadsheet that is quietly wrong in ways nobody notices.

    python corpus_scan.py <root> --depth 1 --out manifest.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import field_retrieve as F
import pdf_pages

# How a file gets read.
#   text       its own text layer is enough, no model needed to get the words
#   table      already structured (spreadsheet/csv)
#   doc        word/powerpoint, text extracted directly
#   needs_ocr  the words only exist as pixels
#   unsupported / unreadable
#
# needs_ocr is never upgraded to ocr unless OCR is explicitly enabled. OCR is
# slow (~2.8s a page against 0.3s for a whole text PDF) and it guesses, so
# turning it on has to be a decision someone made, not a silent fallback when
# a file is hard to read.
ROUTE_BY_EXT = {
    ".pdf": "pdf",           # decided per file: text layer or not
    ".xlsx": "table", ".xls": "table", ".xlsm": "table", ".csv": "table",
    ".tsv": "table",
    ".docx": "doc", ".pptx": "doc",
    ".png": "needs_ocr", ".jpg": "needs_ocr", ".jpeg": "needs_ocr",
    ".tif": "needs_ocr", ".tiff": "needs_ocr", ".webp": "needs_ocr",
    ".bmp": "needs_ocr",
    ".txt": "text", ".md": "text", ".html": "text", ".htm": "text",
    ".json": "text", ".xml": "text",
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".vscode",
             ".idea", "$RECYCLE.BIN", "System Volume Information"}


def classify_pdf(path, sample_pages=24, empty_page_chars=80):
    """Digital or scanned? Decides whether OCR is needed, which is the single
    biggest cost difference in the whole pipeline (0.3s vs ~5 min).

    Judged across a spread of the document, not just the front. A file with a
    typed cover page over scanned content would otherwise be called "text" and
    its scanned pages would vanish silently -- the worst outcome available,
    since the row still fills in and nothing says anything was missed.
    """
    try:
        total_probe = pdf_pages.page_count(path)
    except Exception:
        total_probe = None
    try:
        allp = F.page_texts(path)
        if total_probe and total_probe > sample_pages:
            step = (total_probe - 1) / (sample_pages - 1)
            idx = sorted({round(i * step) for i in range(sample_pages)})
            pages = [allp[i] for i in idx if i < len(allp)]
        else:
            pages = allp
    except Exception as e:
        return {"route": "unreadable", "pages": None,
                "note": f"{type(e).__name__}: {e}"}
    try:
        total = pdf_pages.page_count(path)
    except OSError as e:
        # Only a genuine read failure falls back; a broad except here hid an
        # AttributeError and silently reported 10 pages for a 193-page file.
        total = len(pages)
        print(f"  warning: page count failed for {path}: {e}", file=sys.stderr)
    ok = F.text_layer_ok(pages)
    chars = sum(len(t) for _, t in pages)
    blank = [n for n, t in pages if len(t.strip()) < empty_page_chars]
    frac_blank = len(blank) / max(len(pages), 1)

    note = ""
    if not ok:
        note = "no text layer - needs OCR, which is off by default"
    elif frac_blank >= 0.25:
        pct = round(frac_blank * 100)
        note = (f"MIXED: {pct}% of sampled pages have no text "
                f"(e.g. p{blank[0]}) - those pages need OCR to be read")
    return {
        "route": "text" if ok else "needs_ocr",
        "pages": total,
        "sampled_chars": chars,
        "blank_fraction": round(frac_blank, 2),
        "note": note,
    }


def scan(root, depth=1, follow_links=False, max_files=None, allow_ocr=False):
    """Yield one record per file under root.

    `depth` says which folder level names the property: 1 means the immediate
    children of root are properties, 2 means their children are, and 0 means
    the whole tree is a single group.
    """
    root = Path(root).resolve()
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_links):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.startswith(".")]
        here = Path(dirpath)
        try:
            rel = here.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if depth <= 0:
            group = root.name
        elif len(parts) >= depth:
            group = str(Path(*parts[:depth]))
        else:
            group = "(root)"          # files sitting directly in the root

        for fn in sorted(filenames):
            if fn.startswith("~$") or fn.startswith("."):
                continue
            p = here / fn
            ext = p.suffix.lower()
            route = ROUTE_BY_EXT.get(ext, "unsupported")
            rec = {
                "group": group,
                "path": str(p),
                "name": fn,
                "ext": ext,
                "size": p.stat().st_size if p.exists() else 0,
                "route": route,
                "pages": None,
                "note": "",
            }
            if route == "pdf":
                rec.update(classify_pdf(p))
            if rec["route"] == "needs_ocr":
                if allow_ocr:
                    rec["route"] = "ocr"
                    rec["note"] = "OCR enabled for this run"
                elif not rec["note"]:
                    rec["note"] = "image - OCR is off, so this is skipped"
            yield rec
            seen += 1
            if max_files and seen >= max_files:
                return


def summarise(records):
    groups, routes, pages, bytes_ = {}, {}, 0, 0
    for r in records:
        groups.setdefault(r["group"], []).append(r)
        routes[r["route"]] = routes.get(r["route"], 0) + 1
        pages += r.get("pages") or 0
        bytes_ += r.get("size") or 0
    return {"groups": groups, "routes": routes, "total_pages": pages,
            "total_bytes": bytes_, "n_files": len(records)}


def estimate(summary):
    """Rough wall-clock, from figures measured on this machine."""
    r = summary["routes"]
    text_pdfs = r.get("text", 0)
    ocr_files = r.get("ocr", 0)
    ocr_pages = sum((rec.get("pages") or 1)
                    for recs in summary["groups"].values()
                    for rec in recs if rec["route"] == "ocr")
    # 8.9s per text PDF end to end; 2.8s per page through OCR, plus extraction.
    return {
        "text_pdf_seconds": round(text_pdfs * 8.9),
        "ocr_pages": ocr_pages,
        "ocr_seconds": round(ocr_pages * 2.8),
        "ocr_files": ocr_files,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--depth", type=int, default=1,
                    help="folder level that names the property (default 1)")
    ap.add_argument("--out", default=None, help="write the manifest as JSON")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--allow-ocr", action="store_true",
                    help="let files with no text layer go through OCR. Off by "
                         "default: without it they are listed and skipped, "
                         "never silently guessed at.")
    args = ap.parse_args()

    t0 = time.time()
    records = list(scan(args.root, depth=args.depth, max_files=args.max_files,
                        allow_ocr=args.allow_ocr))
    s = summarise(records)
    est = estimate(s)

    print("=" * 78)
    print(f"  {args.root}")
    print("=" * 78)
    print(f"  {s['n_files']} file(s) in {len(s['groups'])} group(s), "
          f"{s['total_bytes']/1e6:.0f}MB, {s['total_pages']} PDF page(s)")
    print(f"  routes: {s['routes']}")
    print()
    for g, recs in sorted(s["groups"].items()):
        by_route = {}
        for r in recs:
            by_route[r["route"]] = by_route.get(r["route"], 0) + 1
        print(f"  {g}")
        print(f"      {len(recs)} file(s)  {by_route}")
        for r in recs[:4]:
            pg = f"{r['pages']}p" if r.get("pages") else ""
            print(f"        - {r['name'][:52]:<54} {r['route']:<11} {pg} "
                  f"{r['note'][:40]}")
        if len(recs) > 4:
            print(f"        ... and {len(recs)-4} more")
    print()
    print(f"  estimated: {est['text_pdf_seconds']}s for text files")
    skipped = s["routes"].get("needs_ocr", 0)
    if skipped:
        print(f"  {skipped} file(s) hold their words as pixels and were SKIPPED."
              f" Pass --allow-ocr to read them (~2.8s a page).")
    elif est["ocr_files"]:
        print(f"  plus {est['ocr_seconds']}s for {est['ocr_pages']} OCR page(s) "
              f"across {est['ocr_files']} file(s)")
    unsupported = s["routes"].get("unsupported", 0)
    if unsupported:
        print(f"  {unsupported} file(s) are of a type nothing here reads.")
    print(f"  scanned in {time.time()-t0:.1f}s")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"root": str(args.root), "records": records,
                        "summary": {"routes": s["routes"],
                                    "n_files": s["n_files"],
                                    "groups": sorted(s["groups"])},
                        "estimate": est}, indent=2),
            encoding="utf-8")
        print(f"  manifest -> {args.out}")


if __name__ == "__main__":
    main()
