"""Pick the parts of a folder's documents that are worth sending to a model.

A loan folder holds an OM of two hundred pages, a rent roll, a term sheet and
a handful of letters, and the dozen facts wanted from it are scattered across
them. Sending everything is impossible; sending the wrong part is worse,
because the answer comes back confident and wrong.

What gets searched for comes from the field names and the instruction given
for this run -- nothing about loans, appraisals or any other subject is
written into this file. That was a deliberate change: an earlier version
hard-coded appraisal vocabulary and would have been worse than useless on a
contract or a medical record.
"""
import re

STOP = {
    "the", "a", "an", "of", "or", "and", "is", "are", "to", "for", "in", "on",
    "at", "by", "with", "from", "this", "that", "it", "its", "be", "as", "if",
    "what", "which", "any", "all", "per", "each", "value", "data", "info",
    "information", "extract", "field", "fields", "document", "documents",
    "file", "files", "please", "give", "find", "get", "name",
}

MONEY = re.compile(r"[$€£]\s?[\d,]{3,}")
PCT = re.compile(r"\d{1,3}(?:\.\d+)?\s?%")
DATE = re.compile(r"(?:[A-Z][a-z]{2,9}\s+\d{1,2},\s+\d{4})|(?:\d{1,2}/\d{1,2}/\d{2,4})")
NUM = re.compile(r"\b\d[\d,]{2,}(?:\.\d+)?\b")


def _words(text):
    """field names and prose both reduce to the same bag of search words."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text))
    parts = re.split(r"[^A-Za-z0-9]+", text)
    return [p.lower() for p in parts if len(p) > 2 and p.lower() not in STOP]


# Words in a FIELD NAME that suggest the answer is a figure. Derived from what
# is asked for, not from any document's layout -- ask for "sponsor" and no
# numeric preference is applied at all.
NUMERIC_HINT = re.compile(
    r"amount|rate|value|price|cost|total|balance|spread|index|date|year|term|"
    r"percent|pct|ratio|size|sqft|square|count|number|psf|yield|noi|income|"
    r"expense|fee|payment|ltv|dscr", re.I)


def figure_weight_for(fields):
    """How much a chunk's figures should count, given what is being asked for.

    All-textual field sets get none: a list of names and yes/no answers should
    not rank a table of numbers above the sentence that holds the answer.
    """
    if not fields:
        return 0.0
    numeric = sum(1 for f in fields if NUMERIC_HINT.search(str(f)))
    return 0.8 * (numeric / len(fields))


def build_terms(fields, instruction="", extra_terms=(), exclude=()):
    """[(field_name, [regex, ...])] plus the patterns that damn a chunk.

    Each field keeps its own term list so a chunk mentioning four different
    fields scores above one that mentions a single field four times.
    """
    per_field = []
    for f in fields:
        ws = _words(f)
        if ws:
            per_field.append((f, [re.compile(r"\b" + re.escape(w), re.I) for w in ws]))
    loose = _words(instruction) + [str(t).lower() for t in extra_terms]
    loose_pats = [re.compile(r"\b" + re.escape(w), re.I) for w in dict.fromkeys(loose)]
    excl_pats = [re.compile(re.escape(str(x)), re.I) for x in exclude if str(x).strip()]
    return per_field, loose_pats, excl_pats


def score_chunk(text, per_field, loose_pats, excl_pats, figure_weight=0.8):
    """How likely is this chunk to hold the answers?

    Field coverage is the whole of the signal. Figures only add to it, and
    only when the run is actually asking for figures -- an earlier version
    subtracted from any chunk that mentioned a field without a number nearby,
    which pushed down exactly the paragraph naming a sponsor or stating
    whether a loan is interest-only. Answers are not always numbers.
    """
    if not text or not text.strip():
        return 0.0
    hits = 0
    for _name, pats in per_field:
        if any(p.search(text) for p in pats):
            hits += 1
    score = hits * 4.0
    score += min(sum(1 for p in loose_pats if p.search(text)), 8) * 0.5

    if hits and figure_weight:
        figures = (len(MONEY.findall(text)) + len(PCT.findall(text))
                   + len(DATE.findall(text)) + min(len(NUM.findall(text)), 10))
        score += min(figures, 12) * figure_weight

    for p in excl_pats:
        if p.search(text):
            score -= 8.0
    return score


def select_for_group(files, per_field, loose_pats, excl_pats,
                     char_budget=40000, per_file_floor=2, head_units=1,
                     figure_weight=0.8):
    """Choose chunks from across every file in one group.

    Budget is shared, but each file keeps a small reserved allowance. Without
    that, a two-hundred-page offering memorandum wins every slot on volume
    alone and a five-page term sheet -- often where the actual terms are
    stated -- never reaches the model.

    `files` is [(path, [(label, text)])]. Returns (bundle_text, provenance).
    """
    ranked = []
    for path, units in files:
        for idx, (label, text) in enumerate(units):
            s = score_chunk(text, per_field, loose_pats, excl_pats,
                            figure_weight=figure_weight)
            # Documents of every kind tend to identify themselves at the top --
            # title page, letterhead, header row. That opening reads as prose
            # and would otherwise never be picked.
            if idx < head_units:
                s += 3.0
            ranked.append({"score": s, "path": path, "label": label,
                           "text": text, "idx": idx})

    chosen, used, seen = [], 0, set()

    def take(rec):
        nonlocal used
        key = (rec["path"], rec["label"])
        if key in seen:
            return False
        block = len(rec["text"]) + 80
        if used + block > char_budget:
            return False
        seen.add(key)
        chosen.append(rec)
        used += block
        return True

    # reserved allowance first, best-scoring chunks per file
    for path, _units in files:
        mine = sorted((r for r in ranked if r["path"] == path),
                      key=lambda r: -r["score"])
        for rec in mine[:per_file_floor]:
            if rec["score"] > 0 or rec["idx"] == 0:
                take(rec)

    # then the open field, best first
    for rec in sorted(ranked, key=lambda r: -r["score"]):
        if rec["score"] <= 0:
            break
        take(rec)

    chosen.sort(key=lambda r: (r["path"], r["idx"]))
    parts, prov = [], []
    import os
    for rec in chosen:
        fname = os.path.basename(rec["path"])
        parts.append(f"----- FILE: {fname} | {rec['label']} -----\n{rec['text']}")
        prov.append({"file": fname, "label": rec["label"],
                     "score": round(rec["score"], 1)})
    return "\n\n".join(parts), prov
