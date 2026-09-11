import os
import sys
import json

_here = os.path.dirname(os.path.abspath(__file__))
_cert_bundle = os.path.join(_here, "combined_cacert.pem")
if os.path.exists(_cert_bundle):
    os.environ.setdefault("SSL_CERT_FILE", _cert_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _cert_bundle)

from pathlib import Path
from paddleocr import PaddleOCRVL

# Blocks are recognised by the vLLM server, which happily serves many at once;
# this is the cap the pipeline uses when fanning them out.
MAX_CONCURRENCY = int(os.environ.get("PADDLEOCR_VL_CONCURRENCY", "64"))
SERVER_URL = os.environ.get("PADDLEOCR_VL_SERVER", "http://localhost:8000/v1")
API_MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"

_PIPELINE = None


def _load():
    global _PIPELINE
    if _PIPELINE is not None:
        return
    _PIPELINE = PaddleOCRVL(
        vl_rec_backend="vllm-server",
        vl_rec_server_url=SERVER_URL,
        vl_rec_max_concurrency=MAX_CONCURRENCY,
        vl_rec_api_model_name=API_MODEL,
    )


def _text_of(res, out_dir):
    res.save_to_markdown(save_path=str(out_dir))
    if hasattr(res, "markdown"):
        return res.markdown.get("markdown_texts", "") or ""
    return ""


def run_many(image_paths, out_dir):
    """Recognise several page images in one pipeline call.

    Passing the whole batch to predict() lets the pipeline fan every block of
    every page out to the vLLM server together, instead of draining one page
    before starting the next.
    """
    _load()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = []
    for res in _PIPELINE.predict(list(image_paths)):
        texts.append(_text_of(res, out_dir))
    # Guard against the pipeline returning a different count than we sent.
    while len(texts) < len(image_paths):
        texts.append("")
    return texts[: len(image_paths)]


def run_one(image_path, prompt, out_dir):
    # PaddleOCR-VL runs a fixed layout+recognition pipeline; there's no
    # free-form prompt to steer it, unlike the DeepSeek-OCR path.
    texts = run_many([image_path], out_dir)
    text = texts[0] if texts else ""
    if not text:
        md_files = sorted(Path(out_dir).glob("*.md"))
        text = "\n\n".join(f.read_text(encoding="utf-8") for f in md_files)
    return (text or "(no text output produced)"), None


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if req.get("image_paths"):
                texts = run_many(req["image_paths"], req["out_dir"])
                resp = {"ok": True, "texts": texts}
            else:
                text, img_out = run_one(req["image_path"], req.get("prompt", ""),
                                        req["out_dir"])
                resp = {"ok": True, "text": text, "image": img_out}
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print("###RESULT_JSON###" + json.dumps(resp), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve()
    else:
        image_path, prompt, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
        text, img_out = run_one(image_path, prompt, out_dir)
        print("###RESULT_JSON###" + json.dumps({"text": text, "image": img_out}))
