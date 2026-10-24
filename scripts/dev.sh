#!/usr/bin/env bash
# =============================================================================
#  开发辅助：跑测试 + 跑一次自检（不启动桥接、不连设备也能跑完单元测试部分）
#
#  用法：
#    bash scripts/dev.sh              # 全部测试
#    bash scripts/dev.sh check        # 只做单元/路径测试（快）
# =============================================================================
set -u
cd "$(dirname "$0")/.." || exit 1

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python

run() {
  echo "[dev] $*"
  if ! "$PY" "$@"; then
    echo "[dev] 失败：$*" >&2
    exit 1
  fi
}

run tests/test_paths.py
run tests/test_integration.py
run tests/test_plugin_lifecycle.py
if [ "${1:-}" != "check" ]; then
  run tests/test_reported_bugs.py
fi
echo "[dev] 全部通过"
