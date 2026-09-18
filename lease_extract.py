"""Fill a lease-abstraction workbook from a pile of PDFs.

No document is assigned to a tenant up front: every file is a candidate for
every row, and the tenant's own identifiers decide what reaches the model.
That matters here because two rows can share a tenant name and differ only by
suite -- "Common Vines d/b/a The Tasting Room LLC" appears at both 0002 and
0104 -- so suite and DBA are weighted heavily and a chunk naming a *different*
suite is pushed down.

The 45 fields are asked for a section at a time (rent, escalation,
reimbursement, termination, renewal). One call carrying all of them tends to
return renewal terms in escalation fields; a focused call sees only the
clauses that bear on it, and a failure costs one section rather than the row.
"""
import os
import re
import time

import group_select
import lease_template as LT

# Which leaf columns belong together. Matched against the flattened column
# name, first hit wins, anything unmatched lands in "other".
SECTIONS = [
    ("identity", r"dba|status \(dark|leased rsf"),
    ("term_dates", r"lease start|lease end|lease term"),
    ("base_rent", r"annual base rent|annual rent psf|annual % increase"),
    ("escalation", r"escalation"),
    ("reimbursement", r"reimb|pro rata|by&by|gross-up|cam|ret|ins"),
    ("termination", r"terminat"),
    ("renewal", r"renewal"),
    ("docs_comments", r"available docs|missing docs|general comments|discrepanc"),
]

SYSTEM = """You abstract commercial leases.

You are given excerpts from documents that may relate to several tenants in one
building. Report ONLY what the documents state about THIS tenant and THIS
suite:

    {tenant}

Rules:
- If an excerpt concerns a different suite or a different tenant, ignore it
  entirely, even where the tenant name is similar. Suite numbers distinguish
  otherwise identical tenants.
- Use null for anything the documents do not state for this tenant. Never
  guess, never infer, never carry a figure across from another suite.
- Copy values as printed, including currency, percent and date formatting.
- For a "Reference" field, give the document name and the clause or section
  number the value came from.

Reply with ONLY a JSON object, no prose and no code fences, with exactly these
keys: {keys}"""


def sections_for(columns):
    """[(section, [column, ...])] over the fillable columns."""
    buckets = {}
    for c in columns:
        low = c["name"].lower()
        placed = False
        for name, pat in SECTIONS:
            if re.search(pat, low):
                buckets.setdefault(name, []).append(c)
                placed = True
                break
        if not placed:
            buckets.setdefault("other", []).append(c)
    order = [n for n, _ in SECTIONS] + ["other"]
    return [(n, buckets[n]) for n in order if n in buckets]


def tenant_terms(tenant):
    """Regexes that identify this tenant, and those that identify the others.

    The suite number is the sharpest signal available when two rows carry the
    same tenant name, so it is matched on its own as well as within a phrase.
    """
    mine, suite = [], None
    for k, v in tenant.items():
        if k == "_row" or not str(v).strip():
            continue
        kl = k.lower()
        if "suite" in kl:
            suite = str(v).strip()
        if any(h in kl for h in ("tenant", "dba", "suite")):
            for w in re.split(r"[^A-Za-z0-9']+", str(v)):
                if len(w) > 2 and w.lower() not in ("llc", "the", "dba", "d/b/a"):
                    mine.append(re.compile(r"\b" + re.escape(w), re.I))
    if suite:
        stripped = suite.lstrip("0") or suite
        mine.append(re.compile(rf"\b0*{re.escape(stripped)}\b"))
    return mine, suite


def score_for_tenant(text, mine, other_suites, per_field, loose, excl, fig_w):
    """Field relevance, plus a strong pull toward this tenant's own clauses."""
    base = group_select.score_chunk(text, per_field, loose, excl, fig_w)
    ident = sum(1 for p in mine if p.search(text))
    base += min(ident, 4) * 5.0
    # A chunk that names a different suite is about somebody else.
    for s in other_suites:
        if re.search(rf"\b0*{re.escape(s.lstrip('0') or s)}\b", text):
            base -= 12.0
            break
    return base


