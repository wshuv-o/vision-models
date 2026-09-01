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

_PIPELINE = None


def _load():
    global _PIPELINE
    if _PIPELINE is not None:
        return
    _PIPELINE = PaddleOCRVL(device="gpu:0")


def run_one(image_path, prompt, out_dir):
    # PaddleOCR-VL runs a fixed layout+recognition pipeline; there's no
    # free-form prompt to steer it, unlike the VLM/DeepSeek-OCR paths.
    _load()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _PIPELINE.predict(image_path)
    text_parts = []
    img_out = None
    for res in result:
        res.save_to_markdown(save_path=str(out_dir))
        md = res.markdown.get("markdown_texts", "") if hasattr(res, "markdown") else ""
        if md:
            text_parts.append(md)
    if not text_parts:
        md_files = sorted(out_dir.glob("*.md"))
        for f in md_files:
            text_parts.append(f.read_text(encoding="utf-8"))
    text = "\n\n".join(text_parts) if text_parts else "(no text output produced)"
    return text, img_out


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            text, img_out = run_one(req["image_path"], req["prompt"], req["out_dir"])
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
