"""Check a parse against what the document says about itself.

A report that prints its own totals has already done the arithmetic. If the
figures read off the page add up to the printed total, the reading is almost
certainly right; if they do not, something is wrong and the cell must not be
shipped as clean. That is the whole basis for trusting the output, and it is
why no number here is ever "probably fine".

Every check returns the same record so a report can be rendered, a cell
flagged, and a reason given to the person who has to fix it:

    {check, scope, label, expected, got, diff, ok, note}
"""
from __future__ import annotations

from .tabular import parse_number

# Printed figures are rounded, so sums carry a little slack. Anything larger
# than a cent per contributing row is a real disagreement, not rounding.
TOL = 0.011


def _val(row, col):
    return parse_number(row["values"][col]) if row["values"][col] else None


def body_indents(rows, tol=2.0):
    """(item_x0, total_min_x0) measured from the rows that carry figures.

    Taken from x0 rather than the level index for two reasons. Levels are
    numbered per page, so level 1 on page 2 need not be the same indent as
    level 1 on page 1; and `indent_levels` counts the title, date and column
    headings as levels too, which on this income statement produced ten of
    them and left the totals at a level nothing matched.

    The item indent is the MOST COMMON one among rows that hold figures, not
    the shallowest. A grand total is often printed flush with the section
    headings rather than indented under them -- on this income statement
    "TOTAL INCOME" and "NET INCOME (LOSS)" sit at x=53 alongside the headers,
    while the 76 line items sit at x=59. Taking the minimum made a grand total
    look like a line item and every check failed. Items outnumber totals by a
    wide margin in any report, so the mode is the stable signal.
    """
    import collections

    xs = [round(r["x0"], 0) for r in rows
          if r["x0"] is not None and any(v for v in r["values"])]
    if not xs:
        return None, None
    item = collections.Counter(xs).most_common(1)[0][0]
    deeper = [x for x in xs if x > item + tol]
    return item, (min(deeper) if deeper else None)


def section_totals(rows, col, total_level=None, item_level=None, tol=TOL,
                   indent_tol=2.0):
    """Each total row against the sum of the items it follows.

    Sections are delimited by the headers above them, so the items counted
    toward a total are those since the last header -- not every item on the
    page, which would silently pass on a report with one section and fail on
    every report with two.
    """
    item_x0, total_x0 = body_indents(rows, indent_tol)
    if item_x0 is None or total_x0 is None:
        return []

    out, bucket = [], []
    for r in rows:
        x = r["x0"]
        if x is None:
            continue
        if x < item_x0 - indent_tol:        # a header starts a new section
            bucket = []
            continue
        if abs(x - item_x0) <= indent_tol:
            v = _val(r, col)
            if v is not None:
                bucket.append((r["label"], v))
            continue
        if x >= total_x0 - indent_tol:
            printed = _val(r, col)
            if printed is None or not bucket:
                bucket = []
                continue
            got = round(sum(v for _l, v in bucket), 2)
            diff = round(got - printed, 2)
            out.append({
                "check": "section_total",
                "scope": f"col{col}",
                "label": r["label"],
                "expected": printed,
                "got": got,
                "diff": diff,
                "ok": abs(diff) <= max(tol, tol * len(bucket)),
                "note": f"{len(bucket)} item(s)",
                "page": r["page"],
                "y": r["y"],
            })
            bucket = []
    return out


def unbinned_tokens(rows):
    """Any numeric token that matched no column. Must be zero, or explained.

    The pack is unambiguous here and experience agrees: a stray number means
    the layout is not understood yet. Shipping around it is how a column ends
    up quietly holding somebody else's figures.
    """
    out = []
    for r in rows:
        if r["unbinned"]:
            out.append({
                "check": "unbinned_token",
                "scope": "row",
                "label": r["label"][:60],
                "expected": "no stray numbers",
                "got": ", ".join(r["unbinned"]),
                "diff": None,
                "ok": False,
                "note": "numeric token outside every column",
                "page": r["page"],
                "y": r["y"],
            })
    return out


def column_completeness(rows, cols, item_level=None):
    """Item rows that filled some columns but not others.

    Usually means a value was missed rather than absent: a report that prints
    a figure in one column normally prints one in the rest of that row.
    """
    item_x0, _total_x0 = body_indents(rows)
    if item_x0 is None:
        return []
    out = []
    for r in rows:
        if r["x0"] is None or abs(r["x0"] - item_x0) > 2.0:
            continue
        filled = [i for i, v in enumerate(r["values"]) if v]
        if filled and len(filled) != len(cols):
            missing = [i for i in range(len(cols)) if i not in filled]
            out.append({
                "check": "ragged_row",
                "scope": "row",
                "label": r["label"][:60],
                "expected": f"{len(cols)} value(s)",
                "got": f"{len(filled)} value(s)",
                "diff": None,
                "ok": False,
                "note": f"empty column(s): {missing}",
                "page": r["page"],
                "y": r["y"],
            })
    return out


def run_all(rows, cols):
    """Every check, for every numeric column."""
    results = []
    for c in range(len(cols)):
        results += section_totals(rows, c)
    results += unbinned_tokens(rows)
    results += column_completeness(rows, cols)
    return results


def summarise(results):
    passed = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    by_check = {}
    for r in results:
        b = by_check.setdefault(r["check"], {"pass": 0, "fail": 0})
        b["pass" if r["ok"] else "fail"] += 1
    return {"total": len(results), "passed": len(passed),
            "failed": len(failed), "by_check": by_check,
            "failures": failed}


def report(results, show=12):
    s = summarise(results)
    lines = [f"{s['passed']}/{s['total']} checks passed"]
    for name, b in sorted(s["by_check"].items()):
        lines.append(f"  {name:<18} pass {b['pass']:>3}   fail {b['fail']:>3}")
    if s["failures"]:
        lines.append("")
        lines.append("FAILURES:")
        for f in s["failures"][:show]:
            if f["diff"] is not None:
                lines.append(f"  p{f['page']} {f['label'][:44]:<46} "
                             f"printed {f['expected']:>14,.2f}  "
                             f"summed {f['got']:>14,.2f}  diff {f['diff']:>10,.2f}")
            else:
                lines.append(f"  p{f['page']} {f['label'][:44]:<46} "
                             f"{f['check']}: {f['got']}  ({f['note']})")
        if len(s["failures"]) > show:
            lines.append(f"  ... and {len(s['failures']) - show} more")
    return "\n".join(lines)
