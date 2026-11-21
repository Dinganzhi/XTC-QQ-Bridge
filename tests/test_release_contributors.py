# -*- coding: utf-8 -*-
"""离线测试：发布说明的贡献者段落生成（差异优先、累计垫后）。

覆盖三件事：
1. 预发布版 = "新贡献者（自上一个预发布）" + "全部贡献者（自上一个稳定版）"；
2. 正式版    = 只保留"全部贡献者"，不再出现"新贡献者"；
3. 版本号解析、上一个稳定版/上一个预发布的挑选、bot 剔除等纯逻辑。

全部断网运行（--offline 走的纯函数路径），不依赖 GitHub API。

用法：python tests/test_release_contributors.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import release_contributors as rc  # noqa: E402

RC = {"pass": 0, "fail": 0}


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        RC["pass"] += 1
        print(f"[PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        RC["fail"] += 1
        print(f"[FAIL] {name}" + (f"  {detail}" if detail else ""))


# ------------------------------------------------------------------ 版本号解析
def test_parse_version() -> None:
    print("\n---- 版本号解析 ----")
    cases = [
        ("1.0.0", (1, 0, 0, "", 0)),
        ("v1.1.0", (1, 1, 0, "", 0)),
        ("1.1.0-alpha.2", (1, 1, 0, "alpha", 2)),
        ("1.1.0-alpha.1", (1, 1, 0, "alpha", 1)),
        ("1.0.0-rc.2", (1, 0, 0, "rc", 2)),
        ("1.0.0-beta.3", (1, 0, 0, "beta", 3)),
    ]
    for tag, expect in cases:
        info = rc.parse_version(tag)
        got = (info["major"], info["minor"], info["patch"],
               info["stage"], info["num"]) if info else None
        check(f"解析 {tag}", got == expect, f"{got}")

    check("非法 tag 返回 None", rc.parse_version("not-a-version") is None)
    check("空串返回 None", rc.parse_version("") is None)


def test_is_prerelease() -> None:
    print("\n---- 预发布判定 ----")
    for tag in ("1.0.0-alpha.1", "1.0.0-beta.2", "1.0.0-rc.2", "v2.0.0-rc.1"):
        check(f"{tag} 是预发布", rc.is_prerelease(tag) is True)
    for tag in ("1.0.0", "v1.1.0", "2.3.4"):
        check(f"{tag} 是正式版", rc.is_prerelease(tag) is False)
    check("无法解析的 tag 保守判为预发布", rc.is_prerelease("nightly") is True)


def test_pick_previous() -> None:
    print("\n---- 上一个稳定版 / 上一个预发布 ----")
    tags = ["1.0.0", "1.1.0-alpha.1", "1.1.0-alpha.2",
            "1.1.0-beta.1", "1.2.0-alpha.1", "2.0.0-rc.1"]

    check("1.1.0-alpha.2 的上一个稳定版是 1.0.0",
          rc.pick_previous_stable(tags, "1.1.0-alpha.2") == "1.0.0")
    check("1.1.0-alpha.2 的上一个预发布是同版本线的 alpha.1",
          rc.pick_previous_prerelease(tags, "1.1.0-alpha.2") == "1.1.0-alpha.1")
    check("1.1.0-beta.1 的上一个预发布是同版本线最近的 alpha.2",
          rc.pick_previous_prerelease(tags, "1.1.0-beta.1") == "1.1.0-alpha.2",
          str(rc.pick_previous_prerelease(tags, "1.1.0-beta.1")))
    check("2.0.0-rc.1 找不到同版本线预发布时退化为最近的预发布",
          rc.pick_previous_prerelease(tags, "2.0.0-rc.1") == "1.2.0-alpha.1"
          or rc.pick_previous_prerelease(tags, "2.0.0-rc.1") == "1.1.0-beta.1",
          str(rc.pick_previous_prerelease(tags, "2.0.0-rc.1")))

    only_prerelease = ["1.1.0-alpha.1", "1.1.0-alpha.2"]
    check("没有稳定版时上一个稳定版为空",
          rc.pick_previous_stable(only_prerelease, "1.1.0-alpha.2") == "")

    check("首个版本没有可比较对象",
          rc.pick_previous_prerelease(["1.0.0"], "1.0.0") == "")


def test_sorting() -> None:
    print("\n---- 排序：正式版在同号预发布之后 ----")
    tags = ["1.1.0", "1.1.0-rc.1", "1.1.0-alpha.1", "1.1.0-beta.1", "1.1.1"]
    ordered = sorted(tags, key=rc._sort_key)
    check("alpha < beta < rc < 正式版",
          ordered == ["1.1.0-alpha.1", "1.1.0-beta.1", "1.1.0-rc.1",
                      "1.1.0", "1.1.1"], str(ordered))


# ------------------------------------------------------------------ 账号清洗
def test_bot_filter() -> None:
    print("\n---- bot 剔除 ----")
    for name in ("dependabot[bot]", "github-actions", "renovate[bot]",
                 "codecov", "github-actions[bot]"):
        check(f"{name} 判为 bot", rc.is_bot(name) is True)
    for name in ("alice", "bob", "ShameimaruAyaw", "carol"):
        check(f"{name} 不判为 bot", rc.is_bot(name) is False)

    check("clean_logins 去重、去 bot、保序",
          rc.clean_logins(["alice", "dependabot[bot]", "bob", "alice", "  "])
          == ["alice", "bob"])
    check("clean_logins 去掉前导 @",
          rc.clean_logins(["@alice", "@@bob"]) == ["alice", "bob"])


def test_new_contributor_lines() -> None:
    print("\n---- 新贡献者条目改写 ----")
    line = "* @alice made their first contribution in https://github.com/o/r/pull/101"
    check("PR 链接归一成 #编号",
          rc.normalize_new_contributor_line(line)
          == "- @alice made their first contribution in #101",
          rc.normalize_new_contributor_line(line))

    line2 = "- @bob made their first contribution in #105"
    check("已是 #编号时保持不变",
          rc.normalize_new_contributor_line(line2)
          == "- @bob made their first contribution in #105")

    check("bot 条目被丢弃",
          rc.normalize_new_contributor_line(
              "* @dependabot[bot] made their first contribution in "
              "https://github.com/o/r/pull/9") == "")


def test_parse_new_contributors() -> None:
    print("\n---- 从 generate-notes 输出中抽取 New Contributors ----")
    body = """## What's Changed
