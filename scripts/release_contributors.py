# -*- coding: utf-8 -*-
"""发布说明的贡献者段落生成器（"差异优先、累计垫后"的混合策略）。

背景
----
预发布阶段与正式发布阶段的诉求不同：

- **预发布（alpha/beta/rc）**：读者关心"这一轮新增了谁"。同时，整个版本周期里
  已经贡献过的人也不该被遗忘 —— 于是同时列出"自上一个预发布以来的新贡献者"
  与"自上一个稳定版以来的全部贡献者"。
- **正式版**：差异概念失去意义，只保留"自上一个稳定版以来的全部贡献者"。

Release Notes 只负责"这次谁参与了"；想永久铭记全部历史贡献者，交给仓库根部的
`CONTRIBUTORS.md`。

用法
----
::

    # 自动推断上一个预发布/上一个稳定版，输出到 stdout（CI 里用）
    GITHUB_TOKEN=xxx python scripts/release_contributors.py --repo owner/name --tag 1.1.0-alpha.2

    # 把贡献者段落追加到一份已有的手写说明后面
    GITHUB_TOKEN=xxx python scripts/release_contributors.py \\
        --repo owner/name --tag 1.1.0-alpha.2 \\
        --base-body .github/release-notes/1.1.0-alpha.2.md --out release_body.md

    # 不联网，只渲染已知数据（测试/调试用）
    python scripts/release_contributors.py --tag 1.1.0-alpha.2 --offline \\
        --all-contributors alice,bob --new-contributors alice

设计取舍
--------
- 只用标准库（urllib + json），与仓库其它脚本一致，CI 无需装依赖。
- "新贡献者"的逐条文案由 GitHub 的 generate-notes 接口产出（它知道"首次贡献"
  发生在哪个 PR），再统一改写为 `- @user made their first contribution in #101`。
- bot 账号（`[bot]` 后缀、`dependabot` / `github-actions` / `renovate` 等）一律剔除。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"

# 视为机器人的账号（大小写不敏感的子串匹配；另有 "[bot]" 后缀规则）
_BOT_MARKERS = ("[bot]", "dependabot", "github-actions", "renovate", "copilot", "codecov")
# 不属于独立贡献者的提交作者名（按小写比较）。
# `xtc` 是维护者早年的本地 git 身份（与 @Dinganzhi 是同一人），已合并计入维护者，
# 不单独列出，否则同一个人会在名单里出现两次。
_ALIAS_AUTHORS = ("xtc",)

_PRERELEASE_MARKERS = ("alpha", "beta", "rc", "pre", "dev", "snapshot")

_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:[-.]?(?P<stage>alpha|beta|rc|pre|dev|snapshot)\.?(?P<num>\d+)?)?$",
    re.IGNORECASE,
)

# 预发布阶段的先后顺序（数值越大越晚），用于在同一 base 版本内挑选"上一个预发布"
_STAGE_ORDER = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3, "pre": 3, "snapshot": 0}


# ---------------------------------------------------------------- 版本号解析
def parse_version(tag: str) -> dict | None:
    """把 tag 解析成 {major, minor, patch, stage, num}；解析不了返回 None。"""
    m = _VERSION_RE.match((tag or "").strip())
    if not m:
        return None
    stage = (m.group("stage") or "").lower()
    return {
        "major": int(m.group("major")),
        "minor": int(m.group("minor")),
        "patch": int(m.group("patch") or 0),
        "stage": stage,
        "num": int(m.group("num") or 0),
    }


def is_prerelease(tag: str) -> bool:
    """是不是预发布版本（含 alpha / beta / rc / pre 等标记）。

    判不出来时按"预发布"处理：宁可多列一段"新贡献者"，也不要在正式版里
    错误地宣称"这是本轮新面孔"。
    """
    info = parse_version(tag)
    if info is None:
        # 解析不了（如 nightly、日期版本）时保守返回 True：
        # 宁可多列一段"新贡献者"，也不要在正式版里错误宣称"本轮新面孔"。
        return True
    return bool(info["stage"])


def _sort_key(tag: str):
    """排序键：正式版排在同号预发布之后；预发布内部按 dev < alpha < beta < rc 递增。"""
    info = parse_version(tag)
    if info is None:
        return (0, 0, 0, 0, 0)
    # 正式版给一个比任何预发布阶段都大的序号，确保 1.1.0 排在 1.1.0-rc.1 之后。
    stage_rank = 9 if not info["stage"] else _STAGE_ORDER.get(info["stage"], 0)
    return (info["major"], info["minor"], info["patch"], stage_rank, info["num"])


def pick_previous_stable(tags, current: str) -> str:
    """`current` 之前**最近的一个正式版**（不含当前版本）。找不到返回空串。"""
    cur = _sort_key(current)
    candidates = [t for t in tags
                  if not is_prerelease(t) and _sort_key(t) < cur]
    if not candidates:
        return ""
    return max(candidates, key=_sort_key)


def pick_previous_prerelease(tags, current: str) -> str:
    """`current` 之前**最近的同版本线预发布**（如 1.1.0-alpha.2 的上一个是 alpha.1）。

    只在同一个 base（major.minor.patch）内找；找不到则退化为"上一个稳定版"
    之后、当前版本之前的任意预发布，再找不到返回空串。
    """
    info = parse_version(current)
    cur_key = _sort_key(current)
    candidates = [t for t in tags
                  if is_prerelease(t) and _sort_key(t) < cur_key]
    if not candidates:
        return ""
    if info is not None:
        same_base = [t for t in candidates
                     if (lambda i: i is not None
                         and (i["major"], i["minor"], i["patch"])
                         == (info["major"], info["minor"], info["patch"]))(parse_version(t))]
        if same_base:
            return max(same_base, key=_sort_key)
    return max(candidates, key=_sort_key)


# ------------------------------------------------------------------ 账号清洗
def is_bot(login: str) -> bool:
    """是否机器人账号。"""
    name = (login or "").strip().lower()
    if not name:
        return True
    return any(mark in name for mark in _BOT_MARKERS)


def clean_logins(logins) -> list[str]:
    """去重、去 bot、去空、保持首次出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in logins or ():
        name = (raw or "").strip().lstrip("@")
        if not name or is_bot(name) or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# ------------------------------------------------- 新贡献者文案（来自 generate-notes）
