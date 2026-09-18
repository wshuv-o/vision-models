import os, sys, glob, pypdfium2 as pdfium
D = r"C:\path\to\your\folder\statements"
OUT = "st_img"
os.makedirs(OUT, exist_ok=True)
files = sorted(glob.glob(D + "/*.pdf"))
for fi, f in enumerate(files):
    tag = os.path.basename(f).split()[0].replace(".", "_")
    pdf = pdfium.PdfDocument(f)
    for i in range(len(pdf)):
        out = os.path.join(OUT, f"f{fi:02d}_{tag}_p{i:04d}.png")
        if os.path.exists(out):
            continue
        pdf[i].render(scale=3.0).to_pil().save(out)
    print(f"{fi:02d} {os.path.basename(f)[:40]:42s} {len(pdf)} pages", flush=True)
print("RENDER DONE", flush=True)
