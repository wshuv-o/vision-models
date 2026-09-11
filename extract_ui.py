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

# The OCR stage tags each heading with the page it came from; older files
# just have "<h2>Page N</h2>".
HTML_PAGE_RE = re.compile(
    r'<h2\s+data-page="([^"]*)"\s*>.*?</h2>|<h2>\s*(Page\s+\d+)\s*</h2>',
    re.I | re.S,
)


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
        marks = list(HTML_PAGE_RE.finditer(raw))
        if not marks:
            return [("page 1", raw)]
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            out.append((m.group(1) or m.group(2) or f"page {i + 1}",
                        raw[m.end():end]))
        return out

    # .md / .txt -- the OCR stage's page markers
    return save_output.split_pages(raw)


# ------------------------------------------------------------------ llm calls
CTX_STEPS = [8192, 16384, 32768, 65536, 131072]

# Desktop apps alone sit around 4-5GB on this box; above this something is
# still holding the card.
GPU_IDLE_CEILING_MB = 6000


def fit_ctx(page_text, instruction, ceiling):
    """Smallest sane context that still fits this page.

    num_ctx is an allocation request, not a limit: asking for 106k made Ollama
    demand a 7.5GB pinned host buffer for the CPU-offloaded layers and fail
    with "unable to allocate CUDA_Host buffer". Pages here are a few thousand
    characters, so size the window to the page and treat the slider as a cap.
    """
    # ~3 chars per token is deliberately pessimistic for table-heavy text.
    need = (len(page_text) + len(instruction) + len(SYSTEM)) / 3.0 + 1536
    ceiling = int(ceiling)
    for c in CTX_STEPS:
        if c >= need:
            return min(c, ceiling)
    return ceiling


def is_alloc_error(exc):
    body = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = (getattr(resp, "text", "") or "")
    text = f"{exc} {body}".lower()
    return ("unable to allocate" in text or "failed to allocate" in text
            or "out of memory" in text or "cuda_host" in text)


def gpu_used_mb():
    import subprocess
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return -1


def free_ocr_server():
    """Free the GPU that stage 1 is holding, and actually mean it.

    WSL2 does not hand GPU memory back to Windows when a process inside it
    exits -- only when the VM itself stops. Killing `vllm serve` therefore
    frees nothing as far as Ollama is concerned: the card stayed at 15.3GB and
    gemma4 had nowhere to load. The whole distro has to go down.

    The OCR app survives this: it detects the dead worker and restarts both it
    and the server on its next request.
    """
    import subprocess
    before = gpu_used_mb()
    for cmd in (["wsl.exe", "-e", "bash", "-lc",
                 "pkill -f 'vllm serve'; pkill -f paddleocr_worker"],
                ["wsl.exe", "--shutdown"]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
        except Exception:
            pass
    for _ in range(30):
        if gpu_used_mb() < GPU_IDLE_CEILING_MB:
            break
        time.sleep(1)
    return before, gpu_used_mb()


def vllm_running(url="http://localhost:8000/v1/models"):
    try:
        return requests.get(url, timeout=3).status_code == 200
    except Exception:
        return False


def wsl_running():
    """True if any WSL distro is up -- i.e. stage 1 may still hold the card.

    Checked instead of raw GPU usage, because once this stage loads its own
    model the card is legitimately full and re-freeing would be nonsense.
    `-l --running` does not start a distro, and prints UTF-16.
    """
    import subprocess
    try:
        out = subprocess.run(["wsl.exe", "-l", "--running", "--quiet"],
                             capture_output=True, timeout=30)
    except Exception:
        return False
    raw = out.stdout or b""
    for enc in ("utf-16-le", "utf-8"):
        try:
            text = raw.decode(enc, errors="ignore").replace("\x00", "")
            if text.strip():
                return True
        except Exception:
            continue
    return False


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
    ctx = int(num_ctx)
    last = None
    while True:
        body["options"]["num_ctx"] = ctx
        try:
            r = requests.post(f"{OLLAMA}/api/chat", json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return (data.get("message") or {}).get("content", "") or "", ctx
        except Exception as e:
            # An allocation failure is worth one more try at half the window;
            # anything else is a real error and should surface immediately.
            if not is_alloc_error(e) or ctx <= CTX_STEPS[0]:
                raise
            last, ctx = e, max(CTX_STEPS[0], ctx // 2)
            print(f"ctx {ctx * 2} failed to allocate, retrying at {ctx}",
                  flush=True)


def canonicalise_keys(row, canon):
    """Fold keys that differ only in case/spacing onto one column.

    Models are not consistent between calls: gpt-oss returned Description /
    Amount for one page and description / amount for the next, which split a
    3-column table into 5. The first spelling seen wins, so the output keeps
    whatever the model called it first.
    """
    out = {}
    for k, v in row.items():
        norm = re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")
        out[canon.setdefault(norm or "field", str(k))] = v
    return out


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
                  f"model={model}, ctx<={int(num_ctx)} (sized per page)"),
           empty, "")

    # This model does not fit beside the OCR stage on a 16GB card. Check the
    # card itself, not just the server: WSL keeps holding the memory after
    # vllm exits, so the port can be dead while the GPU is still full.
    if vllm_running() or wsl_running():
        yield (status(f"stage 1 still holds the GPU ({gpu_used_mb()}MB); "
                      f"shutting down its WSL session"), empty, "")
        before, after = free_ocr_server()
        if after > GPU_IDLE_CEILING_MB:
            yield (status(f"warning: GPU still at {after}MB -- the model may "
                          f"fail to load"), empty, "")
        else:
            yield status(f"GPU freed: {before}MB -> {after}MB"), empty, ""

    rows, replies, canon = [], [], {}
    t_all = time.time()
    for i, (label, text) in enumerate(pages, 1):
        ctx = fit_ctx(text, instruction, num_ctx)
        yield (status(f"{label}: sending {len(text)} chars to {model} "
                      f"(ctx {ctx}) ..."),
               pd.DataFrame(rows), "\n\n".join(replies))
        t = time.time()
        try:
            reply, ctx = call_llm(model, instruction, text, ctx)
            err = None
        except Exception as e:
            reply, err = "", f"{type(e).__name__}: {e}"
            if is_alloc_error(e):
                err += ("  -- the model could not be allocated even at the "
                        "smallest context; close other GPU users or pick a "
                        "smaller model")
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
            r = canonicalise_keys(r, canon)
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
    _host, _auth, _share = _serve_opts()
    demo.queue().launch(server_name=_host, server_port=7861,
                        inbrowser=(_host == "127.0.0.1"), auth=_auth,
                        share=_share)
