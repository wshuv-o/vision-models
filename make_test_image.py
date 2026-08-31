from PIL import Image, ImageDraw, ImageFont

img = Image.new("RGB", (900, 500), "white")
d = ImageDraw.Draw(img)

try:
    font_big = ImageFont.truetype("arial.ttf", 36)
    font_small = ImageFont.truetype("arial.ttf", 22)
except Exception:
    font_big = ImageFont.load_default()
    font_small = ImageFont.load_default()

d.text((40, 30), "INVOICE #A-2026-0831", fill="black", font=font_big)
d.line((40, 85, 860, 85), fill="black", width=2)
d.text((40, 110), "Bill To: Acme Robotics Ltd.", fill="black", font=font_small)
d.text((40, 145), "Date: 2026-08-31", fill="black", font=font_small)
d.text((40, 180), "Item                Qty   Unit Price   Total", fill="black", font=font_small)
d.line((40, 215, 860, 215), fill="black", width=1)
d.text((40, 230), "RTX 5080 GPU         1      $999.00     $999.00", fill="black", font=font_small)
d.text((40, 260), "NVMe SSD 2TB          2      $149.00     $298.00", fill="black", font=font_small)
d.line((40, 300, 860, 300), fill="black", width=1)
d.text((550, 330), "Subtotal: $1297.00", fill="black", font=font_small)
d.text((550, 360), "Tax (8%):  $103.76", fill="black", font=font_small)
d.text((550, 390), "TOTAL:     $1400.76", fill="black", font=font_big)
d.rectangle((30, 20, 870, 480), outline="black", width=2)

img.save("test_document.png")
print("saved test_document.png")
