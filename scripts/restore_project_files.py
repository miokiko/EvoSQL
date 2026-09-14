"""Restore zero-byte project files when copied through a file viewer."""

import json
from pathlib import Path


def restore_empty_files(project_root: Path) -> list[str]:
    root = project_root.resolve()
    manifest = json.loads((root / "project-empty-files.json").read_text(encoding="utf-8"))
    restored = []
    for relative in manifest["empty_files"]:
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError("empty-file path must remain inside the project")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb"):
                pass
            restored.append(relative)
        except FileExistsError:
            # An existing WAL or index may contain new data; never truncate it.
            continue
    return restored


if __name__ == "__main__":
    restored = restore_empty_files(Path(__file__).resolve().parents[1])
    print("Restored %d empty project files." % len(restored))
