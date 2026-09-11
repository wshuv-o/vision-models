"""Stage 3: pull a handful of fields out of many long appraisal reports.

These reports run to ~190 pages and only a few of them matter. Sending whole
documents is impractical, so each PDF is scored page by page from its own text
layer, the best summary pages are bundled, and the model sees ~23k characters
instead of 190 pages -- one call per document, one row per document.

Retrieval is keyword/number based rather than embedding based, deliberately.
"direct capitalization" scores highest on methodology prose that contains no
figures, while the page that actually holds the rate never uses the phrase, so
semantic similarity retrieves the wrong pages. See field_retrieve.py.

    ./.venv/Scripts/python appraisal_ui.py     # http://127.0.0.1:7862
"""
import io
import json
import os
import re
import time

import gradio as gr
import pandas as pd
import requests

import field_retrieve as F
from extract_ui import (MODELS, OLLAMA, canonicalise_keys, free_ocr_server,
                        gpu_used_mb, vllm_running, wsl_running)

_here = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_here, "outputs")

DEFAULT_FIELDS = """property_name
property_address
property_type
year_built
year_renovated
as_is_value
date_of_value
direct_capitalization_rate"""

SYSTEM = """You read commercial real-estate appraisal reports.

You are given a few pages from ONE appraisal. Extract facts about THE SUBJECT
PROPERTY being appraised.

Critical rules:
- Take figures ONLY from summary/conclusion tables about the subject property.
- NEVER take a figure from a comparable sale, a rent comparable, a listing or
  an adjustment grid. Those describe OTHER properties. A report contains many
  capitalisation rates and nearly all of them belong to comparables.
- The direct capitalisation rate is the subject's own going-in / year-one
  overall rate. Do not report a terminal, reversion or discount rate for it.
- Prefer the "As Is" figure over "Upon Stabilization" or prospective ones.
- Use null when the report does not state it. Never guess or infer.
- Copy values exactly as printed, including $ , % and the date format.

Reply with ONLY a JSON object, no prose and no code fences, with exactly these
keys: {keys}. Add one extra key "source_pages": a list of the PAGE numbers you
took the figures from."""


def parse_obj(reply):
    """Pull a single JSON object out of the model's reply."""
    s = re.sub(r"^```(?:json)?|```$", "", (reply or "").strip(), flags=re.M).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        val = json.loads(s[start:end + 1])
    except Exception:
        return None
    return val if isinstance(val, dict) else None


