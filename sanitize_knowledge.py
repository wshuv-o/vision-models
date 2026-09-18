"""Replace client-identifying names in the knowledge pack with neutral ones.

The pack documents the method, and the method does not depend on whose
buildings were in the reports. Property and entity names carry no teaching
value, so they come out before the pack goes anywhere shared.

One of them is not a cosmetic rename: montrose_extract.py matches the literal
string "Total BSREP" to recognise the entity's grand-total row. Renaming the
comment would leave the client's name doing real work in the parser, so that
becomes a named constant the reader sets for their own report.

    python sanitize_knowledge.py <extracted_dir>
"""
import argparse
import io
import os
import re
import sys

# Longest first, so "Park Avenue Properties Associates LLC" is replaced before
# "Park Avenue" can match half of it.
REPLACEMENTS = [
    ("BSREP II Montrose Metro LLC", "Example Holdings LLC"),
    ("Park Avenue Properties Associates LLC", "Example Borrower LLC"),
    ("Main & Gervais", "Example Plaza"),
    ("Main and Gervais", "Example Plaza"),
    ("Park Avenue", "Example Borrower"),
    ("Montrose Metro", "Example Holdings"),
    ("City View", "Example Tower"),
    ("CityView", "ExampleTower"),
    ("Montrose", "receiver report"),
    ("BSREP", "Example Holdings"),
]

# Script files whose names identify a client. Applied BEFORE the prose
# replacements: "Montrose" -> "receiver report" would otherwise rewrite
# montrose_extract.py into "receiver report_extract.py" inside the docs.
RENAMES = {
    "montrose_extract.py": "receiver_report_rr.py",
    "park_ave_rr.py": "borrower_rr.py",
    "mg_rentroll.py": "property_perf_rr.py",
}

# The same files as import targets -- verify.py does `import park_ave_rr as P`,
# which a rename silently breaks unless the bare module name is handled too.
MODULE_RENAMES = {
    "montrose_extract": "receiver_report_rr",
    "park_ave_rr": "borrower_rr",
    "mg_rentroll": "property_perf_rr",
}

TEXT_EXT = {".py", ".md", ".ps1", ".txt", ".yaml", ".yml", ".json"}


def parameterise_totals_marker(text):
    """Turn the hard-coded entity grand-total prefix into a set constant."""
    if "'Total BSREP'" not in text and '"Total BSREP"' not in text:
        return text, False

    const = (
        "# The report prints its grand total as \"Total <entity name>\". Set this\n"
        "# to the entity exactly as it appears in your own report; it is matched\n"
        "# as a prefix, so a partial name is enough.\n"
        "ENTITY_TOTAL_PREFIX = 'Total Example Holdings LLC'\n"
    )
    # After the imports. Anchoring on a path constant instead landed the block
    # inside a multi-line `XLSX = (...)` assignment and broke the file -- the
    # line-anchored match only saw the first line of the continuation.
    lines = text.split("\n")
    last_import = 0
    for i, ln in enumerate(lines):
        if re.match(r"^(import|from)\s+\w", ln):
            last_import = i
    at = last_import + 1
    text = "\n".join(lines[:at] + ["", const.rstrip("\n")] + lines[at:])

    text = text.replace(
        "flat.startswith(('Totals:', 'Grand Total', 'Total BSREP'))",
        "flat.startswith(('Totals:', 'Grand Total', ENTITY_TOTAL_PREFIX))")
    return text, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    args = ap.parse_args()

    changed, renamed, param = [], [], []

    for dirpath, _dirs, files in os.walk(args.root):
        for fn in files:
            path = os.path.join(dirpath, fn)
            if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                continue
            raw = io.open(path, encoding="utf-8", errors="replace").read()
            orig = raw

            raw, did = parameterise_totals_marker(raw)
            if did:
                param.append(path)

            # File and module names first: a client word inside a filename
            # must be rewritten as the new filename, not as prose.
            for old, new in RENAMES.items():
                raw = raw.replace(old, new)
            for old, new in MODULE_RENAMES.items():
                raw = re.sub(rf"\b{re.escape(old)}\b", new, raw)

            for old, new in REPLACEMENTS:
                raw = re.sub(re.escape(old), new, raw, flags=re.I)

            if raw != orig:
                io.open(path, "w", encoding="utf-8", newline="\n").write(raw)
                changed.append(path)

    for dirpath, _dirs, files in os.walk(args.root):
        for fn in files:
            if fn in RENAMES:
                src = os.path.join(dirpath, fn)
                dst = os.path.join(dirpath, RENAMES[fn])
                os.replace(src, dst)
                renamed.append(f"{fn} -> {RENAMES[fn]}")

    print(f"rewrote {len(changed)} file(s)")
    for p in changed:
        print("   ", os.path.relpath(p, args.root))
    print(f"renamed {len(renamed)}: " + "; ".join(renamed))
    print(f"parameterised totals marker in {len(param)} file(s)")


if __name__ == "__main__":
    main()
