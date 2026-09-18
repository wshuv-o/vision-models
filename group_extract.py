"""Extract a row of fields per folder, from whatever documents the folder holds.

Point it at a root whose sub-folders each represent one thing -- a loan, a
property, a case -- and it reads the documents inside each, finds the parts
that bear on the fields asked for, and returns one row per folder.

Two phases, and the order is forced by the hardware. OCR is served by vLLM and
extraction by Ollama, and on a 16GB card they cannot both be resident. So
every file needing OCR is converted first and cached to disk, the GPU is
handed back, and only then does extraction begin. Interleaving them would mean
loading and unloading a model per folder.

OCR never runs unless it is asked for. A file whose words are pixels is
reported as unread, not guessed at.

    python group_extract.py <root> --fields fields.txt [--allow-ocr]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

import corpus_scan
import doc_text
import group_select

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
_here = Path(__file__).resolve().parent
OCR_CACHE = _here / ".ocr_cache"

SYSTEM = """You read documents and pull out specific facts.

You are given excerpts from the documents belonging to ONE {subject}. Each
excerpt says which file and which page or section it came from.

Rules:
- Report only what the documents actually state about this {subject}.
- Documents often describe other things alongside their subject, for
  comparison, reference or background. Take nothing from those passages; a
  figure that belongs to something else is worse than no figure.
- Use null for anything the documents do not state. Never guess, never infer
  a value from context, never fill a field to be helpful.
- Copy values as printed, including any currency, percent or date formatting.
- Where two documents disagree, prefer the one stating it as a definitive term
  or conclusion rather than in passing, and say so in _notes.

