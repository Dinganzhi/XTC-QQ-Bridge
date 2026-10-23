#!/usr/bin/env bash
# =============================================================================
#  发布辅助：先跑测试，再（可选）编译产物
#
#  用法：
#    bash scripts/release.sh            # 只跑测试
#    bash scripts/release.sh build      # 跑测试 + Nuitka 编当前平台
# =============================================================================
set -u
cd "$(dirname "$0")/.." || exit 1

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python

bash scripts/dev.sh || exit 1

if [ "${1:-}" = "build" ]; then
  echo "[release] Nuitka 打包当前平台…"
  "$PY" packaging/nuitka/build.py || exit 1
  echo "[release] 产物在 dist/"
else
  echo "[release] 测试通过。要编译请执行：bash scripts/release.sh build"
fi
