#!/usr/bin/env bash
# =============================================================================
#  Build machine-code single-file executables with Nuitka (Linux / macOS)
#
#  用法：
#    bash packaging/nuitka/build_linux.sh                     # onefile，产物在仓库根 dist/
#    bash packaging/nuitka/build_linux.sh --mode standalone   # 目录模式（启动更快）
#    bash packaging/nuitka/build_linux.sh --check-env         # 只检查 Python/pyyaml/Nuitka/gcc
#    bash packaging/nuitka/build_linux.sh --dry-run           # 只打印 nuitka 命令行
#
#  依赖（Debian/Ubuntu）：sudo apt install gcc g++ python3-dev patchelf
#  依赖（Fedora/RHEL）：sudo dnf install gcc gcc-c++ python3-devel patchelf
#  依赖（macOS）：xcode-select --install
#
#  关于 patchelf：Linux 下 standalone 与 onefile 都需要它。onefile 内部先做 standalone，
#  两者都要用它改写打包进来的共享库的 RPATH（本项目依赖 cryptography，其自带的
#  libssl/libcrypto 一定会走到这一步）。缺它时 Nuitka 会在编译到一半才抛 FATAL，
#  前面的编译时间全部作废，所以 build.py --check-env 已把它列为硬性检查项。
# =============================================================================
set -u

# 本脚本位于仓库的 packaging/nuitka/ 下，**上两级才是仓库根**：
# build.py、.venv、dist/ 以及下面所有相对路径都以仓库根为基准。
# 旧实现只 cd 到 packaging/nuitka/，再拼 "packaging/nuitka/build.py"，
# 实际会去找 packaging/nuitka/packaging/nuitka/build.py（必然失败）。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi
if [ -z "$PY" ]; then
    echo "[error] 找不到 python3（需要 3.10+）"
    exit 1
fi

echo "============================================"
echo "  Nuitka 编译（Linux / macOS）"
echo "============================================"
echo "[1/2] 检查编译环境 ..."
if ! "$PY" "$REPO_ROOT/packaging/nuitka/build.py" --check-env; then
    echo
    echo "[提示] 需要：python3 -m pip install -U nuitka pyyaml"
    echo "       Linux 还要装 gcc/g++/python3-dev 与 patchelf；macOS 需要 xcode-select --install"
    exit 1
fi

echo
echo "[2/2] 开始编译 ..."
"$PY" "$REPO_ROOT/packaging/nuitka/build.py" "$@"
rc=$?
echo
if [ "$rc" != "0" ]; then
    echo "[退出] 编译失败，返回码 $rc"
else
    echo "[退出] 编译完成，产物在仓库根 dist/（$REPO_ROOT/dist/）"
fi
exit "$rc"