Reply with ONLY a JSON object, no prose and no code fences, with exactly these
keys: {keys}. Add "_sources": an object mapping each field you filled to the
"FILE | page" it came from, and "_notes": a short string, or null."""


def cache_path(src):
    h = hashlib.sha1(str(Path(src).resolve()).encode()).hexdigest()[:16]
    return OCR_CACHE / f"{Path(src).stem[:40]}_{h}.md"


OCR_PAGE_LIMIT = 10


def ocr_needed(records, allow_ocr):
    """Files whose words are pixels. Empty unless OCR was asked for."""
    if not allow_ocr:
        return []
    return [r for r in records if r["route"] == "ocr"
            and not cache_path(r["path"]).exists()]


def split_by_size(files, page_limit=OCR_PAGE_LIMIT):
    """(small, large) -- large ones are worth asking about before starting.

    A page costs about 2.8 seconds, so a short scan is not worth interrupting
    over and a two-hundred-page one is ten minutes of the card.
    """
    small, large = [], []
    for r in files:
        pages = r.get("pages") or 1
        (large if pages > page_limit else small).append(r)
    return small, large


def confirm_interactive(rec):
    """Ask at a terminal. A UI passes its own callback instead."""
    pages = rec.get("pages") or 1
    mins = pages * 2.8 / 60
    est = f"{pages * 2.8:.0f}s" if mins < 1 else f"{mins:.1f} min"
    name = os.path.basename(rec["path"])
    try:
        ans = input(f"  OCR {name}? {pages} pages, about {est}  [y/N] ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def run_ocr_phase(files, dpi=300, batch=4, log=print, confirm=None,
                  page_limit=OCR_PAGE_LIMIT):
    """Convert every pixel-only file once, and cache it.

    Anything longer than `page_limit` pages is put to `confirm` first. A
    declined file is skipped and named in the row, never quietly converted or
    quietly dropped.

    Imported lazily: this pulls in the WSL/vLLM machinery, and a run with no
    scanned documents should not pay for that or start a server.
    """
    declined = set()
    if not files:
        return {}, declined

    small, large = split_by_size(files, page_limit)
    if large:
        if confirm is None:
            # No way to ask means the answer is no. Starting a ten-minute job
            # nobody approved is the worse failure.
            for r in large:
                declined.add(r["path"])
                log(f"  skipping {os.path.basename(r['path'])} "
                    f"({r.get('pages')} pages) - over {page_limit} and nobody to ask")
            large = []
        else:
            keep = []
            for r in large:
                if confirm(r):
                    keep.append(r)
                else:
                    declined.add(r["path"])
                    log(f"  skipping {os.path.basename(r['path'])} "
                        f"({r.get('pages')} pages) - declined")
            large = keep

    files = small + large
    if not files:
        return {}, declined

    import app as ocr_app         # the OCR stage, which owns the vLLM server

    OCR_CACHE.mkdir(parents=True, exist_ok=True)
    done = {}
    log(f"OCR phase: {len(files)} file(s) need converting")
    ocr_app.ensure_model("paddleocr_vl")
    try:
        for i, rec in enumerate(files, 1):
            src = rec["path"]
            out = cache_path(src)
            t = time.time()
            try:
                texts = []
                import tempfile
                work = tempfile.mkdtemp(prefix="grp_ocr_")
                pages = list(pdf_or_image_pages(src, work, dpi))
                for j in range(0, len(pages), batch):
                    chunk = pages[j:j + batch]
                    got = ocr_app._ocr_request_batch([p for _, p in chunk])
                    for (pno, _), txt in zip(chunk, got):
                        texts.append(f"<!-- ===== page {pno} ===== -->\n{txt}")
                out.write_text("\n\n".join(texts), encoding="utf-8")
                done[src] = str(out)
                log(f"  [{i}/{len(files)}] {os.path.basename(src)}: "
                    f"{len(pages)} page(s) in {time.time()-t:.1f}s")
            except Exception as e:
                log(f"  [{i}/{len(files)}] {os.path.basename(src)}: FAILED {e}")
    finally:
        # Hand the card back before anything tries to load the language model.
        try:
            ocr_app.stop_vllm()
        except Exception:
            pass
    return done, declined


def pdf_or_image_pages(src, work, dpi):
    """[(page_no, png_path)] for a PDF, or the image itself."""
    ext = Path(src).suffix.lower()
    if ext == ".pdf":
        import pdf_pages
        for pno, png, _info in pdf_pages.render_pdf(src, work, dpi=dpi):
            yield pno, png
    else:
        yield 1, str(src)


def read_group_files(records, ocr_map, log=print):
    """[(path, units)] for one group, plus a note of what could not be read."""
    files, unread = [], []
    for rec in records:
        p = rec["path"]
        cached = ocr_map.get(p) or (str(cache_path(p)) if cache_path(p).exists()
                                    else None)
        try:
            if cached:
                import save_output
                raw = Path(cached).read_text(encoding="utf-8")
                units = save_output.split_pages(raw)
                files.append((p, units))
                continue
            units = doc_text.read(p)
            if units:
                files.append((p, units))
        except doc_text.NeedsOCR as e:
            unread.append(f"{os.path.basename(p)} (needs OCR)")
        except ValueError as e:
            unread.append(f"{os.path.basename(p)} (no reader)")
        except Exception as e:
            unread.append(f"{os.path.basename(p)} ({type(e).__name__})")
    return files, unread


def parse_obj(reply):
    s = re.sub(r"^```(?:json)?|```$", "", (reply or "").strip(), flags=re.M).strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        v = json.loads(s[a:b + 1])
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def json_schema(fields):
    """A schema the server enforces, rather than an instruction it may ignore.

    Asking for "ONLY a JSON object" is not reliable: gpt-oss answered one
    bundle with "The total amount of the invoice is **$1,400.76**." -- correct,
    conversational, and useless to a spreadsheet. Constrained decoding removes
    the whole failure mode instead of retrying around it.
    """
    props = {f: {"type": ["string", "null"]} for f in fields}
    props["_sources"] = {"type": ["object", "null"]}
    props["_notes"] = {"type": ["string", "null"]}
    return {"type": "object", "properties": props, "required": list(fields)}


def call_model(model, bundle, fields, subject, num_ctx, timeout=1800,
               enforce_schema=True):
    body = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": SYSTEM.format(keys=", ".join(fields), subject=subject)},
            {"role": "user", "content": bundle},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_ctx": int(num_ctx)},
    }
    if enforce_schema:
        body["format"] = json_schema(fields)
    r = requests.post(f"{OLLAMA}/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "") or ""


def run_groups(root, fields, instruction="", exclude=(), model="gpt-oss:20b",
               depth=1, allow_ocr=False, char_budget=40000, num_ctx=16384,
               subject="item", dpi=300, log=print, on_row=None,
               confirm=None, page_limit=OCR_PAGE_LIMIT):
    """Yield one row per group. Phase 1 OCRs if allowed, phase 2 extracts."""
    records = list(corpus_scan.scan(root, depth=depth, allow_ocr=allow_ocr))
    by_group = {}
    for r in records:
        by_group.setdefault(r["group"], []).append(r)
    log(f"{len(records)} file(s) in {len(by_group)} group(s) under {root}")

    pixel = [r for r in records if r["route"] in ("ocr", "needs_ocr")]
    if pixel and not allow_ocr:
        log(f"{len(pixel)} file(s) hold their words as pixels and will be "
            f"skipped -- enable OCR to include them")

    ocr_map, declined = run_ocr_phase(ocr_needed(records, allow_ocr), dpi=dpi,
                                      log=log, confirm=confirm,
                                      page_limit=page_limit)

    per_field, loose, excl = group_select.build_terms(fields, instruction,
                                                      exclude=exclude)
    # Asking only for names and yes/no answers should apply no numeric bias.
    fig_w = group_select.figure_weight_for(fields)
    log(f"fields={len(fields)}, figure weight={fig_w:.2f} "
        f"({'numeric-leaning' if fig_w > 0.4 else 'text-leaning'} field set)")
    for gi, (group, recs) in enumerate(sorted(by_group.items()), 1):
        t0 = time.time()
        readable = [r for r in recs if r["route"] in ("text", "table", "doc", "ocr")]
        files, unread = read_group_files(readable, ocr_map, log=log)
        # Files left out because their words are pixels have to appear on the
        # row. Otherwise a folder whose key document was skipped looks exactly
        # like one whose documents simply did not mention the fields.
        for r in recs:
            if r["path"] in declined:
                unread.append(f"{os.path.basename(r['path'])} "
                              f"({r.get('pages')} pages, OCR declined)")
            elif r["route"] == "needs_ocr":
                unread.append(f"{os.path.basename(r['path'])} (needs OCR, not enabled)")
            elif r["route"] == "unsupported":
                unread.append(f"{os.path.basename(r['path'])} ({r['ext']} unsupported)")
        row = {"_group": group, "_files": len(recs)}
        if not files:
            row["_error"] = f"nothing readable ({', '.join(unread) or 'no files'})"
            log(f"[{gi}/{len(by_group)}] {group}: nothing readable")
            if on_row:
                on_row(row)
            yield row
            continue

        bundle, prov = group_select.select_for_group(
            files, per_field, loose, excl, char_budget=char_budget,
            figure_weight=fig_w)
        log(f"[{gi}/{len(by_group)}] {group}: {len(files)} file(s), "
            f"{len(prov)} excerpt(s), {len(bundle)} chars -> {model}")

        try:
            reply = call_model(model, bundle, fields, subject, num_ctx)
            obj = parse_obj(reply)
            err = None if obj is not None else "reply was not JSON"
        except Exception as e:
            obj, err = None, f"{type(e).__name__}: {e}"

        if obj is None:
            row["_error"] = err
        else:
            srcs = obj.pop("_sources", None)
            notes = obj.pop("_notes", None)
            for f in fields:
                row[f] = obj.get(f)
            for k, v in obj.items():
                row.setdefault(k, v)
            row["_sources"] = json.dumps(srcs, ensure_ascii=False) if srcs else None
            row["_notes"] = notes
        row["_excerpts"] = "; ".join(f"{p['file']}|{p['label']}" for p in prov[:12])
        if unread:
            row["_unread"] = "; ".join(unread)
        found = sum(1 for f in fields if row.get(f) not in (None, "", "null"))
        log(f"          {found}/{len(fields)} field(s) in {time.time()-t0:.1f}s"
            + (f"  [{row['_error']}]" if row.get("_error") else ""))
        if on_row:
            on_row(row)
        yield row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--fields", required=True,
                    help="comma separated, or a path to a file with one per line")
    ap.add_argument("--instruction", default="")
    ap.add_argument("--exclude", default="", help="comma separated hints to avoid")
    ap.add_argument("--subject", default="item",
                    help="what one folder represents, e.g. loan / property")
    ap.add_argument("--model", default="gpt-oss:20b")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--allow-ocr", action="store_true")
    ap.add_argument("--ocr-page-limit", type=int, default=OCR_PAGE_LIMIT,
                    help="ask before OCRing anything longer than this "
                         f"(default {OCR_PAGE_LIMIT})")
    ap.add_argument("--ocr-large", choices=["ask", "yes", "no"], default="ask",
                    help="what to do with files over the limit: ask at the "
                         "terminal, always convert, or always skip")
    ap.add_argument("--budget", type=int, default=40000)
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--out", default="group_rows.csv")
    args = ap.parse_args()

    if os.path.exists(args.fields):
        fields = [l.strip() for l in open(args.fields, encoding="utf-8")
                  if l.strip()]
    else:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    exclude = [e.strip() for e in args.exclude.split(",") if e.strip()]

    import pandas as pd
    rows = []

    def flush(row):
        rows.append(row)
        pd.DataFrame(rows).to_csv(args.out, index=False, encoding="utf-8-sig")

    t0 = time.time()
    confirm = {"ask": confirm_interactive,
               "yes": lambda rec: True,
               "no": lambda rec: False}[args.ocr_large]

    for _ in run_groups(args.root, fields, args.instruction, exclude,
                        model=args.model, depth=args.depth,
                        allow_ocr=args.allow_ocr, char_budget=args.budget,
                        num_ctx=args.ctx, subject=args.subject, on_row=flush,
                        confirm=confirm, page_limit=args.ocr_page_limit):
        pass
    print(f"\n{len(rows)} row(s) in {time.time()-t0:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
