"""
Con Edison statements (scanned) -> Excel.

Source PDFs have no text layer, so pages are rendered and OCR'd with the local
Windows OCR engine first (st_render.py + ocr_batch.ps1). This module parses the
resulting word boxes.

Per statement it pulls:
  account number (NOT the "Prior Account"), provider, billing period,
  every line under "Your new charges" (electricity, gas, and anything else),
  the new-charges total, and the total amount due.

Everything uncertain is flagged for a human: the workbook marks those cells
YELLOW with a note.
"""
import re, json, glob, os
from collections import defaultdict

# --- geometry (page rendered at scale 3.0 -> 1836 x 2376) -------------------
LEFT_MAX = 950.0        # right of this is the usage chart, not statement text
AMT_MIN, AMT_MAX = 780.0, 950.0   # the money column on the left panel
ACCT_MAX_X = 470.0      # the real account sits left of this; Prior Account is ~674

MONEY = re.compile(r'^-?\$-?[\d,]+\.\d{2}$')
ACCT = re.compile(r'^\d{4,6}-\d{4,6}-\d$')
PRIOR = re.compile(r'^\d{2}-\d{4}-\d{4}-\d{4}-\d$')
DATE = re.compile(r'^([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2}),?\s*(\d{4})$')
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}


def money(t):
    if t is None:
        return None
    s = str(t).replace('$', '').replace(',', '').strip()
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def load_words(path):
    ws = json.load(open(path, encoding='utf-8-sig'))
    if isinstance(ws, dict):
        ws = [ws]
    for w in ws:
        w['cy'] = w['y'] + w['h'] / 2
        w['x1'] = w['x'] + w['w']
    return ws


def rejoin_money(ln):
    """OCR splits thousands-separated amounts ("$1 ,003.90" -> "$1" + ",003.90").
    A token starting with a comma is always a fragment, so glue it back on."""
    out = []
    for w in ln:
        if out:
            p = out[-1]
            gap = w['x'] - p['x1']
            if (w['t'].lstrip().startswith(',') and gap < 30
                    and re.fullmatch(r'-?\$?[\d,]*\d', p['t'].strip())):
                p['t'] = p['t'].strip() + w['t'].strip()
                p['x1'] = w['x1']
                continue
        out.append(dict(w))
    return out


def lines_of(words, tol=11.0):
    """Group words into visual lines. OCR jitters the baseline of a wrapped
    fragment by a few px, so cluster on a tolerance rather than a bucket."""
    out, cur = [], []
    for w in sorted(words, key=lambda a: a['cy']):
        if cur and w['cy'] - cur[-1]['cy'] > tol:
            out.append(rejoin_money(sorted(cur, key=lambda a: a['x'])))
            cur = []
        cur.append(w)
    if cur:
        out.append(rejoin_money(sorted(cur, key=lambda a: a['x'])))
    return out


def norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


def nlabel(s):
    """Normalised text for LABEL matching only -- never for values.
    Repairs the OCR confusions seen in these scans (period->perlod,
    Total->Totat/otal, this->thls)."""
    n = norm(s)
    for a, b in (('perlod', 'period'), ('billlng', 'billing'),
                 ('blliing', 'billing'), ('billing', 'billing'),
                 ('thls', 'this'), ('totat', 'total')):
        n = n.replace(a, b)
    return n


def line_text(ln):
    return " ".join(w['t'] for w in ln)


def find_amount(ln):
    """The money value on this line, taken from the left panel's money column."""
    c = [w for w in ln if AMT_MIN <= w['x'] <= AMT_MAX and MONEY.match(w['t'])]
    return money(c[0]['t']) if c else None


