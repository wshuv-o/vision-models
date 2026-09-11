#!/usr/bin/env python
"""Page-by-page PDF extraction with PaddleOCR-VL 1.6 served by vLLM.

Renders each PDF page to PNG (pypdfium2), then runs the PaddleOCR-VL pipeline:
layout detection (PP-DocLayoutV3) locally, block recognition on the vLLM
OpenAI-compatible server. Progress prints live, one line per stage.

Must run where PaddlePaddle works -- on this machine that is WSL, not Windows:
  ~/ocr/.venv-paddle/bin/python pdf_vllm_pages.py <pdf> [--batch 4]
"""
import argparse
import os
import sys
import time
from pathlib import Path

_here = os.path.dirname(os.path.abspath(__file__))
_cert = os.path.join(_here, "combined_cacert.pem")
if os.path.exists(_cert):
    os.environ.setdefault("SSL_CERT_FILE", _cert)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _cert)
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("FLAGS_call_stack_level", "0")

import pdf_pages

SERVER = os.environ.get("PADDLEOCR_VL_SERVER", "http://localhost:8000/v1")
MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"

_t0 = time.time()


def log(msg, end="\n"):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", end=end, flush=True)


def rule(char="-", n=78):
    print(char * n, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="vllm_pages_out")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--pages", default=None, help="e.g. 1-3 or 1,4,7")
    ap.add_argument("--batch", type=int, default=1,
                    help="pages sent to the pipeline per call (1 = one at a time)")
    ap.add_argument("--concurrency", type=int, default=64,
                    help="max blocks in flight to the vLLM server")
    ap.add_argument("--rotate", default="auto", help="auto or 0/90/180/270")
    ap.add_argument("--no-crop", action="store_true")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"no such PDF: {pdf_path}")

    out_dir = Path(args.out)
    (out_dir / "pages").mkdir(parents=True, exist_ok=True)
    (out_dir / "markdown").mkdir(parents=True, exist_ok=True)

    total = pdf_pages.page_count(pdf_path)
    todo = pdf_pages.parse_pages(args.pages, total)

    rule("=")
    print("  PaddleOCR-VL 1.6  |  page-by-page extraction via vLLM", flush=True)
    rule("=")
    print(f"  PDF      : {pdf_path.name}", flush=True)
    print(f"  Pages    : {len(todo)} of {total}", flush=True)
    print(f"  Render   : {args.dpi} dpi, rotate={args.rotate}, "
          f"crop={not args.no_crop}", flush=True)
    print(f"  Batch    : {args.batch} page(s)/call, "
          f"{args.concurrency} blocks in flight", flush=True)
    print(f"  Backend  : vllm-server @ {SERVER}", flush=True)
    print(f"  Output   : {out_dir.resolve()}", flush=True)
    rule()

    os.environ["PADDLEOCR_VL_CONCURRENCY"] = str(args.concurrency)
    os.environ["PADDLEOCR_VL_SERVER"] = SERVER
    import paddleocr_worker as w

    log("loading layout model + connecting to vLLM ...", end=" ")
    t = time.time()
    w._load()
    print(f"ok ({time.time() - t:.1f}s)", flush=True)
    rule()

    # ---- render everything first (cheap) --------------------------------
    rendered = []
    t_render = time.time()
    for pno, png, info in pdf_pages.render_pdf(
            pdf_path, out_dir / "pages", dpi=args.dpi, pages=args.pages,
            rotate=args.rotate, crop=(not args.no_crop)):
        rendered.append((pno, png))
        wpx, hpx = info["size"]
        note = (f" (rot {info['applied_rotation']}deg)"
                if info["applied_rotation"] else "")
        log(f"page {pno:>3}/{total}  rendered {wpx}x{hpx}px{note}")
    t_render = time.time() - t_render
    rule()

    # ---- recognise ------------------------------------------------------
    results, all_md = [], []
    t_ocr_all = time.time()
    for i in range(0, len(rendered), args.batch):
        chunk = rendered[i:i + args.batch]
        lo, hi = chunk[0][0], chunk[-1][0]
        label = f"page {lo}" if lo == hi else f"pages {lo}-{hi}"
        log(f"{label:<16} recognising ({len(chunk)} at once) ...", end=" ")
        t = time.time()
        try:
            texts = w.run_many([p for _, p in chunk], out_dir / "markdown")
            err = None
        except Exception as e:
            texts = [""] * len(chunk)
            err = f"{type(e).__name__}: {e}"
        el = time.time() - t
        if err:
            print("FAILED", flush=True)
            log(f"  !! {err}")
        else:
            print(f"{el:5.1f}s ({el / len(chunk):.1f}s/page)", flush=True)
        for (pno, _), text in zip(chunk, texts):
            (out_dir / "markdown" / f"page_{pno:03d}.md").write_text(
                text, encoding="utf-8")
            all_md.append(f"\n\n<!-- ===== page {pno} ===== -->\n\n{text}")
            results.append((pno, len(text), text.count("<table"), err))
    t_ocr_all = time.time() - t_ocr_all

    combined = out_dir / f"{pdf_path.stem}.md"
    combined.write_text("".join(all_md), encoding="utf-8")

    rule("=")
    print("  SUMMARY", flush=True)
    rule("=")
    print(f"  {'page':>5} {'chars':>8} {'tables':>7}", flush=True)
    for pno, nchar, ntab, err in results:
        print(f"  {pno:>5} {nchar:>8} {ntab:>7}"
              + ("  FAILED" if err else ""), flush=True)
    n = max(len(results), 1)
    print(f"\n  render : {t_render:.1f}s total ({t_render / n:.2f}s/page)", flush=True)
    print(f"  ocr    : {t_ocr_all:.1f}s total ({t_ocr_all / n:.2f}s/page)", flush=True)
    print(f"  chars  : {sum(r[1] for r in results)}", flush=True)
    print(f"  -> {combined.resolve()}", flush=True)
    print(f"  total wall clock: {time.time() - _t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
