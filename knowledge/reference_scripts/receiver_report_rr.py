"""
Example Holdings LLC - Receiver Report rent roll (p38-41) -> Excel.

  Sheet 1 "Source (as printed)" - one row per physical PDF line, totals kept.
  Sheet 2 "Workings"            - one row per suite / additional space, with
      each CHARGE CODE getting its own bump series (bump1..n per code).

Traps in this report:
  * The occupant name overflows through the Rent Start column, so name and
    date characters interleave. Dates are found by PATTERN and the name is
    whatever is left. Where they collide the report has actually DROPPED a
    character ("DENTAL ROUP" for GROUP, "CENTE S, LLC" for CENTERS -- synthetic examples) --
    that character is not in the PDF at all, so those rows are flagged.
  * "Additional Space" rows carry their own bldg/suite as
    "Additional Space 11921 - 107"; the suite must be taken from there.
"""
import re
import collections
import pdfplumber

# The report prints its grand total as "Total <entity name>". Set this
# to the entity exactly as it appears in your own report; it is matched
# as a prefix, so a partial name is enough.
ENTITY_TOTAL_PREFIX = 'Total Example Holdings LLC'

PDF = (r"C:\path\to\your\folder\rentrolls"
       r"\Example Holdings LLC-08.26-Receiver Report_Redacted_p38-41.pdf")
XLSX = (r"C:\path\to\your\folder\rentrolls"
        r"\Example Holdings LLC-08.26-Receiver Report_Redacted_p38-41 (1).xlsx")

BODY_TOP = 110.0
INCR_X0 = 600.0
DATE_COL_X0 = 206.0          # Rent Start starts here; a name reaching it collides

NUM_BINS = [
    ("GLA Sqft",              310, 348),
    ("Monthly Base Rent",     348, 400),
    ("Annual Rate PSF",       400, 442),
    ("Monthly Cost Recovery", 442, 500),
    ("Expense Stop",          500, 545),
    ("Monthly Other Income",  545, 600),
]
SECTIONS = ("Vacant Suites", "Occupied Suites", "New/Renewed Leases",
            "Leased/Unoccupied Suites")
BLDG_RE = re.compile(r'^\d{4,6}$')
DATE_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')


def num(t):
    if t is None:
        return None
    s = str(t).replace(',', '').replace('$', '').strip()
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def rows_of(words, tol=2.0):
    out, cur = [], []
    for w in sorted(words, key=lambda a: a['top']):
        if cur and w['top'] - cur[-1]['top'] > tol:
            out.append(sorted(cur, key=lambda a: a['x0']))
            cur = []
        cur.append(w)
    if cur:
        out.append(sorted(cur, key=lambda a: a['x0']))
    return out


def increase(row):
    cat = date = amt = psf = None
    for w in row:
        if w['x0'] < INCR_X0:
            continue
        t, x1 = w['text'], w['x1']
        if x1 <= 628:
            cat = t
        elif x1 <= 668:
            date = t
        elif x1 <= 732:
            amt = num(t)
        else:
            psf = num(t)
    return cat, date, amt, psf


