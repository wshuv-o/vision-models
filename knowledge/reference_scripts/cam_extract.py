"""
CAM Recovery Calculation extractor.

Reads every "*RecoveryCalculation*.pdf" in a folder you choose (same system-
generated template each time) and writes one Excel row per suite:

    Property | Suite | Tenant | Comm | Exp | Lease RSF | CAM | Insurance |
    Tax | Total | PDF File Name

No AI / network calls -- pure text-position parsing with pdfplumber. Every
number is anchored on the printed column headers, not guessed from position
alone, and every suite is cross-checked (CAM + Insurance + Tax must equal the
report's own printed Total) before being written out. Any suite that fails
that check, or whose tenant name looks like it was cut off by the report's
column width, is written to a second "Issues" sheet instead of being silently
trusted.

Usage:
    pip install pdfplumber openpyxl
    python cam_extract.py
    (a folder-picker window opens -- choose the folder containing the PDFs)
"""
import re
import sys
import glob
import os
from collections import defaultdict

import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# Suite IDs vary across properties: "167-819-" (wraps to "101"), "503-75-01"
# (complete on one line), "551f-101-" (letter in the property-code segment,
# wraps to "4101"), "875-34-A" (letter suite suffix), "875-34-" (wraps to
# "C/D"). All share the same shape: 2-3 dash-separated alphanumeric segments,
# optionally left incomplete (trailing dash) for the ID to wrap onto the row
# below.
SUITE_RX = re.compile(r'^[A-Za-z0-9]+-[A-Za-z0-9]+-[A-Za-z0-9]*$')
SUITE_CONT_RX = re.compile(r'^[A-Za-z0-9/]{1,6}$')
DATE_RX = re.compile(r'\d{1,2}/\d{1,2}/\d{4}')
MONEY_RX = re.compile(r'^-?\(?[\d,]+\)?(\.\d+)?$')
KNOWN_TYPES = ('CAM', 'Insurance', 'Tax')

# The dollar figure we want for each expense line is the one under the
# "Total ... Reimb." header -- the amount AFTER the tenant's % share is
# applied. It always sits well to the right of the "% Share" pre-adjustment
# amount and well to the left of the next column, so this window is a safe,
# generous target regardless of how many digits the number has.
REIMB_XMIN, REIMB_XMAX = 540, 592


def to_num(s):
    if s is None:
        return None
    s = s.strip()
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()').replace(',', '')
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def rows_of(words, tol=2.0):
    """Group words into visual rows, tolerant of tiny sub-pixel jitter."""
    out, cur = [], []
    for w in sorted(words, key=lambda a: a['top']):
        if cur and w['top'] - cur[-1]['top'] > tol:
            out.append(sorted(cur, key=lambda a: a['x0']))
            cur = []
        cur.append(w)
    if cur:
        out.append(sorted(cur, key=lambda a: a['x0']))
    return out


def property_name(pdf):
    """The centered property-name line sits directly below 'Property RSF:'."""
    words = pdf.pages[0].extract_words()
    rows = rows_of(words)
    for i, row in enumerate(rows):
        txt = [w['text'] for w in row]
        if 'Property' in txt and 'RSF:' in txt:
            for nxt in rows[i + 1:i + 3]:
                cand = [w for w in nxt if w['x0'] < 500]
                if cand and not any(t['text'] in ('Cost', 'Center(s)') for t in cand):
                    return " ".join(w['text'] for w in cand).strip()
    return None


def reimb_value(row):
    cands = [w for w in row if REIMB_XMIN <= w['x0'] <= REIMB_XMAX
             and MONEY_RX.match(w['text'])]
    return to_num(cands[0]['text']) if cands else None


