#!/usr/bin/env python3
"""Find static assets with no repository-wide filename reference."""
from __future__ import annotations
import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    skip_dirs = {".git", ".venv", "models", ".pytest_cache", ".ruff_cache"}
    source_ext = {".py", ".js", ".css", ".html", ".yml", ".yaml", ".md", ".sh", ".json"}
    files = [p for p in root.rglob("*") if p.is_file() and not any(part in skip_dirs for part in p.parts) and p.suffix in source_ext]
    corpus = "\n".join(p.read_text(errors="ignore") for p in files)
    orphaned = [asset.name for asset in sorted(list((root / "app/static").glob("v*.css")) + list((root / "app/static").glob("v*.js"))) if corpus.count(asset.name) <= 1]
    if orphaned:
        print("Potentially orphaned assets:")
        print("\n".join(orphaned))
    else:
        print("No filename-level orphaned static assets found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