def parse(pdf_path=PDF):
    lines, notes = [], {}
    section = None
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            for row in rows_of(page.extract_words(x_tolerance=1.6,
                                                  use_text_flow=True)):
                if row[0]['top'] < BODY_TOP:
                    continue
                flat = re.sub(r'\s+', ' ',
                              " ".join(w['text'] for w in row)).strip()
                if flat in SECTIONS:
                    section = flat
                    continue

                rec = {'Page': pno, 'Section': section, 'Line Kind': None,
                       'Bldg Id': None, 'Suite Id': None, 'Occupant Name': None,
                       'Rent Start': None, 'Expiration': None,
                       'Cat': None, 'Date': None, 'Monthly Amount': None,
                       'PSF': None, 'Raw Text': flat}
                for c, _, _ in NUM_BINS:
                    rec[c] = None
                claimed = set()
                for w in row:
                    for c, lo, hi in NUM_BINS:
                        if lo < w['x1'] <= hi:
                            rec.setdefault(c, None)
                            if rec[c] is None:
                                rec[c] = num(w['text'])
                            claimed.add(id(w))
                            break
                cat, date, amt, psf = increase(row)
                rec.update({'Cat': cat, 'Date': date,
                            'Monthly Amount': amt, 'PSF': psf})

                left = [w for w in row
                        if w['x0'] < INCR_X0 and id(w) not in claimed]
                lt = " ".join(w['text'] for w in left)
                is_add = ('Additional' in lt and 'Space' in lt)
                i = len(lines)

                if (flat.startswith(('Totals:', 'Grand Total', ENTITY_TOTAL_PREFIX))
                        or re.match(r'^(Occupied|Vacant|Leased/Unoccupied|Total)\b'
                                    r'.*Sqft:', flat)):
                    rec['Line Kind'] = 'Report Total'
                elif left and left[0]['text'] == 'Total' and left[0]['x0'] > 200:
                    rec['Line Kind'] = 'Suite Total'
                elif is_add or (left and BLDG_RE.match(left[0]['text'])
                                and left[0]['x0'] < 50):
                    rec['Line Kind'] = 'Additional Space' if is_add else 'Suite'
                    ids = [w for w in left if w['x0'] < 200]
                    if is_add:
                        # "Additional Space <bldg> - <suite>"
                        tail = [w for w in ids
                                if w['text'] not in ('Additional', 'Space')]
                        if tail and BLDG_RE.match(tail[0]['text']):
                            rec['Bldg Id'] = tail[0]['text']
                            rest = [w['text'] for w in tail[1:]
                                    if w['text'] != '-']
                            rec['Suite Id'] = rest[0] if rest else None
                        if rec['Suite Id'] is None:
                            notes.setdefault(i, []).append(
                                'could not read the suite id of this '
                                'Additional Space line')
                        # the dates sit to the right of the id block on the
                        # same line and must still be picked up
                        name_toks = [w for w in left if w['x0'] >= 200]
                    else:
                        rec['Bldg Id'] = left[0]['text']
                        suite = next((w for w in left[1:] if w['x0'] < 80), None)
                        rec['Suite Id'] = suite['text'] if suite else None
                        skip = {id(left[0])} | ({id(suite)} if suite else set())
                        name_toks = [w for w in left
                                     if id(w) not in skip and w['x0'] >= 80]
                    ds, nm = [], []
                    for w in name_toks:
                        (ds if DATE_RE.match(w['text']) else nm).append(w)
                    rec['Occupant Name'] = " ".join(w['text'] for w in nm).strip() or None
                    rec['Rent Start'] = ds[0]['text'] if len(ds) > 0 else None
                    rec['Expiration'] = ds[1]['text'] if len(ds) > 1 else None
                    if not is_add:
                        # dates for an Additional Space row sit in the same
                        # zone; grab them from the whole left side instead
                        pass
                    if nm and max(w['x1'] for w in nm) > DATE_COL_X0:
                        notes.setdefault(i, []).append(
                            'the occupant name runs into the Rent Start column; '
                            'the report overwrites a character where they '
                            'collide, so check the name against the PDF')
                    if len(ds) > 2:
                        notes.setdefault(i, []).append(
                            f'{len(ds)} date-like values on this line')
                elif cat:
                    rec['Line Kind'] = 'Rent Increase'
                elif not flat:
                    continue
                else:
                    rec['Line Kind'] = 'Other'
                    notes.setdefault(i, []).append(
                        'line did not match any known row type')
                lines.append(rec)

    return lines, notes


SRC_COLS = (['Page', 'Section', 'Line Kind', 'Bldg Id', 'Suite Id',
             'Occupant Name', 'Rent Start', 'Expiration']
            + [c for c, _, _ in NUM_BINS]
            + ['Cat', 'Date', 'Monthly Amount', 'PSF', 'Raw Text'])


def build_workings(lines, notes):
    out, flags = [], {}
    cur = None
    for i, L in enumerate(lines):
        k = L['Line Kind']
        if k in ('Suite', 'Additional Space'):
            parent = cur if k == 'Additional Space' else None
            cur = {'Bldg Id': L['Bldg Id'], 'Suite Id': L['Suite Id'],
                   'Bldg-Suite': f"{L['Bldg Id'] or ''}-{L['Suite Id'] or ''}".strip('-'),
                   'Occupant Name': (L['Occupant Name']
                                     or (parent['Occupant Name'] if parent else None)),
                   'Section': L['Section'], 'Row Kind': k,
                   'Rent Start': L['Rent Start'], 'Expiration': L['Expiration'],
                   'Page': L['Page'], '_bumps': []}
            for c, _, _ in NUM_BINS:
                cur[c] = L[c]
            if L['Cat']:
                cur['_bumps'].append((L['Cat'], L['Date'],
                                      L['Monthly Amount'], L['PSF']))
            out.append(cur)
            if i in notes:
                flags[len(out) - 1] = list(notes[i])
        elif k == 'Rent Increase' and cur is not None:
            cur['_bumps'].append((L['Cat'], L['Date'],
                                  L['Monthly Amount'], L['PSF']))
        elif k in ('Suite Total', 'Report Total'):
            cur = None
    return out, flags


