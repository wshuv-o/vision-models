"""PaddleOCR-VL benchmark UI - upload a PDF, time it, inspect the output."""
import os, io, time, base64, statistics, traceback
from pathlib import Path

_here = os.path.dirname(os.path.abspath(__file__))
_cert = os.path.join(_here, "combined_cacert.pem")
if os.path.exists(_cert):
    os.environ.setdefault("SSL_CERT_FILE", _cert)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _cert)

import gradio as gr
import pypdfium2 as pdfium
import requests
from PIL import Image
from paddleocr import PaddleOCRVL

VLLM = "http://localhost:8000"
OUTDIR = Path(_here) / "bench_ui_out"
OUTDIR.mkdir(exist_ok=True)
_PIPES = {}

TASK_PROMPTS = {
    "OCR (plain text)": "OCR:",
    "Table Recognition": "Table Recognition:",
    "Formula Recognition": "Formula Recognition:",
    "Chart Recognition": "Chart Recognition:",
    "Custom (free-form)": "",
}

DOWN_MSG = ('vLLM server DOWN - start it with: '
            'wsl -d Ubuntu -e bash -c "~/ocr/start_server.sh"')


def get_pipe(backend):
    if backend in _PIPES:
        return _PIPES[backend]
    if backend.startswith("vLLM"):
        p = PaddleOCRVL(vl_rec_backend="vllm-server",
                        vl_rec_server_url=VLLM + "/v1",
                        vl_rec_max_concurrency=32,
                        vl_rec_api_model_name="PaddlePaddle/PaddleOCR-VL-1.6")
    else:
        p = PaddleOCRVL(device="gpu:0")
    _PIPES[backend] = p
    return p


def server_up():
    try:
        return requests.get(VLLM + "/health", timeout=4).status_code == 200
    except Exception:
        return False


def server_status():
    return "**Server:** OK - vLLM up" if server_up() else "**Server:** " + DOWN_MSG


def render(path, dpi=150, page=1):
    if str(path).lower().endswith(".pdf"):
        d = pdfium.PdfDocument(path)
        i = max(0, min(int(page) - 1, len(d) - 1))
        return d[i].render(scale=float(dpi) / 72.0).to_pil()
    return Image.open(path).convert("RGB")


def tables_to_excel(md_all, stem="ocr_tables"):
    """Pull every HTML table out of the model output into one .xlsx workbook."""
    import pandas as pd
    from io import StringIO
    path = OUTDIR / (stem + ".xlsx")
    made = 0
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for pi, md in enumerate(md_all):
            if not md or "<table" not in md:
                continue
            try:
                dfs = pd.read_html(StringIO(md))
            except Exception:
                dfs = []
            for ti, df in enumerate(dfs):
                if df.empty:
                    continue
                sheet = ("P%d_T%d" % (pi + 1, ti + 1))[:31]
                df.to_excel(xw, sheet_name=sheet, index=False)
                made += 1
        if made == 0:
            pd.DataFrame({"info": ["No tables detected in this document"]}).to_excel(
                xw, sheet_name="no_tables", index=False)
    return str(path), made


