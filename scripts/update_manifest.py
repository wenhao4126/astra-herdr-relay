#!/usr/bin/env python3
"""按文件路径排序并实际计算发行包 SHA-256 清单。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.sha256"
EXCLUDED = {".git", "__pycache__", ".venv", "venv"}


def main():
    files = []
    for current, directories, names in os.walk(ROOT, topdown=True, followlinks=False):
        current_path = Path(current)
        kept = []
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(ROOT)
            if name in EXCLUDED:
                continue
            if path.is_symlink():
                raise SystemExit(f"拒绝将符号链接写入发行清单：{relative.as_posix()}")
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = current_path / name
            relative = path.relative_to(ROOT)
            if relative == Path("MANIFEST.sha256"):
                continue
            if path.is_symlink():
                raise SystemExit(f"拒绝将符号链接写入发行清单：{relative.as_posix()}")
            if path.is_file():
                files.append((relative.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    content = "".join(f"{digest}  {name}\n" for name, digest in sorted(files))
    MANIFEST.write_text(content, encoding="utf-8")
    print(f"已计算 {len(files)} 个文件的 SHA-256：{MANIFEST}")


if __name__ == "__main__":
    main()
