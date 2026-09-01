# Local Vision Models — OCR + VLM on RTX 5080

A local Gradio UI for running open-weight OCR and vision-language models entirely
on your own GPU. Upload an image, pick a model, get a result — nothing leaves
your machine.

## Models

- **[Qwen3-VL-8B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct)** — general-purpose vision-language model (captioning, Q&A, grounding, reading text in context). Loaded 4-bit quantized (bitsandbytes NF4) — ~6GB VRAM instead of ~16GB, and avoids the CPU-offload slowdown that comes from running it too close to full VRAM.
- **[DeepSeek-OCR](https://huggingface.co/deepseek-ai/DeepSeek-OCR)** — dedicated document OCR model (optical context compression), converts documents to markdown.
- **[PaddleOCR-VL](https://huggingface.co/PaddlePaddle/PaddleOCR-VL)** — layout-detection + VLM-recognition document parsing pipeline (PP-DocLayoutV3 + a 0.9B recognition model), runs via the native PaddlePaddle framework.

DeepSeek-OCR's custom modeling code requires an older `transformers` release
(`4.46.3`, pinned for a since-removed internal class), and PaddleOCR-VL runs on
a completely different framework (PaddlePaddle, not PyTorch) — so each gets its
own virtual environment (`.venv-deepseek-ocr`, `.venv-paddleocr`) alongside the
main one (`.venv`) that runs Qwen3-VL and the Gradio UI. `app.py` talks to the
two OCR environments as persistent subprocess workers over stdin/stdout JSON.

## Setup

Requires Python 3.12, an NVIDIA GPU with recent drivers, and ~35GB disk space
for model weights. Tested on an RTX 5080 (Blackwell, sm_120).

```bash
# main environment (Qwen3-VL + Gradio UI)
python -m venv .venv
./.venv/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
./.venv/Scripts/pip install "transformers>=4.57" accelerate bitsandbytes huggingface_hub pillow einops qwen-vl-utils gradio

# dedicated environment for DeepSeek-OCR (pinned transformers)
python -m venv .venv-deepseek-ocr
./.venv-deepseek-ocr/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
./.venv-deepseek-ocr/Scripts/pip install transformers==4.46.3 accelerate huggingface_hub pillow einops addict easydict matplotlib timm safetensors

# dedicated environment for PaddleOCR-VL (PaddlePaddle, not PyTorch)
python -m venv .venv-paddleocr
./.venv-paddleocr/Scripts/pip install paddlepaddle-gpu==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu129/
./.venv-paddleocr/Scripts/pip install -U "paddleocr[doc-parser]"

# download Qwen3-VL / DeepSeek-OCR weights (PaddleOCR-VL downloads on first run)
./.venv/Scripts/python download_models.py
```

## Run

```bash
./.venv/Scripts/python app.py
```

Opens at `http://127.0.0.1:7860`. Pick a model from the dropdown, upload an
image, hit Run. Switching models unloads/stops the previous one so everything
stays within a single GPU's VRAM.

## Notes

- If your system does TLS inspection (corporate AV/proxy), Hugging Face
  downloads may fail SSL verification — see `combined_cacert.pem` handling in
  `download_models.py` for the workaround (build a combined CA bundle and set
  `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`).
- If antivirus software blocks freshly-published PyPI wheels (Windows
  "Application Control" errors on `numpy`, `chardet`, `sentencepiece`, etc. —
  a low-reputation/prevalence heuristic, not real malware), pin the affected
  package to a slightly older release with `pip install <pkg>==<older-version>
  --force-reinstall --no-deps`.
- `make_test_image.py` generates a synthetic invoice image for smoke-testing.
- On Blackwell GPUs (RTX 50-series), keep VRAM headroom generous: letting a
  model creep close to 100% VRAM usage causes `device_map="auto"` to silently
  offload layers to CPU, which is dramatically slower than quantizing further.