def run(pdf_file, max_pages, dpi, backend, batched, progress=gr.Progress()):
    if pdf_file is None:
        return "Upload a PDF first.", None, "", [], None
    try:
        if backend.startswith("vLLM") and not server_up():
            return "**Cannot run.** " + DOWN_MSG, None, "", [], None

        progress(0, desc="Rasterizing...")
        doc = pdfium.PdfDocument(pdf_file)
        n = min(len(doc), int(max_pages))
        imgs = []
        for i in range(n):
            p = OUTDIR / ("pg%03d.png" % i)
            doc[i].render(scale=float(dpi) / 72.0).to_pil().save(p)
            imgs.append(str(p))

        progress(0.1, desc="Loading pipeline...")
        pipe = get_pipe(backend)
        rows, md_all = [], []
        t0 = time.perf_counter()

        if batched:
            progress(0.2, desc="Processing %d pages (batched)..." % n)
            t = time.perf_counter()
            out = list(pipe.predict(imgs))
            total = time.perf_counter() - t
            for i, r in enumerate(out):
                md = r.markdown.get("markdown_texts", "") if hasattr(r, "markdown") else ""
                md_all.append(md or "")
                rows.append([i + 1, round(total / n, 2), len(md or "")])
        else:
            for i, ip in enumerate(imgs):
                progress(0.1 + 0.85 * i / n, desc="Page %d/%d..." % (i + 1, n))
                t = time.perf_counter()
                res = list(pipe.predict(ip))
                dt = time.perf_counter() - t
                md = "".join((r.markdown.get("markdown_texts", "") or "")
                             for r in res if hasattr(r, "markdown"))
                md_all.append(md)
                rows.append([i + 1, round(dt, 2), len(md)])
            total = time.perf_counter() - t0

        per = total / n
        times = [r[1] for r in rows]
        stem = Path(str(pdf_file)).stem[:40] or "ocr_tables"
        xlsx, ntab = tables_to_excel(md_all, stem)
        summary = (
            "## %.2f s/page - %.2f pages/s\n\n"
            "**Total** %.1fs for %d pages | **median** %.2fs | **min** %.2fs | **max** %.2fs\n\n"
            "backend `%s` | dpi %d | %s | 5s target: **%s** | **%d tables -> Excel**"
            % (per, n / total, total, n, statistics.median(times), min(times), max(times),
               backend, dpi, "batched" if batched else "sequential",
               "MET" if per <= 5 else "OVER", ntab)
        )
        html = "<hr>".join("<h4>Page %d</h4>%s" % (i + 1, m) for i, m in enumerate(md_all))
        return summary, rows, html, imgs, xlsx
    except Exception as e:
        tb = traceback.format_exc()[-1200:]
        return "### Error\n```\n%s: %s\n\n%s\n```" % (type(e).__name__, e, tb), None, "", [], None


def run_prompt(src, page, dpi, preset, custom, max_tok):
    if src is None:
        return "Upload a PDF or image first.", None, ""
    try:
        if not server_up():
            return "**Cannot run.** " + DOWN_MSG, None, ""
        prompt = custom.strip() if preset == "Custom (free-form)" else TASK_PROMPTS[preset]
        if not prompt:
            return "Enter a prompt.", None, ""
        im = render(src, dpi, page)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        body = {"model": "PaddlePaddle/PaddleOCR-VL-1.6",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": uri}},
                    {"type": "text", "text": prompt}]}],
                "max_tokens": int(max_tok), "temperature": 0}
        t = time.perf_counter()
        r = requests.post(VLLM + "/v1/chat/completions", json=body, timeout=300)
        dt = time.perf_counter() - t
        if r.status_code != 200:
            return "### HTTP %d\n```\n%s\n```" % (r.status_code, r.text[:1000]), im, ""
        d = r.json()
        txt = d["choices"][0]["message"]["content"]
        u = d.get("usage", {}) or {}
        ct = u.get("completion_tokens", 0) or 0
        info = ("**%.2fs** - %d tokens - **%.0f tok/s** - prompt `%s`"
                % (dt, ct, ct / max(dt, 0.01), prompt))
        return info, im, txt
    except Exception as e:
        return "### Error\n```\n%s: %s\n```" % (type(e).__name__, e), None, ""


# ---------------- stage 2: reasoning over extracted text ----------------
OLLAMA = "http://localhost:11434"
LLM_MODEL = "qwen2.5:14b-instruct-16k"

PRESET_QUERIES = {
    "Extract as JSON": "Convert this table into a JSON array of objects. "
                       "Use the column headers as keys. Output ONLY valid JSON, no commentary.",
    "Summarize": "Summarize this document in 5 bullet points. Be specific with numbers.",
    "List tenants + rent": "List every tenant with their GLA sqft and monthly base rent, "
                           "as a markdown table. If a value is missing, write MISSING.",
    "Find anomalies": "Review this data and flag anything unusual: blank fields, "
                      "outlier values, dates that look wrong, totals that do not add up.",
    "Custom": "",
}


def llm_models():
    try:
        r = requests.get(OLLAMA + "/api/tags", timeout=5)
        return [m["name"] for m in r.json().get("models", [])] or [LLM_MODEL]
    except Exception:
        return [LLM_MODEL]