def select_for_tenant(files, tenant, other_suites, fields, instruction="",
                      char_budget=30000, per_file_floor=1, skip_labels=None):
    """(bundle, provenance) for one tenant and one section's fields.

    `skip_labels` holds (file, label) pairs already shown to the model. The
    second pass sets them aside: re-sending the same pages that produced a null
    the first time would only produce the same null again.
    """
    skip = set(skip_labels or ())
    mine, _suite = tenant_terms(tenant)
    per_field, loose, excl = group_select.build_terms(fields, instruction)
    fig_w = group_select.figure_weight_for(fields)

    ranked = []
    for path, units in files:
        fname = os.path.basename(path)
        for idx, (label, text) in enumerate(units):
            if (fname, label) in skip:
                continue
            s = score_for_tenant(text, mine, other_suites, per_field, loose,
                                 excl, fig_w)
            if idx == 0:
                s += 2.0
            ranked.append({"score": s, "path": path, "label": label,
                           "text": text, "idx": idx})
    if not ranked:                      # everything was already tried
        return "", []

    chosen, used, seen = [], 0, set()

    def take(rec):
        nonlocal used
        key = (rec["path"], rec["label"])
        if key in seen or used + len(rec["text"]) + 80 > char_budget:
            return False
        seen.add(key)
        chosen.append(rec)
        used += len(rec["text"]) + 80
        return True

    for path, _u in files:
        best = sorted((r for r in ranked if r["path"] == path),
                      key=lambda r: -r["score"])[:per_file_floor]
        for rec in best:
            if rec["score"] > 0:
                take(rec)
    for rec in sorted(ranked, key=lambda r: -r["score"]):
        if rec["score"] <= 0:
            break
        take(rec)

    chosen.sort(key=lambda r: (r["path"], r["idx"]))
    parts, prov = [], []
    for rec in chosen:
        fname = os.path.basename(rec["path"])
        parts.append(f"----- FILE: {fname} | {rec['label']} -----\n{rec['text']}")
        prov.append({"file": fname, "label": rec["label"],
                     "score": round(rec["score"], 1)})
    return "\n\n".join(parts), prov


# ------------------------------------------------------------------ the run
def json_schema(fields):
    """Enforced by the server. Asking for JSON in the prompt is not enough:
    gpt-oss will otherwise answer a question conversationally now and then."""
    props = {f: {"type": ["string", "null"]} for f in fields}
    return {"type": "object", "properties": props, "required": list(fields)}


# Same documents in, same spreadsheet out. temperature=0 alone is not enough:
# without a fixed seed Ollama can still sample differently between runs, so
# two identical runs disagree on a handful of cells and neither is obviously
# wrong. Pinning both makes a re-run reproduce the previous answer exactly.
SEED = int(os.environ.get("LEASE_SEED", "42"))


def call_model(model, bundle, fields, tenant_label, num_ctx=16384, timeout=1800,
               seed=None):
    import requests

    body = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": SYSTEM.format(keys=", ".join(fields), tenant=tenant_label)},
            {"role": "user", "content": bundle},
        ],
        "stream": False,
        "format": json_schema(fields),
        "options": {"temperature": 0, "top_p": 1, "top_k": 1,
                    "seed": SEED if seed is None else int(seed),
                    "num_ctx": int(num_ctx)},
    }
    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    # The first call after an OCR run can fail while the card is still being
    # handed back: Ollama answers 500 because the model cannot be allocated
    # yet. That is transient and worth waiting out -- losing a whole section of
    # a lease to it is not acceptable.
    last = None
    for attempt in range(4):
        try:
            r = requests.post(f"{url}/api/chat", json=body, timeout=timeout)
            r.raise_for_status()
            return (r.json().get("message") or {}).get("content", "") or ""
        except requests.HTTPError as e:
            last = e
            if e.response is None or e.response.status_code < 500:
                raise
        except requests.ConnectionError as e:
            last = e
        time.sleep(10 * (attempt + 1))
    raise last


