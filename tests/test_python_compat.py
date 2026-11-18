# -*- coding: utf-8 -*-
"""Python 版本兼容性与入口约定检查。

本项目的 `pyproject.toml` 声明 `requires-python = ">=3.10"`，因此源码必须能被
Python 3.10 解析，且不得调用 3.11+ 才有的标准库接口。此外仓库约定：只有入口
`main.py` 保留 shebang，其余模块一律不带。

`ast.parse(feature_version=...)` 存在已知盲区：它不校验 PEP 701
（f-string 内嵌同种引号，3.12+ 才合法）。该情形由 `_find_pep701` 用 tokenize 单独检查。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.git', '__pycache__', 'build', 'dist', '.tmpvenv', 'node_modules'}
MIN_VERSION = (3, 10)
ENTRY_SHEBANG = '#!/usr/bin/env python3'

# 3.11+ 才提供的标准库接口；出现即说明实际最低版本高于声明值
FORBIDDEN_APIS = (
    ('import tomllib', 'tomllib（3.11+）'),
    ('from tomllib', 'tomllib（3.11+）'),
    ('asyncio.TaskGroup', 'asyncio.TaskGroup（3.11+）'),
    ('asyncio.timeout(', 'asyncio.timeout（3.11+）'),
    ('ExceptionGroup', 'ExceptionGroup（3.11+）'),
    ('except*', 'except*（3.11+）'),
    ('typing.Self', 'typing.Self（3.11+）'),
    ('LiteralString', 'LiteralString（3.11+）'),
    ('StrEnum', 'StrEnum（3.11+）'),
    ('datetime.UTC', 'datetime.UTC（3.11+）'),
    ('contextlib.chdir', 'contextlib.chdir（3.11+）'),
    ('hashlib.file_digest', 'hashlib.file_digest（3.11+）'),
    ('itertools.batched', 'itertools.batched（3.12+）'),
    ('typing.override', 'typing.override（3.12+）'),
    ('eager_task_factory', 'asyncio.eager_task_factory（3.12+）'),
    ('copy.replace', 'copy.replace（3.13+）'),
    ('TypeIs', 'typing.TypeIs（3.13+）'),
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    if ok:
        PASSED.append(name)
        print(f"[PASS] {name}  {detail}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name}  {detail}")


SELF_PATH = pathlib.Path(__file__).resolve()


def iter_py_files() -> list[pathlib.Path]:
    out = []
    for path in ROOT.rglob('*.py'):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.resolve() == SELF_PATH:
            continue          # 本文件自身列有禁用清单，不参与扫描
        out.append(path)
    return sorted(out)


def _find_pep701(source: str, filename: str) -> list[tuple[int, str]]:
    """返回 f-string 内嵌同种引号的位置（PEP 701，3.12+）。

    做法：跟踪 FSTRING_START 的引号种类，若在 f-string 内部遇到引号种类相同的
    STRING / FSTRING_START 记号，即判定为 3.12+ 语法。
    """
    hits: list[tuple[int, str]] = []
    stack: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return hits
    for tok in tokens:
        if tok.type == tokenize.FSTRING_START:
            text = tok.string
            quote = text.lstrip('rbfuRBFU')[:3]
            for candidate in (quote, quote[:1] * 3, quote[:1]):
                if candidate and text.endswith(candidate):
                    quote = candidate
                    break
            if stack and quote and quote[0] == stack[-1][0]:
                hits.append((tok.start[0], tok.line.strip()[:100]))
            stack.append(quote or '"')
        elif tok.type == tokenize.FSTRING_END:
            if stack:
                stack.pop()
        elif tok.type == tokenize.STRING and stack:
            text = tok.string.lstrip('rbfuRBFU')
            if text[:1] and text[:1] == stack[-1][:1]:
                hits.append((tok.start[0], tok.line.strip()[:100]))
    return hits


def main() -> int:
    files = iter_py_files()
    print(f"待检查文件：{len(files)} 个\n")

    # 1) shebang 约定：只有 main.py 带 shebang
    offenders = []
    for path in files:
        try:
            first = path.read_text('utf-8').splitlines()[0] if path.stat().st_size else ''
        except Exception:
            continue
        if first.startswith('#!') and 'python' in first.lower():
            if path.name != 'main.py':
                offenders.append(str(path.relative_to(ROOT)))
    check('仅 main.py 保留 Python shebang', not offenders,
          '违规：' + ', '.join(offenders) if offenders else f'{len(files)} 个文件')

    main_py = ROOT / 'main.py'
    if main_py.exists():
        first = main_py.read_text('utf-8').splitlines()[0]
        check('main.py 的 shebang 为 env python3', first == ENTRY_SHEBANG, first)

    # 2) 语法层：全部可用 3.10 解析
    syntax_bad = []
    for path in files:
        try:
            source = path.read_text('utf-8')
        except Exception:
            continue
        try:
            ast.parse(source, filename=str(path), feature_version=MIN_VERSION)
        except SyntaxError as exc:
            syntax_bad.append(f'{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}')
    check(f'全部文件可用 Python {MIN_VERSION[0]}.{MIN_VERSION[1]} 解析',
          not syntax_bad, '; '.join(syntax_bad[:3]))

    # 2.5) 检测器自检：先证明它确实能识别违规样本，避免"永远通过"
    positive = 'd = {"k": 1}\nprint(f"{d["k"]}")\n'
    negative = 'd = {"k": 1}\nprint(f"{d[\'k\']}")\n'
    check('PEP 701 检测器能识别违规样本', bool(_find_pep701(positive, '<sample>')))
    check('PEP 701 检测器不误报合法写法', not _find_pep701(negative, '<sample>'))

    # 3) PEP 701（feature_version 的盲区）
    pep701 = []
    for path in files:
        try:
            source = path.read_text('utf-8')
        except Exception:
            continue
        for lineno, line in _find_pep701(source, str(path)):
            pep701.append(f'{path.relative_to(ROOT)}:{lineno} {line}')
    check('未使用 PEP 701 的 f-string 嵌套同种引号', not pep701,
          '; '.join(pep701[:3]) if pep701 else '')

    # 4) 标准库接口层
    api_bad = []
    for path in files:
        try:
            source = path.read_text('utf-8')
        except Exception:
            continue
        for needle, desc in FORBIDDEN_APIS:
            if needle in source:
                api_bad.append(f'{path.relative_to(ROOT)} 使用 {desc}')
    check('未使用 3.11+ 专有标准库接口', not api_bad, '; '.join(api_bad[:3]))

    # 5) 运行时解释器不低于声明值（CI 若用低版本，上面的语法检查才有意义）
    check('解释器满足最低版本要求',
          sys.version_info >= MIN_VERSION, f'{sys.version.split()[0]}')

    print()
    print(f"===== 兼容性检查：{len(PASSED)} 通过 / {len(FAILED)} 失败 =====")
    return 0 if not FAILED else 1


if __name__ == '__main__':
    raise SystemExit(main())
