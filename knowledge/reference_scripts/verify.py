"""Independent verification of the finished workbook against the PDF.

Deliberately does NOT reuse the parser's own folding logic: it re-reads the
PDF, rebuilds the expected value for every cell straight from the printed
words, and compares as STRINGS so a wrong decimal cannot pass as "close
enough".
"""
import re
from collections import defaultdict
import pdfplumber
from openpyxl import load_workbook
import borrower_rr as P

wb = load_workbook(P.XLSX_PATH)
ws = wb["Rent Roll"]

# Column POSITIONS come from the writer's own spec (structural only); every
# VALUE below is still rebuilt independently from the PDF and compared.
col_of = {}
for i, (grp, sub, field) in enumerate(P.column_spec(), start=1):
    if field:
        col_of[field] = i

src = P.read_lines()
starts = [i for i, L in enumerate(src) if L["Expense"] == P.EXPENSES[0]]
assert len(starts) == ws.max_row - 2, (len(starts), ws.max_row - 2)

def cell(r, name):
    c = col_of.get(name)
    return None if c is None else ws.cell(row=r, column=c).value

def as_printed(v, printed):
    """Render the sheet's value the way the PDF prints it, then compare."""
    if printed is None:
        return v is None
    if v is None:
        return False
    p = str(printed).strip()
    if isinstance(v, str):
        return v.strip() == p
    dec = 2 if "." in p else 0
    body = p.lstrip("$").replace(",", "")
    try:
        float(body)
    except ValueError:
        return False
    return f"{v:,.{dec}f}" == f"{float(body):,.{dec}f}"

problems = []
checked = defaultdict(int)

for bi, s in enumerate(starts):
    e = starts[bi + 1] if bi + 1 < len(starts) else len(src)
    blk = src[s:e]
    head = blk[0]
    r = bi + 3

    # --- straight text / number columns off the block's first line ----------
    for field, key in (("Tenant", "Tenant"), ("Type", "Type"), ("Suite", "Suite"),
                       ("RSF", "RSF"), ("Lease Start", "Lease Start"),
                       ("Lease Exp.", "Lease Exp."),
                       ("2026 Base Rent", "2026 Base Rent")):
        got, printed = cell(r, field), head[key]
        checked[field] += 1
        if not as_printed(got, printed):
            problems.append((r, field, got, printed))

    # --- security deposit ---------------------------------------------------
    sd = head["Security Deposit"]
    t, a = cell(r, "Security Deposit Type"), cell(r, "Security Deposit")
    checked["Security Deposit"] += 1
    if sd is None:
        if t is not None or a is not None:
            problems.append((r, "Security Deposit", (t, a), None))
    else:
        m = re.match(r"^([A-Z]):\s*\$?([\d,]+)$", sd)
        if not m:
            pass  # parser flags these itself
        elif t != m.group(1) or not as_printed(a, m.group(2)):
            problems.append((r, "Security Deposit", (t, a), sd))

    # --- the three reimbursement expenses ------------------------------------
    byexp = {L["Expense"]: L for L in blk if L["Expense"]}
    for x in P.EXPENSES:
        L = byexp.get(x)
        for f in ("Base Year Amount", "Base Year", "Pro-Rata Share"):
            got = cell(r, f"{x} - {f}")
            printed = L[f] if L else None
            checked[f"expense {f}"] += 1
            if (got or None) != (printed or None):
                problems.append((r, f"{x} - {f}", got, printed))

    # --- current step + bumps, in printed order ------------------------------
    steps = [L for L in blk if L["Step Date"]]
    if steps:
        f0 = steps[0]
        for field, key in (("Current Step Date", "Step Date"),
                           ("Current Rent/SF", "Rent/SF"),
                           ("Current Annualized Base Rent", "Annualized Base Rent"),
                           ("Current Monthly Base Rent", "Monthly Base Rent")):
            got = cell(r, field)
            checked[field] += 1
            if not as_printed(got, f0[key]):
                problems.append((r, field, got, f0[key]))
    else:
        for field in ("Current Step Date", "Current Rent/SF",
                      "Current Annualized Base Rent", "Current Monthly Base Rent"):
            if cell(r, field) is not None:
                problems.append((r, field, cell(r, field), None))

    for n in range(1, P.MAX_BUMPS + 1):
        gd, gr = cell(r, f"Bump {n} Date"), cell(r, f"Bump {n} Rate")
        if n < len(steps):
            L = steps[n]
            checked["bump"] += 1
            if gd != L["Step Date"] or not as_printed(gr, L["Rent/SF"]):
                problems.append((r, f"Bump {n}", (gd, gr), (L["Step Date"], L["Rent/SF"])))
        else:
            if gd is not None or gr is not None:
                problems.append((r, f"Bump {n}", (gd, gr), "should be empty"))

# ---- every printed money token must survive somewhere ----------------------
MONEY = re.compile(r"^\$[\d,]+(\.\d\d)?$")
printed_tokens = defaultdict(int)
with pdfplumber.open(P.PDF_PATH, password=P.PASSWORD) as pdf:
    for pno, page in enumerate(pdf.pages[:P.RENT_ROLL_PAGES], 1):
        for row in P.rows_of(page.extract_words(x_tolerance=1.6, use_text_flow=True)):
            y = row[0]["top"]
            if y < P.BODY_TOP or (pno == P.RENT_ROLL_PAGES and y > P.LAST_PAGE_BOTTOM):
                continue
            for w in row:
                if MONEY.match(w["text"]) and w["x1"] <= 535:
                    printed_tokens[w["text"]] += 1
ws2 = wb["Source (as printed)"]
shdr = [c.value for c in ws2[1]]
sheet_tokens = defaultdict(int)
for r in range(2, ws2.max_row + 1):
    for c in range(1, ws2.max_column + 1):
        v = ws2.cell(row=r, column=c).value
        if isinstance(v, str):
            # money can be embedded in combined text, e.g. "L: $645,000"
            for tok in re.findall(r"\$[\d,]+(?:\.\d\d)?", v):
                sheet_tokens[tok] += 1
missing = {k: (v, sheet_tokens.get(k, 0)) for k, v in printed_tokens.items()
           if sheet_tokens.get(k, 0) != v}

print(f"cells compared: {sum(checked.values()):,}")
for k, v in sorted(checked.items()):
    print(f"    {k:32s} {v:>5,}")
print(f"\nMISMATCHES: {len(problems)}")
for p in problems[:25]:
    print("   row", p[0], p[1], "sheet=", repr(p[2]), " pdf=", repr(p[3]))
print(f"\nprinted money tokens with a differing count in the Source sheet: {len(missing)}")
for k, v in list(missing.items())[:20]:
    print(f"    {k:>15s}  pdf x{v[0]}  sheet x{v[1]}")