def stage1_extract(pdf_file, max_pages, dpi, progress=gr.Progress()):
    """OCR only - cache the text so stage 2 can be asked repeatedly."""
    if pdf_file is None:
        return "Upload a PDF first.", "", ""
    if not server_up():
        return "**Cannot run.** " + DOWN_MSG, "", ""
    try:
        doc = pdfium.PdfDocument(pdf_file)
        n = min(len(doc), int(max_pages))
        imgs = []
        for i in range(n):
            p = OUTDIR / ("s1_%03d.png" % i)
            doc[i].render(scale=float(dpi) / 72.0).to_pil().save(p)
            imgs.append(str(p))
        progress(0.3, desc="OCR (%d pages)..." % n)
        pipe = get_pipe("vLLM server (fast)")
        t = time.perf_counter()
        out = list(pipe.predict(imgs))
        dt = time.perf_counter() - t
        parts = []
        for i, r in enumerate(out):
            md = r.markdown.get("markdown_texts", "") if hasattr(r, "markdown") else ""
            parts.append("--- PAGE %d ---\n%s" % (i + 1, md or ""))
        text = "\n\n".join(parts)
        info = ("**Stage 1 (PaddleOCR-VL):** %.2fs for %d pages (%.2f s/page) - "
                "%d chars extracted" % (dt, n, dt / n, len(text)))
        return info, text, text
    except Exception as e:
        return "### Error\n```\n%s: %s\n```" % (type(e).__name__, e), "", ""


def stage2_ask(extracted, preset, custom, model, max_chars):
    """Send the extracted text to the local LLM."""
    if not extracted:
        return "Run Stage 1 first.", ""
    q = custom.strip() if preset == "Custom" else PRESET_QUERIES[preset]
    if not q:
        return "Enter a question.", ""
    ctx = extracted[: int(max_chars)]
    prompt = ("You are analysing text extracted from a document by OCR.\n"
              "Answer using ONLY this content. If something is not present, say so.\n\n"
              "=== DOCUMENT ===\n" + ctx + "\n=== END ===\n\n" + q)
    try:
        t = time.perf_counter()
        r = requests.post(OLLAMA + "/api/generate",
                          json={"model": model, "prompt": prompt, "stream": False,
                                "options": {"temperature": 0, "num_predict": 2048}},
                          timeout=600)
        dt = time.perf_counter() - t
        if r.status_code != 200:
            return "HTTP %d: %s" % (r.status_code, r.text[:300]), ""
        d = r.json()
        ec = d.get("eval_count", 0) or 0
        ed = max(d.get("eval_duration", 1) / 1e9, 0.001)
        info = ("**Stage 2 (%s):** %.2fs - %d tokens - %.0f tok/s - "
                "%d chars of context" % (model, dt, ec, ec / ed, len(ctx)))
        return info, d.get("response", "")
    except Exception as e:
        return "### Error\n```\n%s: %s\n```" % (type(e).__name__, e), ""


