"""CLI workflow for `offgrid images compress`."""

from __future__ import annotations

import json
from typing import Any, List

from .images import IMAGE_SUFFIXES, ImageError, compress_image
from .workspace import WorkspaceError


def run_images(args: Any, ui: Any, workspace: Any) -> int:
    action = (getattr(args, "action", "compress") or "compress").lower()
    raw_paths = list(getattr(args, "paths", []) or [])
    if action != "compress":
        # `images shots/` puts the first path in the optional verb slot.
        raw_paths.insert(0, action)
        action = "compress"
    if not raw_paths:
        ui.error("provide at least one image file or directory")
        return 2
    if getattr(args, "in_place", False) and getattr(args, "output", ""):
        ui.error("--in-place and --output cannot be used together")
        return 2

    paths: List[Any] = []
    for raw in raw_paths:
        try:
            candidate = workspace.resolve(raw)
        except WorkspaceError as exc:
            ui.error(str(exc))
            return 2
        if candidate.is_dir():
            paths.extend(
                sorted(
                    item
                    for item in candidate.rglob("*")
                    if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
                )
            )
        else:
            paths.append(candidate)
    unique = []
    seen = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    if not unique:
        ui.error("no PNG or JPEG images found")
        return 1

    output_dir = None
    if getattr(args, "output", ""):
        try:
            output_dir = workspace.resolve(args.output)
        except WorkspaceError as exc:
            ui.error(str(exc))
            return 2
        if output_dir.exists() and not output_dir.is_dir():
            ui.error(f"--output is not a directory: {output_dir}")
            return 2

    results = []
    errors = []
    for path in unique:
        destination = None
        if output_dir is not None:
            destination = output_dir / workspace.rel(path)
        try:
            results.append(
                compress_image(
                    path,
                    destination=destination,
                    in_place=bool(getattr(args, "in_place", False)),
                    strip_metadata=bool(getattr(args, "strip_metadata", False)),
                )
            )
        except (ImageError, FileExistsError, OSError) as exc:
            errors.append({"source": str(path), "status": "error", "error": str(exc)})

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "action": "compress",
                    "results": [item.as_dict() for item in results],
                    "errors": errors,
                    "saved_bytes": sum(item.saved_bytes for item in results),
                },
                indent=2,
            )
        )
    else:
        for item in results:
            print("  " + item.text())
        for item in errors:
            ui.error(f"{item['source']}: {item['error']}")
    return 1 if errors else 0
