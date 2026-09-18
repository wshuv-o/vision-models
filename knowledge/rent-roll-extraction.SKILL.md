---
name: rent-roll-extraction
description: Extract commercial real-estate rent rolls, CAM/recovery reports, aged-receivables and similar system-generated PDF reports into verified Excel workbooks. Use whenever the user asks to pull data from a rent roll, CAM recon, recovery calculation, aged delinquency/receivables report, or any fixed-layout property-management PDF into Excel.
---

# Rent roll / property-report PDF → Excel

Hard-won rules from building extractors for several of these reports. Every
report is a different template — **never reuse column coordinates from another
report**, only reuse the method below.

## Non-negotiables

1. **Accuracy over completeness.** A missing value the user can see is far
   better than a wrong value they can't. When unsure, write the cell blank or
   as-printed, flag it YELLOW, and explain in a `Review Note` column.
2. **Never invent precision.** If the PDF prints `Jul-26` (month + year), do
   not silently write `07/26/2026`. Either keep it as printed or convert to the
   1st of the month **and say so**.
3. **Exclude totals** unless asked. Users almost always want line items only.
4. **Always validate against the report's own printed totals.** These reports
   footer their own subtotals — that is free ground truth. If your sums don't
   tie exactly, you have a bug.

## Method

### 1. Probe before writing any parser

```python
import pdfplumber
with pdfplumber.open(PDF, password=PW) as pdf:   # password= only if encrypted
    print(len(pdf.pages))
    for i, pg in enumerate(pdf.pages):
        print(i+1, len(pg.chars), len(pg.images), pg.width, pg.height)
```
- `chars == 0` and `images == 1` → scanned, no text layer → needs OCR
  (see "OCR fallback" below).
- Differing page size mid-document usually means a different report is
  appended (e.g. an income statement after the rent roll). Find where the rent
  roll ends and stop there.

Dump word coordinates, **not** `extract_text()`:
```python
def rows_of(words, tol=2.0):
    out, cur = [], []
    for w in sorted(words, key=lambda a: a["top"]):
        if cur and w["top"] - cur[-1]["top"] > tol:
            out.append(sorted(cur, key=lambda a: a["x0"])); cur = []
        cur.append(w)
    if cur: out.append(sorted(cur, key=lambda a: a["x0"]))
    return out

words = page.extract_words(x_tolerance=1.6, use_text_flow=True)
for r in rows_of(words):
    print(f"y={r[0]['top']:6.1f} | " + " | ".join(
        f"{w['text']}@{w['x0']:.0f}-{w['x1']:.0f}" for w in r))
```
`use_text_flow=True` matters: without it, a long tenant name printed *over* the
date column comes back with its characters interleaved into garbage.

While probing, **mask the values** (`re.sub(r'\d','#',t)`) if the data is
confidential — you only need the layout, not the figures.

### 2. Bin numeric columns on the RIGHT edge (x1)

These reports right-align numbers. `x0` moves left as a number grows a digit;
`x1` is rock stable (±0.1pt). Binning on `x0` silently breaks on the first
6-figure value.

```python
NUM_BINS = [("Leased SF", 250, 295), ("Monthly Base Rent", 342, 382), ...]
for w in row:
    for col, lo, hi in NUM_BINS:
        if lo < w["x1"] <= hi:
            got[col] = w["text"]; break
```
Text columns (names, descriptions) are left-aligned — bin those on `x0`.

**Check the zones don't overlap.** An overlapping Type/Suite zone once produced
`Office 21st` for a suite named `21st & 22nd Floors`.

### 3. Anchor each record on something that appears exactly once per record

Do **not** anchor on the tenant name. Observed failures:
- `** Waiting Tenant **` printed in the tenant column on the *second* line of
  another tenant's block → splits one record into two.
- Long names wrap over 2–3 lines → each wrap looks like a new record.