def code_blocks(lines, work):
    order = []
    for L in lines:
        if L['Cat'] and L['Cat'] not in order:
            order.append(L['Cat'])
    widest = collections.Counter()
    for r in work:
        c = collections.Counter(b[0] for b in r['_bumps'] if b[0])
        for kk, v in c.items():
            widest[kk] = max(widest[kk], v)
    return [(code, widest[code]) for code in order if widest[code]]


def column_spec(lines, work):
    spec = [("", "", "Bldg-Suite", "Bldg-Suite"),
            ("", "", "Bldg Id", "Bldg Id"),
            ("", "", "Suite Id", "Suite Id"),
            ("", "", "Occupant Name", "Occupant Name"),
            ("", "", "Section", "Section"),
            ("", "", "Row Kind", "Row Kind"),
            ("", "", "Rent Start", "Rent Start"),
            ("", "", "Expiration", "Expiration"),
            ("", "", "GLA Sqft", "GLA Sqft"),
            ("", "", "Monthly Base Rent", "Monthly Base Rent"),
            ("", "", "Monthly Cost Recovery", "Monthly Cost Recovery"),
            ("", "", "Monthly Other Income", "Monthly Other Income"),
            ("", "", "Annual Rate PSF", "Annual Rate PSF"),
            ("", "", "Expense Stop", "Expense Stop"),
            ("", "", "", None)]
    for code, n_max in code_blocks(lines, work):
        for n in range(1, n_max + 1):
            g = f"bump{n}"
            spec += [(g, code, "Date", f"{code}|{n}|Date"),
                     (g, code, "Monthly Amount", f"{code}|{n}|Amount"),
                     (g, code, "PSF", f"{code}|{n}|PSF")]
    spec += [("", "", "Page", "Page"), ("", "", "Review Note", "Review Note")]
    return spec


