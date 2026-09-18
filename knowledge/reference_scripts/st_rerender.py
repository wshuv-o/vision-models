import os, glob, re, pypdfium2 as pdfium
D = r"C:\path\to\your\folder\statements"
files = sorted(glob.glob(D + "/*.pdf"))
OUT="st_hi"; os.makedirs(OUT, exist_ok=True)
for name in open('st_redo.txt').read().split():
    m = re.match(r'f(\d+)_(.+)_p(\d+)$', name)
    fi, pg = int(m.group(1)), int(m.group(3))
    out = os.path.join(OUT, name + ".png")
    if os.path.exists(out): continue
    pdf = pdfium.PdfDocument(files[fi])
    pdf[pg].render(scale=6.0).to_pil().save(out)
print("re-rendered", len(glob.glob(OUT+"/*.png")))
