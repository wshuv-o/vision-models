"""
Example Borrower LLC -- Rent Roll (as of Jun 30, 2026)  ->  Excel

    !pip install pdfplumber openpyxl pandas -q

Two sheets:
  "Rent Roll"  - one row per tenant: common data, 2026 base rent, the three
                 reimbursement expenses (Real Estate Taxes / Electric /
                 Operating Expenses, each with Base Year Amount, Base Year and
                 Pro-Rata Share), a blank spacer, then the Current step
                 followed by every rent bump as Date/Rate pairs.
  "Source (as printed)" - one row per physical PDF line, columns exactly as
                 they appear in the report, so any figure can be traced back.

Each tenant block is anchored on its "Real Estate Taxes" line: every block has
exactly one, and it is always the block's first line. Tenant name alone is NOT
a safe anchor -- "** Waiting Tenant **" is printed in the tenant column on the
SECOND line of two blocks and would otherwise split them in half.

Cells the parser is unsure about are filled YELLOW with a note in
"Review Note".
"""
import os
import re
import pdfplumber
import pandas as pd

PDF_PATH = r"C:\path\to\your\folder\rentrolls\Borrower RR & Historical IE.pdf"
XLSX_PATH = r"C:\path\to\your\folder\rentrolls\Example Borrower Rent Roll 06.30.26.xlsx"
PASSWORD = os.environ.get("PDF_PASSWORD") or input("PDF password: ")

RENT_ROLL_PAGES = 13          # page 14 is the income & expense statement
BODY_TOP = 88.0
LAST_PAGE_BOTTOM = 430.0      # the totals block on p13 starts below this

# x0 ranges for text columns
ZONES = {
    "Tenant":  (38, 195),
    "Type":    (195, 238),
    "Suite":   (238, 285),     # a wide suite ("21st & 22nd Floors") starts at 240
    "Expense": (545, 600),
    "Base Year Amount": (605, 660),
    "Base Year":        (660, 690),
    "Pro-Rata Share":   (690, 720),
    "Security Deposit": (350, 388),
}
# right-edge (x1) bins for the right-aligned numeric columns
NUM_BINS = [
    ("RSF",                   280, 306),
    ("Lease Start",           306, 326),
    ("Lease Exp.",            326, 350),
    ("Step Date",             386, 412),
    ("Rent/SF",               412, 438),
    ("Annualized Base Rent",  438, 470),
    ("Monthly Base Rent",     470, 498),
    ("2026 Base Rent",        498, 535),
]
EXPENSES = ["Real Estate Taxes", "Electric", "Operating Expenses"]
MAX_BUMPS = 14

SRC_COLS = (["Page", "Tenant", "Type", "Suite"] + [c for c, _, _ in NUM_BINS]
            + ["Security Deposit", "Expense", "Base Year Amount", "Base Year",
               "Pro-Rata Share"])


