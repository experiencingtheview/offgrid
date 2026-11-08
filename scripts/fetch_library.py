#!/usr/bin/env python3
"""Download offline reference material for OffGrid.

Sources are resolved from live listings, so it picks up the current published
version instead of a stale hardcoded filename.

    python3 scripts/fetch_library.py --list
    python3 scripts/fetch_library.py --verify
    python3 scripts/fetch_library.py --get water medicine python
    python3 scripts/fetch_library.py --get survival coding
    python3 scripts/fetch_library.py --search "water filter"

Downloads go to ./offgrid-library by default (override with --target).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from offgrid.library import COLLECTIONS, Library


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and index OffGrid's offline library.")
    parser.add_argument("--target", default="offgrid-library", help="library folder")
    parser.add_argument("--list", action="store_true", help="show the catalogue")
    parser.add_argument("--verify", action="store_true", help="check every source is still published")
    parser.add_argument("--get", nargs="+", default=[], help="collection ids, or a category name")
    parser.add_argument("--all", action="store_true", help="download everything")
    parser.add_argument("--category", default="", help="download one category")
    parser.add_argument("--search", default="", help="search what is already downloaded")
    parser.add_argument("--index", action="store_true", help="rebuild index.json")
    args = parser.parse_args(argv)

    library = Library(root=Path(args.target).expanduser())

    if args.list or not any([args.verify, args.get, args.all, args.category, args.search, args.index]):
        print()
        print("  OffGrid offline library")
        print(f"  target: {library.root}")
        print()
        for entry in COLLECTIONS:
            got = "downloaded" if library.stats()["files"] and any(
                entry.prefix in str(item.get("path", "")) for item in library.downloaded()
            ) else ""
            marker = "*" if got else " "
            print(f" {marker} {entry.id:<14} {entry.category:<10} ~{entry.approx_gb:>4.1f} GB  {entry.title}")
            print(f"   {'':<14} {entry.description}")
            print(f"   {'':<14} licence: {entry.license}")
        print()
        print("  usage: --verify | --get <id...> | --get survival | --all | --search \"query\"")
        print()
        return 0

    if args.index:
        data = library.index()
        print(f"indexed {len(data['entries'])} file(s) -> {library.index_path()}")
        return 0

    if args.search:
        hits = library.search(args.search)
        if not hits:
            print("no matches. Is anything downloaded? try --list, then --get survival coding")
            return 1
        for hit in hits:
            print("  " + hit)
        return 0

    if args.verify:
        print("  checking sources... (no downloads)")
        failures = 0
        for entry in COLLECTIONS:
            ok, message = library.verify(entry)
            print(f"  {'ok ' if ok else 'FAIL'} {entry.id:<14} {message}")
            failures += 0 if ok else 1
        print()
        print(f"  {len(COLLECTIONS) - failures}/{len(COLLECTIONS)} collections reachable")
        return 0 if failures == 0 else 1

    wanted = []
    if args.all:
        wanted = list(COLLECTIONS)
    else:
        names = list(args.get)
        if args.category:
            names.append(args.category)
        for name in names:
            entry = library.entry(name)
            if entry:
                wanted.append(entry)
                continue
            category_hits = [item for item in COLLECTIONS if item.category == name]
            if category_hits:
                wanted.extend(category_hits)
            else:
                print(f"  unknown collection or category: {name}")

    if not wanted:
        print("nothing selected. try --list")
        return 2

    print()
    for entry in wanted:
        print(f"  fetching {entry.id} - {entry.title}")
        try:
            url, filename = library.resolve(entry)
        except Exception as exc:
            print(f"    skipped: {exc}")
            continue
        print(f"    {filename}")
        started = time.time()

        def progress(name, done, total, _entry=entry):
            if total:
                percent = done * 100 // total
                speed = done / max(time.time() - started, 0.5) / 1e6
                sys.stderr.write(f"\r    {percent:3d}%  {human(done)} / {human(total)}  {speed:.1f} MB/s")
                sys.stderr.flush()
            else:
                sys.stderr.write(f"\r    {human(done)}")
                sys.stderr.flush()

        try:
            path = library.download(entry, progress=progress)
            sys.stderr.write("\n")
            print(f"    saved {path} ({human(path.stat().st_size)})")
        except Exception as exc:
            sys.stderr.write("\n")
            print(f"    failed: {exc}")

    data = library.index()
    print()
    print(f"  indexed {len(data['entries'])} file(s) in {library.root}")
    print("  agents can now use library_search / library_read, even offline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