_NEW_CONTRIB_RE = re.compile(
    r"^[-*]?\s*@(?P<login>[\w.-]+(?:\[bot\])?)\s+made their first contribution"
    r"(?:\s+in\s+(?P<what>\S+))?",
    re.IGNORECASE,
)


def normalize_new_contributor_line(line: str) -> str:
    """把 generate-notes 的条目改写成 `- @user made their first contribution in #101`。

    PR 链接归一成 `#编号`：短、可点、与手写说明里的习惯一致。
    认不出来的行原样返回（去掉行首的 `* `），避免丢信息。
    """
    text = (line or "").strip()
    m = _NEW_CONTRIB_RE.match(text)
    if not m:
        return "- " + re.sub(r"^\*+\s*", "", text) if text else ""
    login = m.group("login")
    if is_bot(login):
        return ""
    what = (m.group("what") or "").strip().rstrip(".")
    if what:
        pr = re.search(r"/pull/(\d+)", what)
        if pr:
            what = "#" + pr.group(1)
        elif re.fullmatch(r"\d+", what):
            what = "#" + what
        return f"- @{login} made their first contribution in {what}"
    return f"- @{login} made their first contribution"


def parse_new_contributors(release_notes_body: str) -> list[str]:
    """从 generate-notes 的完整输出里抽出"New Contributors"段落的条目。"""
    lines = (release_notes_body or "").splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            inside = "New Contributors" in stripped
            continue
        if not inside or not stripped:
            continue
        if stripped.startswith("*") or stripped.startswith("-"):
            rendered = normalize_new_contributor_line(stripped)
            if rendered:
                out.append(rendered)
    return out


def strip_new_contributors_section(body: str) -> str:
    """删掉 GitHub 自动正文里的 `## New Contributors` 整段。

    为什么要删：我们自己会按"预发布 = 差异 + 累计 / 正式版 = 仅累计"重新生成贡献者段落，
    保留 GitHub 那段会出现两个"新贡献者"标题，且其中的 bot 账号没被剔除。
    只删这一个标题下的内容，其余段落（What's Changed / Full Changelog）原样保留。
    """
    lines = (body or "").splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            skipping = "New Contributors" in stripped
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    # 去掉尾部多余空行，避免拼接后出现三个以上连续空行
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


# ------------------------------------------------------------------ 渲染
def _heading(text: str) -> str:
    return f"## {text}"


def render_new_contributors(entries, previous_prerelease: str) -> str:
    """「🎉 New Contributors（自 X 以来）」段落。没有条目时返回空串（整段省略）。"""
    items = [e for e in (entries or []) if e]
    if not items:
        return ""
    since = f"（自 {previous_prerelease} 以来）" if previous_prerelease else ""
    return "\n".join([
        _heading(f"🎉 New Contributors{since}"),
        "",
        "感谢首次为本版本线做出贡献的开发者！",
        "",
        *items,
    ])