def money(t):
    if t is None:
        return None
    s = str(t).strip().replace("$", "").replace(",", "").replace("%", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def rows_of(words, tol=2.0):
    out, cur = [], []
    for w in sorted(words, key=lambda a: a["top"]):
        if cur and w["top"] - cur[-1]["top"] > tol:
            out.append(sorted(cur, key=lambda a: a["x0"]))
            cur = []
        cur.append(w)
    if cur:
        out.append(sorted(cur, key=lambda a: a["x0"]))
    return out


def zone(row, name):
    lo, hi = ZONES[name]
    return " ".join(w["text"] for w in row if lo <= w["x0"] < hi).strip() or None


def bins(row):
    got = {}
    for w in row:
        for col, lo, hi in NUM_BINS:
            if lo < w["x1"] <= hi:
                if col not in got:
                    got[col] = w["text"]
                break
    return got


def read_lines(pdf_path=PDF_PATH):
    """Every physical line of the rent roll, columns as printed."""
    lines = []
    with pdfplumber.open(pdf_path, password=PASSWORD) as pdf:
        for pno, page in enumerate(pdf.pages[:RENT_ROLL_PAGES], 1):
            for row in rows_of(page.extract_words(x_tolerance=1.6, use_text_flow=True)):
                y = row[0]["top"]
                if y < BODY_TOP:
                    continue
                if pno == RENT_ROLL_PAGES and y > LAST_PAGE_BOTTOM:
                    continue   # totals / vacancy summary -- not wanted
                rec = {"Page": pno, "_y": y}
                for k in ("Tenant", "Type", "Suite", "Expense", "Base Year Amount",
                          "Base Year", "Pro-Rata Share", "Security Deposit"):
                    rec[k] = zone(row, k)
                rec.update({c: None for c, _, _ in NUM_BINS})
                rec.update(bins(row))
                # a value that fell outside every known column would be lost
                # the Security Deposit column sits in the gap between the
                # Lease Exp. and Step Date bins and is read as text, so it is
                # not "unbinned" -- exclude it before flagging anything lost.
                sd_lo, sd_hi = ZONES["Security Deposit"]
                unbinned = [w["text"] for w in row
                            if 280 < w["x1"] <= 535
                            and not (sd_lo <= w["x0"] < sd_hi)
                            and not any(lo < w["x1"] <= hi for _, lo, hi in NUM_BINS)]
                rec["_unbinned"] = "; ".join(unbinned) if unbinned else None
                if any(rec[k] for k in rec if not k.startswith("_") and k != "Page"):
                    lines.append(rec)
    return lines


def build(lines):
    """Fold the physical lines into one row per tenant."""
    out, flags = [], {}

    def flag(idx, col, note):
        d = flags.setdefault(idx, {})
        d[col] = f"{d[col]}; {note}" if col in d else note

    # split into blocks on each "Real Estate Taxes" line
    starts = [i for i, L in enumerate(lines) if L["Expense"] == EXPENSES[0]]
    for bi, s in enumerate(starts):
        e = starts[bi + 1] if bi + 1 < len(starts) else len(lines)
        blk = lines[s:e]
        head = blk[0]

        r = {"Tenant": head["Tenant"], "Type": head["Type"], "Suite": head["Suite"],
             "RSF": money(head["RSF"]),
             "Lease Start": head["Lease Start"], "Lease Exp.": head["Lease Exp."],
             "Security Deposit Type": None, "Security Deposit": None,
             "2026 Base Rent": money(head["2026 Base Rent"]),
             "": None,
             "Current Step Date": None, "Current Rent/SF": None,
             "Current Annualized Base Rent": None, "Current Monthly Base Rent": None,
             "Tenant Note": None, "Page": head["Page"]}
        for x in EXPENSES:
            for f in ("Base Year Amount", "Base Year", "Pro-Rata Share"):
                r[f"{x} - {f}"] = None
        for n in range(1, MAX_BUMPS + 1):
            r[f"Bump {n} Date"] = None
            r[f"Bump {n} Rate"] = None
        idx = len(out)

        sd = head["Security Deposit"]
        if sd:
            m = re.match(r"^([A-Z]):\s*\$?([\d,]+)$", sd)
            if m:
                r["Security Deposit Type"], r["Security Deposit"] = m.group(1), money(m.group(2))
            else:
                r["Security Deposit"] = sd
                flag(idx, "Security Deposit", f"could not split deposit text {sd!r}")

        # ---- reimbursement rows: expected to be the 3 known expenses in order
        seen = [L["Expense"] for L in blk if L["Expense"]]
        if seen != EXPENSES:
            flag(idx, "Real Estate Taxes - Base Year Amount",
                 f"expense rows were {seen} (expected {EXPENSES})")
        for L in blk:
            x = L["Expense"]
            if x in EXPENSES:
                r[f"{x} - Base Year Amount"] = L["Base Year Amount"]
                r[f"{x} - Base Year"] = L["Base Year"]
                r[f"{x} - Pro-Rata Share"] = L["Pro-Rata Share"]

        # ---- rent steps: first is "Current", the rest are bumps -------------
        steps = [L for L in blk if L["Step Date"]]
        if steps:
            first = steps[0]
            if first["Step Date"] != "Current":
                flag(idx, "Current Step Date",
                     f"first step is {first['Step Date']!r}, not 'Current'")
            r["Current Step Date"] = first["Step Date"]
            r["Current Rent/SF"] = money(first["Rent/SF"])
            r["Current Annualized Base Rent"] = money(first["Annualized Base Rent"])
            r["Current Monthly Base Rent"] = money(first["Monthly Base Rent"])
            for n, L in enumerate(steps[1:], start=1):
                if n > MAX_BUMPS:
                    flag(idx, "Tenant", f"more than {MAX_BUMPS} bumps; extras not written")
                    break
                r[f"Bump {n} Date"] = L["Step Date"]
                r[f"Bump {n} Rate"] = money(L["Rent/SF"])
                if L["Rent/SF"] is None:
                    flag(idx, f"Bump {n} Rate", "bump row has no Rent/SF value")

        # a tenant-column note on any line other than the first
        notes = [L["Tenant"] for L in blk[1:] if L["Tenant"]]
        if notes:
            r["Tenant Note"] = " ".join(notes)
            flag(idx, "Tenant Note",
                 "extra text printed in the tenant column of this block - check "
                 "what it refers to")

        if r["Tenant"] is None:
            flag(idx, "Tenant", "no tenant name on the block's first line")
        for L in blk:
            if L["_unbinned"]:
                flag(idx, "Tenant", f"value(s) outside any known column: {L['_unbinned']}")

        # ---- arithmetic cross-checks ---------------------------------------
        rsf, rate = r["RSF"], r["Current Rent/SF"]
        ann, mon = r["Current Annualized Base Rent"], r["Current Monthly Base Rent"]
        if ann is not None and mon is not None and abs(ann / 12 - mon) > 1.0:
            flag(idx, "Current Monthly Base Rent",
                 f"annualized/12 = {ann/12:,.2f} but monthly reads {mon:,.2f}")
        if rsf and rate is not None and ann is not None:
            tol = max(2.0, rsf * 0.006)
            if abs(rsf * rate - ann) > tol:
                flag(idx, "Current Annualized Base Rent",
                     f"RSF x Rent/SF = {rsf*rate:,.0f} but annualized reads {ann:,.0f}")
        out.append(r)

    df = pd.DataFrame(out)
    df["Review Note"] = None
    for i, cols in flags.items():
        df.at[i, "Review Note"] = " | ".join(dict.fromkeys(cols.values()))

    # explicit column order: common -> base rent -> the three expenses ->
    # blank spacer -> the Current step -> every bump -> notes
    order = ["Tenant", "Type", "Suite", "RSF", "Lease Start", "Lease Exp.",
             "Security Deposit Type", "Security Deposit", "2026 Base Rent"]
    for x in EXPENSES:
        order += [f"{x} - Base Year Amount", f"{x} - Base Year",
                  f"{x} - Pro-Rata Share"]
    order += ["", "Current Step Date", "Current Rent/SF",
              "Current Annualized Base Rent", "Current Monthly Base Rent"]
    for n in range(1, MAX_BUMPS + 1):
        order += [f"Bump {n} Date", f"Bump {n} Rate"]
    order += ["Tenant Note", "Page", "Review Note"]
    assert set(order) == set(df.columns), set(order) ^ set(df.columns)
    return df[order], flags


def column_spec():
    """(group, sub, field) per column; field None = blank spacer column."""
    spec = [("", "Tenant", "Tenant"),
            ("", "Type", "Type"),
            ("", "Suite", "Suite"),
            ("", "RSF", "RSF"),
            ("Lease", "Start", "Lease Start"),
            ("Lease", "Exp.", "Lease Exp."),
            ("Security Deposit", "Type", "Security Deposit Type"),
            ("Security Deposit", "Amount", "Security Deposit"),
            ("", "2026 Base Rent", "2026 Base Rent")]
    for x in EXPENSES:
        spec += [(x, "Base Year Amount", f"{x} - Base Year Amount"),
                 (x, "Base Year", f"{x} - Base Year"),
                 (x, "Pro-Rata Share", f"{x} - Pro-Rata Share")]
    spec += [("", "", None)]                                    # gap
    spec += [("Current", "Step Date", "Current Step Date"),
             ("Current", "Rent/SF", "Current Rent/SF"),
             ("Current", "Annualized Base Rent", "Current Annualized Base Rent"),
             ("Current", "Monthly Base Rent", "Current Monthly Base Rent")]
    spec += [("", "", None)]                                    # gap before bumps
    for n in range(1, MAX_BUMPS + 1):
        spec += [(f"Bump {n}", "Date", f"Bump {n} Date"),
                 (f"Bump {n}", "Rate", f"Bump {n} Rate")]
    spec += [("", "Tenant Note", "Tenant Note"),
             ("", "Page", "Page"),
             ("", "Review Note", "Review Note")]
    return spec


MONEY0 = '#,##0;(#,##0);-'
MONEY2 = '#,##0.00;(#,##0.00);-'
FMT = {"RSF": MONEY0, "Security Deposit": MONEY0, "2026 Base Rent": MONEY0,
       "Current Rent/SF": MONEY2,
       "Current Annualized Base Rent": MONEY0,
       "Current Monthly Base Rent": MONEY0}
for _n in range(1, MAX_BUMPS + 1):
    FMT[f"Bump {_n} Rate"] = MONEY2
WIDTH = {"Tenant": 34, "Type": 14, "Suite": 18, "Review Note": 70,
         "Tenant Note": 22, "Page": 6, "Current Step Date": 12,
         "Security Deposit Type": 6}


def write_xlsx(df, flags, src, path=XLSX_PATH):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    YELLOW = PatternFill("solid", fgColor="FFFF00")
    GAPF = PatternFill("solid", fgColor="D9D9D9")
    G1 = PatternFill("solid", fgColor="1F3864")   # top (group) header
    G2 = PatternFill("solid", fgColor="2E5496")   # second (field) header
    BAND = PatternFill("solid", fgColor="F2F5FA")
    WHITE = Font(bold=True, color="FFFFFF", size=10)
    thin = Side(style="thin", color="BFBFBF")
    med = Side(style="medium", color="1F3864")

    spec = column_spec()
    wb = Workbook()
    ws = wb.active
    ws.title = "Rent Roll"

    # ---- two-row header -----------------------------------------------------
    for i, (grp, sub, field) in enumerate(spec, start=1):
        L = get_column_letter(i)
        c1, c2 = ws.cell(row=1, column=i), ws.cell(row=2, column=i)
        if field is None:
            c1.fill = c2.fill = GAPF
            ws.column_dimensions[L].width = 2.5
            continue
        c1.value, c2.value = (grp or None), sub
        for c, f in ((c1, G1), (c2, G2)):
            c.fill, c.font = f, WHITE
            c.alignment = Alignment(horizontal="center", vertical="center",
                                    wrap_text=True)
        ws.column_dimensions[L].width = WIDTH.get(field, 14)
    # merge each group label across its run of columns; merge singles vertically
    i = 1
    while i <= len(spec):
        grp, sub, field = spec[i - 1]
        if field is None:
            i += 1
            continue
        j = i
        while (j < len(spec) and spec[j][0] == grp and grp != ""
               and spec[j][2] is not None):
            j += 1
        if grp == "":
            ws.merge_cells(start_row=1, start_column=i, end_row=2, end_column=i)
            ws.cell(row=1, column=i).value = sub
            ws.cell(row=1, column=i).alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=True)
            i += 1
        else:
            if j > i:
                ws.merge_cells(start_row=1, start_column=i, end_row=1, end_column=j)
            i = j + 1        # step PAST the group, not onto its last column
    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 30

    # ---- data ---------------------------------------------------------------
    for r_i, (_, rec) in enumerate(df.iterrows(), start=3):
        for c_i, (grp, sub, field) in enumerate(spec, start=1):
            cell = ws.cell(row=r_i, column=c_i)
            if field is None:
                cell.fill = GAPF
                continue
            v = rec[field]
            cell.value = None if (v is None or (isinstance(v, float) and pd.isna(v))) else v
            if field in FMT:
                cell.number_format = FMT[field]
            cell.border = Border(bottom=thin)
            if r_i % 2 == 1:
                cell.fill = BAND
    # vertical rules at each group boundary
    for c_i in range(2, len(spec) + 1):
        if spec[c_i - 1][0] != spec[c_i - 2][0] and spec[c_i - 1][2] is not None:
            for r_i in range(1, ws.max_row + 1):
                cur = ws.cell(row=r_i, column=c_i)
                cur.border = Border(left=med, bottom=cur.border.bottom)

    ws.freeze_panes = "D3"
    ws.auto_filter.ref = (f"A2:{get_column_letter(len(spec))}{ws.max_row}")

    # ---- yellow review cells -----------------------------------------------
    field_col = {f: i for i, (_, _, f) in enumerate(spec, start=1) if f}
    for idx, cols in flags.items():
        for col in cols:
            if col in field_col:
                ws.cell(row=idx + 3, column=field_col[col]).fill = YELLOW
        ws.cell(row=idx + 3, column=field_col["Review Note"]).fill = YELLOW
    for cell in ws[get_column_letter(field_col["Review Note"])][2:]:
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    # ---- source sheet -------------------------------------------------------
    ws2 = wb.create_sheet("Source (as printed)")
    srcdf = pd.DataFrame(src)[SRC_COLS]
    ws2.append(SRC_COLS)
    for _, rec in srcdf.iterrows():
        ws2.append([None if (v is None or (isinstance(v, float) and pd.isna(v)))
                    else v for v in rec.tolist()])
    for i, name in enumerate(SRC_COLS, start=1):
        L = get_column_letter(i)
        ws2.column_dimensions[L].width = 30 if name == "Tenant" else 14
        c = ws2[f"{L}1"]
        c.fill, c.font = G1, WHITE
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = ws2.dimensions

    wb.save(path)
    return path


if __name__ == "__main__":
    src = read_lines()
    df, flags = build(src)
    write_xlsx(df, flags, src)
    nb = max((n for n in range(1, MAX_BUMPS + 1)
              if df[f"Bump {n} Date"].notna().any()), default=0)
    print(f"{len(df)} tenants | {len(src)} source lines | bumps used: 1..{nb}")
    print(f"yellow cells: {sum(len(v) for v in flags.values())} across {len(flags)} tenants")
    print(f"total RSF: {df['RSF'].sum():,.0f}")
    print(f"total 2026 Base Rent: {df['2026 Base Rent'].sum():,.0f}")
    print("\n->", XLSX_PATH)