Good anchors: a building-ID token in a fixed column, or the first of a fixed
set of sub-rows (e.g. every block has exactly one `Real Estate Taxes` line and
it's always line 1). Verify by counting: anchor count must equal record count.

### 4. Multi-line is the norm

Expect all of these, often at once:
- **Wrapped tenant name** (up to 3 lines) and **wrapped description** (up to 4).
- **Rent steps / bumps** stacked below the record, sometimes as *two* physical
  lines each (date + amount on one, PSF on the next).
- Fields printed on line 2, not line 1 (lease end date, current PSF).
- A record's data **continuing across a page break**.
- A **footnote line** between the last data row and the next record.

Two independent stacks can interleave (rent steps on the right, expense rows in
the middle) — handle them separately by x-range, not by line number.

### 5. Totals hide in unexpected places

Filtering on `text.startswith("Total")` is not enough. Real examples that
slipped through and inflated sums:
- `Vacant Sqft: 17.58% 38,914` — a continuation line of the Totals block that
  starts with "Vacant".
- `Grand Total:` blocks at a different indent.

Better: detect a marker unique to the totals block (e.g. `"Sqft:" in text`),
and/or stop at a known y on the last page.

### 6. Glue and overlap artifacts

- Name butted against its date with no gap → one token
  (`NESTLE PURINA PETCARE COMPANY9/1/2022`). Split on a trailing date regex.
- Category butted against a code (`RECONCILI` + `NC`). If the trailing field
  has a known small value set (CH/CR/NC), strip it as a suffix.
- Thousands separators split by OCR (`1` + `,499.29`) — rejoin tokens where the
  second starts with `,`. Cluster rows by **tolerance**, never by
  `round(y/N)` buckets: bucket edges split tokens 2px apart.
- Genuinely overlapping text runs (two fields printed at the same x/y) are
  **not recoverable** — extract what's unambiguous and flag it.
- The report can **overprint/drop characters** (`DENTAL ROUP` for GROUP).
  Prove it at character level before touching the parser:
  `[c for c in page.chars if abs(c['top']-y) < 2 and x0 <= c['x0'] <= x1]`.
  If the char isn't there, flag the cell — never auto-complete a name.
- Suite id glued to lease id (`-00101002186`): lease = trailing 6 digits.
- A numeric token already binned (e.g. 6-digit sqft starting at x0 309) must
  be **claimed** and excluded from the name zone.

### 7. Validate — three independent checks

```
a) Sums vs the report's own printed totals, per section AND per group.
b) Counts: anchors found == records written; step-dates in PDF == steps written.
c) Arithmetic the report implies:
     annualized / 12        == monthly
     RSF * rate             == annualized      (tolerance ~0.6% for rounded rate)
     sum(line items)        == printed total   (allow $1 rounding — these
                                                reports round each line first)
```
Also: **flag any numeric token that fell outside every bin**. That's how you
catch a column you didn't know existed. It should be zero.

A verifier must also report **how many items it could not check** — a glued
OCR header (`DescriptionAmount`) once left 56 files silently unverified, and
an off-by-one skipped the last row. "0 mismatches" means nothing without
"0 unchecked". When the user asks "are you sure?", re-verify by an
independent path and report your own bugs too.

Keep renders / OCR JSON until the user accepts the output — deleting them
early forced a full OCR re-run for a one-column follow-up.

Full knowledge base (all document types, error catalogue, code templates):
`LOCAL_AI_KNOWLEDGE/DOCUMENT_EXTRACTION_KNOWLEDGE.md`.

For maximum rigor, write a *separate* verifier that re-reads the PDF and
compares **as strings** (`f"{v:,.2f}" == printed`) so a wrong decimal can't
pass as "close enough". Then count every distinct `$` token in the PDF and
confirm it appears the same number of times in the workbook.

### 8. Output shape (latest agreed format — apply to every new rent roll unasked)

- **Sheet "Source (as printed)"** — one row per physical PDF line, totals
  kept, with `Line Kind` and `Raw Text` columns, columns exactly as printed.
  This is what lets the user trace any figure back.
- **Sheet "Workings"** — one row per suite / additional space, totals
  excluded, **Suite Id first**. Order: identifiers → common fields up to
  security deposit → base rent → grouped repeating blocks (e.g. 3 expenses ×
  Base Year Amount / Base Year / Pro-Rata Share) → **narrow grey gap column**
  → current step → bumps → Page → Review Note.
- **Three header rows, NO merged cells** (users filter/copy columns):
  - row 1 = lowercase `bump1`, `bump2` … written only in the FIRST cell of
    its group, the other cells blank but styled;
  - row 2 = charge code (RNT, FRE, TIA …) repeated in every cell of the group;
  - row 3 = field (`Date` / `Monthly Amount` / `PSF`, or `Date` / `Rate`).
- **Each charge code gets its own bump series** in first-appearance order
  (RNT bump1..n, then FRE bump1..n …), width = max count of that code on any
  row. A group is (bump label, charge code). Collapsing all increases into one
  series was a real mistake the user had to point out.
- `Additional Space` rows carry their own suite id and dates (e.g. from
  `Additional Space <bldg> - <suite>`) and inherit the parent tenant name.
- **No gridlines, no borders.** Freeze at `D4`; autofilter on row 3.
- Real numbers/dates as real types with number formats — never strings.
  Month-year → 1st of month only when asked, matching existing date format.
- **YELLOW fill** on any cell needing a human look + a `Review Note` column.
- Save to the file the user names (e.g. "save on the existing v2"); if it is
  open in Excel, ask them to close it — don't spray v2/v3 copies.

### 9. Editing a workbook the user has already modified

Never regenerate from the PDF — it destroys their edits. Load, mutate only the
target cells, save. Procedure:

1. **Gate**: `zipfile.ZipFile(path).namelist()` — if it contains
   `xl/charts/`, `xl/media/`, `xl/drawings/`, `xl/pivotCache/` or `vbaProject`,
   openpyxl will silently destroy them. Stop and use direct XML editing or
   Excel COM instead.
2. **Locate columns by reading the header**, never hardcoded letters — the user
   may have rearranged them.
3. **Transform conservatively**: skip cells already in the target state, and
   assign only `.value` (leave `number_format` alone).
4. **Diff before/after** every cell's value, number format, fill, font, border,
   plus merges, freeze panes, autofilter and column widths. Classify each
   difference as intended or not; require zero unintended.
5. Keep a backup copy of the original.

## OCR fallback (no text layer)

Windows has a local OCR engine — no install, nothing leaves the machine:
render with `pypdfium2` at scale 3.0, then `Windows.Media.Ocr` via PowerShell
(`[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()`), which
returns word text + bounding boxes. Re-render failures at scale 4.5/6.0 and
reconcile across passes, preferring whichever pass is internally consistent.
Beware: OCR drops thousands separators and reads `0` as `o`, `1` as `l`.

## Confidentiality

This data is usually confidential. Do the work locally, keep values out of the
conversation (mask when probing), and don't send files anywhere. If the user
asks for a reusable script, that is also the cheaper path — one script run
locally beats reading N documents into context.