def render_all_contributors(logins, since_tag: str) -> str:
    """「🙌 All Contributors / Contributors（自 X 以来）」段落。"""
    names = clean_logins(logins)
    if not names:
        return ""
    since = f"（自 {since_tag} 以来）" if since_tag else ""
    return "\n".join([
        _heading(f"🙌 All Contributors{since}"),
        "",
        "感谢每一位参与本版本周期的贡献者！",
        "",
        *[f"- @{n}" for n in names],
    ])


def compose_body(base_body: str, sections, *, stable: bool) -> str:
    """把各段落拼到正文末尾。

    - 正文为空时，直接从段落开始（不会留下开头空行）。
    - 末尾统一补一个换行（GitHub 渲染更稳）。
    """
    parts: list[str] = []
    base = (base_body or "").rstrip()
    if base:
        parts.append(base)
    for section in sections:
        if section and section.strip():
            parts.append(section.strip())
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def build_sections(*, tag: str, all_contributors, new_contributor_entries,
                   previous_stable: str, previous_prerelease: str) -> list[str]:
    """按"预发布 = 差异 + 累计、正式版 = 仅累计"决定要输出哪些段落。"""
    sections: list[str] = []
    if is_prerelease(tag):
        new_section = render_new_contributors(new_contributor_entries, previous_prerelease)
        if new_section:
            sections.append(new_section)
    sections.append(render_all_contributors(all_contributors, previous_stable))
    return [s for s in sections if s]