* fix: something by @alice in https://github.com/o/r/pull/100

## New Contributors
* @alice made their first contribution in https://github.com/o/r/pull/101
* @bob made their first contribution in https://github.com/o/r/pull/105
* @dependabot[bot] made their first contribution in https://github.com/o/r/pull/9

## Full Changelog
https://github.com/o/r/compare/v1.0.0...v1.1.0
"""
    entries = rc.parse_new_contributors(body)
    check("只取 New Contributors 段落", len(entries) == 2, str(entries))
    check("bot 已被剔除", all("dependabot" not in e for e in entries))
    check("第一条已归一", entries[0] == "- @alice made their first contribution in #101",
          entries[0])


def test_strip_new_contributors_section() -> None:
    print("\n---- 剔除自动正文里自带的 New Contributors 段 ----")
    body = """## What's Changed
* fix: something by @alice in https://github.com/o/r/pull/100

## New Contributors
* @alice made their first contribution in https://github.com/o/r/pull/101
* @dependabot[bot] made their first contribution in https://github.com/o/r/pull/9

## Full Changelog
https://github.com/o/r/compare/v1.0.0...v1.1.0
"""
    out = rc.strip_new_contributors_section(body)
    check("New Contributors 标题已删除", "New Contributors" not in out)
    check("其中的 bot 条目一并删除", "dependabot" not in out)
    check("What's Changed 保留", "What's Changed" in out)
    check("Full Changelog 保留", "Full Changelog" in out)
    check("结尾无多余空行", out == out.rstrip())

    check("没有该段时原样返回",
          rc.strip_new_contributors_section("## A\n* b").strip() == "## A\n* b".strip())
    check("空输入返回空", rc.strip_new_contributors_section("") == "")


# ------------------------------------------------------------------ 场景验证
def test_scenario_prerelease() -> None:
    print("\n---- 场景一：发布 1.1.0-alpha.2（预发布）----")
    sections = rc.build_sections(
        tag="1.1.0-alpha.2",
        all_contributors=["alice", "bob", "carol", "dave"],
        new_contributor_entries=[
            "- @alice made their first contribution in #101",
            "- @bob made their first contribution in #105",
        ],
        previous_stable="v1.0.0",
        previous_prerelease="1.1.0-alpha.1")
    body = rc.compose_body("", sections, stable=False)

    check("包含 New Contributors 段", "## 🎉 New Contributors" in body)
    check("New Contributors 注明范围（自 1.1.0-alpha.1）",
          "（自 1.1.0-alpha.1 以来）" in body)
    check("包含 All Contributors 段", "## 🙌 All Contributors" in body)
    check("All Contributors 注明范围（自 v1.0.0）",
          "（自 v1.0.0 以来）" in body)
    check("新贡献者在前、累计在后",
          body.index("New Contributors") < body.index("All Contributors"))
    check("alpha.1 的老贡献者仍在名单里",
          "- @carol" in body and "- @dave" in body)
    check("本次新贡献者也在累计名单里",
          body.count("- @alice") >= 2, f"alice 出现 {body.count('- @alice')} 次")
    check("新贡献者条目带 PR 编号", "#101" in body and "#105" in body)


def test_scenario_stable() -> None:
    print("\n---- 场景二：发布 1.1.0（正式版）----")
    sections = rc.build_sections(
        tag="1.1.0",
        all_contributors=["alice", "bob", "carol", "dave", "erin"],
        new_contributor_entries=["- @erin made their first contribution in #120"],
        previous_stable="v1.0.0",
        previous_prerelease="1.1.0-rc.1")
    body = rc.compose_body("", sections, stable=True)

    check("正式版不再出现 New Contributors 段", "New Contributors" not in body)
    check("正式版只有累计名单", "## 🙌 All Contributors" in body)
    check("整个版本周期的贡献者都在",
          all(f"- @{n}" in body for n in ("alice", "bob", "carol", "dave", "erin")))
    check("只有一段贡献者标题", body.count("## 🙌") == 1)


def test_compose_body() -> None:
    print("\n---- 正文拼接 ----")
    out = rc.compose_body("### 修复\n\n- 修了一个问题", ["## 贡献者\n\n- @alice"], stable=False)
    check("手写正文在前", out.startswith("### 修复"))
    check("贡献者段落在后", out.index("## 贡献者") > out.index("### 修复"))
    check("两段之间有空行", "\n\n## 贡献者" in out)
    check("结尾有换行", out.endswith("\n"))

    check("正文为空时不留开头空行",
          not rc.compose_body("", ["## 贡献者"], stable=False).startswith("\n"))
    check("全部为空时返回空串", rc.compose_body("", [], stable=False) == "")
    check("空段落被忽略",
          rc.compose_body("正文", ["", "  "], stable=False) == "正文\n")


def test_empty_contributors() -> None:
    print("\n---- 边界：没有贡献者 ----")
    sections = rc.build_sections(
        tag="1.1.0-alpha.2", all_contributors=[], new_contributor_entries=[],
        previous_stable="v1.0.0", previous_prerelease="1.1.0-alpha.1")
    check("无贡献者时不输出空标题", sections == [], str(sections))
    check("只有 bot 时也视为空",
          rc.render_all_contributors(["dependabot[bot]"], "v1.0.0") == "")


def test_offline_cli() -> None:
    print("\n---- 命令行（离线模式）----")
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rc.main(["--tag", "1.1.0-alpha.2", "--offline",
                        "--previous-prerelease", "1.1.0-alpha.1",
                        "--previous-stable", "v1.0.0",
                        "--new-contributors", "alice,bob",
                        "--all-contributors", "alice,bob,carol,dave"])
    out = buf.getvalue()
    check("离线模式返回 0", code == 0)
    check("离线模式输出两个段落",
          "New Contributors" in out and "All Contributors" in out)


def main() -> int:
    print("=" * 60)
    print("发布说明贡献者段落生成 —— 离线测试")
    print("=" * 60)
    test_parse_version()
    test_is_prerelease()
    test_pick_previous()
    test_sorting()
    test_bot_filter()
    test_new_contributor_lines()
    test_parse_new_contributors()
    test_strip_new_contributors_section()
    test_scenario_prerelease()
    test_scenario_stable()
    test_compose_body()
    test_empty_contributors()
    test_offline_cli()
    print()
    print(f"===== 发布说明测试：{RC['pass']} 通过 / {RC['fail']} 失败 =====")
    return 0 if RC["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
