# -*- coding: utf-8 -*-
"""开发辅助：清理运行产物 / 编译中间件（**不动** config.yaml、data/、logs/）。

用法：
    python scripts/clean.py            # 清理 __pycache__ / *.pyc / 临时文件
    python scripts/clean.py --build    # 额外清理 Nuitka/dist/build 产物
    python scripts/clean.py --dry-run  # 只打印将要删除的内容
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMP_PATTERNS = ("*.tmp", "*.pyc", "*.pyo", "*.log.*")
BUILD_DIRS = (".nuitka-cache", "dist", "build", "*.dist", "*.build", "*.onefile-build")


def main() -> int:
    ap = argparse.ArgumentParser(description="清理开发产物")
    ap.add_argument("--build", action="store_true", help="同时清理 Nuitka 编译产物")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不删除")
    args = ap.parse_args()

    targets: list[Path] = []
    for d in ROOT.rglob("__pycache__"):
        targets.append(d)
    for pat in TEMP_PATTERNS:
        targets.extend(ROOT.rglob(pat))
    if args.build:
        for pat in BUILD_DIRS:
            targets.extend(ROOT.glob(pat))

    seen: set[Path] = set()
    removed = 0
    for p in targets:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        if any(part in (".git", ".venv", "data", "logs") for part in p.parts):
            continue
        print(("would remove " if args.dry_run else "remove ") + str(p.relative_to(ROOT)))
        if not args.dry_run:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink()
                except OSError:
                    pass
        removed += 1
    print(f"共 {removed} 项" + ("（dry-run）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
