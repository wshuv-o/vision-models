"""Shared PDF -> page-image rendering.

Used by both the CLI (`pdf_vllm_pages.py`) and the Gradio app so page
handling (rotation, cropping, page selection) behaves identically in both.
"""
import os
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image


def parse_pages(spec, total):
    """'1-3', '1,4,7', '2-' -> zero-based page indices."""
    if not spec or not str(spec).strip():
        return list(range(total))
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) - 1 if a.strip() else 0
            end = int(b) if b.strip() else total
            out.extend(range(start, end))
        else:
            out.append(int(part) - 1)
    seen, uniq = set(), []
    for p in out:
        if 0 <= p < total and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def autocrop(img, pad=24, thresh=245):
    """Trim uniform white margins so the content fills the frame."""
    a = np.asarray(img.convert("L"))
    mask = a < thresh
    if not mask.any():
        return img, None
    rows = np.where(mask.any(1))[0]
    cols = np.where(mask.any(0))[0]
    box = (
        max(int(cols[0]) - pad, 0),
        max(int(rows[0]) - pad, 0),
        min(int(cols[-1]) + pad, img.width),
        min(int(rows[-1]) + pad, img.height),
    )
    return img.crop(box), box


def render_page(page, dpi=300, rotate="auto", crop=True):
    """Render one pdfium page to a PIL image.

    pypdfium2 does not apply the page's /Rotate flag on render, so a landscape
    sheet saved as rotated-portrait comes out sideways -- which makes the layout
    model file the whole table as a single 'image' block. Undo it here.
    """
    page_rot = page.get_rotation()
    rot = (360 - page_rot) % 360 if rotate == "auto" else int(rotate) % 360
    img = page.render(scale=dpi / 72.0, rotation=rot).to_pil()
    full = (img.width, img.height)
    box = None
    if crop:
        img, box = autocrop(img)
    info = {"page_rotation": page_rot, "applied_rotation": rot,
            "full_size": full, "crop_box": box, "size": (img.width, img.height)}
    return img, info


def render_pdf(pdf_path, out_dir, dpi=300, pages=None, rotate="auto", crop=True):
    """Render selected pages to PNG. Yields (page_no_1based, png_path, info)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf_path))
    total = len(doc)
    for pno in parse_pages(pages, total):
        img, info = render_page(doc[pno], dpi=dpi, rotate=rotate, crop=crop)
        png = out_dir / f"page_{pno + 1:03d}.png"
        img.save(png)
        info["total_pages"] = total
        yield pno + 1, str(png), info


def page_count(pdf_path):
    return len(pdfium.PdfDocument(str(pdf_path)))


def win_to_wsl(path):
    """Windows path -> WSL /mnt/<drive> path (pass-through if already POSIX)."""
    p = str(path)
    if len(p) > 1 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")
    return p.replace("\\", "/")
