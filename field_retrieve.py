"""Find the few pages of a long appraisal that actually hold the answers.

A 190-page appraisal mentions "capitalization rate" on twenty pages and almost
all of them are comparable sales. The subject property's own figures live in a
handful of summary tables whose position moves from report to report, so the
pages have to be found by content rather than by number.

Scoring is deliberately keyword-and-number based rather than embedding based.
The phrase "direct capitalization" scores highest on methodology prose that
contains no figures at all ("the primary methods are direct capitalization,
room revenue multiplier and discounted cash flow"), so semantic similarity
retrieves exactly the wrong pages. What matters is a page carrying both a
label and a number, in a summary rather than a comparables section.
"""
import re

import pypdfium2 as pdfium

# Pages that talk about the subject's own conclusions.
GOOD = [
    (r"executive summary", 6.0),
    (r"salient facts", 6.0),
    (r"value conclusion", 5.0),
    (r"reconcil", 4.0),
    (r"key valuation assumption", 5.0),
    (r"property summary", 3.0),
    (r"subject property", 1.0),
    (r"as[- ]is", 1.5),
    (r"upon stabilization", 1.5),
]

# Pages that are about *other* properties. These are the trap: they are dense
# with exactly the words we are looking for.
BAD = [
    (r"comparable", -6.0),
    (r"sale no\.", -5.0),
    (r"improved sale", -5.0),
    (r"rent comp", -4.0),
    (r"adjustment", -3.0),
    (r"transactional adjustment", -4.0),
    (r"listing", -2.0),
    (r"survey", -1.5),
]

FIELD_HINTS = {
    "name": [r"commonly known as", r"appraisal of", r"property name"],
    "address": [r"located at", r"property address", r"street address"],
    "value": [r"market value", r"value conclusion", r"reconciled value",
              r"opinion of value", r"as[- ]is value"],
    "date": [r"date of value", r"effective date", r"valuation date"],
    "rate": [r"capitali[sz]ation rate", r"cap rate", r"discount rate"],
    "physical": [r"year built", r"year renovated", r"property type",
                 r"number of rooms", r"net rentable", r"yr\.? built"],
}

MONEY = re.compile(r"\$\s?[\d,]{6,}")
PCT = re.compile(r"\d{1,2}\.\d{1,2}\s?%")
DATE = re.compile(r"[A-Z][a-z]{2,9}\s+\d{1,2},\s+\d{4}")


def page_texts(pdf_path, max_pages=None):
    """[(page_no, text)] from the PDF's own text layer."""
    doc = pdfium.PdfDocument(str(pdf_path))
    n = len(doc)
    if max_pages:
        n = min(n, max_pages)
    out = []
    for i in range(n):
        try:
            t = doc[i].get_textpage().get_text_range() or ""
        except Exception:
            t = ""
        out.append((i + 1, " ".join(t.split())))
    return out


def text_layer_ok(pages, sample=10):
    """False when the text layer is missing or mojibake (broken ToUnicode)."""
    joined = " ".join(t for _, t in pages[:sample])
    if len(joined) < 200 * sample / 2:
        return False
    letters = sum(ch.isalpha() or ch.isspace() or ch.isdigit() for ch in joined)
    return letters / max(len(joined), 1) > 0.75


def score_page(text):
    """How likely is this page to hold the subject's own summary figures?"""
    low = text.lower()
    score = 0.0
    for rx, w in GOOD:
        if re.search(rx, low):
            score += w
    for rx, w in BAD:
        if re.search(rx, low):
            score += w          # negative
    fields = 0
    for hints in FIELD_HINTS.values():
        if any(re.search(h, low) for h in hints):
            fields += 1
    score += fields * 2.0
    # A label with no number is prose, which is exactly what we do not want.
    numbers = len(MONEY.findall(text)) + len(PCT.findall(text)) + len(DATE.findall(text))
    if fields and numbers:
        score += min(numbers, 8) * 1.2
    elif fields and not numbers:
        score -= 3.0
    return score


def select_pages(pages, top_k=8, always_first=3):
    """Page numbers worth sending on: the front matter plus the best summaries.

    The first pages are taken unconditionally -- the cover and the transmittal
    letter carry the property name and address in every report, and they score
    poorly because they are prose.
    """
    chosen = {p for p, _ in pages[:always_first]}
    ranked = sorted(((score_page(t), p) for p, t in pages if p not in chosen),
                    reverse=True)
    for sc, p in ranked:
        if len(chosen) >= top_k + always_first or sc <= 0:
            break
        chosen.add(p)
    return sorted(chosen), {p: round(s, 1) for s, p in ranked[:12]}


def build_bundle(pages, chosen, char_budget=40000):
    """The selected pages, marked up, trimmed to a budget."""
    by_no = dict(pages)
    parts, used = [], []
    for p in chosen:
        t = by_no.get(p, "")
        if not t:
            continue
        block = f"----- PAGE {p} -----\n{t}"
        if sum(len(x) for x in parts) + len(block) > char_budget:
            break
        parts.append(block)
        used.append(p)
    return "\n\n".join(parts), used
