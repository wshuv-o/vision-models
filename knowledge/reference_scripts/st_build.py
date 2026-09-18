"""
Reconcile the two OCR passes and write the statements workbook.

Pass 1: every page rendered at scale 3.0
Pass 2: only the pages pass 1 flagged, re-rendered at scale 6.0

For each page we keep whichever pass produced fewer problems; where both are
clean they must agree, otherwise the page is flagged.
"""
import os, re, json, glob, importlib
import st_parse as S

importlib.reload(S)
_orig_load = S.load_words


def _load_scaled(div):
    def _f(path):
        ws = json.load(open(path, encoding='utf-8-sig'))
        if isinstance(ws, dict):
            ws = [ws]
        for w in ws:
            for k in ('x', 'y', 'w', 'h'):
                w[k] = w[k] / div
            w['cy'] = w['y'] + w['h'] / 2
            w['x1'] = w['x'] + w['w']
        return ws
    return _f


def _load_hi(path):
    ws = json.load(open(path, encoding='utf-8-sig'))
    if isinstance(ws, dict):
        ws = [ws]
    for w in ws:                      # scale-6 coords -> the scale-3 frame
        for k in ('x', 'y', 'w', 'h'):
            w[k] = w[k] / 2.0
        w['cy'] = w['y'] + w['h'] / 2
        w['x1'] = w['x'] + w['w']
    return ws


KEYS = ('Provider', 'Account Number', 'Prior Account (excluded)',
        'Service Address', 'Billing Period', 'Billing Period Start',
        'Billing Period End', 'Electric Billing Period', 'Gas Billing Period',
        'Electricity Charges', 'Electricity Days', 'Gas Charges', 'Gas Days',
        'Total New Charges', 'Total Amount Due')
COMPARE = ('Account Number', 'Electricity Charges', 'Gas Charges',
           'Total New Charges', 'Total Amount Due', 'Billing Period Start',
           'Billing Period End', 'As Of Date')


def parse_dir(d, hi=False, div=None):
    S.load_words = (_load_scaled(div) if div else (_load_hi if hi else _orig_load))
    out = {}
    for p in sorted(glob.glob(os.path.join(d, '*.json'))):
        r = S.parse_page(p)
        if not r['is_statement']:
            continue
        S.validate(r)
        out[os.path.basename(p).replace('.json', '')] = r
    S.load_words = _orig_load
    return out


def reconcile():
    lo = parse_dir('st_img')
    hi = parse_dir('st_hi', hi=True)
    # third pass at scale 4.5 on whatever was still unreadable
    h45 = {}
    for k, v in parse_dir('st_hi45', div=1.5).items():
        m = re.match(r'f(\d+)_x_p(\d+)$', k)
        if m:
            h45[(int(m.group(1)), int(m.group(2)))] = v
    recs = []
    for name in sorted(lo):
        m0 = re.match(r'f(\d+)_(.+)_p(\d+)$', name)
        c = h45.get((int(m0.group(1)), int(m0.group(3)))) if m0 else None
        a, b = lo[name], hi.get(name)
        cands = [(a, 'scale 3.0'), (b, 'scale 6.0'), (c, 'scale 4.5')]
        cands = [(x, lbl) for x, lbl in cands if x is not None]
        pick, label = min(cands, key=lambda t: len(t[0]['notes']))
        other = next((x for x, _ in cands if x is not pick), None)
        r = dict(pick)
        r['_pass'] = label
        if other is not None and not pick['notes'] and not other['notes']:
            diff = [k for k in COMPARE if pick.get(k) != other.get(k)]
            if diff:
                r['notes'] = list(r['notes']) + [
                    f'the two OCR passes disagree on {", ".join(diff)}']
        m = re.match(r'f(\d+)_(.+)_p(\d+)$', name)
        r['Source PDF'] = m.group(2).replace('_', '.') if m else name
        r['PDF Page'] = int(m.group(3)) + 1 if m else None
        r['_file_idx'] = int(m.group(1)) if m else 0
        recs.append(r)
    recs.sort(key=lambda x: (x['_file_idx'], x['PDF Page']))
    return recs


MAX_OTHER = 4
COLS = (['Source PDF', 'PDF Page', 'Provider', 'Account Number',
         'Service Address',
         'As Of Date', 'As Of (as printed)',
         'Billing Period Start', 'Billing Period End', 'Billing Period (as printed)',
         'Electric Billing Period', 'Gas Billing Period',
         'Electricity Charges', 'Electricity Days',
         'Gas Charges', 'Gas Days']
        + [f'Other Charge {i} {k}' for i in range(1, MAX_OTHER + 1)
           for k in ('Description', 'Amount')]
        + ['Total New Charges', 'Sum of Charge Lines', 'Total Amount Due',
           'Prior Account (excluded)', 'OCR Pass', 'Review Note'])


