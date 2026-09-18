# Document Extraction Knowledge Base
### PDF reports → verified Excel: SOP, skills, lessons and code

Version 1.0 · written 2026-09-18 · distilled from a working session covering property tax bills, sales
comparables, CAM recovery reports, aged receivables, six rent-roll templates and scanned utility statements.

> **Who this is for.** A local AI model (≈26B parameters) and the engineers building a private,
> on-premise extraction system around it. It records *how* the work was done, what went wrong, and
> which rules the users settled on, so the local system can do the same work without sending any
> document to a third party.
>
> **Every example value in this file is synthetic.** No real tenant, account or figure from the
> source documents is reproduced here.

---

## Contents

0. [Read this first — the core in 30 lines](#0-read-this-first--the-core-in-30-lines)
1. [The job and the standard](#1-the-job-and-the-standard)
2. [Non-negotiable rules](#2-non-negotiable-rules)
3. [Universal SOP — what to do when any new task arrives](#3-universal-sop--what-to-do-when-any-new-task-arrives)
4. [Toolkit and environment setup](#4-toolkit-and-environment-setup)
5. [Core techniques, with code](#5-core-techniques-with-code)
6. [OCR pipeline for scanned PDFs](#6-ocr-pipeline-for-scanned-pdfs)
7. [Validation toolkit](#7-validation-toolkit)
8. [Excel output conventions (the users' house style)](#8-excel-output-conventions-the-users-house-style)
9. [Editing a workbook the user already changed](#9-editing-a-workbook-the-user-already-changed)
10. [Playbooks by document type](#10-playbooks-by-document-type)
11. [Error catalogue](#11-error-catalogue)
12. [How the users work — preferences and communication](#12-how-the-users-work--preferences-and-communication)
13. [Keeping skills up to date over time](#13-keeping-skills-up-to-date-over-time)
14. [Building the local system — architecture advice](#14-building-the-local-system--architecture-advice)
15. [Checklists and prompt templates](#15-checklists-and-prompt-templates)
16. [Reference scripts inventory](#16-reference-scripts-inventory)

Each section stands on its own, so this file can be split into chunks for retrieval.

---

## 0. Read this first — the core in 30 lines

If only one section fits in the model's context, use this one as the system prompt.

```
You extract data from system-generated PDF reports into Excel. Accuracy beats completeness.

1. Numbers are NEVER typed from reading a document. Code reads PDF tokens; code writes cells.
2. Probe first: page count, text layer or scan, password, page sizes.
3. Study layout from a coordinate dump with digits masked (#). Never dump real values.
4. Group words into rows by y-TOLERANCE, never by rounding y into buckets.
5. Right-aligned numbers: bin by right edge x1. Left-aligned text: bin by x0.
6. Anchor each record on a token that appears exactly once per record — never the tenant name.
7. Find dates by regex pattern, not by position. Names are "what is left".
8. Exclude totals unless asked, but USE the printed totals to validate.
9. Validate three ways: sums vs printed totals; counts (anchors == rows, steps == steps);
   arithmetic the report implies (RSF x rate = annual, parts = total, amount/value = rate).
10. Any token that falls outside every column bin is a bug until explained.
11. Uncertain cell -> leave it blank or as printed, fill YELLOW, explain in "Review Note".
12. Never invent precision (month-year stays month-year unless the user wants day 1, and say so).
13. Rent rolls: Sheet "Source (as printed)" + Sheet "Workings". Workings has a 3-row header,
    no merged cells, lowercase bump1.., each charge code its own series, gap column before
    bumps, no gridlines, no borders, Suite Id first.
14. Editing a user's file: gate risky parts, find columns by header, change only .value of
    target cells, diff before/after, zero unintended changes, keep a backup, save in place.
15. Many similar PDFs -> write one script the user runs (folder picker), not N manual reads.
16. Data is confidential: work locally, mask when probing, delete temp renders when done
    (but only AFTER the user accepts the output).
17. When asked "are you sure?", re-verify with an INDEPENDENT method and report every
    problem found, including your own bugs.
```

---

## 1. The job and the standard

The users work in commercial real-estate accounting and asset management. They regularly receive
PDF reports produced by property-management and tax systems (MRI, KBS, broker reports, county tax
offices, utilities) and need the figures in Excel for their "workings" (their analysis models).

Their standard, in their own words:

- "make like that never pulls wrong data"
- "make sure no mistake on a single decimal point"
- "if there is a single mistake/confusion try to make the cell highlight"
- "dont touch anything else" (when editing their file)

So the product is **not** "an Excel file". It is *an Excel file that can be trusted without
re-keying*, and whose few doubtful cells are clearly marked.

Types of documents seen so far (details in [§10](#10-playbooks-by-document-type)):

| # | Document | Source type | Volume | Output |
|---|----------|-------------|--------|--------|
| A | County property tax bills (current year) | Scanned PDF, many bills per file | ~230 bills | 1 sheet per file |
| B | Older tax bills (prior years, other template) | Scanned PDFs, 1 per file | ~370 files | Parcel # + Total Billed |
| C | Broker sales comparables report | PDF with OCR text layer | ~1 file, many profiles | 1 row per sale |
| D | CAM / recovery calculation | Text PDFs, identical template | 36 files | Runnable script with folder picker |
| E | Aged receivables / delinquencies | Text PDF | 1 file | Fill an existing Excel template |
| F | Rent roll — MRI "ALT_CMROLL_1" | Text PDF | 1 | Rent roll sheet with increases |
| G | Rent roll — KBS Property Performance Report | Text PDF | 1 | Rent roll sheet with steps |
| H | Borrower rent roll (password-protected) | Text PDF | 1 | Source + working sheet |
| I | Rent roll — MRI with Future Rent Increases by charge code | Text PDF | 1 | Source + Workings |
| J | Receiver report rent roll (pages of a larger report) | Text PDF | 1 | Source + Workings |
| K | Utility statements (electric + gas) | Scanned PDFs | ~200 statements | 1 row per statement |

---

## 2. Non-negotiable rules

1. **Accuracy over completeness.** A blank yellow cell the user can see is better than a wrong
   value they can't. Never guess to fill a cell.
2. **Values come from code, not from reading.** The model may *look* at a layout, but every value
   written to Excel must come from a token the code pulled out of the PDF (text layer or OCR). A
   language model that retypes figures will eventually transpose digits, and nothing will catch it.
3. **Validate against the document's own totals.** System reports print subtotals and grand
   totals. They are free ground truth. If the sums don't tie, there is a bug. Find it.
4. **Exclude totals from the data** unless the user asks for them. Keep them in the Source sheet,
   marked as totals, so the user can trace them.
5. **Never invent precision.** `Jul-26` is a month, not a date. Convert it to `07/01/2026` only when
   the user wants that, and say that you did.
6. **Real types in Excel.** Numbers are numbers and dates are dates, with number formats. Never
   store `"1,234.56"` or `"7/1/2026"` as text.
7. **Flag, don't hide.** Anything uncertain gets a YELLOW fill plus a plain-language
   `Review Note`, or a row on an "Issues (please verify)" sheet.
8. **Don't damage user work.** When the user has edited a workbook, never regenerate it. Change only
   the cells asked for, and prove nothing else moved (see [§9](#9-editing-a-workbook-the-user-already-changed)).
9. **Confidentiality.** Documents never leave the machine. Mask digits when printing layouts.
   Don't echo tenant names or amounts into chat logs unless needed to show a specific problem.
10. **Honesty in reporting.** Report what was checked and how, what tied, what did not, what is
    flagged, and anything unresolved. If you find your own bug while re-checking, say so.

---

## 3. Universal SOP — what to do when any new task arrives

This is the loop followed for every task in the session. It works for any fixed-layout,
system-generated document.

### Step 1 — Understand the request (intake)

- List the **exact fields** the user wants. Screenshots with highlighted boxes are the spec, so
  follow them literally, including column order and names.
- Write down the **output target**: a new workbook, an existing template to fill, or an existing
  workbook to edit in place. Also note the file name and folder.
- Decide the **scope**: which files and pages, and whether totals are in or out (default: out).
- Only ask a question if the answer changes the work and cannot be inferred. Typical real questions:
  *where are the source files* (if not given), *which file to save into*.
  Defaults that need no question: totals excluded, yellow flags, a Source sheet for rent rolls, and
  the house style in [§8](#8-excel-output-conventions-the-users-house-style).
- **Recognise repeat tasks.** If the task matches a known playbook ([§10](#10-playbooks-by-document-type)),
  apply its conventions without being told again. For example, a new rent roll gets Source +
  Workings in the agreed format automatically.

### Step 2 — Inventory and probe the files

```python
import pdfplumber, os, glob
for f in sorted(glob.glob(r"<folder>\*.pdf")):
    with pdfplumber.open(f) as pdf:              # add password=... if encrypted
        p = pdf.pages
        chars = sum(len(pg.chars) for pg in p)
        imgs  = sum(len(pg.images) for pg in p)
        sizes = {(round(pg.width), round(pg.height)) for pg in p}
        print(f"{os.path.basename(f)[:50]:50s} pages={len(p):4d} chars={chars:7d} "
              f"images={imgs:4d} sizes={sizes}")
```

Read the output like this:

- `chars == 0` and about one image per page → **scanned**, so use the OCR pipeline ([§6](#6-ocr-pipeline-for-scanned-pdfs)).
- A file named `*_ocr.pdf`, or odd glued words → it has an **OCR text layer**. Treat it as text,
  but expect OCR-type errors.
- Page sizes change mid-file → **a different report is appended**. Find where the target report
  ends and stop there.
- An encryption error → ask the user for the password, and pass it at runtime. Never hardcode it
  into shared files.

### Step 3 — Study the layout with a masked coordinate dump

Never use `extract_text()` for layout study: it destroys the column geometry. Dump words with their
positions and **mask the digits**:

```python
import re
def masked_dump(page, y_from=0, y_to=9999, tol=2.0):
    words = page.extract_words(x_tolerance=1.6, use_text_flow=True)
    for r in rows_of([w for w in words if y_from <= w['top'] <= y_to], tol):
        print(f"y={r[0]['top']:6.1f} | " + " | ".join(
            f"{re.sub(r'[0-9]', '#', w['text'])}@{w['x0']:.0f}-{w['x1']:.0f}" for w in r))
```

From 1–3 dumped pages, determine:

- The header rows, and the x-range of every column (right edge for numbers, left edge for text).
- The **record anchor**: a token that appears exactly once per record.
- Which fields wrap onto 2–4 lines, and which fields sit on line 2 of a record.
- The shape of the totals lines, and of anything else that is not data (section headers, group
  headers, footnotes, page headers).
- Where the body starts (`BODY_TOP`) and ends on each page.

### Step 4 — Decide the approach

| Situation | Approach |
|-----------|----------|
| Many files, one template (e.g. 36 CAM PDFs) | One script, with a folder picker the user runs locally. Cheapest and repeatable. |
| One large file, one template | Parser script, run once, with a full validation report. |
| Scanned documents | Render → local OCR → the same parsing techniques on OCR word boxes. |
| User has already edited the output | Safe in-place edit ([§9](#9-editing-a-workbook-the-user-already-changed)), never a rebuild. |
| Fill the user's template | Load the template, locate its header row, write the data rows only, keep its styling. |

The user asked about this directly: "would I upload and you pull yourself, or make the code to
run? which would be efficient?" The answer is **code**. It costs a fraction of the effort, runs
offline, can be re-run on next month's files, and is deterministic.

### Step 5 — Build the parser

Order of work:

1. Group words into rows (`rows_of` with a tolerance).
2. Skip the page header (`top < BODY_TOP`) and the footer.
3. Classify each row: section header, group header, record anchor, continuation, step or bump
   line, totals, footnote, or other.
4. Bin fields: numbers on x1, text on x0, **dates by regex**.
5. Fold continuation rows into their record (wrapped names, steps, expense sub-rows).
6. Carry records across page breaks.
7. Collect notes for anything doubtful (these become the yellow cells).

Details and code are in [§5](#5-core-techniques-with-code).

### Step 6 — Validate (see [§7](#7-validation-toolkit))

Required, every time:

- **(a)** Sums vs the printed totals, per section and per group.
- **(b)** Counts: anchors found == records written; increase lines in the PDF == increases written.
- **(c)** Arithmetic identities the report implies.
- **(d)** Unbinned numeric tokens == 0.

Where the stakes are high: an independent verifier that re-reads the PDF with a *different*
method and compares values **as strings**.

### Step 7 — Write the workbook

Follow the house style in [§8](#8-excel-output-conventions-the-users-house-style). Save to the
location the user named. If the file is open in Excel (`PermissionError`), ask the user to close
it rather than silently creating `v2`, `v3` and so on.

### Step 8 — Hand over

Report briefly:

- Where the file is, and which sheets it contains.
- Row counts.
- Validation results, e.g. "all printed totals tie exactly: sqft, monthly rent, 76/76 increases".
- The number of yellow cells, and the *reasons* grouped. For example: "5 rows where the report
  itself overwrote letters of the tenant name".
- Anything unresolved, stated plainly. Example: "the report says 22 units but lists 21 rows with
  square footage. Totals still tie, so I did not force it."

### Step 9 — When the user asks "are you sure everything is accurate?"

Do **not** just re-run the same code. Verify with an independent path, for example:

- Re-extract with a different method (text-layer strings vs parsed numbers; a second OCR scale).
- Recompute from a different direction (rates from amounts; totals from lines).
- Check every row, including the first and last (an off-by-one once skipped the last row).

Report what the re-check found, *including bugs in your own earlier verification*. The session's
re-check found two such bugs; being candid about them built trust.

### Step 10 — Clean up

- Keep the intermediate files (renders, OCR JSON) **until the user has accepted the output**. Once,
  they were deleted early, and a follow-up request ("add the As Of Date column") forced a full OCR
  re-run.
- Afterwards, delete renders and OCR dumps of confidential documents.
- Save the reusable script next to the data if the user will run it again.
- Update the skill file with any new lesson ([§13](#13-keeping-skills-up-to-date-over-time)).

---

## 4. Toolkit and environment setup

Everything runs locally on Windows. Versions used successfully:

| Tool | Version | Used for |
|------|---------|----------|
| Python | 3.13 | Everything |
| pdfplumber | 0.11.x | Words with coordinates, chars, passwords |
| pdfminer.six | (pdfplumber dependency) | — |
| pypdfium2 | 5.x | Render PDF pages to PNG for OCR |
| Pillow | 12.x | Image crops for re-OCR |
| openpyxl | 3.1.x | Write, read and edit .xlsx |
| pandas | 3.x | Optional, for quick tabulation |
| Windows.Media.Ocr | built into Windows 10/11 | Local OCR via PowerShell/WinRT; no install, no network |

Install:

```bash
pip install pdfplumber pypdfium2 openpyxl pandas pillow
```

Windows OCR needs no install, but it does need an OCR language installed in Windows (English
normally is). If `TryCreateFromUserProfileLanguages()` returns null, add the language pack in
Settings → Time & Language → Language.

**Non-Windows or alternative OCR** (not validated in this session, but local): Tesseract via
`pytesseract.image_to_data`, or PaddleOCR. Whatever engine is used, normalise its output to the same
word-box shape `{t, x, y, w, h}` so the parsers don't change.

**Running a script for a user** (they are not developers, so be explicit):

```
1. Open a terminal in the folder that contains the script:
       cd "C:\path\to\CAM Recs"
2. Install once:
       pip install pdfplumber openpyxl
3. Run:
       python cam_extract.py
4. A folder window opens. Choose the folder with the PDFs.
   The Excel file is written into that same folder.
```

The classic failure is `can't open file 'cam_extract.py'`, which means the terminal is in the wrong
folder. Tell the user to `cd` to the script's folder first.

**Colab** also worked for users who preferred it: `!pip install pdfplumber openpyxl pandas -q`,
upload the PDF, and set the paths at the top of the script.

---

## 5. Core techniques, with code

### 5.1 Row grouping by tolerance

```python
def rows_of(words, tol=2.0):
    """Group pdfplumber words into visual rows. Cluster on a y-tolerance.
    NEVER bucket with round(y / N): two tokens 2px apart can land in different buckets."""
    out, cur = [], []
    for w in sorted(words, key=lambda a: a['top']):
        if cur and w['top'] - cur[-1]['top'] > tol:
            out.append(sorted(cur, key=lambda a: a['x0'])); cur = []
        cur.append(w)
    if cur: out.append(sorted(cur, key=lambda a: a['x0']))
    return out

words = page.extract_words(x_tolerance=1.6, use_text_flow=True)
```

- `x_tolerance=1.6` keeps tight columns from merging into one token.
- `use_text_flow=True` prevents garbage like `MAGNEAMNATGEMENT`, where a long name printed *over*
  a date column has its characters interleaved.
- Typical tolerances: text PDFs 2.0 pt. OCR at render scale 3: about 11 px, measured on the
  word's vertical centre (`cy`).

### 5.2 Column binning

```python
NUM_BINS = [              # (column, x1_min, x1_max) -- right edges, measured from the dump
    ("GLA Sqft",          310, 348),
    ("Monthly Base Rent", 348, 400),
    ("Annual Rate PSF",   400, 442),
]
def bins(row):
    got = {}
    for w in row:
        for col, lo, hi in NUM_BINS:
            if lo < w['x1'] <= hi:
                got.setdefault(col, w['text']); break
    return got
```

- **Numbers are right-aligned**, so bin on `x1`. `x0` moves left every time a number gains a
  digit; `x1` is stable to ±0.1 pt.
- **Text is left-aligned**, so bin on `x0`.
- **Zones must not overlap.** An overlapping Type/Suite zone once produced `Office 21st` for a
  suite named `21st & 22nd Floors`.
- **Claim tokens.** Once a token is binned into a numeric column, exclude it from the text zones.
  A 6-digit square-footage value started at x0 = 309 and was being appended to the tenant name.
- **Unbinned check.** Any numeric-looking token (`^[\d,().$-]+$` containing a digit) that matches
  no bin must be reported. It reveals unknown columns or wrong bin edges. Expect zero, or explain
  each exception (e.g. a security-deposit zone that is legitimately separate).

### 5.3 Anchoring records

Pick an anchor that appears **exactly once per record**, then verify that the anchor count equals
the record count.

| Report | Good anchor | Bad anchor (and why it failed) |
|--------|-------------|--------------------------------|
| Borrower rent roll | The `Real Estate Taxes` line (always line 1 of a block) | Tenant name: `** Waiting Tenant **` is printed on line 2 of some blocks |
| MRI rent roll | A building-ID token in the ids zone (`^[A-Z]{2,6}\d{3}$`, x0 < 150) | Name: wraps across 2–3 lines |
| Receiver report | A building-ID token `^\d{4,6}$` | — |
| Aged receivables | The `(ENTITY:` token on property rows | — |
| Tax bills | The "Page 1 of N" marker, or a label unique to the bill | Fixed y positions: bill height varies |
| CAM recovery | A suite id regex in the suite column | Last row of the block (footnotes follow it) |

### 5.4 Multi-line records are the norm

Expect several of these at once:

- A tenant name wrapped over up to 3 lines, and a description (e.g. an industry code) over up to 4.
- Rent steps stacked below the record, sometimes **two physical lines per step** (date + amount +
  annual PSF on one line, monthly PSF alone on the next).
- Fields that appear only on line 2 (lease end date, current PSF).
- A record continuing across a page break (carry `cur` across pages; don't reset per page).
- Footnotes between the last data row and the next record.
- Two independent stacks interleaving: expense rows in the middle and rent steps on the right.
  Split them by **x-range**, not by line number.

The folding pattern:

```python
cur, out = None, []
for L in lines:                      # lines already classified
    if L['kind'] == 'anchor':
        cur = new_record(L); out.append(cur)
    elif L['kind'] == 'step' and cur is not None:
        cur['_bumps'].append(step_of(L))
    elif L['kind'] == 'name_wrap' and cur is not None:
        cur['Tenant'] += ' ' + L['text']
    elif L['kind'] in ('total', 'section_total'):
        cur = None                   # nothing after a total may attach to the previous record
```

### 5.5 Dates by pattern; names are "what's left"

When a long name overflows through the date columns, name words and date words interleave by x:
`Property@176 | 2/1/2027@215 | and@218 | Maintenance@232 | 1/31/2037@256`.

```python
DATE_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')
ds, nm = [], []
for w in zone_tokens:
    (ds if DATE_RE.match(w['text']) else nm).append(w['text'])
name = " ".join(nm); start = ds[0] if ds else None; end = ds[1] if len(ds) > 1 else None
if len(ds) > 2: note("more than two date-like values on this line")
```

### 5.6 Glue artifacts (two fields in one token)

| Glue | Example (synthetic) | Fix |
|------|---------------------|-----|
| Name + date | `ACME PET SUPPLY CO9/1/2022` | `re.match(r'^(.*?)(\d{1,2}/\d{1,2}/\d{4})$', t)` |
| Category + source code | `RECONCILINC` (category + `NC`) | The code has a known small set {CH, CR, NC}, so strip it as a suffix |
| Suite + lease id | `-00101002186` | Lease id = the trailing 6 digits: `^(-.{3,})(\d{6})$` |
| Header words | `DescriptionAmount` | Match headers on a normalised substring, not an exact token |
| OCR split money | `$1` + `,003.90` | Rejoin: a token starting with `,` belongs to the one before it |

### 5.7 Totals hide in unexpected places

`text.startswith("Total")` is not enough. Totals that slipped through and inflated sums included:

- `Vacant Sqft: 17.58% 38,914`, a continuation line of the totals block.
- `Grand Total:` at a different indent.
- `Total for <group>` and `<section> Totals`.

Use markers that are unique to totals (e.g. `"Sqft:" in text`), and/or stop at a known y on the
last page. Once a totals line is seen, set `cur = None` so nothing attaches to the previous record.

### 5.8 Distinguishing header levels by indent

In one report, section headers sit at x0 ≈ 38 and group headers at x0 ≈ 42. That 4 pt difference
was the only reliable signal. The same report produced false group headers until a *gap-zone check*
was added: a real group header has nothing in the numeric columns
(`not any(100 <= w['x0'] < 172 for w in row)` style tests). Always confirm with counts.

### 5.9 Proving that characters are missing from the source

Sometimes the report itself overprints or clips text (synthetic examples: `DENTAL ROUP` for GROUP,
`CENTE S, LLC`, `J.B. SMITH C`). Before you "fix" the parser, prove that the characters are absent
from the PDF:

```python
def chars_in(page, y, x0, x1, tol=2.0):
    cs = [c for c in page.chars if abs(c['top'] - y) < tol and x0 <= c['x0'] <= x1]
    return ''.join(c['text'] for c in sorted(cs, key=lambda c: c['x0']))
```

If the character isn't in `page.chars`, no parser can recover it. Keep what is printed, flag the
cell yellow, and write "report overprinted/clipped this name; verify against the lease".
**Never** auto-complete names.

A related case: a name that runs up to a column's right edge may be truncated by the report. In
the CAM playbook, a name ending at `x1 >= 109.5` was flagged as "possibly cut off".

### 5.10 Money and number parsing

```python
def num(t):
    if t is None: return None
    s = str(t).replace(',', '').replace('$', '').strip()
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    try: v = float(s)
    except ValueError: return None
    return -v if neg else v
```

Handle `(1,234.56)` as negative, `-` or blank as None, and trailing `-` if the report uses it.
Round money to 2 dp only at output time. Compare using the printed string where possible.

### 5.11 Month-year and date handling

```python
import datetime, re
MON = {m: i for i, m in enumerate('jan feb mar apr may jun jul aug sep oct nov dec'.split(), 1)}

def month_year_first_day(s):
    """'Jul-26', 'Jul 2026', '07/2026' -> datetime(2026, 7, 1). Returns None if not month-year."""
    s = s.strip()
    m = re.fullmatch(r'([A-Za-z]{3})[a-z]*[-/ ]?(\d{2}|\d{4})', s)
    if m and m.group(1).lower() in MON:
        y = int(m.group(2)); y += 2000 if y < 100 else 0
        return datetime.datetime(y, MON[m.group(1).lower()], 1)
    m = re.fullmatch(r'(\d{1,2})/(\d{4})', s)
    if m: return datetime.datetime(int(m.group(2)), int(m.group(1)), 1)
    return None

def mdy(s):
    m = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', s.strip())
    return datetime.datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
```

Write dates as `datetime` with `cell.number_format = 'mm/dd/yyyy'`. If the user's sheet already has
converted dates in a particular format, **match that format** exactly.

---

## 6. OCR pipeline for scanned PDFs

### 6.1 Render pages (pypdfium2)

```python
import os, glob, pypdfium2 as pdfium
def render_all(src_dir, out_dir, scale=3.0):
    os.makedirs(out_dir, exist_ok=True)
    for fi, f in enumerate(sorted(glob.glob(os.path.join(src_dir, '*.pdf')))):
        pdf = pdfium.PdfDocument(f)
        for i in range(len(pdf)):
            out = os.path.join(out_dir, f"f{fi:02d}_p{i:04d}.png")
            if not os.path.exists(out):          # resumable
                pdf[i].render(scale=scale).to_pil().save(out)
```

Scale 3.0 (about 216 dpi) is the default. Re-render only the failing pages at 4.5 or 6.0.

### 6.2 OCR with the built-in Windows engine (PowerShell 5.1)

Save as `ocr_batch.ps1` and run it with `powershell -ExecutionPolicy Bypass -File ocr_batch.ps1 -Dir "C:\...\st_img"`.
It writes `<page>.json` next to each PNG as a list of `{t, x, y, w, h}` word boxes, and skips pages
that are already done, so it is resumable.

```powershell
param([Parameter(Mandatory=$true)][string]$Dir)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($task, $type) {
  $m = $asTaskGeneric.MakeGenericMethod($type)
  $t = $m.Invoke($null, @($task)); $t.Wait(-1) | Out-Null; $t.Result
}
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Write-Error "No OCR engine"; exit 2 }

$Dir = [System.IO.Path]::GetFullPath($Dir.Replace("/","\"))     # WinRT rejects forward slashes
$pngs = Get-ChildItem -Path $Dir -Filter *.png | Sort-Object Name
$n = 0
foreach ($png in $pngs) {
  $outJson = [System.IO.Path]::ChangeExtension($png.FullName, ".json")
  if (Test-Path $outJson) { $n++; continue }
  $file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($png.FullName)) ([Windows.Storage.StorageFile])
  $stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result  = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $words = New-Object System.Collections.ArrayList
  foreach ($line in $result.Lines) { foreach ($w in $line.Words) {
    $r = $w.BoundingRect
    [void]$words.Add([pscustomobject]@{ t=$w.Text; x=[math]::Round($r.X,1); y=[math]::Round($r.Y,1); w=[math]::Round($r.Width,1); h=[math]::Round($r.Height,1) }) } }
  [System.IO.File]::WriteAllText($outJson, ($words | ConvertTo-Json -Compress -Depth 3), [System.Text.Encoding]::UTF8)
  $bitmap.Dispose(); $stream.Dispose()
  $n++
  if ($n % 10 -eq 0) { Write-Host "ocr $n/$($pngs.Count)" }
}
Write-Host "DONE $n"
```

Gotchas:

- **Paths must use backslashes.** WinRT `GetFileFromPathAsync` fails on `/`.
- `ConvertTo-Json` of a single word returns an object, not a list. The loader must wrap it.
- The files are UTF-8 with a BOM, so read them with `encoding='utf-8-sig'`.
- For long runs, run in the background and make it resumable. Don't chain `cd` into a background
  `&` command: the `cd` gets backgrounded too.

### 6.3 Load OCR words

```python
import json
def load_words(path, div=1.0):
    ws = json.load(open(path, encoding='utf-8-sig'))
    if isinstance(ws, dict): ws = [ws]
    for w in ws:
        for k in ('x', 'y', 'w', 'h'): w[k] = w[k] / div   # div maps a hi-res pass to the base frame
        w['cy'] = w['y'] + w['h'] / 2
        w['x1'] = w['x'] + w['w']
    return ws
```

Use `div=2.0` for a scale-6 pass and `div=1.5` for scale-4.5, so that all passes share the scale-3
coordinate frame and the same bin constants work.

### 6.4 Repair OCR output (values: minimal and safe; labels: liberal)

**Rejoin split thousands.** OCR splits `$1,003.90` into `$1` + `,003.90`.

```python
def rejoin_money(line):
    out = []
    for w in line:
        if out:
            p = out[-1]
            if (w['t'].lstrip().startswith(',') and w['x'] - p['x1'] < 30
                    and re.fullmatch(r'-?\$?[\d,]*\d', p['t'].strip())):
                p['t'] = p['t'].strip() + w['t'].strip(); p['x1'] = w['x1']; continue
        out.append(dict(w))
    return out
```

Apply it to **every** line, not just the last one (that was a real bug).

**Fold letters back to digits, only inside a known numeric fragment.**

```python
DIGITISH = str.maketrans({'o': '0', 'O': '0', 'l': '1', 'I': '1'})
# Only after establishing that the token sits in a numeric slot, e.g. behind a comma:
#   '311' + ',ooo'  ->  '311,000'        bill no. '-oooo-oo' -> '-0000-00'
```

A true story: without folding, `311` was taken as the value, and the **rate check** (amount ÷ value
× 100 ≠ printed rate) is what caught it. Keep such arithmetic checks as first-class validators.

**Normalise labels only (never values).**

```python
def norm(s): return re.sub(r'[^a-z0-9]', '', s.lower())
def nlabel(s):
    n = norm(s)
    for a, b in (('perlod', 'period'), ('billlng', 'billing'), ('blliing', 'billing'),
                 ('thls', 'this'), ('totat', 'total')):
        n = n.replace(a, b)
    return n
# then: if 'totalamountdue' in nlabel(line) or 'otalamountdue' in nlabel(line): ...
```

A label with a dropped first letter (`otal amount due`) was once miscounted as a *charge line*.
Match labels on robust substrings, and exclude known total labels from the charge-line list.

**Collapse broken dates.** For example, `Apr 1 5, 2026` → `Apr 15, 2026`: inside a detected date
pattern only, join digit-space-digit.

### 6.5 Multi-pass reconciliation

1. Pass 1: every page at scale 3.0.
2. Pass 2: only the pages that pass 1 flagged, at scale 6.0.
3. Pass 3: whatever is still failing, at scale 4.5.
4. For each page, keep the pass with the **fewest validation problems**. If two passes are both
   clean but disagree on a key field, flag the page ("the two OCR passes disagree on …").
5. Record the pass used in an `OCR Pass` column, so a human knows what to double-check.

### 6.6 Re-OCR a crop for a stubborn field

For a glued or garbled field (e.g. a property name), crop that region at a higher scale and OCR
only the crop. This fixed names that had merged into their neighbours.

---

## 7. Validation toolkit

### 7.1 Tie to printed totals

```python
def tie(label, ours, printed, tol=0.005):
    ok = printed is not None and abs(round(ours, 2) - printed) <= tol
    print(f"{'OK ' if ok else 'BAD'} {label:40s} ours={ours:,.2f} printed={printed if printed is None else f'{printed:,.2f}'}")
    return ok
```

Tie per **section** and per **group**, not only at the grand total. Offsetting errors can hide in
a grand total.

### 7.2 Counts

- Anchors found == records written.
- Step or increase lines in the PDF (counted independently, e.g. date tokens in the steps zone) ==
  steps written.
- Suite rows == anchors. Units printed in the totals == rows with area (if it doesn't tie, report
  it; don't force it).
- Files processed == files in the folder. Statements found == pages that look like statements.

### 7.3 Arithmetic identities

| Identity | Tolerance | Catches |
|----------|-----------|---------|
| `sum(line items) == printed total` | $1 (reports round each line) | Missed or duplicated line, OCR digit error |
| `amount / taxable_value * 100 == printed rate` (tax) | 0.002 | Dropped thousands (`311` vs `311,000`) |
| `RSF × annual rate ≈ annual rent` | ~0.6% (rate is rounded) | Wrong column binned |
| `annual / 12 == monthly` | 0.01 | Monthly/annual swapped |
| `CAM + Insurance + Tax == Total` (CAM) | 1.5 | Missed expense row |
| `price / homesites == price per site` (comps) | printed rounding | Wrong price or unit count |
| `NOI / price == cap rate` (comps) | printed rounding | Wrong NOI |
| `electric + gas + other == total new charges` | $0.01–$1 | Missed or doubled line |

### 7.4 String-level independent verifier (maximum rigour)

Write a **separate** script that re-reads the PDF without using the parser's code, and for every
cell compares `f"{value:,.2f}"` with the printed token string. A wrong decimal can't pass as "close".
Then count every distinct money token in the PDF (outside totals) and check that each appears the
same number of times in the workbook:

```python
from collections import Counter
pdf_money = Counter(t for t in all_body_tokens if re.fullmatch(r'-?\(?[\d,]+\.\d{2}\)?', t))
xl_money  = Counter(f"{v:,.2f}" for v in all_money_cells if v is not None)
missing = pdf_money - xl_money; extra = xl_money - pdf_money
```

In one run this compared about 2,070 cells, with 0 mismatches.

### 7.5 Unbinned-token audit

After parsing, list every numeric token that no bin claimed. It should be zero, or every
exception must be explained (e.g. a separate security-deposit zone). This is how unknown columns
are discovered.

### 7.6 Verifier bugs are bugs too

Two real verification bugs from this session:

- An off-by-one loop that skipped the last row.
- A header matcher that looked for `Amount`, where OCR had glued it into `DescriptionAmount`. That
  left 56 files "unverifiable", which looked like success.

**A verifier must report how many items it could NOT check.** "0 mismatches" means nothing if
half the items were skipped.

---

## 8. Excel output conventions (the users' house style)

### 8.1 General

- One header row for simple extracts (tax bills, statements, CAM, comps). Freeze panes below the
  header, and turn on an autofilter.
- Money format `#,##0.00;(#,##0.00);-`; area format `#,##0;(#,##0);-`; dates `mm/dd/yyyy`.
- A `Review Note` column at the far right. Yellow fill (`FFFF00`) on the doubtful cell **and** on
  its Review Note.
- For batch tools, add an **"Issues (please verify)"** sheet listing file, row and the problem.
- Traceability columns are welcome: `Source PDF`, `PDF Page`, `OCR Pass`, `Raw Text`,
  and `Prior Account (excluded)` (shows what was deliberately not used).
- Header fill dark blue (`1F3864`/`2E5496`) with white bold text; light banding (`F2F5FA`) is fine.
- **No gridlines** (`ws.sheet_view.showGridLines = False`) and **no borders** on rent-roll
  workings. The users asked for this explicitly.

### 8.2 Rent roll: two sheets

**Sheet 1, "Source (as printed)"**

- One row per physical PDF line, including totals, with a `Line Kind` column (Suite, Additional
  Space, Rent Increase, Suite Total, Report Total, Other) and a `Raw Text` column.
- Columns exactly as the report prints them.
- Purpose: any figure can be traced back to the PDF.

**Sheet 2, "Workings"**

- One row per suite or space. **Totals excluded.** Suite Id first.
- Column order:
  1. Identifiers (Suite Id / Bldg-Suite, Lease ID, Tenant)
  2. Common fields up to the security deposit
  3. Base rent
  4. Repeating expense groups (e.g. Real Estate Taxes / Electric / Operating Expenses × Base Year
     Amount, Base Year, Pro-Rata Share)
  5. A **gap column**
  6. The current step (date, rate)
  7. The bumps
  8. Page, then Review Note
- `Additional Space` rows inherit the parent tenant's name, and carry their own suite id and dates.

### 8.3 Workings header: three rows, no merges

```
row 1 | bump1 |       |     | bump2 |       |     | bump1 |       |     |
row 2 | RNT   | RNT   | RNT | RNT   | RNT   | RNT | FRE   | FRE   | FRE |
row 3 | Date  | Monthly Amount | PSF | Date | Monthly Amount | PSF | Date | Monthly Amount | PSF |
```

- Row 1: the **lowercase** group label (`bump1`, `bump2`, …), only in the **first** cell of its
  group; the rest of the group stays blank but styled.
- Row 2: the **charge code**, repeated in every cell of the group (blank for single fields).
- Row 3: the field name (`Date`, `Monthly Amount`, `PSF`, or for simpler reports `Date` / `Rate`).
- **Never merge cells.** The users filter and copy columns; merges break that.
- **Each charge code gets its own series**, in the order codes first appear in the report:
  RNT bump1..n, then FRE bump1..n, then TIA bump1..n, and so on. The width of each series is the
  maximum count of that code on any single row.
  *Collapsing every increase into one series under the dominant code was a real mistake. The user
  had to point it out by comparing with their old software's output.*
- A group is identified by (bump label, charge code), so `FRE bump1` and `TIA bump1` are different
  groups.
- A narrow grey gap column (width 2.5) goes before the bumps.
- Freeze panes at `D4` (ids plus 3 header rows); autofilter on row 3.

Generic writer:

```python
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

def code_blocks(lines, work):
    """Charge codes in first-appearance order, and the widest per-row count of each."""
    import collections
    order = []
    for L in lines:
        if L.get('Cat') and L['Cat'] not in order: order.append(L['Cat'])
    widest = collections.Counter()
    for r in work:
        for k, v in collections.Counter(b[0] for b in r['_bumps'] if b[0]).items():
            widest[k] = max(widest[k], v)
    return [(c, widest[c]) for c in order if widest[c]]

def column_spec(common_fields, blocks, fields=("Date", "Monthly Amount", "PSF")):
    """(row1 label, row2 code, row3 field, data key); key None = gap column."""
    spec = [("", "", f, f) for f in common_fields] + [("", "", "", None)]
    for code, n_max in blocks:
        for n in range(1, n_max + 1):
            spec += [(f"bump{n}", code, f, f"{code}|{n}|{f}") for f in fields]
    return spec + [("", "", "Page", "Page"), ("", "", "Review Note", "Review Note")]

def write_header(ws, spec):
    G1, G2, GAP = (PatternFill('solid', fgColor=c) for c in ('1F3864', '2E5496', 'D9D9D9'))
    WHITE = Font(bold=True, color='FFFFFF', size=10)
    for i, (grp, code, field, key) in enumerate(spec, start=1):
        c1, c2, c3 = (ws.cell(row=r, column=i) for r in (1, 2, 3))
        if key is None:
            for c in (c1, c2, c3): c.fill = GAP
            ws.column_dimensions[get_column_letter(i)].width = 2.5
            continue
        prev = spec[i - 2] if i > 1 else None
        first = prev is None or prev[3] is None or (prev[0], prev[1]) != (grp, code)
        c1.value = grp if (grp and first) else None
        c2.value = code or None
        c3.value = field
        for c, f in ((c1, G1), (c2, G2), (c3, G2)):
            c.fill, c.font = f, WHITE
            c.alignment = Alignment(horizontal='left' if c is c1 else 'center',
                                    vertical='center', wrap_text=True)
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = 'D4'

def bump_values(r):
    """Spread a row's increases into its own charge code's series."""
    vals, seen = {}, {}
    for cat, date, amt, psf in r['_bumps']:
        if not cat: continue
        seen[cat] = seen.get(cat, 0) + 1; n = seen[cat]
        vals[f"{cat}|{n}|Date"], vals[f"{cat}|{n}|Monthly Amount"], vals[f"{cat}|{n}|PSF"] = date, amt, psf
    return vals
```

After writing, check that every increase landed in an allocated column. If any didn't, add a
Review Note: "`<code>` bump`<n>` has no column allocated".

### 8.4 Filling the user's own template

- Load the template, **find the header row by reading it** (in one template the headers were on row
  2, B–P, with data from row 3), and map fields to header text.
- Write data rows only, and keep the template's styles.
- Watch key types: dictionary keys that came through JSON become strings (`'30'` instead of `30`),
  which silently broke one template fill. Normalise the keys.
- Add an "Issues (please verify)" sheet if anything was flagged.

---

## 9. Editing a workbook the user already changed

Scenario: the user reworked a generated workbook and asked for only the bump dates to be converted
from `Jul-26`-style strings to real dates on the first of the month, adding "dont touch anything
else". 150 cells were converted and nothing else changed. The user later asked how it was done; the
answer is below. **The file was not regenerated.**

### Procedure

1. **Gate on the file's parts.** openpyxl silently drops charts, images, drawings, pivot caches and
   VBA. If any are present, stop and use Excel COM automation or direct XML editing instead.

   ```python
   import zipfile
   RISKY = ('xl/charts/', 'xl/media/', 'xl/drawings/', 'xl/pivotCache', 'vbaProject')
   bad = [n for n in zipfile.ZipFile(path).namelist() if any(k in n for k in RISKY)]
   if bad: raise SystemExit(f"openpyxl would destroy: {bad[:5]} — use COM/XML instead")
   ```

2. **Back up**: `shutil.copy2(path, path.replace('.xlsx', ' (backup before date fix).xlsx'))`.
3. **Snapshot** every cell's value, number format, fill, font, border, plus merges, freeze panes,
   autofilter and column widths.
4. **Locate the target columns by reading the header rows**, never by hardcoded letters. The user
   may have moved columns.
5. **Transform conservatively.** Touch only cells in the target columns whose value is in the source
   state (e.g. a month-year string). Skip cells that are already converted. Assign `.value`, and set
   `number_format` only where the target state needs it, matching the format of the cells the user
   already converted.
6. **Save in place** (the user said not to create new versions).
7. **Diff after vs before.** Every difference must be an intended one (target cell, value changed
   from string to date, format now matching). Unintended differences must be **zero**.

```python
from openpyxl import load_workbook

def snapshot(path):
    wb = load_workbook(path); snap = {}
    for ws in wb.worksheets:
        snap[(ws.title, '__meta__')] = (
            tuple(sorted(str(m) for m in ws.merged_cells.ranges)), ws.freeze_panes,
            ws.auto_filter.ref,
            tuple(sorted((k, d.width) for k, d in ws.column_dimensions.items())))
        for row in ws.iter_rows():
            for c in row:
                snap[(ws.title, c.coordinate)] = (
                    c.value, c.number_format, c.fill.fgColor.rgb, c.fill.fill_type,
                    c.font.b, c.font.i, str(c.font.color.rgb) if c.font.color else None,
                    c.border.left.style, c.border.right.style, c.border.top.style, c.border.bottom.style)
    return snap

def diff(before, after, intended_cells):
    unintended = []
    for k in set(before) | set(after):
        if before.get(k) != after.get(k) and k not in intended_cells:
            unintended.append((k, before.get(k), after.get(k)))
    return unintended          # must be []
```

---

## 10. Playbooks by document type

Each playbook lists: what to extract → anchors and layout → traps met → validation. Coordinates are
**per template**. Never reuse another template's numbers; re-measure from a masked dump.

### A. County property tax bills — current year (scanned)

- **Fields:** Tax Year; Parcel ID; Total Taxable Value; county line (rate and amount); city line
  (rate and amount); **"Other"**, meaning any extra line item that appears *between* the standard
  lines (e.g. a special district), captured as extra columns; Total Amount Due.
- **Layout:** many bills per PDF, and bill height varies, so locate fields by **label**, not fixed y.
  One sheet per source file.
- **Traps:**
  - OCR split thousands (sum mismatches of exactly −1000) → `merge_fragments`.
  - Y-bucket edges split rows → tolerance clustering.
  - `,ooo` for `,000` → digit folding.
  - Bill numbers read as `-oooo-oo` → fold o→0.
- **Validation:**
  - `amount / taxable_value × 100 == printed rate` for each line (tolerance 0.002). Rates are per
    $100 of value.
  - `sum(lines) == Total Amount Due`, within $1.

### B. Prior-year tax bills (different template)

- **Fields:** only Parcel # and Total Billed. The user explicitly limited these years to two fields.
- **Trap:** the verifier's header search failed on the glued `DescriptionAmount` token, leaving 56
  files unverified. Match normalised substrings, and count unverified files explicitly.

### C. Broker sales-comparables report (PDF with an OCR text layer)

- **Fields (one row per sale profile):** Property Name, Address, City, State, ZIP Code, Market,
  Transaction Date, Transaction Price, Number of Homesites, Year Built, Year Renovated (if
  available), NOI, Occupancy at Sale. Added for traceability: Market Source, Comparable ID, Sale
  Profile / Page.
- **Layout:** label/value pairs on profile pages.
- **Traps:**
  - Using one x-window for both label and value found the wrong value. Use separate label bounds and
    value bounds.
  - Property names glued to neighbouring text → re-OCR the name crop at a higher scale.
- **Validation:**
  - `price / homesites` vs the printed price-per-site.
  - `NOI / price` vs the printed cap rate (OAR).
  - Blank "Year Renovated" is legitimate. Don't flag it.

### D. CAM / recovery calculation reports (text, 36 identical files → script)

- **Output columns:** Property | Suite | Tenant | Comm | Exp | Lease RSF | CAM | Insurance | Tax |
  Total | PDF File Name. One row per suite. Plus an "Issues (please verify)" sheet.
- **Delivery:** a standalone, no-AI script with a tkinter folder picker. It writes
  `CAM Recovery Extract.xlsx` **into the chosen folder**. When re-run on a subset (7 files), the
  output goes into that subset's folder, as the user asked.
- **Layout:** the reimbursement column sits at x ≈ 540–592.
  - Suite ids match `^[A-Za-z0-9]+-[A-Za-z0-9]+-[A-Za-z0-9]*$`, with continuation fragments matching
    `^[A-Za-z0-9/]{1,6}$`.
  - The tenant name stops at the first token containing `/` (dates follow).
- **Traps:**
  - `block[1:]` skipped the first expense (CAM) row.
  - The totals row is **not** the block's last row, because footnotes follow it. It sits at
    `block[last_type_row + 2]`.
  - A "CAP" footnote was read as an expense type. An expense row counts only if it has a
    reimbursement value.
  - A too-narrow suite regex silently skipped 7 of 36 files. **Every file must produce rows or an
    Issues entry.** A silent zero is a bug.
  - A name reaching the column edge (x1 ≥ 109.5) is flagged as possibly truncated. Beware false
    flags on short names that happen to end near the edge.
  - Reports round each line, so allow 1.5 when comparing CAM + Insurance + Tax to Total.
- **Validation:** per suite, `CAM + Insurance + Tax == printed Total`. Failures go to Issues, not to
  the main sheet as trusted rows.

### E. Aged receivables / delinquencies (text, 1 file → fill template)

- **Output:** the user's `Template 1.xlsx` (headers on row 2, columns B–P, data from row 3). Tenant
  lines only, no totals.
- **Layout:**
  - Property rows are anchored on the `(ENTITY:` token.
  - Numeric columns are binned on x1 with ±4 tolerance: Amount, Current, 30, 60, 90, 120 days.
  - The category code sits at x0 ≈ 88–98.
  - Source code ∈ {CH, CR, NC}.
- **Traps:**
  - The source code is glued onto the category text → strip the known suffix.
  - The tenant and suite columns overprint, garbling names → keep the unambiguous part and flag it.
  - JSON-stringified keys (`'30'`) broke the template mapping.
- **Validation:**
  - Each line: Current + 30 + 60 + 90 + 120 == Amount.
  - Per property: sums == the printed property total.

### F. MRI rent roll "ALT_CMROLL_1" (text)

- **Output:** a sheet laid out like the report, with Suite Id first. Future rent increases
  (Cat / Date / Monthly Amount / PSF) go on the **same** sheet: the first inline on the suite row,
  the rest on continuation rows beneath it. Continuation rows repeat Suite Id / Space Id / Tenant,
  so an increase can never be read against the wrong suite. Money and area appear **once per suite**,
  so column sums stay right.
- **Layout:**
  - Numeric bins on x1: area, base rent, rate, cost recovery, security deposit, expense stop, other.
  - Increases start at x0 ≥ ~595.
- **Traps:**
  - The "Vacant Sqft:" totals line leaked in → filter on `Sqft:`.
  - A name glued to its date → split on a trailing date.
  - The printed security-deposit total didn't match the line sum. That was the *report's* own
    aggregation, not a bug: explain it, don't force it.
- **Validation:**
  - Occupied area and monthly rent == the printed totals.
  - Increase count == date tokens in the increases zone.

### G. KBS "Property Performance Report → Rent Roll" (text)

- **Layout:**
  - Section headers at x0 ≈ 38, tenant-group headers at x0 ≈ 42.
  - The industry-code description wraps over up to 4 lines; the tenant name over up to 3.
  - Each rent step takes **two** physical lines.
  - Groups end with "Total for <group>"; sections end with "<section> Totals".
- **Trap:** false group-header detection dropped 8 steps → a gap-zone check (a real header has no
  numeric tokens).
- **Validation:**
  - Every section and group total ties.
  - Steps written == steps printed (86/86 in the session).

### H. Borrower rent roll (text, password-protected)

- **Password:** the user supplies it. Pass it as `pdfplumber.open(path, password=PW)`, read from a
  prompt or environment variable. Never commit it.
- **Pages:** the rent roll ends before the appended historical income/expense pages. On the last
  rent-roll page, the totals block sits below a fixed y.
- **Anchor:** the `Real Estate Taxes` line. Every block has exactly one, and it is always line 1.
- **Working sheet order:**
  1. Common fields up to the security deposit
  2. Base rent
  3. Three expense groups (Real Estate Taxes, Electric, Operating Expenses), each with Base Year
     Amount, Base Year and Pro-Rata Share
  4. Gap
  5. Current step
  6. Bumps as Date / Rate pairs (up to 14)
- **Traps:**
  - `** Waiting Tenant **` on line 2 of a block.
  - Type/Suite zone overlap (fix: Type zone 195–238).
  - The unbinned-token check raised false alarms on the security-deposit zone → exclude that zone.
  - An explicit column order is required.
  - An infinite merge loop (the fix was `i = j + 1`).
  - Validate that each block's expense rows are exactly the 3 expected, in order. Flag otherwise.
- **Follow-up edit:** bump dates printed month-year → converted in place to the first of the month
  as real dates ([§9](#9-editing-a-workbook-the-user-already-changed)).
- **Validation:**
  - Area and annual rent by type == the printed totals.
  - The independent string verifier: ~2,070 cells, 0 mismatches.

### I. MRI rent roll with "Future Rent Increases" by charge code (text)

- **Sections:** New/Renewed Leases, Vacant Suites, Occupied Suites.
- **Ids:** building id `^[A-Z]{2,6}\d{3}$` at x0 < 150; suite starts with `-`; lease id is 6 digits.
- **Increases block:** right of x0 ≈ 600, as Cat (charge code) / Date / Monthly Amount / PSF.
  Codes seen include RNT, FRE, TIA, TIF and ANT.
- **Traps:**
  - A name overflowing through the date columns → find dates by pattern.
  - Suite and lease glued (`-00101002186`) → the lease is the trailing 6 digits. One suite had no
    separable lease id at all, so it was flagged rather than guessed.
  - A 6-digit area leaking into the name → claim binned tokens first.
  - `Additional Space` rows inherit the parent's name.
- **Workings:** each charge code gets its own bump series (the key user correction), with no
  gridlines and no borders.
- **Validation:**
  - Area and monthly rent by section == printed.
  - Units per section == printed.
  - Increases written == printed (169/169 in the session).

### J. Receiver report rent roll (a page range inside a larger report)

- **Ids:** building id `^\d{4,6}$`. Dates start at x0 ≈ 206.
- **Additional Space rows** print as `Additional Space <bldg> - <suite>`. Take the suite id from
  that text, and take the dates from the tokens at x0 ≥ 200 on that row. A bug that emptied this
  list lost those rows' dates; the user noticed "some little data missed".
- **Charge codes seen:** BRT, ABA, CAM, RET, each with its own bump series.
- **Traps:**
  - The report overprinted name characters (proved at character level, [§5.9](#59-proving-that-characters-are-missing-from-the-source))
    → flag, don't guess.
  - The printed "units" count may disagree with the rows that have area, even when area and rent
    tie. Report it as an open question.
- **If the user supplies their own quick conversion** (e.g. an Excel export of the PDF) and says
  data is missing: rebuild from the PDF, then compare with their file to show what was recovered.

### K. Utility statements — electric and gas (scanned)

- **Fields:**
  - Provider
  - **Account Number** (NOT the prior account number, which is shown in a separate column as
    excluded)
  - Service Address
  - Billing Period (start, end, as printed), plus separate electric and gas periods when printed
  - Electricity Charges (+ days), Gas Charges (+ days)
  - Other charges 1–4 (description + amount)
  - Total New Charges, Sum of Charge Lines (computed), Total Amount Due
  - **As Of Date** (+ as printed)
  - OCR Pass, Review Note
- **Patterns (this provider):**
  - Account `^\d{4,6}-\d{4,6}-\d$`; prior account `^\d{2}-\d{4}-\d{4}-\d{4}-\d$`.
  - Money `^-?\$-?[\d,]+\.\d{2}$`.
  - Dates `Mon DD, YYYY`, with OCR variants.
- **Geometry at scale 3:** left panel x ≤ 950; amount column x 780–950; account id x ≤ 470.
- **Traps:**
  - Billing periods prefixed by the commodity ("Electric billing period …").
  - Money split at the comma, on every line.
  - The bare total line was double-counted as a charge.
  - `otal amount due` (a dropped letter) was taken as a charge.
- **Validation:**
  - `sum(charge lines) == Total New Charges`.
  - Account number found on every statement.
  - Multi-pass agreement.
  - The session outcome: 188 clean, 15 flagged, with the account number on 203/203.

---

## 11. Error catalogue

| # | Symptom | Root cause | Fix | Prevention rule |
|---|---------|-----------|-----|-----------------|
| 1 | Totals off by exactly 1,000 × k | OCR split `1` + `,499.29` | Rejoin comma-led fragments | Rejoin on every line |
| 2 | Tokens 2px apart land in different rows | `round(y/N)` bucketing | Tolerance clustering | Never bucket |
| 3 | Value `311` instead of `311,000` | `,ooo` read as letters | Digit-fold inside numeric fragments | Keep the rate/arithmetic check |
| 4 | Garbled names like `MAGNEAMNATGEMENT` | Characters interleaved across columns | `use_text_flow=True` | Always use it for dumps |
| 5 | Wrong column after a number grows | Binned on x0 | Bin numbers on x1 | Right-aligned → x1 |
| 6 | `Office 21st` | Overlapping zones | Tighten the zone | Assert zones don't overlap |
| 7 | Area digits appended to the name | Numeric token also in the text zone | Claim binned tokens | Exclude claimed ids |
| 8 | One record split in two | Anchored on tenant name | Anchor on a once-per-record token | Anchors == records |
| 9 | Sums inflated | Totals continuation line (`Vacant Sqft:`) | Marker filter | Tie to printed totals |
| 10 | Record missing its first expense row | `block[1:]` | Include the anchor row | Count rows per block |
| 11 | Totals taken from a footnote | "Last row is the total" | Locate by relative position to known rows | Don't assume last == total |
| 12 | Footnote read as an expense | Word matched the expense list | Require a value in the reimbursement column | Types must carry values |
| 13 | 7 of 36 files produced nothing | Suite regex too narrow | Broaden + continuation regex | Every file yields rows or an Issue |
| 14 | 8 steps dropped | False group-header detection | Gap-zone check | Steps written == steps printed |
| 15 | Infinite loop | Merge loop index not advanced | `i = j + 1` | Loops must strictly progress |
| 16 | Charge lines double-counted | Total line matched as a charge | Exclude total labels | Sum of charges == total |
| 17 | Mangled label counted as data | `otal amount due` | Substring label match | Normalise labels, not values |
| 18 | Additional Space rows had no dates | Token list for them set to `[]` | Take tokens at x0 ≥ date column | Every row type has field tests |
| 19 | All increases under one code | Collapsed series | Per-code series | Compare with the users' old software output |
| 20 | Template fill silently blank | JSON turned int keys into strings | Normalise key types | Assert cells written > 0 |
| 21 | Verifier "passed" but skipped files | Glued header `DescriptionAmount` | Substring header match | Verifier reports unchecked counts |
| 22 | Verifier missed the last row | Off-by-one | `range(len(rows))` | Test the verifier on known-bad data |
| 23 | Wrong label/value picked | One x-window used for both | Separate label/value bounds | — |
| 24 | PermissionError on save | File open in Excel | Ask the user to close it | Don't spray v2/v3 files |
| 25 | `can't open file script.py` | User's terminal in the wrong folder | `cd` into the script folder | Give exact run steps |
| 26 | WinRT OCR fails on paths | Forward slashes | Normalise to backslashes | — |
| 27 | Long script truncated when created via heredoc | Shell heredoc limits | Write the file with a file-writing tool | — |
| 28 | Follow-up needed a full OCR rerun | Intermediates deleted early | Keep until the user accepts | Clean up last |
| 29 | Background job ran in the wrong dir | `cd … &` backgrounded the `cd` | Use absolute paths | — |
| 30 | Name shows `ROUP` for `GROUP` | Report overprinted the character | Prove via `page.chars`, then flag | Never auto-complete |
| 31 | Security-deposit total ≠ line sum | The report's own aggregation | Explain it | Not every mismatch is yours |
| 32 | Rounding false alarms | Report rounds each line | $1 / 1.5 tolerance on sums; exact on lines | Choose tolerance per identity |
| 33 | False "truncated name" flag | Edge threshold too loose | Tune the threshold on known-good rows | Review flags for noise |

---

## 12. How the users work — preferences and communication

- **Accuracy first, and visibly so.** They check. They ask "are you sure everything is accurate?"
  Answer with evidence: what tied, the counts, what is flagged.
- **Screenshots are the spec.** When they send a screenshot of a desired layout (often from their
  old software's output), match it: column order, header wording, grouping, lowercase `bump1`.
- **Terse, informal messages with typos.** Read for intent. "cat date monthly amount psf are on
  different page, i want on the same page" meant *the same sheet/row block*, not a printed page.
- **Cost-conscious.** They don't want wasted tokens. Prefer scripts for batches, don't re-read large
  files needlessly, and keep reports short.
- **Save where they say.** "save on the existing v2" means overwrite that file, not create v3. "just
  save on that 7 pdfs folder" means output next to that subset only.
- **Don't touch anything else** in files they've edited.
- **Human-check cells in yellow**, always with a reason.
- **Totals: not wanted** in the data unless asked.
- **Confidential.** "you should not read the data or use it for training, it's very confidential."
  This is why the local system exists.
- **They iterate the format.** The Workings format evolved over several rounds:
  2-row merged header → 3-row unmerged header → bump label only in the first cell → per-code series
  → no gridlines or borders. Apply the **latest** agreed format to new rent rolls automatically.
- They sometimes run scripts in **Colab**, sometimes locally, so scripts should work in both (paths
  set at the top, or a folder picker locally).

---

## 13. Keeping skills up to date over time

This is how the assistant "learned" during the session, and how the local system should too.

### 13.1 What a skill file is

A Markdown file per task family (e.g. `rent-roll-extraction/SKILL.md`) with:

1. **Trigger description**: one or two lines saying when to use it ("extract rent rolls, CAM
   recovery, aged receivables and similar fixed-layout PDFs into verified Excel").
2. **Non-negotiables**: 4–10 short rules.
3. **Method**: the steps and techniques, with short code.
4. **Output shape**: the users' current agreed format.
5. **Known templates**: a fingerprint → parser/config → validation facts for each.
6. **Error catalogue**: symptom → cause → fix.

### 13.2 When to update it

| Event | Update |
|-------|--------|
| The user corrects the output format | Change **Output shape** immediately. The newest instruction wins, and the old one is removed. |
| A new template is handled | Add it to **Known templates**: fingerprint strings, anchor, bins, traps, what the totals tie to. |
| A bug is found (by you or by the user) | Add it to the **Error catalogue**, and add a check that would have caught it. |
| A validation idea proves useful | Add it to **Method → Validate**. |
| The user states a lasting preference ("always", "from now on") | Add it to the **preferences** section. |

Keep it short. Rules the model must always follow go at the top. Long stories become one-line
lessons.

### 13.3 How it's used on a new task (the "no instructions needed" behaviour)

1. Match the new request against skill triggers (e.g. "rent roll" → rent-roll skill).
2. Load the skill, and apply its defaults *without being told*: Source + Workings sheets, the
   3-row header, yellow flags, totals excluded, validation against printed totals.
3. Check **Known templates** by fingerprint. If it matches, reuse the parser and re-validate. If it
   doesn't, run the SOP from step 2 (probe), reusing the *method* but never the coordinates.
4. After delivery, update the skill with anything new.

### 13.4 Regression safety

- Keep a **golden set**: for each template, one source PDF plus the accepted output workbook plus the
  validation printout.
- After changing shared code, re-run every golden file, and require identical outputs, or
  differences that are explained.

---

## 14. Building the local system — architecture advice

### 14.1 Principle: the model designs and checks; code extracts

A ~26B local model is capable, but weaker than a frontier model at long, exact, multi-step work.
Design around this:

- **Never let the model transcribe numbers from documents into Excel.** It *will* drift.
- Let the model do what it's good at:
  1. Classify a document or template.
  2. Read a *masked* coordinate dump and propose a template config (bins, anchors, regexes).
  3. Write or repair a parser from a skeleton.
  4. Read a validation report and propose a targeted fix.
  5. Talk to the user.
- Let deterministic code do the rest: text and OCR extraction, parsing, validation, Excel writing.

### 14.2 Pipeline

```
┌─────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────┐   ┌───────────┐   ┌─────────┐
│ Ingest  │──▶│ Geometry     │──▶│ Template ID  │──▶│ Parser   │──▶│ Validator │──▶│ Writer  │
│ files,  │   │ pdfplumber   │   │ fingerprint  │   │ config + │   │ totals,   │   │ house   │
│ password│   │ or render+OCR│   │ → registry   │   │ plugins  │   │ counts,   │   │ style + │
└─────────┘   └──────────────┘   └──────┬───────┘   └────┬─────┘   │ identities│   │ flags   │
                                        │ unknown        │ fail    └─────┬─────┘   └────┬────┘
                                        ▼                ▼               │              ▼
                                 ┌──────────────────────────────┐        │        Review queue
                                 │ LLM: masked layout dump →    │◀───────┘        (yellow cells,
                                 │ draft config / parser fix    │                 Issues sheet)
                                 └──────────────────────────────┘
```

- **Geometry layer.** Output one normalised format for both text PDFs and OCR:
  `{page, text, x0, x1, top, bottom, source: text|ocr, pass}`. Everything downstream is the same.
- **Template registry.** A JSON/YAML file per template. Fingerprint it by header strings (report
  ids like `ALT_CMROLL_1`, titles like "Aged Delinquencies"), page size, and key labels.
- **Parser engine.** A generic engine handles rows, bins, anchors, folding and totals markers from
  config. Anything weird goes into a small per-template **plugin** function (e.g. "lease id = the
  trailing 6 digits").
- **Validator.** Declarative checks per template (see the config below). The output is a
  machine-readable report: pass/fail, the numbers, and the items that could not be checked.
- **Writer.** One implementation of the house style ([§8](#8-excel-output-conventions-the-users-house-style)), shared by every template.
- **Review queue.** Every flag carries a reason, file, page and y. A small UI (or just the Issues
  sheet) lets a person accept or correct. Save corrections to improve the template.

### 14.3 Example template config

```yaml
template_id: mri_future_increases_v1
fingerprint:
  all_of: ["Rent Roll", "Future Rent Increases"]
  page_size: [792, 612]
body_top: 110
row_tolerance: 2.0
sections: ["New/Renewed Leases", "Vacant Suites", "Occupied Suites"]
anchor: { regex: '^[A-Z]{2,6}\d{3}$', x0_max: 150, field: "Bldg Id" }
ids:
  suite: { starts_with: "-", glued_lease_regex: '^(-.{3,})(\d{6})$' }
  lease: { regex: '^\d{6}$' }
text_zone: { x0_max: 320, dates_by_regex: '^\d{1,2}/\d{1,2}/\d{4}$', date_fields: ["Rent Start", "Expiration"] }
numeric_bins_x1:
  "GLA Sqft": [310, 348]
  "Monthly Base Rent": [348, 400]
  "Annual Rate PSF": [400, 442]
  "Monthly Cost Recovery": [442, 500]
  "Expense Stop": [500, 545]
  "Monthly Other Income": [545, 600]
bump_block:
  x0_min: 600
  fields_x1: { Cat: [600, 628], Date: [628, 668], "Monthly Amount": [668, 732], PSF: [732, 800] }
  per_charge_code_series: true
totals_markers: ["Totals:", "Grand Total", "Sqft:"]
additional_space: { marker: ["Additional", "Space"], inherit: ["Occupant Name"] }
validations:
  - { type: tie_sum, field: "GLA Sqft", by: "Section", to: printed_section_total }
  - { type: tie_sum, field: "Monthly Base Rent", by: "Section", to: printed_section_total }
  - { type: count_equal, a: increases_written, b: increase_lines_in_pdf }
  - { type: unbinned_numeric_tokens, expect: 0 }
output: { style: rent_roll_workings_v3, sheets: ["Source (as printed)", "Workings"] }
```

### 14.4 Agent loop for a new, unknown template

1. The code probes the file and produces a **masked** dump of 1–2 representative pages (60–120 rows).
   That is small enough for the local model's context.
2. The model drafts a config (or a parser from a skeleton). Output must be structured
   (JSON/YAML), not prose.
3. The code runs the parser on **all** pages and runs every validation.
4. If any check fails, the code gives the model a *small* failure report (the check name, expected vs
   got, and the masked rows around the problem). The model proposes one targeted change. Repeat,
   capping the iterations at about 5–8.
5. Once everything passes, write the workbook, save the config to the registry, and add the file to
   the golden set.
6. If it still fails after the cap, hand it to a human with the failure report. Never ship
   unvalidated numbers as clean.

### 14.5 Making a smaller model reliable

- **Skeletons over blank pages.** Give it the functions in [§5](#5-core-techniques-with-code) and
  [§8](#8-excel-output-conventions-the-users-house-style) as a library, and ask it to fill in
  config, not to write 400 lines from scratch.
- **One step per prompt.** "Identify the anchor", then "propose numeric bins", then "classify these
  8 unusual rows". Don't ask for everything at once.
- **Structured outputs** with a schema, validated by code before use.
- **Low temperature** (0–0.2) for code and config.
- **Checklists** ([§15](#15-checklists-and-prompt-templates)) pasted into the prompt at each stage.
- **Retrieval, not the whole file.** Index this document by section: put §0 in the system prompt
  and retrieve the relevant playbook, the error catalogue rows, and the code sections on demand.
- **Code is the judge.** A step is done when validation passes, not when the model says it is.
- **If the local model can see images**, use it to read the *user's screenshots of desired
  layouts*, never to read values from documents.
- Keep all of it offline: no telemetry, no cloud OCR, no pip installs at runtime in production
  (vendor the wheels).

### 14.6 Hardware note

A ~26B model on a single consumer GPU (e.g. an RTX 5080 with 16 GB) will need a quantised build,
and long contexts cost memory and speed. That is another reason to keep prompts small: masked dumps
of 1–2 pages, retrieved sections of this file, and short failure reports. The heavy lifting (OCR,
parsing, validation) is CPU work in Python and needs no GPU.

---

## 15. Checklists and prompt templates

### 15.1 New-task checklist

```
[ ] Fields listed exactly as the user asked (screenshot matched)
[ ] Output target known: new file / fill template / edit in place; file name + folder
[ ] Totals excluded (unless asked)
[ ] Probe done: pages, text vs scan, password, page sizes, appended reports
[ ] Masked dump of 1–3 pages studied
[ ] Anchor chosen; anchors == records verified
[ ] Numeric bins on x1, text on x0, zones non-overlapping, binned tokens claimed
[ ] Dates by regex; glue cases handled
[ ] Multi-line + page-break continuation handled
[ ] Totals lines detected by marker, not only "Total" prefix
[ ] Validation: sums vs printed (per section/group), counts, identities, unbinned == 0
[ ] Verifier reports unchecked items == 0
[ ] Workbook in house style; real numbers/dates; yellow + Review Note on doubts
[ ] Saved where the user said; no stray versions
[ ] Handover: counts, ties, flags with reasons, open questions
[ ] Intermediates kept until accepted; skill file updated with new lessons
```

### 15.2 Safe-edit checklist

```
[ ] Risky parts gate passed (no charts/media/drawings/pivots/VBA) or switched to COM/XML
[ ] Backup copy made
[ ] Snapshot before
[ ] Target columns found by header text
[ ] Only target-state cells changed; already-converted cells skipped
[ ] Saved in place
[ ] Diff: unintended changes == 0; intended count reported
```

### 15.3 Prompt: layout analysis (local model)

```
SYSTEM: <paste §0>
USER:
Below is a masked coordinate dump of page {p} of a "{report title}" PDF.
Each line: y=<top> | token@x0-x1 | ...   Digits are masked as #.
Task: return JSON with
  body_top, row_tolerance,
  anchor {regex, x0_min, x0_max, why_unique},
  numeric_bins_x1 {column: [lo, hi]},
  text_zones_x0 {column: [lo, hi]},
  multiline_fields [..], totals_markers [..], section_header_rule, notes [..]
Rules: bins must not overlap; numbers are right-aligned (use x1); do not guess
columns you cannot see; list anything ambiguous in notes.
DUMP:
{dump}
```

### 15.4 Prompt: fix a failing check

```
SYSTEM: <paste §0>
USER:
Template config: {config_json}
Validation failed:
  check: {name}
  expected: {expected}   got: {got}
  unchecked items: {n}
Masked rows around the first discrepancy (page {p}):
{rows}
Relevant lessons: {retrieved error-catalogue rows}
Return ONE minimal change to the config or plugin as a JSON patch, and the check
you expect it to fix. Do not change unrelated fields.
```

### 15.5 Prompt: hand-over summary

```
Write a short report for the user:
- output file path and sheets
- rows written; files processed
- validation: each check with PASS/FAIL and the numbers
- flagged cells: count, grouped by reason, with how to verify
- open questions (only real ones)
No marketing language. Do not include confidential values beyond what is needed.
```

---

## 16. Reference scripts inventory

Working implementations from the session (copies without passwords are in
`LOCAL_AI_KNOWLEDGE/reference_scripts/`). They are **reference designs**: the coordinates inside are
specific to each template, and file paths are hardcoded at the top.

| File | Template | Shows how to |
|------|----------|--------------|
| `cam_extract.py` | CAM recovery (§10 D) | Folder picker, batch over PDFs, per-suite identity check, Issues sheet |
| `rentroll_extract.py` | MRI ALT_CMROLL_1 (§10 F) | x1 binning, glued name+date, continuation rows repeating ids |
| `property_perf_rr.py` | KBS performance report (§10 G) | Indent-level headers, two-line steps, wrapped names |
| `borrower_rr.py` + `verify.py` | Borrower rent roll (§10 H) | Anchor on expense line, grouped expense columns, independent string verifier |
| `rr05_extract.py` | MRI future increases (§10 I) | Dates by pattern, glued ids, claimed tokens, per-charge-code bump series, 3-row header |
| `receiver_report_rr.py` | Receiver report (§10 J) | Additional Space parsing, overprinted-name flags |
| `st_render.py`, `ocr_batch.ps1`, `st_parse.py`, `st_build.py` | Utility statements (§10 K) | Render → Windows OCR → parse → multi-pass reconcile → flagged workbook |

In the reference copies:

- The password-protected template reads its password from the environment variable
  `PDF_PASSWORD` (or asks for it at run time), not from the source code.
- Real tenant names in code comments have been replaced with synthetic ones.

The same folder also holds `rent-roll-extraction.SKILL.md`, the compact skill file the assistant
loaded for every rent-roll task. It is the short form of §§2, 5, 7, 8 and 9.
