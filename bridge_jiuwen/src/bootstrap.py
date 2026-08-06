from __future__ import annotations

import sys
from pathlib import Path


def bootstrap_repo_paths() -> list[str]:
    repo_root = Path(__file__).resolve().parents[2]
    extra_paths = [
        repo_root / "sdk" / "src",
        repo_root / "plugins" / "openclaw" / "src",
    ]
    inserted: list[str] = []
    for path in extra_paths:
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
            inserted.append(path_str)
    return inserted
