"""Stage 2: turn recognised pages into rows.

Takes the output of the OCR stage (.md / .html / .xlsx), sends one full page at
a time to a local LLM together with your instruction, and collects whatever the
model extracts into a single table -- one row per record, one column per field.

Runs entirely against Ollama on 127.0.0.1:11434. No GPU is held by this process
itself; the model is loaded and served by Ollama.

    ./.venv/Scripts/python extract_ui.py
"""
import io
import json
import os
import re
import time

import gradio as gr
import pandas as pd
import requests

import pdf_pages
import save_output

_here = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_here, "outputs")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

# Most capable first -- gemma4 is 25.2B against gpt-oss's 20.9B.
MODELS = [
    "gemma4-32k:latest",
    "gemma4:26b",
    "gpt-oss-128k:latest",
    "gpt-oss-64k:latest",
    "gpt-oss:20b",
    "qwen2.5:14b-instruct",
]

SYSTEM = """You extract structured data from one page of a document.

Rules:
- Reply with ONLY a JSON array of objects. No prose, no markdown fences.
- Each object is one row. Use the SAME keys for every object.
- Keep values exactly as they appear on the page; do not reformat numbers,
  dates or currency, and do not invent values.
- Use null for a field that is genuinely absent on this page.
- If the page contains nothing matching the instruction, reply with []."""

HTML_PAGE_RE = re.compile(r"<h2>\s*Page\s+(\d+)\s*</h2>", re.I)


def _collapse_runs(cells):
    """Squash runs of the same value down to one.

    A cell spanning N columns in the source table comes back as N identical
    cells when the HTML is read into a sheet, so a header row arrives as the
    same string repeated nineteen times. That is pure token cost, and it hides
    the row structure from the model rather than showing it.
    """
    out = []
    for c in cells:
        c = c.strip()
        if not out or out[-1] != c:
            out.append(c)
    while out and out[-1] == "":
        out.pop()
    return out


# --------------------------------------------------------------- page loading
def load_pages(path):
    """[(label, text)] -- one entry per page of the uploaded document."""
    ext = os.path.splitext(str(path))[1].lower()

    if ext in (".xlsx", ".xls", ".xlsm"):
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
        out = []
        for name, df in sheets.items():
            df = df.fillna("")
            rows = []
            for row in df.itertuples(index=False):
                rows.append("\t".join(_collapse_runs([str(c) for c in row])))
            out.append((str(name), "\n".join(rows)))
        return out

    raw = io.open(path, encoding="utf-8", errors="replace").read()

    if ext in (".html", ".htm"):
        parts = HTML_PAGE_RE.split(raw)
        if len(parts) >= 3:
            return [(f"page {parts[i]}", parts[i + 1])
                    for i in range(1, len(parts) - 1, 2)]
        return [("page 1", raw)]

    # .md / .txt -- the OCR stage's page markers
    return [(f"page {no}", body) for no, body in save_output.split_pages(raw)]