# ------------------------------------------------------------------ GitHub API
class GitHubAPI:
    """最小化的 GitHub REST 客户端（只用标准库）。"""

    def __init__(self, repo: str, token: str = ""):
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN", "") \
            or os.environ.get("GH_TOKEN", "")

    def _request(self, method: str, path: str, payload=None):
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "xtc-qq-bridge-release-contributors",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}

    def list_tags(self, limit: int = 300) -> list[str]:
        """仓库的 tag 列表（GitHub 按提交日期倒序返回，这里取够用即可）。"""
        tags: list[str] = []
        page = 1
        while len(tags) < limit:
            batch = self._request("GET", f"/repos/{self.repo}/tags?per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            tags.extend(str(t.get("name") or "") for t in batch)
            if len(batch) < 100:
                break
            page += 1
        return [t for t in tags if t]

    def compare_authors(self, base: str, head: str) -> list[str]:
        """`base..head` 区间内所有提交作者（去重、去 bot）。"""
        if not base:
            return []
        logins: list[str] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/repos/{self.repo}/compare/{base}...{head}?per_page=100&page={page}")
            commits = data.get("commits") or []
            for commit in commits:
                author = commit.get("author") or {}
                login = author.get("login") or ""
                if login:
                    logins.append(login)
                    continue
                # 没有关联 GitHub 账号的提交：退回提交信息里的名字（可能是真人）
                name = ((commit.get("commit") or {}).get("author") or {}).get("name") or ""
                if name and name.strip().lower() not in _ALIAS_AUTHORS:
                    logins.append(name.strip())
            if len(commits) < 100:
                break
            page += 1
            if page > 30:          # 防御：单次比较最多 3000 个提交
                break
        return clean_logins(logins)

    def generate_notes(self, tag: str, previous_tag: str = "") -> str:
        """调用 generate-notes 接口，返回 GitHub 自动生成的说明正文。"""
        payload = {"tag_name": tag}
        if previous_tag:
            payload["previous_tag_name"] = previous_tag
        data = self._request(
            "POST", f"/repos/{self.repo}/releases/generate-notes", payload)
        return str(data.get("body") or "")


# ------------------------------------------------------------------ 主流程
def build_release_body(api, tag: str, base_body: str = "",
                       auto_base: bool = False) -> tuple[str, dict]:
    """生成最终正文，并返回 (正文, 诊断信息)。"""
    tags = api.list_tags()
    if tag not in tags:
        tags = tags + [tag]           # 允许对尚未出现在列表里的 tag 求值

    previous_stable = pick_previous_stable(tags, tag)
    previous_prerelease = pick_previous_prerelease(tags, tag)

    # 没有手写说明时，用 GitHub 自动生成的变更正文当底稿（再去掉它自带的贡献者段落）。
    # 底稿的比对范围取"上一个预发布"（预发布版）或"上一个稳定版"（正式版），
    # 这样 What's Changed 与本次发布的实际增量一致。
    if not base_body and auto_base:
        compare_base = previous_prerelease if is_prerelease(tag) else previous_stable
        generated = api.generate_notes(tag, compare_base)
        base_body = strip_new_contributors_section(generated)

    # "累计"范围：上一个稳定版之后到当前版本
    all_contributors = api.compare_authors(previous_stable, tag) if previous_stable else []

    # "差异"范围：上一个预发布之后到当前版本（仅预发布版需要）
    new_entries: list[str] = []
    if is_prerelease(tag) and previous_prerelease:
        notes = api.generate_notes(tag, previous_prerelease)
        new_entries = parse_new_contributors(notes)

    sections = build_sections(
        tag=tag,
        all_contributors=all_contributors,
        new_contributor_entries=new_entries,
        previous_stable=previous_stable,
        previous_prerelease=previous_prerelease)
    body = compose_body(base_body, sections, stable=not is_prerelease(tag))
    info = {
        "tag": tag,
        "prerelease": is_prerelease(tag),
        "previous_stable": previous_stable,
        "previous_prerelease": previous_prerelease,
        "all_contributors": clean_logins(all_contributors),
        "new_contributors": new_entries,
    }
    return body, info


def _read_text(path: str) -> str:
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="生成发布说明的贡献者段落（预发布=差异+累计，正式版=仅累计）")
    ap.add_argument("--tag", required=True, help="要发布的 tag（如 1.1.0-alpha.2）")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="owner/name（默认取 GITHUB_REPOSITORY）")
    ap.add_argument("--base-body", default="", help="已有的手写说明文件，贡献者段落会追加其后")
    ap.add_argument("--out", default="", help="输出文件；留空写到 stdout")
    ap.add_argument("--auto-base", action="store_true",
                    help="没有 --base-body 时，用 generate-notes 的变更正文当底稿"
                         "（会剔除其中的 New Contributors 段，避免与我们生成的重复）")
    ap.add_argument("--offline", action="store_true", help="不联网，仅用给定数据渲染（调试/测试）")
    ap.add_argument("--all-contributors", default="", help="离线模式：逗号分隔的累计贡献者")
    ap.add_argument("--new-contributors", default="", help="离线模式：逗号分隔的新贡献者")
    ap.add_argument("--previous-stable", default="", help="离线模式：指定上一个稳定版 tag")
    ap.add_argument("--previous-prerelease", default="", help="离线模式：指定上一个预发布 tag")
    args = ap.parse_args(argv)

    base_body = _read_text(args.base_body)

    if args.offline:
        all_names = [n for n in (args.all_contributors or "").split(",") if n.strip()]
        new_names = [n for n in (args.new_contributors or "").split(",") if n.strip()]
        new_entries = [f"- @{n.strip().lstrip('@')} made their first contribution"
                       for n in new_names]
        sections = build_sections(
            tag=args.tag,
            all_contributors=all_names,
            new_contributor_entries=new_entries,
            previous_stable=args.previous_stable,
            previous_prerelease=args.previous_prerelease)
        body = compose_body(base_body, sections, stable=not is_prerelease(args.tag))
    else:
        if not args.repo:
            print("[错误] 缺少 --repo（或设置 GITHUB_REPOSITORY）", file=sys.stderr)
            return 2
        api = GitHubAPI(args.repo)
        try:
            body, info = build_release_body(api, args.tag, base_body,
                                            auto_base=args.auto_base)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            print(f"[错误] GitHub API 返回 {e.code}: {detail}", file=sys.stderr)
            return 1
        except urllib.error.URLError as e:
            print(f"[错误] 无法访问 GitHub API: {e.reason}", file=sys.stderr)
            return 1
        if not info["all_contributors"] and not info["new_contributors"]:
            print("[警告] 未取到任何贡献者：请确认 GITHUB_TOKEN 具备 contents:read 权限，"
                  "且此前存在可比对的 tag", file=sys.stderr)

    # 诊断信息一律走 stderr，不污染 body 输出
    if not args.offline:
        print(f"[信息] tag={info['tag']} "
              f"{'预发布' if info['prerelease'] else '正式版'} | "
              f"上一个稳定版={info['previous_stable'] or '(无)'} | "
              f"上一个预发布={info['previous_prerelease'] or '(无)'} | "
              f"累计贡献者 {len(info['all_contributors'])} 人 | "
              f"新贡献者 {len(info['new_contributors'])} 人",
              file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"[完成] 已写入 {args.out}（{len(body.splitlines())} 行）", file=sys.stderr)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
