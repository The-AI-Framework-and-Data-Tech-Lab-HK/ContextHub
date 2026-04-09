from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from bridge import ContextHubJiuwenExtension  # noqa: E402


async def register_extensions(registry):
    extension = ContextHubJiuwenExtension()
    extension.set_extension_dir(ROOT)
    await extension.initialize(registry.config)
    return extension
