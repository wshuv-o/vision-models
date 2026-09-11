import os
import gc
import json
import collections
import time
import atexit
import shutil
import tempfile
import threading
import subprocess
import urllib.request

_here = os.path.dirname(os.path.abspath(__file__))
_cert_bundle = os.path.join(_here, "combined_cacert.pem")
if os.path.exists(_cert_bundle):
    os.environ.setdefault("SSL_CERT_FILE", _cert_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _cert_bundle)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import torch
import gradio as gr

import pdf_pages
import save_output

# PaddleOCR-VL cannot run on Windows here: Application Control blocks
# libpaddle's DLL. It runs in WSL instead, against the vLLM server that also
# lives there, so its worker is launched through wsl.exe and every path handed
# to it is translated to /mnt/<drive>/... form.
WSL_PADDLE_PYTHON = "/home/esme_abha/ocr/.venv-paddle/bin/python"
WSL_VLLM_START = "/home/esme_abha/ocr/start_server.sh"
VLLM_URL = "http://localhost:8000/v1"
# Desktop apps (Chrome/Teams/explorer) sit around 4.5GB; anything much above
# that means a model is still resident.
VRAM_IDLE_CEILING_MB = 6000
OUTPUT_DIR = os.path.join(_here, "outputs")

OCR_WORKERS = {
    "deepseek_ocr": {
        "cmd": [
            os.path.join(_here, ".venv-deepseek-ocr", "Scripts", "python.exe"),
            os.path.join(_here, "deepseek_ocr_worker.py"),
            "--serve",
        ],
        "wsl": False,
    },
    "paddleocr_vl": {
        "cmd": [
            "wsl.exe", "-e", WSL_PADDLE_PYTHON, "-u",
            pdf_pages.win_to_wsl(os.path.join(_here, "paddleocr_worker.py")),
            "--serve",
        ],
        "wsl": True,
    },
}

MODELS = {
    "PaddleOCR-VL (document OCR)": "paddleocr_vl",
    "DeepSeek-OCR (document OCR)": "deepseek_ocr",
}

DEFAULT_PROMPTS = {
    "deepseek_ocr": "<image>\n<|grounding|>Convert the document to markdown. ",
    "paddleocr_vl": "(no prompt needed - fixed layout+recognition pipeline)",
}

_lock = threading.Lock()
_state = {"kind": None, "model": None, "processor": None, "ocr_proc": None,
          "vllm_proc": None}


def _vllm_up(timeout=3):
    try:
        with urllib.request.urlopen(VLLM_URL + "/models", timeout=timeout):
            return True
    except Exception:
        return False


