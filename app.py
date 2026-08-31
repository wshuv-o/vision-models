import os
import gc
import tempfile
import threading

_here = os.path.dirname(os.path.abspath(__file__))
_cert_bundle = os.path.join(_here, "combined_cacert.pem")
if os.path.exists(_cert_bundle):
    os.environ.setdefault("SSL_CERT_FILE", _cert_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _cert_bundle)

import torch
import gradio as gr
from transformers import AutoModel, AutoTokenizer, Qwen3VLForConditionalGeneration, AutoProcessor

MODELS = {
    "Qwen3-VL-8B-Instruct (general VLM)": "qwen3vl",
    "DeepSeek-OCR (document OCR)": "deepseek_ocr",
}

DEFAULT_PROMPTS = {
    "qwen3vl": "Read all text in this image and describe what you see.",
    "deepseek_ocr": "<image>\n<|grounding|>Convert the document to markdown. ",
}

_lock = threading.Lock()
_state = {"kind": None, "model": None, "tokenizer": None, "processor": None}


def unload_current():
    if _state["model"] is not None:
        del _state["model"]
    _state["kind"] = None
    _state["model"] = None
    _state["tokenizer"] = None
    _state["processor"] = None
    gc.collect()
    torch.cuda.empty_cache()


def load_qwen3vl():
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        "Qwen/Qwen3-VL-8B-Instruct",
        dtype="auto",
        device_map="auto",
    )
    processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-8B-Instruct")
    _state.update(kind="qwen3vl", model=model, processor=processor, tokenizer=None)


def load_deepseek_ocr():
    tokenizer = AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-OCR", trust_remote_code=True)
    attn_impl = "eager"
    try:
        import flash_attn  # noqa: F401
        attn_impl = "flash_attention_2"
    except ImportError:
        pass
    model = AutoModel.from_pretrained(
        "deepseek-ai/DeepSeek-OCR",
        _attn_implementation=attn_impl,
        trust_remote_code=True,
        use_safetensors=True,
    )
    model = model.eval().cuda().to(torch.bfloat16)
    _state.update(kind="deepseek_ocr", model=model, tokenizer=tokenizer, processor=None)


def ensure_model(kind, progress=None):
    if _state["kind"] == kind:
        return
    if progress:
        progress(0, desc=f"Loading {kind} (first switch downloads/loads weights)...")
    unload_current()
    if kind == "qwen3vl":
        load_qwen3vl()
    elif kind == "deepseek_ocr":
        load_deepseek_ocr()


def run_qwen3vl(image_path, prompt):
    model = _state["model"]
    processor = _state["processor"]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": f"file://{image_path}"},
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
    model = _state["model"]
    tokenizer = _state["tokenizer"]
    out_dir = tempfile.mkdtemp(prefix="ocr_")
    if "<image>" not in prompt:
        prompt = "<image>\n" + prompt
    model.infer(
        tokenizer,
        prompt=prompt,
        image_file=image_path,
        output_path=out_dir,
        base_size=1024,
        image_size=640,
        crop_mode=True,
        save_results=True,
        test_compress=True,
    )
    mmd_path = os.path.join(out_dir, "result.mmd")
    text = open(mmd_path, encoding="utf-8").read() if os.path.exists(mmd_path) else "(no text output produced)"
    boxed_img = os.path.join(out_dir, "result_with_boxes.jpg")
    img_out = boxed_img if os.path.exists(boxed_img) else None
    return text, img_out


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
            else:
                return run_deepseek_ocr(image_path, prompt)
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
    demo.queue().launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