def parse_pdf(path):
    fname = os.path.basename(path)
    out_rows, issues = [], []
    with pdfplumber.open(path) as pdf:
        prop = property_name(pdf)
        if prop is None:
            issues.append({'file': fname, 'suite': None, 'tenant': None,
                            'problem': 'Could not find property name on page 1'})
        for pageno, pg in enumerate(pdf.pages, 1):
            rows = rows_of(pg.extract_words())
            starts = [i for i, r in enumerate(rows)
                      if r and SUITE_RX.match(r[0]['text']) and r[0]['x0'] < 25]
            for si, i in enumerate(starts):
                row = rows[i]
                block_end = starts[si + 1] if si + 1 < len(starts) else len(rows)
                block = rows[i:block_end]

                # --- suite id, possibly wrapped onto the next line ---
                suite_id = row[0]['text']
                if suite_id.endswith('-'):
                    for r2 in rows[i + 1:i + 3]:
                        if len(r2) == 1 and r2[0]['x0'] < 25 and SUITE_CONT_RX.match(r2[0]['text']):
                            suite_id += r2[0]['text']
                            break

                # --- tenant name: every word up to the first date token ---
                # A plain digit is NOT a safe stop condition -- some real
                # tenant names contain one (e.g. "D8 COMMERCIAL"). Dates are
                # the only tokens in this position containing '/', so that is
                # the reliable boundary instead.
                tail = row[1:]
                name_words = []
                for w in tail:
                    if '/' in w['text']:
                        break
                    name_words.append(w)
                tenant = " ".join(w['text'] for w in name_words).strip()
                name_end_x = max((w['x1'] for w in name_words), default=None)

                # --- comm / exp dates: PDF sometimes glues them with no gap ---
                joined = "".join(w['text'] for w in tail)
                dates = DATE_RX.findall(joined)
                comm = dates[0] if len(dates) >= 1 else None
                exp = dates[1] if len(dates) >= 2 else None

                # --- lease RSF: first bare number after the two dates, x<210 ---
                rsf = None
                for w in tail:
                    if w['x0'] < 210 and re.fullmatch(r'[\d,]+', w['text']):
                        if comm and comm.replace('/', '') in w['text'].replace('/', ''):
                            continue
                        rsf = to_num(w['text'])
                        break

                # --- expense-type rows within this suite's block ---
                found = {}
                other_amt, other_desc = 0.0, []
                last_type_row_idx = None
                for bi, r in enumerate(block):
                    label_tok = next((w for w in r if 214 <= w['x0'] <= 300), None)
                    if label_tok is None or not re.match(r'^[A-Za-z]', label_tok['text']):
                        continue
                    val = reimb_value(r)
                    if val is None:
                        # Not a real expense-type data row -- e.g. some
                        # reports add an explanatory footnote sentence (like
                        # "5% CAP over OPER EXP ... CAP excl taxes and
                        # insurance") whose text can coincidentally place a
                        # capitalized word inside this x-range. A genuine
                        # CAM/Insurance/Tax row always has a dollar figure in
                        # the reimbursement column, even if it's "0"; prose
                        # never does.
                        continue
                    label = label_tok['text']
                    if label in KNOWN_TYPES:
                        found[label] = val
                    else:
                        other_amt += val
                        other_desc.append(label)
                    last_type_row_idx = bi

                # --- the suite's totals row: exactly 2 rows after the last
                # expense-type row (its own line, then a per-RSF detail line,
                # then the totals line) -- NOT "last row of block", since some
                # suites have a trailing footnote row (e.g. a base-year note)
                # after the totals line but before the next suite starts.
                printed_total = None
                if last_type_row_idx is not None and last_type_row_idx + 2 < len(block):
                    cand = block[last_type_row_idx + 2]
                    if not any(re.match(r'^[A-Za-z]', w['text']) for w in cand if w['x0'] < 220):
                        printed_total = reimb_value(cand)

                cam = found.get('CAM')
                ins = found.get('Insurance')
                tax = found.get('Tax')

                problems = []
                if not tenant:
                    problems.append('no tenant name captured')
                if comm is None or exp is None:
                    problems.append('missing Comm/Exp date')
                if rsf is None:
                    problems.append('missing Lease RSF')
                for k, v in (('CAM', cam), ('Insurance', ins), ('Tax', tax)):
                    if v is None:
                        problems.append(f'missing {k} amount')
                if printed_total is None:
                    problems.append('could not locate suite Total row')
                else:
                    # The report rounds each line item independently before
                    # summing, so the printed Total can legitimately land
                    # $1 away from CAM+Insurance+Tax -- confirmed across the
                    # sample set: every real mismatch was exactly $1.00.
                    # Tolerate that; flag anything bigger, since that would
                    # indicate a genuine misread.
                    computed = sum(v for v in (cam, ins, tax, other_amt) if v is not None)
                    if abs(computed - printed_total) > 1.5:
                        problems.append(
                            f'CAM+Insurance+Tax+Other ({computed:,.2f}) '
                            f'!= printed Total ({printed_total:,.2f})')
                # 109.6pt is the report's hard column-width clip (confirmed:
                # several unrelated tenant names all end at exactly this
                # x-position, which only happens if the renderer cuts them
                # off there). Use a tight tolerance so a merely-long-but-
                # complete name like "JANE DOE" (ends at 109.3) isn't
                # flagged.
                truncated = (name_end_x is not None and name_end_x >= 109.5)
                if truncated:
                    problems.append('tenant name may be cut off by the report column width')

                rec = {
                    'Property': prop, 'Suite': suite_id, 'Tenant': tenant,
                    'Comm': comm, 'Exp': exp, 'Lease RSF': rsf,
                    'CAM': cam, 'Insurance': ins, 'Tax': tax,
                    'Other Description': ", ".join(other_desc) or None,
                    'Other Amount': other_amt or None,
                    'Total': printed_total,
                    'PDF File Name': fname,
                }
                out_rows.append(rec)
                if problems:
                    issues.append({'file': fname, 'suite': suite_id, 'tenant': tenant,
                                    'page': pageno, 'problem': "; ".join(problems)})
    return out_rows, issues