def to_rows(recs):
    rows, flags = [], {}
    for i, r in enumerate(recs):
        parts = [v for v in (r['Electricity Charges'], r['Gas Charges']) if v is not None]
        parts += [a for _, a in r['others'] if a is not None]
        row = {c: None for c in COLS}
        row.update({
            'Source PDF': r['Source PDF'], 'PDF Page': r['PDF Page'],
            'Provider': r['Provider'], 'Account Number': r['Account Number'],
            'Service Address': r['Service Address'],
            'As Of Date': r['As Of Date'],
            'As Of (as printed)': r['As Of (as printed)'],
            'Billing Period Start': r['Billing Period Start'],
            'Billing Period End': r['Billing Period End'],
            'Billing Period (as printed)': r['Billing Period'],
            'Electric Billing Period': r['Electric Billing Period'],
            'Gas Billing Period': r['Gas Billing Period'],
            'Electricity Charges': r['Electricity Charges'],
            'Electricity Days': r['Electricity Days'],
            'Gas Charges': r['Gas Charges'], 'Gas Days': r['Gas Days'],
            'Total New Charges': r['Total New Charges'],
            'Sum of Charge Lines': round(sum(parts), 2) if parts else None,
            'Total Amount Due': r['Total Amount Due'],
            'Prior Account (excluded)': r['Prior Account (excluded)'],
            'OCR Pass': r['_pass'],
        })
        for j, (desc, amt) in enumerate(r['others'][:MAX_OTHER], start=1):
            row[f'Other Charge {j} Description'] = desc
            row[f'Other Charge {j} Amount'] = amt
        if len(r['others']) > MAX_OTHER:
            r['notes'].append(f'{len(r["others"])} other charge lines found; '
                              f'only the first {MAX_OTHER} are shown')
        rows.append(row)

        # map each note to the cells a human should look at
        cells = {}
        for n in r['notes']:
            if 'account' in n.lower():
                cells['Account Number'] = n
            elif 'as of' in n.lower() or 'billing summary' in n.lower():
                cells['As Of Date'] = n
            elif 'billing period' in n.lower() and 'Total from' not in n:
                cells['Billing Period Start'] = n
                cells['Billing Period End'] = n
            elif 'Total from this billing period' in n:
                cells['Total New Charges'] = n
            elif 'Total amount due' in n:
                cells['Total Amount Due'] = n
            elif 'sum to' in n:
                cells['Total New Charges'] = n
                cells['Sum of Charge Lines'] = n
            elif 'charge lines found' in n:
                cells['Electricity Charges'] = n
                cells['Gas Charges'] = n
            elif 'provider' in n.lower():
                cells['Provider'] = n
            elif 'disagree' in n:
                for k in ('Account Number', 'Total New Charges'):
                    cells[k] = n
            else:
                cells['Review Note'] = n
        if r['notes']:
            rows[-1]['Review Note'] = " | ".join(dict.fromkeys(r['notes']))
            flags[i] = cells or {'Review Note': r['notes'][0]}
    return rows, flags


def write(rows, flags, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    YELLOW = PatternFill('solid', fgColor='FFFF00')
    HDR = PatternFill('solid', fgColor='1F3864')
    BAND = PatternFill('solid', fgColor='F2F5FA')
    thin = Side(style='thin', color='BFBFBF')

    wb = Workbook()
    ws = wb.active
    ws.title = 'Statements'
    ws.append(COLS)
    for i, c in enumerate(COLS, start=1):
        cell = ws.cell(row=1, column=i)
        cell.fill, cell.font = HDR, Font(bold=True, color='FFFFFF')
        cell.alignment = Alignment(horizontal='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = {
            'Source PDF': 30, 'Service Address': 30, 'Review Note': 70,
            'Billing Period (as printed)': 34, 'As Of (as printed)': 18, 'Electric Billing Period': 26,
            'Gas Billing Period': 26, 'Account Number': 16,
            'Prior Account (excluded)': 22, 'OCR Pass': 10,
        }.get(c, 14)
    MONEY = '#,##0.00;(#,##0.00);-'
    for ri, row in enumerate(rows, start=2):
        for ci, c in enumerate(COLS, start=1):
            cell = ws.cell(row=ri, column=ci, value=row[c])
            cell.border = Border(bottom=thin)
            if 'Charges' in c or 'Amount' in c or c in ('Total New Charges',
                                                        'Sum of Charge Lines',
                                                        'Total Amount Due'):
                cell.number_format = MONEY
            if ri % 2 == 0:
                cell.fill = BAND
    ws.freeze_panes = 'C2'
    ws.auto_filter.ref = ws.dimensions
    col_of = {c: i + 1 for i, c in enumerate(COLS)}
    for idx, cells in flags.items():
        for c in cells:
            if c in col_of:
                ws.cell(row=idx + 2, column=col_of[c]).fill = YELLOW
        ws.cell(row=idx + 2, column=col_of['Review Note']).fill = YELLOW
    for cell in ws[get_column_letter(col_of['Review Note'])][1:]:
        cell.alignment = Alignment(wrap_text=True, vertical='top')
    wb.save(path)


OUT = (r"C:\path\to\your\folder\statements"
       r"\Con Edison Statements Extract.xlsx")

if __name__ == '__main__':
    recs = reconcile()
    rows, flags = to_rows(recs)
    write(rows, flags, OUT)
    print(f"statements: {len(rows)}")
    print(f"  clean: {len(rows)-len(flags)}   flagged for review: {len(flags)}")
    tot = sum(r['Total New Charges'] or 0 for r in rows)
    print(f"  total new charges across all statements: {tot:,.2f}")
    print(f"  used hi-res pass on: {sum(1 for r in rows if r['OCR Pass']=='scale 6.0')}")
    print("\n->", OUT)
