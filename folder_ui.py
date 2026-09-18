"""Point at a folder, get one row per sub-folder.

Two steps on purpose. Scan first: it reports what is in the tree, what it can
read, and what would have to be OCR'd, without starting anything. Only then do
you run it. A long OCR job should be a decision someone made looking at the
numbers, not a surprise that starts when a button is pressed.

    ./.venv/Scripts/python folder_ui.py      # http://127.0.0.1:7863
"""
import os
import time

import gradio as gr
import pandas as pd

import corpus_scan
import group_extract as GE
from extract_ui import MODELS, free_ocr_server, gpu_used_mb, vllm_running, wsl_running

_here = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_here, "outputs")

EXAMPLE_FIELDS = """loan_amount
interest_rate_index
interest_rate_spread
interest_only
sponsor
borrower
acquisition_or_refinance"""


def _fmt_bytes(n):
    return f"{n/1e9:.1f}GB" if n >= 1e9 else f"{n/1e6:.0f}MB"


def resolve_folder(raw):
    """(path, error). Says what is actually wrong rather than 'not a folder'.

    The path is opened by the machine running this app, not by the browser. A
    path typed on another computer refers to a folder this process cannot see,
    and that is by far the most common reason this fails.
    """
    if not raw or not str(raw).strip():
        return None, "Enter a folder path."

    p = str(raw).strip()
    for q in ('"', "'"):
        if p.startswith(q) and p.endswith(q):
            p = p[1:-1]
    p = os.path.expandvars(os.path.expanduser(p.strip()))
    p = p.replace("/", os.sep) if os.sep == "\\" else p
    p = p.rstrip("\\/") or p          # a trailing slash is harmless, strip it

    if os.path.isdir(p):
        return p, None
    if os.path.isfile(p):
        return None, (f"That is a file, not a folder:\n  {p}\n\n"
                      "Give the folder that CONTAINS your sub-folders.")

    # Walk back up to find the deepest part that does exist -- usually makes
    # the typo or the missing drive obvious immediately.
    probe, missing = p, []
    while probe and not os.path.exists(probe):
        probe, tail = os.path.split(probe)
        if not tail:
            break
        missing.insert(0, tail)
    hint = ""
    if probe and os.path.isdir(probe):
        try:
            nearby = sorted(os.listdir(probe))[:8]
            hint = (f"\n\nThis part exists:\n  {probe}\n"
                    f"but '{missing[0] if missing else '?'}' is not in it. "
                    f"It contains:\n  " + "\n  ".join(nearby))
        except OSError:
            hint = f"\n\nThis part exists: {probe}"
    else:
        hint = ("\n\nNone of that path exists on the machine running this app. "
                "If you are using this from another computer, the path must be "
                "one THIS machine can see -- a local path on the server, or a "
                "network share like \\\\server\\share.")
    return None, f"Not a folder:\n  {p}{hint}"


UPLOAD_DIR = os.path.join(_here, "uploads")
_zip_cache = {}


def _safe_members(zf, dest):
    """Yield members that stay inside dest.

    A zip entry can name ../../etc or an absolute path, and extractall will
    happily follow it. Files arriving over the network get checked.
    """
    dest = os.path.realpath(dest)
    for m in zf.infolist():
        name = m.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            continue
        if os.path.isabs(name) or (len(name) > 1 and name[1] == ":"):
            continue
        target = os.path.realpath(os.path.join(dest, name))
        if target == dest or target.startswith(dest + os.sep):
            yield m


def _descend_single(root):
    """Step through wrapper folders like om-tms-asr 22/om-tms-asr 22/.

    Zipping a folder usually nests it once, and sometimes twice. Without this,
    'which level names the row' silently points at the wrapper and every loan
    collapses into one row.
    """
    for _ in range(4):
        try:
            entries = [e for e in os.listdir(root) if not e.startswith((".", "__"))]
        except OSError:
            return root
        subdirs = [e for e in entries if os.path.isdir(os.path.join(root, e))]
        if len(entries) == 1 and len(subdirs) == 1:
            root = os.path.join(root, subdirs[0])
        else:
            return root
    return root