def call_model(model, bundle, fields, num_ctx, timeout=1800):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM.format(keys=", ".join(fields))},
            {"role": "user", "content": bundle},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_ctx": int(num_ctx)},
    }
    r = requests.post(f"{OLLAMA}/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "") or ""


def _paths(file_obj):
    if not file_obj:
        return []
    items = file_obj if isinstance(file_obj, (list, tuple)) else [file_obj]
    return [str(getattr(i, "name", i)) for i in items if i]


def run(files, fields_text, model, top_k, char_budget, num_ctx,
        progress=gr.Progress()):
    """Yields (status, dataframe, raw_replies) after each PDF."""
    paths = _paths(files)
    log, empty = [], pd.DataFrame()

    def status(msg):
        log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        return "\n".join(log)

    if not paths:
        yield "Upload one or more PDFs first.", empty, ""
        return
    fields = [f.strip() for f in (fields_text or "").splitlines() if f.strip()]
    if not fields:
        yield "List at least one field to extract.", empty, ""
        return

    yield (status(f"{len(paths)} PDF(s), {len(fields)} field(s), model={model}"),
           empty, "")

    if vllm_running() or wsl_running():
        yield (status(f"stage 1 still holds the GPU ({gpu_used_mb()}MB); "
                      f"shutting down its WSL session"), empty, "")
        before, after = free_ocr_server()
        yield status(f"GPU freed: {before}MB -> {after}MB"), empty, ""

    rows, replies, canon = [], [], {}

    # A 70-PDF batch runs for minutes, and until now the results existed only
    # in the browser until you pressed Save. Write every row to disk as it
    # lands so a dropped connection costs you the remaining files, not all of
    # them.
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    live_csv = os.path.join(
        OUTPUT_DIR, f"appraisal_live_{time.strftime('%Y%m%d_%H%M%S')}.csv")

    def add(row):
        rows.append(row)
        try:
            pd.DataFrame(rows).to_csv(live_csv, index=False, encoding="utf-8-sig")
        except Exception as e:
            print(f"live csv write failed: {type(e).__name__}: {e}", flush=True)

    yield status(f"writing results as they finish -> {live_csv}"), empty, ""
    t_all = time.time()
    for i, path in enumerate(paths, 1):
        name = os.path.basename(path)
        base = {"_file": name}

        t = time.time()
        try:
            pages = F.page_texts(path)
        except Exception as e:
            add({**base, "_error": f"unreadable: {type(e).__name__}: {e}"})
            yield (status(f"{name}: unreadable -- {e}"),
                   pd.DataFrame(rows), "\n\n".join(replies))
            continue

        if not F.text_layer_ok(pages):
            # Silently emitting nulls here would look like "no data in the
            # report" when the real problem is that it needs OCR first.
            add({**base, "_error": "no usable text layer - OCR this "
                                           "PDF in stage 1 first"})
            yield (status(f"{name}: no usable text layer, skipping "
                          f"(run it through the OCR page first)"),
                   pd.DataFrame(rows), "\n\n".join(replies))
            continue

        chosen, _top = F.select_pages(pages, top_k=int(top_k))
        bundle, used = F.build_bundle(pages, chosen, char_budget=int(char_budget))
        yield (status(f"{name}: {len(pages)} pages -> {used} "
                      f"({len(bundle)} chars) in {time.time()-t:.1f}s, asking "
                      f"{model} ..."),
               pd.DataFrame(rows), "\n\n".join(replies))

        t = time.time()
        try:
            reply = call_model(model, bundle, fields, num_ctx)
            err = None
        except Exception as e:
            reply, err = "", f"{type(e).__name__}: {e}"
        el = time.time() - t

        if err:
            add({**base, "_error": err})
            yield (status(f"{name}: FAILED after {el:.1f}s -- {err}"),
                   pd.DataFrame(rows), "\n\n".join(replies))
            continue

        replies.append(f"===== {name} =====\n{reply}")
        obj = parse_obj(reply)
        if obj is None:
            add({**base, "_error": "reply was not JSON (see raw output)"})
            yield (status(f"{name}: {el:.1f}s but reply was not JSON"),
                   pd.DataFrame(rows), "\n\n".join(replies))
            continue

        src = obj.pop("source_pages", None)
        obj = canonicalise_keys(obj, canon)
        row = {**base, **{k: obj.get(k) for k in fields if k in obj}}
        for k, v in obj.items():
            row.setdefault(k, v)
        row["_source_pages"] = ", ".join(str(x) for x in src) if isinstance(src, list) else src
        row["_retrieved_pages"] = ", ".join(str(x) for x in used)
        add(row)

        found = sum(1 for k in fields if row.get(k) not in (None, "", "null"))
        progress(i / len(paths), desc=name)
        yield (status(f"{name}: {el:.1f}s, {found}/{len(fields)} field(s) found "
                      f"(pages {row['_source_pages']})"),
               pd.DataFrame(rows), "\n\n".join(replies))

    el = time.time() - t_all
    df = pd.DataFrame(rows)
    yield (status(f"DONE: {len(rows)} row(s) from {len(paths)} PDF(s) in "
                  f"{el:.1f}s ({el/max(len(paths),1):.1f}s/PDF)"),
           df, "\n\n".join(replies))


def save_table(df):
    if df is None or (hasattr(df, "empty") and df.empty):
        return gr.update(value=None), "Nothing to save yet."
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = f"appraisal_fields_{time.strftime('%Y%m%d_%H%M%S')}"
    out = []
    csv = os.path.join(OUTPUT_DIR, f"{stem}.csv")
    df.to_csv(csv, index=False, encoding="utf-8-sig")
    out.append(csv)
    try:
        xlsx = os.path.join(OUTPUT_DIR, f"{stem}.xlsx")
        df.to_excel(xlsx, index=False)
        out.append(xlsx)
    except Exception as e:
        print(f"xlsx skipped: {type(e).__name__}: {e}", flush=True)
    return gr.update(value=out), "Saved:\n" + "\n".join("  " + f for f in out)


def _serve_opts():
    """Where to listen, and whether to demand a password.

    Defaults to localhost: these pages have no auth of their own and accept
    file uploads, so binding wider has to be a deliberate act. Set VM_HOST to
    0.0.0.0 to reach them from another machine, and set VM_USER/VM_PASS unless
    the network is one you fully trust.
    """
    host = os.environ.get("VM_HOST", "127.0.0.1")
    user, password = os.environ.get("VM_USER"), os.environ.get("VM_PASS")
    auth = (user, password) if user and password else None
    share = os.environ.get("VM_SHARE", "").lower() in ("1", "true", "yes")
    if host != "127.0.0.1" and not auth:
        print("WARNING: listening on %s with no VM_USER/VM_PASS set -- anyone "
              "who can reach this port can upload files and read results"
              % host, flush=True)
    if share:
        # A share link is a tunnel through Gradio's relay, so uploads and
        # results leave this machine even though the model does not. Worth
        # saying out loud in a project whose point is staying local.
        print("NOTE: VM_SHARE is on -- a public gradio.live URL will be created "
              "and traffic will pass through Gradio's servers", flush=True)
        if not auth:
            print("REFUSING to open a public link with no password; set "
                  "VM_USER and VM_PASS", flush=True)
            share = False
    return host, auth, share


with gr.Blocks(title="Appraisal field extraction") as demo:
    gr.Markdown(
        "# Appraisal field extraction\n"
        "Upload many long appraisal PDFs. Each one is scored page by page, the "
        "few summary pages that hold the answers are sent to the model, and you "
        "get **one row per PDF**. Needs a text layer - scanned PDFs should go "
        "through the OCR page first."
    )
    with gr.Row():
        with gr.Column(scale=1):
            files_in = gr.File(label="Appraisal PDFs", file_types=[".pdf"],
                               file_count="multiple")
            fields_in = gr.Textbox(label="Fields to extract (one per line)",
                                   value=DEFAULT_FIELDS, lines=9)
            model_in = gr.Dropdown(choices=MODELS, value="gpt-oss:20b",
                                   label="Model")
            with gr.Row():
                topk_in = gr.Slider(3, 20, value=8, step=1,
                                    label="Candidate pages per PDF")
                ctx_in = gr.Slider(8192, 65536, value=16384, step=4096,
                                   label="Context tokens")
            budget_in = gr.Slider(8000, 80000, value=40000, step=4000,
                                  label="Character budget per PDF")
            run_btn = gr.Button("Run", variant="primary")
            save_btn = gr.Button("Save table (.csv / .xlsx)")
            files_out = gr.File(label="Download", file_count="multiple")
        with gr.Column(scale=2):
            status_out = gr.Textbox(label="Progress (live)", lines=12,
                                    max_lines=12, autoscroll=True)
            table_out = gr.Dataframe(label="Extracted fields", wrap=True)
            raw_out = gr.Textbox(label="Raw model replies", lines=12)

    run_btn.click(run,
                  inputs=[files_in, fields_in, model_in, topk_in, budget_in,
                          ctx_in],
                  outputs=[status_out, table_out, raw_out])
    save_btn.click(save_table, inputs=[table_out],
                   outputs=[files_out, status_out])

if __name__ == "__main__":
    _host, _auth, _share = _serve_opts()
    demo.queue().launch(server_name=_host, server_port=7862,
                        inbrowser=(_host == "127.0.0.1"), auth=_auth,
                        share=_share)
