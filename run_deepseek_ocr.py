import os
import sys
import torch
from transformers import AutoModel, AutoTokenizer

model_name = "deepseek-ai/DeepSeek-OCR"
image_file = sys.argv[1] if len(sys.argv) > 1 else "test_document.png"
output_dir = "ocr_output"
os.makedirs(output_dir, exist_ok=True)

print(f"Loading {model_name} ...")
tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)

attn_impl = "eager"
try:
    import flash_attn  # noqa
    attn_impl = "flash_attention_2"
except ImportError:
    pass
print(f"Using attn_implementation={attn_impl}")

model = AutoModel.from_pretrained(
    model_name,
    _attn_implementation=attn_impl,
    trust_remote_code=True,
    use_safetensors=True,
)
model = model.eval().cuda().to(torch.bfloat16)

prompt = "<image>\n<|grounding|>Convert the document to markdown. "

print(f"Running OCR on {image_file} ...")
res = model.infer(
    tokenizer,
    prompt=prompt,
    image_file=image_file,
    output_path=output_dir,
    base_size=1024,
    image_size=640,
    crop_mode=True,
    save_results=True,
    test_compress=True,
)

print("\n=== RESULT ===")
print(res)
print(f"\nFull results saved under: {output_dir}")
