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

## PaddleOCR-VL 1.6 with vLLM

PaddleOCR-VL is **two models**, and only one of them is served by vLLM:

| stage | model | runs where |
|---|---|---|
| layout detection | PP-DocLayoutV3 | PaddlePaddle, locally |
| block recognition | PaddleOCR-VL-1.6 (0.9B) | vLLM, OpenAI-compatible server |

The layout stage is not optional. It crops the page into blocks and the 0.9B
model recognises them one at a time. Send a whole page straight to the vLLM
endpoint and it reads a single block and stops -- a full page of a lease report
came back as 58 characters, just the header.

**Both stages must run in WSL on this machine.** Windows Application Control
blocks `libpaddle`'s DLL (`ImportError: DLL load failed while importing
libpaddle: An Application Control policy has blocked this file`), so
`.venv-paddleocr` on Windows cannot import PaddleOCR at all. WSL has no such
policy, and the vLLM server already lives there.

### Start the server

```bash
wsl
~/ocr/start_server.sh          # vllm serve PaddlePaddle/PaddleOCR-VL-1.6, port 8000
```

Do not `nohup` it and let the launching `wsl.exe` exit -- the distro is torn
down with it and the server dies. Keep the session open, or launch it as a
child process that stays alive (which is what `app.py` does).

### Run a PDF, page by page

```bash
~/ocr/.venv-paddle/bin/python pdf_vllm_pages.py "<pdf>" --batch 4
```

Options: `--pages 1-3`, `--dpi 300`, `--batch N` (pages per pipeline call),
`--concurrency N` (blocks in flight to vLLM), `--rotate`, `--no-crop`.

Or use the Gradio app, which has a PDF upload, a page range, a DPI slider and a
live per-page progress panel.

### WSL setup

```bash
uv venv --python 3.12 ~/ocr/.venv-paddle
uv pip install --python ~/ocr/.venv-paddle/bin/python \
    paddlepaddle-gpu==3.2.1 \
    --index-url https://www.paddlepaddle.org.cn/packages/stable/cu129/ \
    --index-strategy unsafe-best-match
uv pip install --python ~/ocr/.venv-paddle/bin/python 'paddleocr[doc-parser]' pypdfium2
```

Install the **GPU** build. `paddlepaddle` (CPU) and `paddlepaddle-gpu` both
provide the `paddle` module, so uninstall the CPU one first or it shadows the
GPU build and `paddle.is_compiled_with_cuda()` silently stays `False`.

### Speed

Measured on a 3-page tenancy schedule, RTX 5080:

| configuration | per page |
|---|---|
| CPU layout, one page per call | 7.9s - 23.1s |
| GPU layout, one page per call | 5.9s |
| GPU layout + batched pages | **2.8s** |

Two things matter, in this order:

1. **Layout detection on the GPU.** It runs in PaddlePaddle, not vLLM, so a CPU
   build leaves it competing with everything else on the box -- which is why the
   CPU numbers swing so widely.
2. **Batch the pages.** One `predict()` call per batch lets every block of every
   page fan out to vLLM together instead of draining one page before starting
   the next.

`--gpu-memory-utilization` is a fraction of the *whole* card and does not know
about the layout model, which now wants VRAM too. At 0.80 the card sat at 96.6%
(15749/16303 MiB); 0.62 leaves ~3GB of headroom and costs no throughput.

### Saving output

The app has a **Save output** button that writes three files to `outputs/` and
offers them for download:

| file | what it is |
|---|---|
| `.md` | raw pipeline output (markdown with embedded HTML tables) |
| `.html` | the same, styled, opens in a browser |
| `.xlsx` | one sheet per page, via `pandas.read_html` |

The CLI writes files on its own -- `--out DIR` gets per-page markdown plus a
combined document. `save_output.save_all(text, base_name, out_dir)` is callable
directly if you want the same three files from your own code.

Two caveats on the workbook:

- Cells spanning several columns are **repeated across each column** they span,
  because that is how `read_html` expands `colspan`. Header rows in particular
  look duplicated.
- A table the model failed to split into rows (see below) lands as one very wide
  single-row sheet.

### Reproducibility

Recognition is **not deterministic**. The same 3-page PDF produced 70,175 /
72,570 / 73,676 characters on three runs -- a few percent of drift, with row
counts moving too (page 2 came back as 44 rows once and 39 another time). For
anything where the numbers matter, diff two runs before trusting a single one.

### Gotchas worth knowing

- **`/Rotate` is ignored by pypdfium2.** A landscape sheet saved as
  rotated-portrait renders sideways, and the layout model then files the entire
  table as one `image` block. `pdf_pages.render_page` undoes this; without it a
  tenancy schedule produced 120 characters instead of 2,876.
- **A corrupt text layer beats OCR only when it is not corrupt.** Digital PDFs
  normally extract exactly, but a broken ToUnicode CMap yields mojibake
  (`<DI/I-Clt.----"""""|W:.(PW)`). Check before trusting extraction.
- **Very wide tables lose their row structure.** A 15:1 table block comes back
  as one giant row of ~716 cells instead of ~18 rows; raising DPI to 600 fixes
  character accuracy (headers went from `Species/Link Type/Plant Type` to the
  correct `Space/Unit Type/Rental Type`) but not the row splitting. Fixing it
  needs the block sliced into column chunks before recognition.
- **Worker stderr must be drained.** PaddleOCR is chatty; if nothing reads the
  pipe it fills at ~64KB and the worker blocks forever mid-request.

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