with gr.Blocks(title="PaddleOCR-VL Benchmark") as demo:
    gr.Markdown("# PaddleOCR-VL 1.6 - PDF benchmark")
    status = gr.Markdown(server_status())
    refresh = gr.Button("Recheck server", size="sm")

    with gr.Tabs():
        with gr.Tab("Pipeline benchmark"):
            gr.Markdown("Full layout + recognition pipeline. **No prompt** - it picks "
                        "`OCR:` / `Table Recognition:` / `Formula Recognition:` / "
                        "`Chart Recognition:` per detected block automatically.")
            with gr.Row():
                with gr.Column(scale=1):
                    pdf = gr.File(label="PDF", file_types=[".pdf"], type="filepath")
                    backend = gr.Radio(["vLLM server (fast)",
                                        "Local PaddlePaddle (slow baseline)"],
                                       value="vLLM server (fast)", label="Backend")
                    maxp = gr.Slider(1, 50, value=5, step=1, label="Pages")
                    dpi = gr.Slider(72, 300, value=150, step=6, label="DPI")
                    batched = gr.Checkbox(value=False, label="Batched (fastest)")
                    go = gr.Button("Run OCR", variant="primary")
                with gr.Column(scale=2):
                    summary = gr.Markdown()
                    xlsx_out = gr.File(label="Tables as Excel (.xlsx) - one sheet per table")
                    table = gr.Dataframe(headers=["Page", "Seconds", "Chars"],
                                         label="Per-page timing", wrap=True)
            with gr.Tabs():
                with gr.Tab("Extracted output"):
                    out_html = gr.HTML()
                with gr.Tab("Page images"):
                    gallery = gr.Gallery(columns=3, height=520)

        with gr.Tab("Ask questions (2-stage)"):
            gr.Markdown(
                "**Stage 1** PaddleOCR-VL extracts the document faithfully. "
                "**Stage 2** a local LLM reasons over that text. "
                "Extract once, then ask as many questions as you like - "
                "stage 1 does not re-run."
            )
            s_state = gr.State("")
            with gr.Row():
                with gr.Column(scale=1):
                    s_pdf = gr.File(label="PDF", file_types=[".pdf"], type="filepath")
                    s_pages = gr.Slider(1, 30, value=3, step=1, label="Pages")
                    s_dpi = gr.Slider(72, 300, value=150, step=6, label="DPI")
                    s_go1 = gr.Button("1 - Extract (OCR)", variant="secondary")
                    gr.Markdown("---")
                    s_model = gr.Dropdown(llm_models(), value=LLM_MODEL, label="LLM (stage 2)")
                    s_preset = gr.Dropdown(list(PRESET_QUERIES), value="Extract as JSON",
                                           label="Question")
                    s_custom = gr.Textbox(label="Custom question", lines=3,
                                          placeholder="Which units expire before 2027?")
                    s_ctx = gr.Slider(2000, 40000, value=12000, step=1000,
                                      label="Max chars sent to LLM")
                    s_go2 = gr.Button("2 - Ask", variant="primary")
                with gr.Column(scale=2):
                    s_info1 = gr.Markdown()
                    s_info2 = gr.Markdown()
                    s_answer = gr.Textbox(label="Answer", lines=16)
                    with gr.Accordion("Extracted text (stage 1 output)", open=False):
                        s_text = gr.Textbox(label="", lines=14)
            s_go1.click(stage1_extract, [s_pdf, s_pages, s_dpi], [s_info1, s_text, s_state])
            s_go2.click(stage2_ask, [s_state, s_preset, s_custom, s_model, s_ctx],
                        [s_info2, s_answer])

        with gr.Tab("Direct prompt (raw model)"):
            gr.Markdown("Send an image straight to the model, bypassing the layout pipeline. "
                        "The presets are what PaddleOCR-VL was **trained** on - free-form "
                        "instructions often work poorly, since this is an OCR model, "
                        "not a chat VLM.")
            with gr.Row():
                with gr.Column(scale=1):
                    p_src = gr.File(label="PDF or image",
                                    file_types=[".pdf", ".png", ".jpg", ".jpeg"],
                                    type="filepath")
                    p_page = gr.Number(value=1, label="Page (if PDF)", precision=0)
                    p_dpi = gr.Slider(72, 300, value=150, step=6, label="DPI")
                    p_preset = gr.Dropdown(list(TASK_PROMPTS), value="OCR (plain text)",
                                           label="Prompt preset")
                    p_custom = gr.Textbox(label="Custom prompt", lines=3,
                                          placeholder="Table Recognition:")
                    p_tok = gr.Slider(64, 8192, value=2048, step=64, label="Max tokens")
                    p_go = gr.Button("Send", variant="primary")
                with gr.Column(scale=2):
                    p_info = gr.Markdown()
                    p_img = gr.Image(label="Input sent to model", height=280)
                    p_out = gr.Textbox(label="Model output", lines=18)

    go.click(run, [pdf, maxp, dpi, backend, batched],
             [summary, table, out_html, gallery, xlsx_out])
    p_go.click(run_prompt, [p_src, p_page, p_dpi, p_preset, p_custom, p_tok],
               [p_info, p_img, p_out])
    refresh.click(lambda: server_status(), None, status)

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7861, inbrowser=False)