# ------------------------------------------------------------------ llm calls
def call_llm(model, instruction, page_text, num_ctx, timeout=1800):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user",
             "content": f"{instruction.strip()}\n\n--- PAGE ---\n{page_text}"},
        ],
        "stream": False,
        # Deterministic: the OCR stage drifted a few percent between identical
        # runs and that is not something you want in extracted figures.
        "options": {"temperature": 0, "num_ctx": int(num_ctx)},
    }
    r = requests.post(f"{OLLAMA}/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return (data.get("message") or {}).get("content", "") or ""


def parse_rows(reply):
    """Pull a JSON array of objects out of the model's reply."""
    s = (reply or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I | re.M).strip()
    try:
        val = json.loads(s)
    except Exception:
        start, end = s.find("["), s.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            val = json.loads(s[start:end + 1])
        except Exception:
            return None
    if isinstance(val, dict):
        val = [val]
    if not isinstance(val, list):
        return None
    return [v for v in val if isinstance(v, dict)]


# ------------------------------------------------------------------ the run
def run(file_obj, instruction, model, pages_spec, num_ctx, add_page_col,
        progress=gr.Progress()):
    """Yields (status, dataframe, raw_replies) after every page."""
    path = getattr(file_obj, "name", file_obj)
    log, empty = [], pd.DataFrame()

    def status(msg):
        log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        return "\n".join(log)

    if not path:
        yield "Upload a .md / .html / .xlsx file first.", empty, ""
        return
    if not (instruction or "").strip():
        yield "Enter an extraction instruction first.", empty, ""
        return

    try:
        pages = load_pages(path)
    except Exception as e:
        yield status(f"could not read file: {type(e).__name__}: {e}"), empty, ""
        return

    if pages_spec and pages_spec.strip():
        idx = pdf_pages.parse_pages(pages_spec, len(pages))
        pages = [pages[i] for i in idx]

    yield (status(f"{os.path.basename(str(path))}: {len(pages)} page(s), "
                  f"model={model}, ctx={int(num_ctx)}"), empty, "")

    rows, replies = [], []
    t_all = time.time()
    for i, (label, text) in enumerate(pages, 1):
        yield (status(f"{label}: sending {len(text)} chars to {model} ..."),
               pd.DataFrame(rows), "\n\n".join(replies))
        t = time.time()
        try:
            reply = call_llm(model, instruction, text, num_ctx)
            err = None
        except Exception as e:
            reply, err = "", f"{type(e).__name__}: {e}"
        el = time.time() - t

        if err:
            yield status(f"{label}: FAILED after {el:.1f}s -- {err}"), \
                  pd.DataFrame(rows), "\n\n".join(replies)
            continue

        replies.append(f"===== {label} =====\n{reply}")
        parsed = parse_rows(reply)
        if parsed is None:
            yield (status(f"{label}: {el:.1f}s but reply was not JSON "
                          f"(see raw output below)"),
                   pd.DataFrame(rows), "\n\n".join(replies))
            continue

        for r in parsed:
            if add_page_col:
                r = {"_page": label, **r}
            rows.append(r)
        progress(i / len(pages), desc=label)
        yield (status(f"{label}: {el:.1f}s, +{len(parsed)} row(s) "
                      f"({len(rows)} total)"),
               pd.DataFrame(rows), "\n\n".join(replies))

    el = time.time() - t_all
    df = pd.DataFrame(rows)
    yield (status(f"DONE: {len(rows)} row(s) x {len(df.columns)} column(s) "
                  f"from {len(pages)} page(s) in {el:.1f}s "
                  f"({el / max(len(pages), 1):.1f}s/page)"),
           df, "\n\n".join(replies))


def save_table(df, file_obj):
    if df is None or len(df) == 0:
        return gr.update(value=None), "Nothing to save yet."
    src = getattr(file_obj, "name", file_obj) or "extracted"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(
        os.path.basename(str(src)))[0])
    base = os.path.join(OUTPUT_DIR,
                        f"{stem}_extracted_{time.strftime('%Y%m%d_%H%M%S')}")
    out = []
    csv_path = base + ".csv"
    df.to_csv(csv_path, index=False)
    out.append(csv_path)
    try:
        xlsx_path = base + ".xlsx"
        df.to_excel(xlsx_path, index=False)
        out.append(xlsx_path)
    except Exception as e:
        print(f"xlsx failed: {e}", flush=True)
    return gr.update(value=out), "Saved:\n" + "\n".join("  " + p for p in out)


with gr.Blocks(title="Extract values from recognised pages") as demo:
    gr.Markdown(
        "# Extract to columns\n"
        "Upload the OCR stage's output (`.md`, `.html` or `.xlsx`). Each page is "
        "sent **whole**, one per iteration, to a local model along with your "
        "instruction. Extracted records are collected into one table."
    )
    with gr.Row():
        with gr.Column(scale=1):
            file_in = gr.File(label="Recognised document (.md / .html / .xlsx)",
                              file_types=[".md", ".html", ".htm", ".xlsx", ".xls",
                                          ".txt"])
            instruction_in = gr.Textbox(
                label="Extraction instruction (sent with every page)",
                lines=6,
                placeholder="e.g. Extract every lease row. Columns: tenant name, "
                            "suite, area sqft, lease from, lease to, rent psf.",
            )
            model_in = gr.Dropdown(choices=MODELS, value=MODELS[0], label="Model")
            with gr.Row():
                pages_in = gr.Textbox(label="Pages", value="",
                                      placeholder="all, or 1-3 / 1,4")
                ctx_in = gr.Slider(8192, 131072, value=32768, step=8192,
                                   label="Context tokens (num_ctx)")
            page_col_in = gr.Checkbox(value=True, label="Add a _page column")
            run_btn = gr.Button("Run", variant="primary")
            save_btn = gr.Button("Save table (.csv / .xlsx)")
            files_out = gr.File(label="Download", file_count="multiple")
        with gr.Column(scale=2):
            status_out = gr.Textbox(label="Progress (live)", lines=10,
                                    max_lines=10, autoscroll=True)
            table_out = gr.Dataframe(label="Extracted rows", wrap=True,
                                     interactive=False)
            raw_out = gr.Textbox(label="Raw model replies", lines=12)

    run_btn.click(
        run,
        inputs=[file_in, instruction_in, model_in, pages_in, ctx_in, page_col_in],
        outputs=[status_out, table_out, raw_out],
    )
    save_btn.click(save_table, inputs=[table_out, file_in],
                   outputs=[files_out, status_out])

if __name__ == "__main__":
    demo.queue().launch(server_name="127.0.0.1", server_port=7861, inbrowser=True)