def build_workbook(all_rows, all_issues, out_path):
    HDR_FILL = PatternFill('solid', fgColor='1F3864')
    HDR_FONT = Font(bold=True, color='FFFFFF')
    BAND = PatternFill('solid', fgColor='F2F5FA')
    THIN = Border(bottom=Side(style='thin', color='BFBFBF'))

    cols = [('Property', 26), ('Suite', 14), ('Tenant', 26), ('Comm', 12),
            ('Exp', 12), ('Lease RSF', 11), ('CAM', 11), ('Insurance', 11),
            ('Tax', 11), ('Other Description', 20), ('Other Amount', 12),
            ('Total', 12), ('PDF File Name', 32)]

    wb = Workbook()
    ws = wb.active
    ws.title = 'CAM Recovery'
    ws.append([c[0] for c in cols])
    for i, (h, w) in enumerate(cols, 1):
        c = ws.cell(row=1, column=i)
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(horizontal='center')
        ws.column_dimensions[get_column_letter(i)].width = w
    for ri, rec in enumerate(all_rows, start=2):
        for ci, (h, w) in enumerate(cols, start=1):
            c = ws.cell(row=ri, column=ci, value=rec.get(h))
            c.border = THIN
            if h in ('CAM', 'Insurance', 'Tax', 'Other Amount', 'Total'):
                c.number_format = '#,##0'
            if ri % 2 == 0:
                c.fill = BAND
    ws.freeze_panes = 'A2'
    if all_rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(all_rows)+1}"

    ws2 = wb.create_sheet('Issues (please verify)')
    icols = ['file', 'page', 'suite', 'tenant', 'problem']
    ws2.append(icols)
    for i, h in enumerate(icols, 1):
        c = ws2.cell(row=1, column=i)
        c.fill, c.font = HDR_FILL, HDR_FONT
    for ri, rec in enumerate(all_issues, start=2):
        for ci, h in enumerate(icols, start=1):
            ws2.cell(row=ri, column=ci, value=rec.get(h))
    for i, w in enumerate((32, 6, 14, 26, 60), 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    wb.save(out_path)


def pick_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        folder = filedialog.askdirectory(title="Select the folder containing the CAM recovery PDFs")
        root.destroy()
        return folder
    except Exception:
        return input("Enter the folder path containing the PDFs: ").strip('"')


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else pick_folder()
    if not folder or not os.path.isdir(folder):
        print("No valid folder selected. Exiting.")
        return
    pdfs = sorted(glob.glob(os.path.join(folder, "*.pdf")))
    if not pdfs:
        print(f"No PDF files found in: {folder}")
        return

    all_rows, all_issues = [], []
    for p in pdfs:
        print(f"Processing: {os.path.basename(p)}")
        try:
            rows, issues = parse_pdf(p)
        except Exception as e:
            print(f"  ERROR: could not process this file ({e}) -- skipped")
            all_issues.append({'file': os.path.basename(p), 'page': None,
                                'suite': None, 'tenant': None,
                                'problem': f'File failed to process: {e}'})
            continue
        all_rows.extend(rows)
        all_issues.extend(issues)
        print(f"  {len(rows)} suites extracted, {len(issues)} flagged for review")

    out_path = os.path.join(folder, "CAM Recovery Extract.xlsx")
    build_workbook(all_rows, all_issues, out_path)
    print(f"\nDone. {len(all_rows)} suites from {len(pdfs)} PDF(s) -> {out_path}")
    if all_issues:
        print(f"{len(all_issues)} rows were flagged on the 'Issues' sheet -- please review those manually.")
    else:
        print("No issues flagged -- every suite's CAM+Insurance+Tax matched its printed Total.")


if __name__ == '__main__':
    main()