def parse_obj(reply):
    import json

    s = re.sub(r"^```(?:json)?|```$", "", (reply or "").strip(), flags=re.M).strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        v = json.loads(s[a:b + 1])
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def _ask(files, tenant, others, cols, label, model, budget, num_ctx, log,
         tag, skip_labels=None):
    """One model call for one set of columns. Returns (values, sources, prov)."""
    fields = [c["name"] for c in cols]
    bundle, prov = select_for_tenant(
        files, tenant, others, fields,
        instruction=f"lease terms for {label}", char_budget=budget,
        skip_labels=skip_labels)
    if not bundle.strip():
        log(f"    {tag}: no relevant text found")
        return {}, {}, prov
    try:
        reply = call_model(model, bundle, fields, label, num_ctx)
        obj = parse_obj(reply)
    except Exception as e:
        log(f"    {tag}: FAILED {type(e).__name__}: {e}")
        return {}, {}, prov
    if obj is None:
        log(f"    {tag}: reply was not JSON")
        return {}, {}, prov
    values, sources = {}, {}
    for c in cols:
        v = obj.get(c["name"])
        if v not in (None, "", "null"):
            values[c["name"]] = v
            sources[c["name"]] = "; ".join(f"{q['file']}|{q['label']}"
                                           for q in prov[:3])
    log(f"    {tag}: {len(values)}/{len(fields)} from {len(prov)} excerpt(s)")
    return values, sources, prov


def run_tenants(files, columns, tenants, model="gpt-oss:20b", char_budget=30000,
                num_ctx=16384, log=print, on_row=None, second_pass=True):
    """Yield one record per tenant.

    Pass 1 asks section by section. Pass 2 then re-asks ONLY the fields that
    came back empty, and -- this is the part that makes it worth doing --
    rebuilds retrieval from just those fields. In pass 1 a section's bundle is
    chosen for fifteen fields at once, so the evidence for the two that failed
    competes with thirteen that succeeded. Asking again for the stragglers
    alone selects different pages, with a wider budget and the pass-1 excerpts
    set aside.
    """
    fill = LT.fillable_columns(columns, tenants)
    secs = sections_for(fill)
    suites = [str(t.get("Suite", "")).strip() for t in tenants]

    for ti, tenant in enumerate(tenants, 1):
        label = LT.describe_tenant(tenant)
        mine_suite = str(tenant.get("Suite", "")).strip()
        others = [s for s in suites if s and s != mine_suite]
        values, sources, used_labels = {}, {}, set()
        t0 = time.time()
        log(f"[{ti}/{len(tenants)}] {label}")

        for sname, cols in secs:
            v, sc, prov = _ask(files, tenant, others, cols, label, model,
                               char_budget, num_ctx, log, f"pass1 {sname}")
            values.update(v)
            sources.update(sc)
            used_labels |= {(q["file"], q["label"]) for q in prov}

        if second_pass:
            for sname, cols in secs:
                missing = [c for c in cols if c["name"] not in values]
                if not missing:
                    continue
                v, sc, _p = _ask(
                    files, tenant, others, missing, label, model,
                    int(char_budget * 1.5), num_ctx, log,
                    f"pass2 {sname} ({len(missing)} left)",
                    skip_labels=used_labels)
                for k in v:
                    sources[k] = (sc[k] + "  [2nd pass]")
                values.update(v)

        rec = {"_row": tenant["_row"], "_label": label,
               "values": values, "sources": sources,
               "seconds": round(time.time() - t0, 1)}
        log(f"  {label}: {len(values)}/{len(fill)} field(s) in {rec['seconds']}s")
        if on_row:
            on_row(rec)
        yield rec


def fill_workbook(template_path, out_path, columns, results, header_row=2):
    """Write values into a copy of the template, preserving its layout.

    A second sheet records where every value came from; without it a filled
    cell and a guessed cell look identical.
    """
    import openpyxl

    wb = openpyxl.load_workbook(template_path)
    ws = wb.worksheets[0]
    by_name = {c["name"]: c["idx"] for c in columns}

    n = 0
    for rec in results:
        excel_row = rec["_row"] + 1          # openpyxl is 1-indexed
        for name, val in rec["values"].items():
            idx = by_name.get(name)
            if idx is None:
                continue
            ws.cell(row=excel_row, column=idx + 1).value = val
            n += 1

    src = wb.create_sheet("Sources")
    src.append(["Row", "Tenant", "Column", "Value", "Came from"])
    for rec in results:
        for name, val in rec["values"].items():
            src.append([rec["_row"] + 1, rec["_label"], name, str(val),
                        rec["sources"].get(name, "")])
    for col, width in zip("ABCDE", (6, 42, 40, 40, 60)):
        src.column_dimensions[col].width = width

    wb.save(out_path)
    return n
