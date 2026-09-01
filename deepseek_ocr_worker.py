import os
import sys
import json

_here = os.path.dirname(os.path.abspath(__file__))
_cert_bundle = os.path.join(_here, "combined_cacert.pem")
if os.path.exists(_cert_bundle):
    os.environ.setdefault("SSL_CERT_FILE", _cert_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _cert_bundle)
os.environ["HF_HUB_DISABLE_XET"] = "1"

import torch
from transformers import AutoModel, AutoTokenizer

_MODEL = None
_TOKENIZER = None


def _load():
    global _MODEL, _TOKENIZER
    if _MODEL is not None:
        return
    _TOKENIZER = AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-OCR", trust_remote_code=True)
    attn_impl = "eager"
    try:
        import flash_attn  # noqa: F401
        attn_impl = "flash_attention_2"
    except ImportError:
        pass
    _MODEL = AutoModel.from_pretrained(
        "deepseek-ai/DeepSeek-OCR",
        _attn_implementation=attn_impl,
        trust_remote_code=True,
        use_safetensors=True,
    )
    _MODEL = _MODEL.eval().cuda().to(torch.bfloat16)


def run_one(image_path, prompt, out_dir):
    _load()
    if "<image>" not in prompt:
        prompt = "<image>\n" + prompt
    os.makedirs(out_dir, exist_ok=True)
    _MODEL.infer(
        _TOKENIZER,
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


def serve():
    # Persistent worker: one JSON request per line on stdin, one JSON response
    # per line on stdout (prefixed so it can't be confused with library logs).
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