def extract_zip(zip_path):
    """(root, note). Extracted once per upload and reused."""
    import zipfile

    zip_path = str(getattr(zip_path, "name", zip_path))
    key = (zip_path, os.path.getsize(zip_path))
    if key in _zip_cache and os.path.isdir(_zip_cache[key]):
        return _zip_cache[key], "using the already-extracted copy"

    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(UPLOAD_DIR, f"zip_{stamp}")
    os.makedirs(dest, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        members = list(_safe_members(zf, dest))
        skipped = len(zf.infolist()) - len(members)
        for m in members:
            zf.extract(m, dest)
            n += 1
    root = _descend_single(dest)
    note = f"unpacked {n} entr{'y' if n == 1 else 'ies'}"
    if skipped:
        note += f" ({skipped} unsafe path(s) skipped)"
    if root != dest:
        note += f"; using inner folder '{os.path.basename(root)}'"
    _zip_cache[key] = root
    return root, note


def _pick_root(root_text, zip_file):
    """The zip wins when one is uploaded; otherwise the typed path."""
    if zip_file:
        try:
            root, note = extract_zip(zip_file)
            return root, None, note
        except Exception as e:
            return None, f"Could not read that zip: {type(e).__name__}: {e}", ""
    root, err = resolve_folder(root_text)
    return root, err, ""


def scan_folder(root, zip_file, depth, allow_ocr, page_limit):
    """Report what is there. Starts nothing, loads nothing."""
    root, err, note = _pick_root(root, zip_file)
    if err:
        return err, gr.update(choices=[], value=[]), pd.DataFrame()

    t0 = time.time()
    records = list(corpus_scan.scan(root, depth=int(depth), allow_ocr=allow_ocr))
    if not records:
        return "No files found under that folder.", gr.update(choices=[], value=[]), pd.DataFrame()

    s = corpus_scan.summarise(records)
    lines = []
    if note:
        lines += [f"zip: {note}", f"root: {root}", ""]
    lines += [f"{s['n_files']} file(s) in {len(s['groups'])} group(s), "
             f"{_fmt_bytes(s['total_bytes'])}, {s['total_pages']} PDF page(s)",
             f"routes: {s['routes']}", ""]

    rows = []
    for g, recs in sorted(s["groups"].items()):
        by_route = {}
        for r in recs:
            by_route[r["route"]] = by_route.get(r["route"], 0) + 1
        rows.append({"group": g, "files": len(recs),
                     "breakdown": ", ".join(f"{k}:{v}" for k, v in sorted(by_route.items())),
                     "pages": sum(r.get("pages") or 0 for r in recs)})

    # What OCR would cost, and which files are big enough to be worth asking about.
    pixel = [r for r in records if r["route"] in ("ocr", "needs_ocr")]
    cached = [r for r in pixel if GE.cache_path(r["path"]).exists()]
    todo = [r for r in pixel if r not in cached]
    small, large = GE.split_by_size(todo, int(page_limit))

    if not pixel:
        lines.append("Nothing needs OCR - every file has readable text.")
    else:
        lines.append(f"{len(pixel)} file(s) hold their words as pixels:")
        if cached:
            lines.append(f"  {len(cached)} already converted (cached, free to reuse)")
        if not allow_ocr:
            lines.append("  OCR is OFF - these will be skipped and named in the "
                         "row. Tick 'Allow OCR' to include them.")
        else:
            secs = sum((r.get("pages") or 1) for r in small) * 2.8
            lines.append(f"  {len(small)} under {page_limit} pages: will convert "
                         f"automatically (~{secs:.0f}s)")
            if large:
                lines.append(f"  {len(large)} over {page_limit} pages: tick below "
                             f"to include, otherwise skipped")

    choices = []
    for r in large:
        pages = r.get("pages") or 1
        mins = pages * 2.8 / 60
        est = f"{pages*2.8:.0f}s" if mins < 1 else f"{mins:.1f} min"
        choices.append(f"{r['path']}  ({pages} pages, ~{est})")

    lines.append("")
    lines.append(f"scanned in {time.time()-t0:.1f}s")
    return "\n".join(lines), gr.update(choices=choices, value=[]), pd.DataFrame(rows)


def run_folder(root, zip_file, depth, fields_text, instruction, exclude_text,
               subject, model, allow_ocr, page_limit, approved_large, budget,
               num_ctx, progress=gr.Progress()):
    """Stream a row per group."""
    log, empty = [], pd.DataFrame()

    def status(msg):
        log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        return "\n".join(log)

    root, err, note = _pick_root(root, zip_file)
    if err:
        yield err, empty
        return
    if note:
        yield status(f"zip: {note}"), empty
    fields = [f.strip() for f in (fields_text or "").splitlines() if f.strip()]
    if not fields:
        yield "List at least one field to extract.", empty
        return
    exclude = [e.strip() for e in (exclude_text or "").splitlines() if e.strip()]

    # Only the large files ticked in the scan step get converted.
    approved = {c.split("  (")[0] for c in (approved_large or [])}

    def confirm(rec):
        return rec["path"] in approved

    if vllm_running() or wsl_running():
        yield status(f"stage 1 still holds the GPU ({gpu_used_mb()}MB); freeing"), empty
        before, after = free_ocr_server()
        yield status(f"GPU freed: {before}MB -> {after}MB"), empty

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    live = os.path.join(OUTPUT_DIR,
                        f"folder_rows_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    rows = []

    def on_row(row):
        rows.append(row)
        try:
            pd.DataFrame(rows).to_csv(live, index=False, encoding="utf-8-sig")
        except Exception as e:
            print(f"live csv failed: {e}", flush=True)

    yield status(f"writing rows as they finish -> {live}"), empty

    msgs = []

    def log_cb(m):
        msgs.append(str(m))

    t0 = time.time()
    gen = GE.run_groups(root, fields, instruction, exclude, model=model,
                        depth=int(depth), allow_ocr=allow_ocr,
                        char_budget=int(budget), num_ctx=int(num_ctx),
                        subject=subject or "item", log=log_cb, on_row=on_row,
                        confirm=confirm, page_limit=int(page_limit))
    try:
        for _row in gen:
            while msgs:
                status(msgs.pop(0))
            progress(len(rows) / max(len(rows) + 1, 1))
            yield "\n".join(log), pd.DataFrame(rows)
    except Exception as e:
        import traceback
        traceback.print_exc()          # the status line alone hides where it came from
        while msgs:
            status(msgs.pop(0))
        yield status(f"ERROR: {type(e).__name__}: {e}"), pd.DataFrame(rows)
        return

    while msgs:
        status(msgs.pop(0))
    el = time.time() - t0
    yield (status(f"DONE: {len(rows)} row(s) in {el:.1f}s "
                  f"({el/max(len(rows),1):.1f}s/group) -> {live}"),
           pd.DataFrame(rows))


def save_table(df):
    if df is None or (hasattr(df, "empty") and df.empty):
        return gr.update(value=None), "Nothing to save yet."
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = f"folder_extract_{time.strftime('%Y%m%d_%H%M%S')}"
    out = []
    csv = os.path.join(OUTPUT_DIR, f"{stem}.csv")
    df.to_csv(csv, index=False, encoding="utf-8-sig")
    out.append(csv)
    try:
        x = os.path.join(OUTPUT_DIR, f"{stem}.xlsx")
        df.to_excel(x, index=False)
        out.append(x)
    except Exception as e:
        print(f"xlsx skipped: {e}", flush=True)
    return gr.update(value=out), "Saved:\n" + "\n".join("  " + f for f in out)


def _serve_opts():
    host = os.environ.get("VM_HOST", "127.0.0.1")
    user, password = os.environ.get("VM_USER"), os.environ.get("VM_PASS")
    auth = (user, password) if user and password else None
    share = os.environ.get("VM_SHARE", "").lower() in ("1", "true", "yes")
    if host != "127.0.0.1" and not auth:
        print(f"WARNING: listening on {host} with no VM_USER/VM_PASS set",
              flush=True)
    if share and not auth:
        print("REFUSING a public link with no password; set VM_USER/VM_PASS",
              flush=True)
        share = False
    return host, auth, share


with gr.Blocks(title="Folder extraction") as demo:
    gr.Markdown(
        "# Folder extraction\n"
        "Give it a folder. Each **sub-folder** is one thing - a loan, a "
        "property, a case - and you get **one row per sub-folder**, drawn from "
        "every document inside it.\n\n"
        "**Scan first.** Nothing is read, converted or loaded until you press Run."
    )
    with gr.Row():
        with gr.Column(scale=1):
            root_in = gr.Textbox(
                label="Folder path (on the machine running this app)",
                placeholder=r"D:\loans")
            zip_in = gr.File(
                label="...or upload a .zip of that folder (keeps sub-folders)",
                file_types=[".zip"])
            with gr.Row():
                depth_in = gr.Slider(0, 3, value=1, step=1,
                                     label="Which level names the row")
                subject_in = gr.Textbox(label="Each folder is a...", value="loan")
            fields_in = gr.Textbox(label="Fields to extract (one per line)",
                                   value=EXAMPLE_FIELDS, lines=8)
            instruction_in = gr.Textbox(label="Instruction (optional)", lines=2,
                                        placeholder="Extract the loan terms for this deal")
            exclude_in = gr.Textbox(label="Ignore passages mentioning (one per line)",
                                    lines=2, placeholder="comparable sale")
            model_in = gr.Dropdown(choices=MODELS, value="gpt-oss:20b", label="Model")
            with gr.Row():
                ocr_in = gr.Checkbox(value=False, label="Allow OCR")
                limit_in = gr.Slider(1, 100, value=10, step=1,
                                     label="Ask before OCR over N pages")
            with gr.Row():
                budget_in = gr.Slider(8000, 120000, value=40000, step=4000,
                                      label="Characters per group")
                ctx_in = gr.Slider(8192, 65536, value=16384, step=4096,
                                   label="Context tokens")
            scan_btn = gr.Button("1. Scan folder")
            large_in = gr.CheckboxGroup(
                choices=[], label="Large scanned files - tick any you want OCR'd",
                value=[])
            run_btn = gr.Button("2. Run", variant="primary")
            save_btn = gr.Button("Save table (.csv / .xlsx)")
            files_out = gr.File(label="Download", file_count="multiple")
        with gr.Column(scale=2):
            status_out = gr.Textbox(label="Status", lines=14, max_lines=14,
                                    autoscroll=True)
            groups_out = gr.Dataframe(label="What is in the folder", wrap=True)
            table_out = gr.Dataframe(label="Extracted rows", wrap=True)

    scan_btn.click(scan_folder,
                   inputs=[root_in, zip_in, depth_in, ocr_in, limit_in],
                   outputs=[status_out, large_in, groups_out])
    run_btn.click(run_folder,
                  inputs=[root_in, zip_in, depth_in, fields_in, instruction_in,
                          exclude_in, subject_in, model_in, ocr_in, limit_in,
                          large_in, budget_in, ctx_in],
                  outputs=[status_out, table_out])
    save_btn.click(save_table, inputs=[table_out], outputs=[files_out, status_out])

if __name__ == "__main__":
    _host, _auth, _share = _serve_opts()
    demo.queue().launch(server_name=_host, server_port=7863,
                        inbrowser=(_host == "127.0.0.1"), auth=_auth,
                        share=_share)