def write(lines, work, wflags, path=XLSX):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    YELLOW = PatternFill('solid', fgColor='FFFF00')
    G1 = PatternFill('solid', fgColor='1F3864')
    G2 = PatternFill('solid', fgColor='2E5496')
    GAPF = PatternFill('solid', fgColor='D9D9D9')
    BAND = PatternFill('solid', fgColor='F2F5FA')
    WHITE = Font(bold=True, color='FFFFFF', size=10)
    M2 = '#,##0.00;(#,##0.00);-'
    M0 = '#,##0;(#,##0);-'

    wb = Workbook()
    ws = wb.active
    ws.title = 'Source (as printed)'
    ws.append(SRC_COLS)
    for i, c in enumerate(SRC_COLS, start=1):
        cell = ws.cell(row=1, column=i)
        cell.fill, cell.font = G1, WHITE
        cell.alignment = Alignment(horizontal='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = {
            'Occupant Name': 34, 'Raw Text': 62, 'Section': 17,
            'Line Kind': 15, 'Suite Id': 11, 'Bldg Id': 9,
        }.get(c, 13)
    for ri, L in enumerate(lines, start=2):
        for ci, c in enumerate(SRC_COLS, start=1):
            cell = ws.cell(row=ri, column=ci, value=L.get(c))
            if c == 'GLA Sqft':
                cell.number_format = M0
            elif c in ('Monthly Base Rent', 'Annual Rate PSF',
                       'Monthly Cost Recovery', 'Expense Stop',
                       'Monthly Other Income', 'Monthly Amount', 'PSF'):
                cell.number_format = M2
            if ri % 2 == 0:
                cell.fill = BAND
    ws.freeze_panes = 'C2'
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False

    spec = column_spec(lines, work)
    ws2 = wb.create_sheet('Workings')
    for i, (grp, code, field, key) in enumerate(spec, start=1):
        L = get_column_letter(i)
        c1, c2, c3 = (ws2.cell(row=r, column=i) for r in (1, 2, 3))
        if key is None:
            c1.fill = c2.fill = c3.fill = GAPF
            ws2.column_dimensions[L].width = 2.5
            continue
        prev = spec[i - 2] if i > 1 else None
        first = (prev is None or prev[3] is None
                 or (prev[0], prev[1]) != (grp, code))
        c1.value = (grp or None) if (grp and first) else None
        c2.value = code or None
        c3.value = field
        for c, f in ((c1, G1), (c2, G2), (c3, G2)):
            c.fill, c.font = f, WHITE
            c.alignment = Alignment(horizontal='left' if c is c1 else 'center',
                                    vertical='center', wrap_text=True)
        ws2.column_dimensions[L].width = {
            'Occupant Name': 34, 'Bldg-Suite': 14, 'Section': 17,
            'Row Kind': 15, 'Review Note': 62, 'Page': 6,
            'Monthly Amount': 15, 'Monthly Cost Recovery': 16,
            'Monthly Other Income': 16, 'Monthly Base Rent': 15,
        }.get(field, 13)
    for r in (1, 2, 3):
        ws2.row_dimensions[r].height = 20

    field_col = {k: i for i, (_, _, _, k) in enumerate(spec, start=1) if k}
    for ri, r in enumerate(work, start=4):
        vals = dict(r)
        note = list(wflags.get(ri - 4, []))
        seen = {}
        for cat, date, amt, psf in r['_bumps']:
            if not cat:
                continue
            seen[cat] = seen.get(cat, 0) + 1
            n = seen[cat]
            vals[f'{cat}|{n}|Date'] = date
            vals[f'{cat}|{n}|Amount'] = amt
            vals[f'{cat}|{n}|PSF'] = psf
            if f'{cat}|{n}|Date' not in field_col:
                note.append(f'{cat} bump{n} has no column allocated')
        vals['Review Note'] = " | ".join(note) or None
        for ci, (grp, code, field, key) in enumerate(spec, start=1):
            cell = ws2.cell(row=ri, column=ci)
            if key is None:
                cell.fill = GAPF
                continue
            cell.value = vals.get(key)
            if key == 'GLA Sqft':
                cell.number_format = M0
            elif ('|' in key and (key.endswith('|Amount') or key.endswith('|PSF'))) \
                    or key in ('Monthly Base Rent', 'Annual Rate PSF',
                               'Monthly Cost Recovery', 'Expense Stop',
                               'Monthly Other Income'):
                cell.number_format = M2
            if ri % 2 == 0:
                cell.fill = BAND
        if note:
            ws2.cell(row=ri, column=field_col['Review Note']).fill = YELLOW
            for nz in note:
                if 'occupant name' in nz:
                    ws2.cell(row=ri, column=field_col['Occupant Name']).fill = YELLOW
                if 'suite id' in nz:
                    ws2.cell(row=ri, column=field_col['Suite Id']).fill = YELLOW
    ws2.sheet_view.showGridLines = False
    ws2.freeze_panes = 'D4'
    ws2.auto_filter.ref = f"A3:{get_column_letter(len(spec))}{ws2.max_row}"
    for cell in ws2[get_column_letter(field_col['Review Note'])][3:]:
        cell.alignment = Alignment(wrap_text=True, vertical='top')
    wb.save(path)


if __name__ == '__main__':
    lines, notes = parse()
    work, wflags = build_workings(lines, notes)
    write(lines, work, wflags)
    print(f"source lines: {len(lines)}")
    print("  by kind:", dict(collections.Counter(L['Line Kind'] for L in lines)))
    print(f"workings rows: {len(work)}  flagged: {len(wflags)}")
    for sec in ('Occupied Suites', 'Vacant Suites'):
        s = [r for r in work if r['Section'] == sec]
        print(f"    {sec:18s} rows={len(s):3d} sqft={sum(r['GLA Sqft'] or 0 for r in s):>9,.0f}"
              f" rent={sum(r['Monthly Base Rent'] or 0 for r in s):>12,.2f}")
    print("  increases:", sum(len(r['_bumps']) for r in work),
          "| blocks:", code_blocks(lines, work))
    print("\n->", XLSX)
