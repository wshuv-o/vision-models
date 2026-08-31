import sys
from transformers import Qwen3VLForConditionalGeneration, AutoProcessor

model_name = "Qwen/Qwen3-VL-8B-Instruct"
image_file = sys.argv[1] if len(sys.argv) > 1 else "test_document.png"
question = sys.argv[2] if len(sys.argv) > 2 else (
    "Read every piece of text in this image and transcribe it exactly. "
    "Then answer: what is the TOTAL amount on this invoice?"
)

print(f"Loading {model_name} ...")
model = Qwen3VLForConditionalGeneration.from_pretrained(
    model_name,
    dtype="auto",
    device_map="auto",
)
processor = AutoProcessor.from_pretrained(model_name)

messages = [
    {
        "role": "user",
        "content": [
            {"type": "image", "image": f"file://{image_file}"},
            {"type": "text", "text": question},
        ],
    }
]

inputs = processor.apply_chat_template(
    messages,
    tokenize=True,
    add_generation_prompt=True,
    return_dict=True,
    return_tensors="pt",
)
inputs = inputs.to(model.device)

print("Generating...")
generated_ids = model.generate(**inputs, max_new_tokens=512)
generated_ids_trimmed = [
    out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
]
output_text = processor.batch_decode(
    generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
)

print("\n=== RESULT ===")
print(output_text[0])
