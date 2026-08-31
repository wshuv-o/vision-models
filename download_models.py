import time
from huggingface_hub import snapshot_download

models = [
    "deepseek-ai/DeepSeek-OCR",
    "Qwen/Qwen3-VL-8B-Instruct",
]

for repo in models:
    print(f"=== downloading {repo} ===", flush=True)
    t0 = time.time()
    path = snapshot_download(repo_id=repo, ignore_patterns=["*.bin", "*.pth", "*.gguf", "*.onnx"])
    print(f"=== done {repo} -> {path} ({time.time()-t0:.0f}s) ===", flush=True)

print("ALL DOWNLOADS COMPLETE", flush=True)
