"""
"05-Rent Roll" (IRVUBS / 100 SUMMER)  ->  Excel, two sheets.

  Sheet 1 "Source (as printed)" - one row per physical PDF line, columns
      exactly as the report prints them, totals included. Traceability.
  Sheet 2 "Workings"            - one row per suite / additional space, with
      every future rent increase laid out across as Cat/Date/Amount/PSF.
      Totals excluded.

Two traps in this report, both of which corrupt a naive extract:

  * A long occupant name overflows RIGHT THROUGH the Rent Start / Expiration
    columns, so name words and date words interleave by x
    ("Management@176 | 2/1/2027@215 | and@218 | Maintenance@232 | 1/31/2037@256").
    Dates are therefore found by PATTERN, and the name is whatever is left --
    never by x-position alone.
  * The Suite Id and Lease ID are sometimes printed as one glued token
    ("-00101002186"). The Lease ID is always the trailing 6 digits.
"""
import re
import pdfplumber

PDF = r"C:\path\to\your\folder\rentrolls\05-Rent Roll.pdf"
XLSX = r"C:\path\to\your\folder\rentrolls\05-Rent Roll - extracted v2.xlsx"

BODY_TOP = 110.0
IDS_MAX_X = 210.0        # bldg / suite / lease ids live left of this
NAME_MAX_X = 320.0       # name + dates zone ends where GLA Sqft begins
INCR_X0 = 600.0          # right of this = Future Rent Increases

NUM_BINS = [
    ("GLA Sqft",              310, 348),
    ("Monthly Base Rent",     348, 400),
    ("Annual Rate PSF",       400, 442),
    ("Monthly Cost Recovery", 442, 500),
    ("Expense Stop",          500, 545),
    ("Monthly Other Income",  545, 600),
]
SECTIONS = ("New/Renewed Leases", "Vacant Suites", "Occupied Suites")
BLDG_RE = re.compile(r'^[A-Z]{2,6}\d{3}$')
DATE_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')
LEASE_RE = re.compile(r'^\d{6}$')
SUITE_GLUED_RE = re.compile(r'^(-.{3,})(\d{6})$')
MAX_BUMPS = 20


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


def bins(row):
    got = {}
    for w in row:
        for col, lo, hi in NUM_BINS:
            if lo < w['x1'] <= hi:
                got.setdefault(col, w['text'])
                break
    return got


