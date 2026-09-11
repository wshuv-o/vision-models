"""pdf2text - give a text-only model the contents of a PDF, accurately.

Digital PDFs carry exact text. Rasterising them for OCR loses accuracy, so
this reads the text layer directly and only reports SCANNED when there is
genuinely nothing to read.

    python pdf2text.py FILE.pdf                 # plain text
    python pdf2text.py FILE.pdf --tables        # detected tables as TSV
    python pdf2text.py FILE.pdf --excel out.xlsx
    python pdf2text.py FILE.pdf --pages 1-3
"""
import argparse
import sys
from pathlib import Path

import pdfplumber

MIN_CHARS = 120


def parse_pages(spec, n):
    if not spec:
        return list(range(n))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a) - 1, min(int(b), n)))
        else:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < n]


OCR_SERVER = "http://localhost:8000"


def render_images(a):
    """Render PDF pages to PNG so a vision model can Read them.

    No poppler / pdftoppm needed - pypdfium2 is bundled.
    Prints one absolute path per line; Read each of those.
    """
    import tempfile
    import pypdfium2 as pdfium

    src = Path(a.pdf)
    out = Path(a.outdir) if a.outdir else Path(tempfile.gettempdir()) / "pdfpages" / src.stem[:40]
    out.mkdir(parents=True, exist_ok=True)

    doc = pdfium.PdfDocument(str(src))
    idxs = parse_pages(a.pages, len(doc))
    print(f"{len(doc)} page(s) in file; rendering {len(idxs)} at {a.dpi} dpi")
    for i in idxs:
        f = out / f"page{i+1:03d}.png"
        doc[i].render(scale=a.dpi / 72.0).to_pil().save(f)
        print(str(f))
    print("\nRead the PNG paths above with the Read tool - your vision handles them.")
    return 0


def run_ocr(a):
    """OCR a scanned PDF through the PaddleOCR-VL server.

    Do NOT hand-roll PaddleOCR here - the server path is already configured
    and benchmarked (~3 s/page). This function only checks it is reachable
    and reports clearly if it is not.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(OCR_SERVER + "/health", timeout=4) as r:
            up = r.status == 200
    except Exception:
        up = False
    if not up:
        print("OCR SERVER NOT RUNNING.\n"
              "  Start it:  wsl -d Ubuntu -e bash -c \"~/ocr/start_server.sh\"\n"
              "  It needs ~6GB VRAM. If a large agent model is loaded, free it first:\n"
              "      ollama stop gpt-oss-64k\n"
              "  Do not write a replacement OCR script - this path already exists.")
        return 3

    import pypdfium2 as pdfium
    from paddleocr import PaddleOCRVL
    import tempfile

    pipe = PaddleOCRVL(vl_rec_backend="vllm-server",
                       vl_rec_server_url=OCR_SERVER + "/v1",
                       vl_rec_max_concurrency=32,
                       vl_rec_api_model_name="PaddlePaddle/PaddleOCR-VL-1.6")
    doc = pdfium.PdfDocument(a.pdf)
    idxs = parse_pages(a.pages, len(doc))
    tmp = Path(tempfile.gettempdir()) / "pdf2text_ocr"
    tmp.mkdir(exist_ok=True)
    imgs = []
    for i in idxs:
        f = tmp / f"p{i:03d}.png"
        doc[i].render(scale=a.dpi / 72.0).to_pil().save(f)
        imgs.append(str(f))

    print(f"OCR: {len(imgs)} page(s) via PaddleOCR-VL server ...")
    res = list(pipe.predict(imgs))
    htmls = []
    for i, r in zip(idxs, res):
        md = r.markdown.get("markdown_texts", "") if hasattr(r, "markdown") else ""
        htmls.append(md or "")
        if not a.excel:
            print(f"\n--- PAGE {i+1} ---\n{md}")

    if a.excel:
        import pandas as pd
        from io import StringIO
        n = 0
        with pd.ExcelWriter(a.excel, engine="openpyxl") as xw:
            for i, md in zip(idxs, htmls):
                if "<table" not in (md or ""):
                    continue
                try:
                    frames = pd.read_html(StringIO(md))
                except Exception:
                    continue
                for j, df in enumerate(frames):
                    if df.empty:
                        continue
                    df.to_excel(xw, sheet_name=f"P{i+1}_T{j+1}"[:31], index=False)
                    n += 1
            if n == 0:
                pd.DataFrame({"info": ["no tables detected"]}).to_excel(
                    xw, sheet_name="empty", index=False)
        print(f"\nWROTE: {a.excel}  ({n} table sheet(s))")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--tables", action="store_true", help="emit tables as TSV")
    ap.add_argument("--excel", help="write tables to this .xlsx")
    ap.add_argument("--pages", help="e.g. 1-3 or 1,4,7")
    ap.add_argument("--max-chars", type=int, default=200000)
    ap.add_argument("--ocr", action="store_true",
                    help="force OCR (scanned PDFs). Needs the PaddleOCR-VL server.")
    ap.add_argument("--images", action="store_true",
                    help="render pages to PNG and print their paths (for vision models)")
    ap.add_argument("--outdir", help="where --images writes (default: temp dir)")
    ap.add_argument("--dpi", type=int, default=150)
    a = ap.parse_args()

    if a.images:
        return render_images(a)
    if a.ocr:
        return run_ocr(a)

    p = Path(a.pdf)
    if not p.exists():
        print(f"ERROR: not found: {p}")
        return 2

    with pdfplumber.open(str(p)) as pdf:
        idxs = parse_pages(a.pages, len(pdf.pages))
        total = sum(len(pdf.pages[i].extract_text() or "") for i in idxs)
        if total < MIN_CHARS:
            print(f"SCANNED: {p.name} has no usable text layer "
                  f"({total} chars over {len(idxs)} page(s)). OCR is required; "
                  f"do not guess at the contents.")
            return 1

        print(f"DIGITAL: {p.name} · {len(pdf.pages)} page(s) · {total} chars of exact text")

        if a.tables or a.excel:
            import importlib.util
            here = Path(__file__).resolve().parent
            spec = importlib.util.spec_from_file_location("pc", here / "pipeline_core.py")
            pc = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pc)
            frames = []
            for i in idxs:
                df, conf = pc.extract_page(pdf.pages[i])
                if df is None or df.empty:
                    continue
                frames.append((f"P{i+1}", df))
                if a.tables:
                    print(f"\n=== page {i+1} · {len(df)} rows x {len(df.columns)} cols "
                          f"(column confidence {conf}) ===")
                    print(df.to_csv(sep="\t", index=False)[:a.max_chars])
            if a.excel and frames:
                import pandas as pd
                with pd.ExcelWriter(a.excel, engine="openpyxl") as xw:
                    for sn, df in frames:
                        df.to_excel(xw, sheet_name=sn[:31], index=False)
                print(f"\nWROTE: {a.excel}  ({len(frames)} sheet(s))")
            elif a.excel:
                print("\nNo tables detected; nothing written.")
            return 0

        out, used = [], 0
        for i in idxs:
            t = pdf.pages[i].extract_text() or ""
            if used + len(t) > a.max_chars:
                out.append(f"\n[truncated at {a.max_chars} chars]")
                break
            out.append(f"\n--- PAGE {i+1} ---\n{t}")
            used += len(t)
        print("".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
