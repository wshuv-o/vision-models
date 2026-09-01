import os
import gc
import json
import tempfile
import threading
import subprocess

_here = os.path.dirname(os.path.abspath(__file__))
_cert_bundle = os.path.join(_here, "combined_cacert.pem")
if os.path.exists(_cert_bundle):
    os.environ.setdefault("SSL_CERT_FILE", _cert_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _cert_bundle)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import torch
import gradio as gr
from transformers import Qwen3VLForConditionalGeneration, AutoProcessor, BitsAndBytesConfig

OCR_WORKERS = {
    "deepseek_ocr": (
        os.path.join(_here, ".venv-deepseek-ocr", "Scripts", "python.exe"),
        os.path.join(_here, "deepseek_ocr_worker.py"),
    ),
    "paddleocr_vl": (
        os.path.join(_here, ".venv-paddleocr", "Scripts", "python.exe"),
        os.path.join(_here, "paddleocr_worker.py"),
    ),
}

MODELS = {
    "Qwen3-VL-8B-Instruct (general VLM)": "qwen3vl",
    "DeepSeek-OCR (document OCR)": "deepseek_ocr",
    "PaddleOCR-VL (document OCR)": "paddleocr_vl",
}

DEFAULT_PROMPTS = {
    "qwen3vl": "Read all text in this image and describe what you see.",
    "deepseek_ocr": "<image>\n<|grounding|>Convert the document to markdown. ",
    "paddleocr_vl": "(no prompt needed - fixed layout+recognition pipeline)",
}

_lock = threading.Lock()
_state = {"kind": None, "model": None, "processor": None, "ocr_proc": None}


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


def load_qwen3vl():
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        "Qwen/Qwen3-VL-8B-Instruct",
        quantization_config=bnb_config,
        device_map={"": 0},
    )
    processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-8B-Instruct")
    _state.update(kind="qwen3vl", model=model, processor=processor)


def load_ocr_worker(kind):
    python_exe, worker_script = OCR_WORKERS[kind]
    proc = subprocess.Popen(
        [python_exe, worker_script, "--serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=_here,
    )
    _state.update(kind=kind, ocr_proc=proc)
    # Warm the model now so the first real request isn't slower than the rest.
    warmup_prompt = DEFAULT_PROMPTS[kind]
    _ocr_request(_here + "\\test_document.png", warmup_prompt, warmup=True)


def ensure_model(kind, progress=None):
    if _state["kind"] == kind:
        return
    if progress:
        progress(0, desc=f"Loading {kind} (first switch loads weights, ~10-20s)...")
    unload_current()
    if kind == "qwen3vl":
        load_qwen3vl()
    elif kind in OCR_WORKERS:
        load_ocr_worker(kind)


def _ocr_request(image_path, prompt, warmup=False):
    proc = _state["ocr_proc"]
    if proc is None or proc.poll() is not None:
        raise RuntimeError("OCR worker process is not running")
    out_dir = tempfile.mkdtemp(prefix="ocr_")
    req = json.dumps({"image_path": image_path, "prompt": prompt, "out_dir": out_dir})
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
    stderr_tail = proc.stderr.read()[-2000:] if proc.stderr else ""
    raise RuntimeError(f"OCR worker exited unexpectedly.\n{stderr_tail}")


def run_qwen3vl(image_path, prompt):
    model = _state["model"]
    processor = _state["processor"]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image_path},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    ).to(model.device)
    generated_ids = model.generate(**inputs, max_new_tokens=768)
    trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, generated_ids)]
    text = processor.batch_decode(trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
    return text, None


def run_deepseek_ocr(image_path, prompt):
    if "<image>" not in prompt:
        prompt = "<image>\n" + prompt
    return _ocr_request(image_path, prompt)


def run_paddleocr_vl(image_path, prompt):
    return _ocr_request(image_path, prompt)


def run(model_label, image_path, prompt, progress=gr.Progress()):
    if image_path is None:
        return "Please upload an image first.", None
    kind = MODELS[model_label]
    with _lock:
        try:
            ensure_model(kind, progress)
            progress(0.5, desc="Running inference...")
            if kind == "qwen3vl":
                return run_qwen3vl(image_path, prompt)
            elif kind == "deepseek_ocr":
                return run_deepseek_ocr(image_path, prompt)
            elif kind == "paddleocr_vl":
                return run_paddleocr_vl(image_path, prompt)
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}", None


def on_model_change(label):
    return DEFAULT_PROMPTS[MODELS[label]]


with gr.Blocks(title="Local Vision Models - RTX 5080") as demo:
    gr.Markdown(
        "# Local OCR / VLM\n"
        "Runs entirely on your RTX 5080. Pick a model, upload an image, hit Run.\n"
        "Switching models unloads the previous one to stay within 16GB VRAM."
    )
    with gr.Row():
        with gr.Column():
            model_dd = gr.Dropdown(
                choices=list(MODELS.keys()),
                value=list(MODELS.keys())[0],
                label="Model",
            )
            image_in = gr.Image(type="filepath", label="Upload image")
            prompt_in = gr.Textbox(
                value=DEFAULT_PROMPTS["qwen3vl"],
                label="Prompt / instruction",
                lines=3,
            )
            run_btn = gr.Button("Run", variant="primary")
        with gr.Column():
            text_out = gr.Textbox(label="Result", lines=28)
            img_out = gr.Image(label="Annotated output (OCR grounding, if produced)")

    model_dd.change(on_model_change, inputs=model_dd, outputs=prompt_in)
    run_btn.click(run, inputs=[model_dd, image_in, prompt_in], outputs=[text_out, img_out])

if __name__ == "__main__":
    try:
        demo.queue().launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
    finally:
        _stop_ocr_worker()