def increase(row):
    """Cat / Date / Monthly Amount / PSF from the right-hand block."""
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
    """Return (source_lines, notes_by_line_index)."""
    lines, notes = [], {}
    section = None
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            # use_text_flow keeps a name that prints over the date columns from
            # being interleaved character-by-character
            for row in rows_of(page.extract_words(x_tolerance=1.6,
                                                  use_text_flow=True)):
                if row[0]['top'] < BODY_TOP:
                    continue
                raw = " ".join(w['text'] for w in row)
                flat = re.sub(r'\s+', ' ', raw).strip()

                if flat in SECTIONS:
                    section = flat
                    continue

                rec = {'Page': pno, 'Section': section, 'Line Kind': None,
                       'Bldg Id': None, 'Suite Id': None, 'Lease ID': None,
                       'Occupant Name': None, 'Rent Start': None,
                       'Expiration': None, 'Cat': None, 'Date': None,
                       'Monthly Amount': None, 'PSF': None, 'Raw Text': flat}
                for c, _, _ in NUM_BINS:
                    rec[c] = None
                for c, v in bins(row).items():
                    rec[c] = num(v)
                cat, date, amt, psf = increase(row)
                rec.update({'Cat': cat, 'Date': date,
                            'Monthly Amount': amt, 'PSF': psf})

                # tokens already claimed by a numeric column must never be
                # mistaken for part of the occupant name: the GLA Sqft value
                # starts as far left as x=309 and would otherwise be appended
                # to the name.
                claimed = set()
                for w in row:
                    for _c, lo, hi in NUM_BINS:
                        if lo < w['x1'] <= hi:
                            claimed.add(id(w))
                            break
                left = [w for w in row
                        if w['x0'] < INCR_X0 and id(w) not in claimed]
                lt = " ".join(w['text'] for w in left)
                is_add = ('Additional' in lt and 'Space' in lt)
                bldg = next((w for w in left
                             if BLDG_RE.match(w['text']) and w['x0'] < 150), None)

                if flat.startswith(('Totals:', 'Grand Total')) or \
                        re.match(r'^(Occupied|Vacant|Leased/Unoccupied|Total)\b.*Sqft:', flat):
                    rec['Line Kind'] = 'Report Total'
                elif left and left[0]['text'] == 'Total' and bldg is None:
                    rec['Line Kind'] = 'Suite Total'
                elif bldg is not None:
                    rec['Line Kind'] = 'Additional Space' if is_add else 'Suite'
                    ids = [w for w in left if w['x0'] < IDS_MAX_X]
                    rec['Bldg Id'] = bldg['text']
                    suite = lease = None
                    for w in ids:
                        t = w['text']
                        if w is bldg or t in ('Additional', 'Space'):
                            continue
                        if t.startswith('-'):
                            m = SUITE_GLUED_RE.match(t)
                            if m:
                                suite, lease = m.group(1), m.group(2)
                            else:
                                suite = t
                        elif LEASE_RE.match(t) and lease is None:
                            lease = t
                    rec['Suite Id'] = suite
                    rec['Lease ID'] = lease
                    # dates by pattern; name = everything else in the zone
                    ds, nm = [], []
                    for w in left:
                        t = w['text']
                        if w is bldg or t in ('Additional', 'Space'):
                            continue
                        if t == suite or t == lease or (suite and lease
                                                        and t == suite + lease):
                            continue
                        if DATE_RE.match(t):
                            ds.append(t)
                        else:
                            nm.append(t)
                    rec['Occupant Name'] = " ".join(nm).strip() or None
                    rec['Rent Start'] = ds[0] if len(ds) > 0 else None
                    rec['Expiration'] = ds[1] if len(ds) > 1 else None
                    i = len(lines)
                    if len(ds) > 2:
                        notes.setdefault(i, []).append(
                            f'{len(ds)} date-like values on this line')
                    if rec['Lease ID'] is None and rec['Line Kind'] == 'Suite' \
                            and section != 'Vacant Suites':
                        notes.setdefault(i, []).append('no Lease ID found')
                elif cat:
                    rec['Line Kind'] = 'Rent Increase'
                elif not flat:
                    continue
                else:
                    rec['Line Kind'] = 'Other'
                    notes.setdefault(len(lines), []).append(
                        'line did not match any known row type')
                lines.append(rec)
    return lines, notes


SRC_COLS = (['Page', 'Section', 'Line Kind', 'Bldg Id', 'Suite Id', 'Lease ID',
             'Occupant Name', 'Rent Start', 'Expiration']
            + [c for c, _, _ in NUM_BINS]
            + ['Cat', 'Date', 'Monthly Amount', 'PSF', 'Raw Text'])