def start_vllm(log=print):
    """Bring up the PaddleOCR-VL vLLM server in WSL (idempotent).

    Held as a child process so the WSL distro stays alive; nohup'ing it inside
    WSL is not enough, the distro is torn down when the launching wsl.exe exits.
    """
    if _vllm_up():
        log("vLLM server already running")
        return
    log("starting vLLM server in WSL (first start loads the model, ~40s)...")
    _state["vllm_proc"] = subprocess.Popen(
        ["wsl.exe", "-e", "bash", "-lc", WSL_VLLM_START],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for i in range(120):
        if _vllm_up():
            log("vLLM server ready")
            return
        time.sleep(1)
    raise RuntimeError("vLLM server did not come up within 120s")


def gpu_used_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return -1


def stop_vllm(log=print):
    """Stop the server and WAIT for its VRAM to come back.

    This must block. The card is 16GB; vLLM holds ~5.5GB and Qwen3-VL's 4-bit
    load needs ~6GB on top of ~4.3GB of desktop apps. Starting the load while
    the server is still shutting down segfaults the process partway through
    loading weights, so returning early here crashes the app.
    """
    proc = _state.get("vllm_proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _state["vllm_proc"] = None
    subprocess.run(["wsl.exe", "-e", "bash", "-lc", "pkill -f 'vllm serve'"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not _vllm_up(timeout=2) and gpu_used_mb() < 0:
        return
    before = gpu_used_mb()
    for _ in range(60):
        if not _vllm_up(timeout=2) and gpu_used_mb() <= VRAM_IDLE_CEILING_MB:
            break
        time.sleep(1)
    else:
        log(f"warning: VRAM still at {gpu_used_mb()}MB after stopping vLLM")
        return
    time.sleep(2)  # let the driver actually reclaim the pages
    log(f"vLLM stopped, VRAM {before}MB -> {gpu_used_mb()}MB")


atexit.register(stop_vllm)


def _stop_ocr_worker():
    proc = _state.get("ocr_proc")
    if proc is not None and proc.poll() is None:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _state["ocr_proc"] = None


def unload_current():
    if _state["model"] is not None:
        del _state["model"]
    _stop_ocr_worker()
    _state["kind"] = None
    _state["model"] = None
    _state["processor"] = None
    gc.collect()
    torch.cuda.empty_cache()


def load_ocr_worker(kind):
    spec = OCR_WORKERS[kind]
    if spec["wsl"]:
        start_vllm()
    proc = subprocess.Popen(
        spec["cmd"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=_here,
    )
    # PaddleOCR is extremely chatty on stderr. Nothing was reading this pipe,
    # so once the OS buffer (~64KB) filled, the worker blocked forever on write
    # and the whole request hung. Drain it continuously into a ring buffer.
    tail = collections.deque(maxlen=400)

    def _drain(stream, sink):
        try:
            for line in stream:
                sink.append(line.rstrip("\n"))
        except Exception:
            pass

    threading.Thread(target=_drain, args=(proc.stderr, tail), daemon=True).start()
    _state.update(kind=kind, ocr_proc=proc, stderr_tail=tail)
    # Warm the model now so the first real request isn't slower than the rest.
    warmup_prompt = DEFAULT_PROMPTS[kind]
    _ocr_request(_here + "\\test_document.png", warmup_prompt, warmup=True)


def ensure_model(kind, progress=None):
    proc = _state.get("ocr_proc")
    if _state["kind"] == kind and proc is not None and proc.poll() is None:
        # The worker is up, but the server it talks to may not be: it lives in
        # WSL and can be stopped independently of this process.
        if OCR_WORKERS[kind]["wsl"] and not _vllm_up():
            start_vllm()
        return
    if _state["kind"] == kind:
        # Worker died (crash, or killed outside this process) -- rebuild it
        # instead of failing every request from here on.
        print("OCR worker is gone, restarting it", flush=True)
        _state["kind"] = None
    if progress:
        progress(0, desc=f"Loading {kind} (first switch loads weights, ~10-20s)...")
    unload_current()
    if kind in OCR_WORKERS:
        load_ocr_worker(kind)
    else:
        raise ValueError(f"unknown model kind: {kind}")


def _ocr_request(image_path, prompt, warmup=False):
    proc = _state["ocr_proc"]
    if proc is None or proc.poll() is not None:
        raise RuntimeError("OCR worker process is not running")
    out_dir = tempfile.mkdtemp(prefix="ocr_")
    send_img, send_out = image_path, out_dir
    if OCR_WORKERS[_state["kind"]]["wsl"]:
        send_img = pdf_pages.win_to_wsl(image_path)
        send_out = pdf_pages.win_to_wsl(out_dir)
    req = json.dumps({"image_path": send_img, "prompt": prompt, "out_dir": send_out})
    proc.stdin.write(req + "\n")
    proc.stdin.flush()
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("###RESULT_JSON###"):
            resp = json.loads(line[len("###RESULT_JSON###"):])
            if not resp["ok"]:
                raise RuntimeError(resp["error"])
            if warmup:
                return None, None
            return resp["text"], resp["image"]
    tail = "\n".join(_state.get("stderr_tail") or [])[-2000:]
    raise RuntimeError(f"OCR worker exited unexpectedly.\n{tail}")


def run_deepseek_ocr(image_path, prompt):
    if "<image>" not in prompt:
        prompt = "<image>\n" + prompt
    return _ocr_request(image_path, prompt)


def run_paddleocr_vl(image_path, prompt):
    return _ocr_request(image_path, prompt)


def _ocr_request_batch(image_paths):
    """Recognise several pages in a single worker call (PaddleOCR-VL only)."""
    proc = _state["ocr_proc"]
    if proc is None or proc.poll() is not None:
        raise RuntimeError("OCR worker process is not running")
    out_dir = tempfile.mkdtemp(prefix="ocr_")
    paths, send_out = list(image_paths), out_dir
    if OCR_WORKERS[_state["kind"]]["wsl"]:
        paths = [pdf_pages.win_to_wsl(p) for p in paths]
        send_out = pdf_pages.win_to_wsl(out_dir)
    proc.stdin.write(json.dumps({"image_paths": paths, "out_dir": send_out}) + "\n")
    proc.stdin.flush()
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("###RESULT_JSON###"):
            resp = json.loads(line[len("###RESULT_JSON###"):])
            if not resp["ok"]:
                raise RuntimeError(resp["error"])
            return resp["texts"]
    tail = "\n".join(_state.get("stderr_tail") or [])[-2000:]
    raise RuntimeError(f"OCR worker exited unexpectedly.\n{tail}")


def _run_one(kind, image_path, prompt):
    if kind == "deepseek_ocr":
        return run_deepseek_ocr(image_path, prompt)
    if kind == "paddleocr_vl":
        return run_paddleocr_vl(image_path, prompt)
    raise ValueError(kind)


def run(model_label, image_path, pdf_file, prompt, pages_spec, dpi, batch=4,
        progress=gr.Progress()):
    """Streaming handler. Yields (status, text, image, html) as pages finish."""
    kind = MODELS[model_label]
    pdf_path = getattr(pdf_file, "name", pdf_file)
    show_html = gr.update(visible=(kind == "paddleocr_vl"))

    if not pdf_path and image_path is None:
        yield "Upload an image or a PDF first.", "", None, show_html
        return

    log = []

    def status(msg):
        log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        return "\n".join(log)

    with _lock:
        try:
            # ---------------------------------------------------- single image
            if not pdf_path:
                yield status(f"loading {kind}..."), "", None, show_html
                ensure_model(kind, progress)
                yield status("running inference..."), "", None, show_html
                t = time.time()
                text, img = _run_one(kind, image_path, prompt)
                st = status(f"done in {time.time() - t:.1f}s, {len(text)} chars")
                html = text if kind == "paddleocr_vl" else ""
                yield st, text, img, gr.update(value=html,
                                               visible=(kind == "paddleocr_vl"))
                return

            # ------------------------------------------------------ PDF, live
            total = pdf_pages.page_count(pdf_path)
            todo = pdf_pages.parse_pages(pages_spec, total)
            yield (status(f"{os.path.basename(pdf_path)}: {total} pages, "
                          f"processing {len(todo)} at {int(dpi)} dpi"),
                   "", None, show_html)

            yield status(f"loading {kind}..."), "", None, show_html
            ensure_model(kind, progress)

            work = tempfile.mkdtemp(prefix="pdfpages_")
            t_all = time.time()
            rendered = []
            for pno, png, info in pdf_pages.render_pdf(
                    pdf_path, work, dpi=int(dpi), pages=pages_spec):
                w, h = info["size"]
                note = (f", rotated {info['applied_rotation']}deg"
                        if info["applied_rotation"] else "")
                rendered.append((pno, png))
                yield (status(f"page {pno}/{total}: rendered {w}x{h}px{note}"),
                       "", None, show_html)

            # PaddleOCR-VL fans a whole batch of pages out to vLLM at once;
            # DeepSeek-OCR has no batch path, so it stays one page at a time.
            step = int(batch) if kind == "paddleocr_vl" else 1
            parts, last_img, done = [], None, 0
            for i in range(0, len(rendered), step):
                chunk = rendered[i:i + step]
                lo, hi = chunk[0][0], chunk[-1][0]
                label = f"page {lo}" if lo == hi else f"pages {lo}-{hi}"
                yield (status(f"{label}: recognising ({len(chunk)} at once)..."),
                       "\n\n".join(parts), last_img, show_html)
                t = time.time()
                try:
                    if step > 1:
                        texts = _ocr_request_batch([p for _, p in chunk])
                    else:
                        text, img = _run_one(kind, chunk[0][1], prompt)
                        texts, last_img = [text], img or last_img
                except Exception as e:
                    texts = [f"ERROR on {label}: {type(e).__name__}: {e}"] * len(chunk)
                el = time.time() - t
                for (pno, _), text in zip(chunk, texts):
                    parts.append(f"<!-- ===== page {pno} ===== -->\n{text}")
                done += len(chunk)
                progress(done / max(len(rendered), 1), desc=label)
                joined = "\n\n".join(parts)
                html = joined if kind == "paddleocr_vl" else ""
                yield (status(f"{label}: done in {el:.1f}s "
                              f"({el / len(chunk):.1f}s/page), "
                              f"{sum(len(x) for x in texts)} chars"),
                       joined, last_img,
                       gr.update(value=html, visible=(kind == "paddleocr_vl")))

            joined = "\n\n".join(parts)
            html = joined if kind == "paddleocr_vl" else ""
            el = time.time() - t_all
            st = status(f"ALL DONE: {len(parts)} pages in {el:.1f}s "
                        f"({el / max(len(parts), 1):.1f}s/page), {len(joined)} chars")
            shutil.rmtree(work, ignore_errors=True)
            yield st, joined, last_img, gr.update(value=html,
                                                  visible=(kind == "paddleocr_vl"))
        except Exception as e:
            yield status(f"ERROR: {type(e).__name__}: {e}"), "", None, show_html


def save_result(text, pdf_file, image_path):
    """Write the current result to .md / .html / .xlsx and offer them for download."""
    if not text or not str(text).strip():
        return gr.update(value=None), "Nothing to save yet - run something first."
    src = getattr(pdf_file, "name", pdf_file) or image_path or "output"
    try:
        files = save_output.save_all(str(text), base_name=src, out_dir=OUTPUT_DIR)
    except Exception as e:
        return gr.update(value=None), f"Save failed: {type(e).__name__}: {e}"
    names = "\n".join("  " + f for f in files)
    return gr.update(value=files), f"Saved {len(files)} file(s):\n{names}"


def on_model_change(label):
    kind = MODELS[label]
    return (
        DEFAULT_PROMPTS[kind],
        gr.update(visible=(kind != "paddleocr_vl")),
        gr.update(visible=(kind == "paddleocr_vl"), value=""),
    )


with gr.Blocks(title="Local Vision Models - RTX 5080") as demo:
    gr.Markdown(
        "# Local OCR / VLM\n"
        "Runs entirely on your RTX 5080. Pick a model, upload an image **or a "
        "PDF**, hit Run. PDFs are processed page by page and stream in live.\n"
        "Switching models unloads the previous one to stay within 16GB VRAM."
    )
    with gr.Row():
        with gr.Column():
            model_dd = gr.Dropdown(
                choices=list(MODELS.keys()),
                value="PaddleOCR-VL (document OCR)",
                label="Model",
            )
            image_in = gr.Image(type="filepath", label="Upload image")
            pdf_in = gr.File(label="...or upload a PDF", file_types=[".pdf"])
            with gr.Row():
                pages_in = gr.Textbox(label="Pages", value="",
                                      placeholder="all, or 1-3 / 1,4,7", scale=1)
                dpi_in = gr.Slider(150, 600, value=300, step=50,
                                   label="Render DPI", scale=2)
            batch_in = gr.Slider(1, 16, value=4, step=1,
                                 label="Pages per vLLM batch (higher = faster, "
                                       "coarser live updates)")
            prompt_in = gr.Textbox(
                value=DEFAULT_PROMPTS["paddleocr_vl"],
                label="Prompt / instruction",
                lines=3,
            )
            run_btn = gr.Button("Run", variant="primary")
            with gr.Row():
                save_btn = gr.Button("Save output (.md / .html / .xlsx)")
            files_out = gr.File(label="Download", file_count="multiple")
        with gr.Column():
            status_out = gr.Textbox(label="Progress (live)", lines=10,
                                    max_lines=10, autoscroll=True)
            text_out = gr.Textbox(label="Result (raw)", lines=22)
            img_out = gr.Image(label="Annotated output (OCR grounding, if produced)")
            html_out = gr.HTML(label="Rendered table (PaddleOCR-VL)", visible=False)

    model_dd.change(on_model_change, inputs=model_dd, outputs=[prompt_in, img_out, html_out])
    save_btn.click(save_result, inputs=[text_out, pdf_in, image_in],
                   outputs=[files_out, status_out])
    run_btn.click(
        run,
        inputs=[model_dd, image_in, pdf_in, prompt_in, pages_in, dpi_in, batch_in],
        outputs=[status_out, text_out, img_out, html_out],
    )

if __name__ == "__main__":
    try:
        demo.queue().launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
    finally:
        _stop_ocr_worker()
