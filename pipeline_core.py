"""Core of the document pipeline: extract -> match -> validate -> remember.

Design notes
------------
* Extraction is exact for digital PDFs (text layer). OCR is a fallback for
  scans only, because OCR on a digital PDF introduces errors - proven on the
  YTD P&L, where it misread a $448K variance as $198K.
* Matching is tiered cheapest-first: learned correction -> exact -> fuzzy ->
  LLM. Every tier reports a confidence so the UI knows what to show you.
* Corrections you make are persisted keyed by (source_profile, raw_label) and
  reused forever. That is the trial-and-correction loop.
* Tie-out is a deterministic oracle: rows must sum to their own total. It
  catches extraction errors without a human looking.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pandas as pd
import pdfplumber
import requests
from rapidfuzz import fuzz, process as rf_process

HERE = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))

CANON_PATH = HOME / "canonical_items.json"
MEMORY_PATH = HOME / "pipeline_memory.json"

OLLAMA = "http://127.0.0.1:11434"
LLM_MODEL = os.environ.get("PIPELINE_LLM", "gpt-oss-64k:latest")
VLLM_URL = "http://localhost:8000/v1"

NUM_RE = re.compile(r"^\(?[-+]?[\d,]*\.?\d+\)?%?$")
MIN_TEXT_CHARS = 120


# ----------------------------------------------------------------- numbers
def is_num(t: str) -> bool:
    t = (t or "").strip()
    return bool(t) and bool(NUM_RE.match(t)) and any(c.isdigit() for c in t)


def to_num(t: str):
    s = (t or "").strip().replace(",", "").replace("%", "").replace("$", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


# ----------------------------------------------------------------- memory
def load_memory() -> dict:
    if MEMORY_PATH.exists():
        try:
            return json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"corrections": {}, "stats": {"applied": 0, "learned": 0}}


def save_memory(mem: dict) -> None:
    MEMORY_PATH.write_text(json.dumps(mem, indent=1), encoding="utf-8")


def remember(mem: dict, profile: str, raw: str, canonical: str) -> None:
    mem.setdefault("corrections", {})[f"{profile}||{norm(raw)}"] = canonical
    mem.setdefault("stats", {}).setdefault("learned", 0)
    mem["stats"]["learned"] += 1
    save_memory(mem)


def recall(mem: dict, profile: str, raw: str):
    return mem.get("corrections", {}).get(f"{profile}||{norm(raw)}")


def load_canonical() -> list[str]:
    if CANON_PATH.exists():
        return json.loads(CANON_PATH.read_text(encoding="utf-8"))
    return []


# ----------------------------------------------------------------- extract
def norm(s: str) -> str:
    """Normalise a label for comparison: lowercase, collapse punctuation."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def source_profile(pdf_path: str) -> str:
    """Fingerprint the document's ORIGIN, so corrections generalise to the
    next statement from the same system rather than the same file."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            md = pdf.metadata or {}
            producer = str(md.get("Producer") or md.get("Creator") or "")[:40]
            first = (pdf.pages[0].extract_text() or "")[:400]
        head = " ".join(norm(l)[:40] for l in first.splitlines()[:4] if l.strip())
        return f"{norm(producer)}|{head}"[:160] or "unknown"
    except Exception:
        return "unknown"


def _cluster(xs, tol):
    out = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [sum(g) / len(g) for g in out]


def auto_columns(rights, page_width):
    """Column count that survives the widest band of tolerances (stability
    plateau) - avoids per-document tuning."""
    if len(rights) < 4:
        return [], 0.0
    tols = list(range(4, max(10, int(page_width * 0.08)), 2))
    from collections import Counter
    counts, layouts = [], {}
    for t in tols:
        c = _cluster(rights, t)
        counts.append(len(c))
        layouts.setdefault(len(c), c)
    tally = Counter(c for c in counts if c >= 2) or Counter(counts)
    best, hits = tally.most_common(1)[0]
    return layouts[best], hits / len(tols)


def extract_page(page):
    words = page.extract_words(keep_blank_chars=False)
    if not words:
        return None, 0.0
    rows = {}
    for w in words:
        rows.setdefault(round(w["top"] / 4) * 4, []).append(w)
    rights = [w["x1"] for ws in rows.values() for w in ws if is_num(w["text"])]
    cols, conf = auto_columns(rights, page.width)
    if not cols:
        return None, 0.0
    data = []
    for k in sorted(rows):
        line = sorted(rows[k], key=lambda w: w["x0"])
        label = " ".join(w["text"] for w in line if not is_num(w["text"])).strip()
        cells = [None] * len(cols)
        for w in line:
            if not is_num(w["text"]):
                continue
            j = min(range(len(cols)), key=lambda i: abs(cols[i] - w["x1"]))
            cells[j] = to_num(w["text"])
        if label or any(c is not None for c in cells):
            data.append([label] + cells)
    df = pd.DataFrame(data, columns=["Line Item"] + [f"C{i+1}" for i in range(len(cols))])
    return df.dropna(axis=1, how="all"), conf


def is_digital(pdf_path) -> bool:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return sum(len(p.extract_text() or "") for p in pdf.pages) >= MIN_TEXT_CHARS
    except Exception:
        return False


def extract(pdf_path):
    """-> (list[(sheet, df)], method, confidence, seconds)"""
    t0 = time.perf_counter()
    out, confs = [], []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if len(page.extract_text() or "") < MIN_TEXT_CHARS:
                continue
            df, c = extract_page(page)
            if df is not None and not df.empty:
                out.append((f"P{i+1}", df))
                confs.append(c)
    method = "digital (exact)" if out else "none"
    conf = round(sum(confs) / len(confs), 2) if confs else 0.0
    return out, method, conf, round(time.perf_counter() - t0, 3)


# ----------------------------------------------------------------- match
def match_one(raw, canon, mem, profile, fuzzy_cut=88):
    """-> (canonical|None, confidence 0..1, tier)"""
    if not raw or not raw.strip():
        return None, 0.0, "empty"
    hit = recall(mem, profile, raw)
    if hit:
        return hit, 1.0, "learned"
    n = norm(raw)
    for c in canon:
        if norm(c) == n:
            return c, 1.0, "exact"
    best = rf_process.extractOne(raw, canon, scorer=fuzz.WRatio)
    if best and best[1] >= fuzzy_cut:
        return best[0], round(best[1] / 100, 2), "fuzzy"
    if best:
        return best[0], round(best[1] / 100, 2), "weak"
    return None, 0.0, "none"


DATEISH = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}:\d{2}\s*[AP]M", re.I)
META_HINT = re.compile(r"^(period|book|database|prepared|page|report|statement|"
                       r"\*|as of|for the|month|year|budget comparison)\b", re.I)


def is_line_item(raw: str, numeric_cells) -> bool:
    """A real line item has a label AND at least one number. Report headers,
    timestamps and entity names have one or the other, not both."""
    raw = (raw or "").strip()
    if not raw:
        return False
    if DATEISH.search(raw) or META_HINT.match(raw):
        return False
    return any(v is not None for v in numeric_cells)


def match_df(df, canon, mem, profile, fuzzy_cut=88):
    numcols = [c for c in df.columns if c != "Line Item"]
    rows = []
    for _, r in df.iterrows():
        raw = r["Line Item"] if pd.notna(r["Line Item"]) else ""
        cells = [r[c] for c in numcols]
        if not is_line_item(raw, cells):
            rows.append({"raw": raw, "canonical": "", "confidence": 0.0, "tier": "skip"})
            continue
        c, conf, tier = match_one(raw, canon, mem, profile, fuzzy_cut)
        rows.append({"raw": raw, "canonical": c or "", "confidence": conf, "tier": tier})
    return pd.DataFrame(rows)


def llm_match(raw, canon, k=25, model=None):
    """Ask the local LLM to choose among the closest candidates. Only used for
    the ambiguous tail - sending all 277 items every time is slow and worse."""
    model = model or LLM_MODEL
    cands = [c for c, _, _ in rf_process.extract(raw, canon, scorer=fuzz.WRatio, limit=k)]
    prompt = (
        "You map accounting line items to a fixed chart of accounts.\n"
        f'Source line item: "{raw}"\n\nCandidates:\n'
        + "\n".join(f"{i+1}. {c}" for i, c in enumerate(cands))
        + "\n\nReply with ONLY the number of the best match, or 0 if none fit."
    )
    try:
        r = requests.post(f"{OLLAMA}/api/generate",
                          json={"model": model, "prompt": prompt, "stream": False,
                                "options": {"temperature": 0, "num_predict": 8}},
                          timeout=120)
        txt = r.json().get("response", "")
        m = re.search(r"\d+", txt)
        if not m:
            return None, 0.0
        i = int(m.group())
        if 1 <= i <= len(cands):
            return cands[i - 1], 0.75
    except Exception:
        pass
    return None, 0.0


# ----------------------------------------------------------------- validate
def _check(sub, computed, reported, label):
    diff = (computed - reported).abs()
    ok = int((diff < 0.02).sum())
    fails = sub[diff >= 0.02].copy()
    if not fails.empty:
        fails["_rule"] = label
        fails["_computed"] = computed[diff >= 0.02]
        fails["_reported"] = reported[diff >= 0.02]
        fails["_diff"] = diff[diff >= 0.02]
    return ok, len(sub), fails


def tie_out(df):
    """Deterministic arithmetic oracle. Two layouts are recognised:

      periodic : sum(period columns) == Total          (monthly statements)
      variance : Actual - Budget == Variance           (budget comparisons)

    -> (ok, checked, pct, failures)
    """
    if df is None or df.empty:
        return 0, 0, None, pd.DataFrame()
    num = [c for c in df.columns if c != "Line Item"]
    if len(num) < 3:
        return 0, 0, None, pd.DataFrame()

    # --- variance layout: consecutive (actual, budget, variance) triples ---
    if len(num) >= 3:
        best = None
        for i in range(len(num) - 2):
            a, b, v = num[i], num[i + 1], num[i + 2]
            sub = df.dropna(subset=[a, b, v])
            if len(sub) < 5:
                continue
            ok, tot, fails = _check(sub, sub[a] - sub[b], sub[v], f"{a}-{b}={v}")
            if tot and (best is None or ok / tot > best[0] / max(best[1], 1)):
                best = (ok, tot, fails)
        if best and best[1] and best[0] / best[1] >= 0.5:
            ok, tot, fails = best
            return ok, tot, round(100 * ok / tot, 1), fails

    # --- periodic layout: parts sum to a total column ---
    tcol = next((c for c in num if str(c).strip().lower() in ("total", "ytd", "sum")), num[-1])
    parts = [c for c in num if c != tcol]
    sub = df.dropna(subset=[tcol])
    sub = sub[sub[parts].notna().sum(axis=1) >= 2]
    if sub.empty:
        return 0, 0, None, pd.DataFrame()
    ok, tot, fails = _check(sub, sub[parts].sum(axis=1), sub[tcol], f"sum(parts)={tcol}")
    return ok, tot, round(100 * ok / tot, 1), fails


def ask_llm(prompt, context="", model=None, max_chars=24000):
    model = model or LLM_MODEL
    full = (f"Context from the current document:\n{context[:max_chars]}\n\n{prompt}"
            if context else prompt)
    t = time.perf_counter()
    try:
        r = requests.post(f"{OLLAMA}/api/generate",
                          json={"model": model, "prompt": full, "stream": False,
                                "options": {"temperature": 0, "num_predict": 1200}},
                          timeout=600)
        d = r.json()
        dt = time.perf_counter() - t
        ec = d.get("eval_count", 0) or 0
        ed = max(d.get("eval_duration", 1) / 1e9, 1e-3)
        return d.get("response", ""), f"{dt:.1f}s · {ec} tok · {ec/ed:.0f} tok/s"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}", ""