def build_workings(lines, notes):
    """Fold the source lines into one row per suite / additional space."""
    out, flags = [], {}
    cur = None
    for i, L in enumerate(lines):
        kind = L['Line Kind']
        if kind in ('Suite', 'Additional Space'):
            parent = cur if kind == 'Additional Space' else None
            cur = {
                'Bldg Id': L['Bldg Id'], 'Suite Id': L['Suite Id'],
                'Bldg-Suite': f"{L['Bldg Id'] or ''}{L['Suite Id'] or ''}" or None,
                'Lease ID': L['Lease ID'],
                # an additional space belongs to the suite printed above it
                'Occupant Name': (L['Occupant Name']
                                  or (parent['Occupant Name'] if parent else None)),
                'Section': L['Section'], 'Row Kind': kind,
                'Rent Start': L['Rent Start'], 'Expiration': L['Expiration'],
                'Page': L['Page'], '_bumps': [],
            }
            for c, _, _ in NUM_BINS:
                cur[c] = L[c]
            if L['Cat']:
                cur['_bumps'].append((L['Cat'], L['Date'],
                                      L['Monthly Amount'], L['PSF']))
            out.append(cur)
            if i in notes:
                flags[len(out) - 1] = list(notes[i])
        elif kind == 'Rent Increase' and cur is not None:
            cur['_bumps'].append((L['Cat'], L['Date'],
                                  L['Monthly Amount'], L['PSF']))
        elif kind == 'Rent Increase':
            pass  # an increase with no suite above it; surfaced below
        elif kind in ('Suite Total', 'Report Total'):
            cur = None
    for idx, r in enumerate(out):
        if len(r['_bumps']) > MAX_BUMPS:
            flags.setdefault(idx, []).append(
                f"{len(r['_bumps'])} increases found; only the first "
                f"{MAX_BUMPS} are shown")
    return out, flags


def code_blocks(lines, work):
    """Charge codes in order of first appearance, and how many bumps of each
    the widest row needs. Each code gets its OWN bump series, exactly as the
    report groups them -- RNT bump1..n, then FRE bump1..n, and so on."""
    import collections
    order = []
    for L in lines:
        if L['Cat'] and L['Cat'] not in order:
            order.append(L['Cat'])
    widest = collections.Counter()
    for r in work:
        c = collections.Counter(b[0] for b in r['_bumps'] if b[0])
        for k, v in c.items():
            widest[k] = max(widest[k], v)
    return [(code, widest[code]) for code in order if widest[code]]