def parse_page(path):
    ws = load_words(path)
    left = [w for w in ws if w['x'] < LEFT_MAX]
    lines = lines_of(left)
    r = {'notes': [], 'others': []}

    flat = norm(" ".join(w['t'] for w in ws))

    # ---- is this the first page of a statement? ---------------------------
    r['is_statement'] = ('billingperiod' in flat and 'account' in flat)

    # ---- provider ----------------------------------------------------------
    if 'conedison' in flat:
        r['Provider'] = 'Con Edison'
    elif 'nationalgrid' in flat:
        r['Provider'] = 'National Grid'
    else:
        r['Provider'] = None
        r['notes'].append('provider not recognised')

    # ---- account number (never the Prior Account) --------------------------
    accts = [w['t'] for w in left if w['x'] < ACCT_MAX_X and ACCT.match(w['t'])]
    priors = [w['t'] for w in ws if PRIOR.match(w['t'])]
    r['Account Number'] = accts[0] if accts else None
    r['Prior Account (excluded)'] = priors[0] if priors else None
    if not accts:
        # fall back: any account-shaped token that is not the prior account
        alt = [w['t'] for w in ws if ACCT.match(w['t'])]
        if alt:
            r['Account Number'] = alt[0]
            r['notes'].append('account number found outside its usual column')
        else:
            r['notes'].append('no account number found')
    elif len(set(accts)) > 1:
        r['notes'].append(f'several account-shaped values found: {sorted(set(accts))}')

    # ---- service address ---------------------------------------------------
    for ln in lines:
        t = line_text(ln)
        if 'delivered' in t.lower():
            m = re.split(r'to:?', t, flags=re.I)
            if len(m) > 1:
                r['Service Address'] = m[-1].strip() or None
            break
    else:
        deliv = [w for w in ws if 'deliver' in w['t'].lower()]
        if deliv:
            y = deliv[0]['cy']
            tail = [w['t'] for w in ws if abs(w['cy'] - y) < 12 and w['x'] > deliv[0]['x']]
            r['Service Address'] = " ".join(tail[1:]).strip() or None
        else:
            r['Service Address'] = None

    # ---- billing period ----------------------------------------------------
    r['Billing Period'] = r['Billing Period Start'] = r['Billing Period End'] = None
    r['Electric Billing Period'] = r['Gas Billing Period'] = None
    found = []
    for ln in lines:
        t = line_text(ln)
        n = nlabel(t)
        # "Last billing period" is a chart caption; "Total from this billing
        # period" is the total line -- neither is the statement's period.
        if 'billingperiod' not in n or 'lastbilling' in n or 'fromthisbilling' in n:
            continue
        pre = re.match(r'^(.*?)billing\s*p[e3]r[il]od', t, flags=re.I)
        commodity = (pre.group(1).strip() if pre else '') or None
        seg = re.sub(r'^.*?p[e3]r[il]od:?', '', t, flags=re.I).strip()
        seg = re.sub(r'(?<=\d)\s+(?=\d)', '', seg)
        ds = re.findall(r'([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2}),?\s*(\d{4})', seg)
        found.append((commodity, seg, ds))
    if not found:
        r['notes'].append('no billing period line found')
    else:
        for commodity, seg, ds in found:
            if commodity and commodity.lower().startswith('elec'):
                r['Electric Billing Period'] = seg
            elif commodity and commodity.lower().startswith('gas'):
                r['Gas Billing Period'] = seg
        commodity, seg, ds = found[0]
        r['Billing Period'] = " | ".join(f"{(c + ': ') if c else ''}{sg}"
                                         for c, sg, _ in found)
        if len(ds) >= 2:
            r['Billing Period Start'] = fmt_date(ds[0])
            r['Billing Period End'] = fmt_date(ds[1])
        elif ds:
            r['notes'].append(f'only one date parsed from billing period: {seg!r}')
        else:
            r['notes'].append(f'could not read dates from billing period: {seg!r}')
        if len(found) > 1:
            r['notes'].append(
                'this statement prints separate billing periods per commodity - '
                'the Start/End columns show the first one')

    # ---- statement "as of" date --------------------------------------------
    r['As Of Date'] = r['As Of (as printed)'] = None
    for ln in lines:
        t = line_text(ln)
        n = nlabel(t)
        if 'billingsummaryasof' in n or ('billingsummary' in n and 'asof' in n):
            seg = re.split(r'as\s*of', t, flags=re.I)[-1].strip(' :')
            seg = re.sub(r'(?<=\d)\s+(?=\d)', '', seg)
            r['As Of (as printed)'] = seg or None
            d = re.search(r'([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2}),?\s*(\d{4})', seg)
            if d:
                r['As Of Date'] = fmt_date(d.groups())
            else:
                r['notes'].append(
                    f'could not read the "billing summary as of" date from {seg!r}')
            break
    else:
        r['notes'].append('no "Your billing summary as of" line found')

    # ---- the "Your new charges" block --------------------------------------
    start = end = None
    for i, ln in enumerate(lines):
        n = nlabel(line_text(ln))
        if start is None and 'yournewcharges' in n:
            start = i
        if start is not None and 'totalamountdue' in n:
            end = i
            break
    if start is None:
        r['notes'].append('could not find the "Your new charges" heading')
    else:
        for ln in lines[start + 1: end if end is not None else len(lines)]:
            t = line_text(ln)
            n = nlabel(t)
            amt = find_amount(ln)
            if 'billingperiod' in n and 'fromthisbilling' not in n:
                continue
            if 'amountdue' in n or n.endswith('amountdue'):
                continue
            if 'fromthisbilling' in n:
                r['Total New Charges'] = amt
                continue
            if amt is None:
                continue
            bare = re.sub(r'-?\$?[\d,]+\.\d{2}', '', t).strip()
            if len(bare) <= 3:
                # no label at all -> this is the unlabelled new-charges total
                if r.get('Total New Charges') is None:
                    r['Total New Charges'] = amt
                    r['notes'].append(
                        'the "Total from this billing period" label was not '
                        'legible; used the unlabelled amount printed above '
                        '"Total amount due"')
                continue
            label = re.sub(r'\s*-?\s*for\s+\d+\s*days?.*$', '', t, flags=re.I)
            label = re.sub(r'-?\$?[\d,]+\.\d{2}', '', label).strip(' -')
            days = re.search(r'for\s+(\d+)\s*days?', t, flags=re.I)
            key = nlabel(label)
            if key.startswith('electricity') or key.startswith('electric'):
                r['Electricity Charges'] = amt
                r['Electricity Days'] = int(days.group(1)) if days else None
            elif key.startswith('gas'):
                r['Gas Charges'] = amt
                r['Gas Days'] = int(days.group(1)) if days else None
            else:
                r['others'].append((label or t.strip(), amt))

    # If OCR dropped the label, the new-charges total is the bare amount on the
    # line directly above "Total amount due".
    if r.get('Total New Charges') is None and start is not None and end is not None:
        for ln in reversed(lines[start + 1:end]):
            amt = find_amount(ln)
            if amt is None:
                continue
            rest = re.sub(r'-?\$?[\d,]+\.\d{2}', '', line_text(ln)).strip()
            if len(rest) <= 3:          # essentially just the number
                r['Total New Charges'] = amt
                r['notes'].append(
                    'the "Total from this billing period" label was not legible; '
                    'used the unlabelled amount printed directly above '
                    '"Total amount due"')
            break

    # ---- total amount due ---------------------------------------------------
    for ln in lines:
        if 'totalamountdue' in nlabel(line_text(ln)):
            r['Total Amount Due'] = find_amount(ln)
            break

    r.setdefault('Electricity Charges', None)
    r.setdefault('Gas Charges', None)
    r.setdefault('Electricity Days', None)
    r.setdefault('Gas Days', None)
    r.setdefault('Total New Charges', None)
    r.setdefault('Total Amount Due', None)
    return r


