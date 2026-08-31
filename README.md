# Local Vision Models — OCR + VLM on RTX 5080

A local Gradio UI for running open-weight OCR and vision-language models entirely
on your own GPU. Upload an image, pick a model, get a result — nothing leaves
your machine.

## Models

- **[Qwen3-VL-8B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct)** — general-purpose vision-language model (captioning, Q&A, grounding, reading text in context).
- **[DeepSeek-OCR](https://huggingface.co/deepseek-ai/DeepSeek-OCR)** — dedicated document OCR model (optical context compression), converts documents to markdown.

DeepSeek-OCR's custom modeling code requires an older `transformers` release
(`4.46.3`, pinned for a since-removed internal class), which conflicts with the
recent `transformers` Qwen3-VL needs — so it runs in its own virtual environment
(`.venv-deepseek-ocr`) alongside the main one (`.venv`).

## Setup

Requires Python 3.12, an NVIDIA GPU with recent drivers, and ~30GB disk space
for model weights.

```bash
# main environment (Qwen3-VL + Gradio UI)
python -m venv .venv
./.venv/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
./.venv/Scripts/pip install "transformers>=4.57" accelerate huggingface_hub pillow einops qwen-vl-utils gradio

# dedicated environment for DeepSeek-OCR (pinned transformers)
python -m venv .venv-deepseek-ocr
./.venv-deepseek-ocr/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
./.venv-deepseek-ocr/Scripts/pip install transformers==4.46.3 accelerate huggingface_hub pillow einops addict easydict matplotlib timm safetensors

# download model weights
./.venv/Scripts/python download_models.py
```

## Run

```bash
./.venv/Scripts/python app.py
```

Opens at `http://127.0.0.1:7860`. Pick a model from the dropdown, upload an
image, hit Run. Switching models unloads the previous one so both stay within
a single GPU's VRAM.

## Notes

- If your system does TLS inspection (corporate AV/proxy), Hugging Face
  downloads may fail SSL verification — see `combined_cacert.pem` handling in
  `download_models.py` for the workaround (build a combined CA bundle and set
  `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`).
- `make_test_image.py` generates a synthetic invoice image for smoke-testing.