def column_spec(lines, work):
    """(row1 bump label, row2 charge code, row3 field, data key)."""
    spec = [("", "", "Bldg-Suite", "Bldg-Suite"),
            ("", "", "Lease ID", "Lease ID"),
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
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    YELLOW = PatternFill('solid', fgColor='FFFF00')
    G1 = PatternFill('solid', fgColor='1F3864')
    G2 = PatternFill('solid', fgColor='2E5496')
    GAPF = PatternFill('solid', fgColor='D9D9D9')
    BAND = PatternFill('solid', fgColor='F2F5FA')
    WHITE = Font(bold=True, color='FFFFFF', size=10)
    thin = Side(style='thin', color='BFBFBF')
    med = Side(style='medium', color='1F3864')
    M2 = '#,##0.00;(#,##0.00);-'
    M0 = '#,##0;(#,##0);-'

    wb = Workbook()

    # ---------- Sheet 1: source ------------------------------------------
    ws = wb.active
    ws.title = 'Source (as printed)'
    ws.append(SRC_COLS)
    for i, c in enumerate(SRC_COLS, start=1):
        cell = ws.cell(row=1, column=i)
        cell.fill, cell.font = G1, WHITE
        cell.alignment = Alignment(horizontal='center', wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = {
            'Occupant Name': 34, 'Raw Text': 60, 'Section': 18,
            'Line Kind': 15, 'Bldg Id': 10, 'Suite Id': 13, 'Lease ID': 10,
        }.get(c, 13)
    for ri, L in enumerate(lines, start=2):
        for ci, c in enumerate(SRC_COLS, start=1):
            cell = ws.cell(row=ri, column=ci, value=L.get(c))
            if c in ('GLA Sqft',):
                cell.number_format = M0
            elif c in ('Monthly Base Rent', 'Annual Rate PSF',
                       'Monthly Cost Recovery', 'Expense Stop',
                       'Monthly Other Income', 'Monthly Amount', 'PSF'):
                cell.number_format = M2
            if ri % 2 == 0:
                cell.fill = BAND
    ws.freeze_panes = 'D2'
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False

    # ---------- Sheet 2: workings ----------------------------------------
    spec = column_spec(lines, work)
    ws2 = wb.create_sheet('Workings')

    # three header rows, NO merged cells: the group label (bump1, bump2 ...)
    # and the charge code are written into every column of their group.
    for i, (grp, code, field, key) in enumerate(spec, start=1):
        L = get_column_letter(i)
        c1 = ws2.cell(row=1, column=i)
        c2 = ws2.cell(row=2, column=i)
        c3 = ws2.cell(row=3, column=i)
        if key is None:
            c1.fill = c2.fill = c3.fill = GAPF
            ws2.column_dimensions[L].width = 2.5
            continue
        # the group label sits ONLY in the first column of its group; the
        # remaining cells of the group stay blank (still styled), so the
        # header reads like the report without any merged cells
        # a group is (charge code, bump label) -- FRE bump1 and TIA bump1
        # are different groups even though both read 'bump1'
        prev = spec[i - 2] if i > 1 else None
        first_of_group = (prev is None or prev[3] is None
                          or (prev[0], prev[1]) != (grp, code))
        c1.value = (grp or None) if (grp and first_of_group) else None
        c2.value = code or None
        c3.value = field
        for c, f in ((c1, G1), (c2, G2), (c3, G2)):
            c.fill, c.font = f, WHITE
            c.alignment = Alignment(
                horizontal='left' if c is c1 else 'center',
                vertical='center', wrap_text=True)
        ws2.column_dimensions[L].width = {
            'Occupant Name': 34, 'Bldg-Suite': 16, 'Section': 18,
            'Row Kind': 15, 'Review Note': 60, 'Page': 6,
            'Monthly Amount': 15,
        }.get(field, 14)
    for r in (1, 2, 3):
        ws2.row_dimensions[r].height = 20

    field_col = {k: i for i, (_, _, _, k) in enumerate(spec, start=1) if k}
    for ri, r in enumerate(work, start=4):
        vals = dict(r)
        notes_here = list(wflags.get(ri - 4, []))
        # each increase goes into its OWN charge code's bump series, in the
        # order the report prints them
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
                notes_here.append(f'{cat} bump{n} has no column allocated')
        vals['Review Note'] = " | ".join(notes_here) or None
        for ci, (grp, code, field, key) in enumerate(spec, start=1):
            cell = ws2.cell(row=ri, column=ci)
            if key is None:
                cell.fill = GAPF
                continue
            cell.value = vals.get(key)
            if key == 'GLA Sqft':
                cell.number_format = M0
            elif '|' in key and (key.endswith('|Amount') or key.endswith('|PSF')):
                cell.number_format = M2
            elif key in ('Monthly Base Rent', 'Annual Rate PSF',
                         'Monthly Cost Recovery', 'Expense Stop',
                         'Monthly Other Income'):
                cell.number_format = M2
            if ri % 2 == 0:
                cell.fill = BAND
        if notes_here:
            ws2.cell(row=ri, column=field_col['Review Note']).fill = YELLOW
            for nz in notes_here:
                if 'Lease ID' in nz:
                    ws2.cell(row=ri, column=field_col['Lease ID']).fill = YELLOW
                if 'date-like' in nz:
                    for f in ('Rent Start', 'Expiration', 'Occupant Name'):
                        ws2.cell(row=ri, column=field_col[f]).fill = YELLOW

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
    import collections
    print(f"source lines: {len(lines)}")
    print("  by kind:", dict(collections.Counter(L['Line Kind'] for L in lines)))
    print(f"workings rows: {len(work)}   flagged: {len(wflags)}")
    for sec in SECTIONS:
        s = [r for r in work if r['Section'] == sec]
        print(f"    {sec:20s} rows={len(s):3d}  sqft={sum(r['GLA Sqft'] or 0 for r in s):>10,.0f}"
              f"  rent={sum(r['Monthly Base Rent'] or 0 for r in s):>14,.2f}")
    print("  increases total:", sum(len(r['_bumps']) for r in work),
          "| max per row:", max(len(r['_bumps']) for r in work))
    print("\n->", XLSX)