def fmt_date(t):
    mon, day, yr = t
    m = MONTHS.get(mon.lower()[:3])
    return f"{m:02d}/{int(day):02d}/{yr}" if m else f"{mon} {day}, {yr}"


def validate(r):
    """Cross-check: the individual charge lines must sum to the new-charges
    total. This is what catches an OCR'd decimal."""
    parts = [v for v in (r['Electricity Charges'], r['Gas Charges']) if v is not None]
    parts += [a for _, a in r['others'] if a is not None]
    tot = r['Total New Charges']
    r['_sum_ok'] = None
    if parts and tot is not None:
        s = round(sum(parts), 2)
        r['_sum_ok'] = abs(s - tot) <= 0.02
        if not r['_sum_ok']:
            r['notes'].append(
                f'charge lines sum to {s:,.2f} but "Total from this billing '
                f'period" reads {tot:,.2f}')
    elif tot is None:
        r['notes'].append('no "Total from this billing period" value found')
    elif not parts:
        r['notes'].append('no individual charge lines found')
    if r['Total Amount Due'] is None:
        r['notes'].append('no "Total amount due" value found')
    return r


def run(img_dir='st_img'):
    out = []
    for p in sorted(glob.glob(os.path.join(img_dir, '*.json'))):
        base = os.path.basename(p)
        m = re.match(r'f(\d+)_(.+)_p(\d+)\.json$', base)
        r = parse_page(p)
        if not r['is_statement']:
            continue
        r['Source PDF'] = m.group(2) if m else base
        r['PDF Page'] = int(m.group(3)) + 1 if m else None
        r['_file_idx'] = int(m.group(1)) if m else None
        out.append(validate(r))
    return out


if __name__ == '__main__':
    recs = run()
    print(f"statements parsed: {len(recs)}")
    ok = sum(1 for r in recs if not r['notes'])
    print(f"  clean: {ok}   flagged: {len(recs)-ok}")
    from collections import Counter
    c = Counter(n.split(':')[0][:60] for r in recs for n in r['notes'])
    for k, v in c.most_common():
        print(f"    {v:3d}x {k}")
    json.dump(recs, open('st_records.json', 'w'), indent=0, default=str)
