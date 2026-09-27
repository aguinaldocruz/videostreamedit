#!/usr/bin/env python3
"""Find static assets without runtime source references (candidates, not proof)."""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    source_ext = {".py", ".js", ".css", ".html", ".json"}
    # Documentation/test inventories and self-references cannot establish
    # runtime reachability. Dynamic construction still needs manual review.
    files = [p for p in (root / 'app').rglob('*') if p.is_file() and p.suffix in source_ext and 'vendor' not in p.parts]
    sources = {p: p.read_text(errors='ignore') for p in files}
    assets = sorted(p for p in (root / 'app/static').iterdir() if p.suffix in {'.css', '.js'})
    orphaned = [asset.name for asset in assets if not any(asset.name in text for path, text in sources.items() if path != asset)]
    if orphaned:
        print("Potentially orphaned assets:")
        print("\n".join(orphaned))
    else:
        print("No filename-level orphaned static assets found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
