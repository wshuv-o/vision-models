"""Read a lease-abstraction workbook: which tenants, and which columns to fill.

The template carries its own schema. Row 1 holds group headers that span
several columns ("Renewal Option 1", "Next Rent Escalation 1") and row 2 holds
the leaf names ("Term (Yr.)", "Rent PSF", "Notice (Mos.)"). Read on its own,
row 2 has four columns called "Rent PSF" and three called "Reference", so the
two rows are combined into names that mean something on their own.

Nothing here is specific to one property or tenant: the tenants, the columns
and the groups all come out of whatever workbook is handed in.
"""
import re

import pandas as pd

# Columns that identify the row rather than being extracted into it.
KEY_HINTS = ("property name", "suite", "tenant name")


def _clean(v):
    s = "" if v is None else str(v)
    s = s.replace("\n", " ").strip()
    return "" if s.lower() in ("nan", "none") else s


def read_template(path, sheet=0, group_row=1, header_row=2):
    """(columns, tenants, meta).

    columns  [{'idx', 'group', 'leaf', 'name'}]
    tenants  [{'row', <key column>: value, ...}]
    """
    df = pd.read_excel(path, sheet_name=sheet, header=None, dtype=str).fillna("")
    if df.empty:
        raise ValueError("empty sheet")

    groups = [_clean(v) for v in df.iloc[group_row].tolist()]
    leaves = [_clean(v) for v in df.iloc[header_row].tolist()]

    # A group header sits above the first of its columns; carry it rightwards
    # until the next one starts, but only while leaf names keep appearing.
    carried, cur = [], ""
    for i, g in enumerate(groups):
        if g:
            cur = g
        carried.append(cur if leaves[i] else "")

    columns = []
    for i, leaf in enumerate(leaves):
        if not leaf:
            continue
        grp = carried[i]
        # Only qualify when the leaf would otherwise be ambiguous or orphaned.
        dupes = [j for j, l2 in enumerate(leaves) if l2 == leaf]
        name = f"{grp} - {leaf}" if grp and (len(dupes) > 1 or grp not in leaf) else leaf
        columns.append({"idx": i, "group": grp, "leaf": leaf,
                        "name": re.sub(r"\s+", " ", name).strip()})

    # Some group headers repeat ("Lease" sits above both the escalation
    # reference and the termination reference), so two columns can still end up
    # with the same name. Identical names silently collapse into one another
    # when a row is built as a dict, losing a whole column, so qualify them
    # with the nearest preceding section that differs.
    seen = {}
    for c in columns:
        seen.setdefault(c["name"], []).append(c)
    for name, group in seen.items():
        if len(group) < 2:
            continue
        for c in group:
            section = ""
            for j in range(c["idx"] - 1, -1, -1):
                g = groups[j]
                if g and g != c["group"]:
                    section = g
                    break
            c["name"] = f"{section} - {name}" if section else f"{name} (col {c['idx']})"

    key_idx = {c["idx"]: c["name"] for c in columns
               if c["leaf"].lower().startswith(KEY_HINTS)}

    tenants = []
    for r in range(header_row + 1, len(df)):
        row = [_clean(v) for v in df.iloc[r].tolist()]
        if not any(row):
            continue
        rec = {"_row": r}
        for c in columns:
            v = row[c["idx"]] if c["idx"] < len(row) else ""
            if v:
                rec[c["name"]] = v
        # A row with nothing but a property name is a stray, not a tenant.
        if len(rec) > 2:
            tenants.append(rec)

    meta = {"key_columns": list(key_idx.values()),
            "n_columns": len(columns),
            "header_row": header_row}
    return columns, tenants, meta


def fillable_columns(columns, tenants):
    """Columns worth asking a model for: everything except the row keys."""
    keys = {c["name"] for c in columns
            if c["leaf"].lower().startswith(KEY_HINTS)}
    return [c for c in columns if c["name"] not in keys]


def describe_tenant(t):
    """A short label for logs and for matching documents to this row."""
    bits = [v for k, v in t.items()
            if k != "_row" and any(h in k.lower() for h in ("suite", "tenant", "dba"))]
    return " | ".join(bits) if bits else f"row {t['_row']}"
