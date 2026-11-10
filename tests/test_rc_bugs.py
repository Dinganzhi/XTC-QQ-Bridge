# -*- coding: utf-8 -*-
"""回归测试：rc/1.0.0 代码审查发现的 6 个缺陷。

每个用例都对应一条真实缺陷，修复前必然失败、修复后必须通过。
全部离线，不需要设备。

用法：python3 -B tests/test_rc_bugs.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_checks = 0
_fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if not ok:
        _fails.append(label + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- bug 1
def test_weekday_tian_equals_ri() -> None:
    """`星期天` 与 `星期日` 必须解析到同一天。

    修复前："一二三四五六日天".index("天") 得 7，+1 成 8，
    days_back 算到前一天，"星期天"被当成周一。
    """
    from xtc_qq_bridge.utils.timelabel import abs_label

    now = datetime(2026, 11, 10, 18, 0)      # 周二
    tian = abs_label("星期天 16:18", now=now)
    ri = abs_label("星期日 16:18", now=now)
    check("星期天 == 星期日", tian == ri, f"{tian} vs {ri}")
    check("星期天 落在 11-08", "11-08" in tian, tian)


def test_weekday_all_correct() -> None:
    """七个星期几都要落在正确的那天（以 2026-11-10 周二为基准）。"""
    from xtc_qq_bridge.utils.timelabel import abs_label

    now = datetime(2026, 11, 10, 18, 0)
    expect = {
        "星期一": "11-09", "星期二": "11-10", "星期三": "11-04",
        "星期四": "11-05", "星期五": "11-06", "星期六": "11-07",
        "星期日": "11-08", "星期天": "11-08",
    }
    for label, want in expect.items():
        got = abs_label(f"{label} 16:18", now=now)
        check(f"{label} -> {want}", want in got, got)


# ---------------------------------------------------------------- bug 2
def test_history_filter_private_and_group() -> None:
    """`私聊` / `群聊` 过滤要能命中真实写入的 source 格式。

    写入侧是 "QQ私聊 <号>" / "QQ群 <号>"（无方括号），
    修复前按 "[QQ私聊" 匹配，两个分支恒为空。
    """
    entries = [
        {"kind": "qq", "source": "QQ私聊 10001", "text": "a"},
        {"kind": "qq", "source": "QQ群 123456", "text": "b"},
        {"kind": "xtc", "source": "手表", "text": "c"},
    ]
    # _match_source 是 HistoryMixin 的方法；它只用 entries/source 两个入参，
    # 不碰 self 的状态，所以直接以 None 当 self 调用即可。
    from xtc_qq_bridge.bridge.history import HistoryMixin

    got_private = HistoryMixin._match_source(None, entries, "私聊")
    got_group = HistoryMixin._match_source(None, entries, "群聊")
    check("私聊命中 1 条", len(got_private) == 1, str(len(got_private)))
    check("私聊命中正确的那条", got_private and got_private[0]["text"] == "a")
    check("群聊命中 1 条", len(got_group) == 1, str(len(got_group)))
    check("群聊命中正确的那条", got_group and got_group[0]["text"] == "b")


def test_history_source_format_matches_writer() -> None:
    """写入侧的格式必须与过滤侧一致 —— 这条防止两边再次漂移。"""
    import inspect

    from xtc_qq_bridge.bridge import history, reverse

    rev = inspect.getsource(reverse)
    check("写入侧用 'QQ私聊 ' 前缀", 'f"QQ私聊 {' in rev, "找不到")
    check("写入侧用 'QQ群 ' 前缀", 'f"QQ群 {' in rev, "找不到")
    his = inspect.getsource(history)
    check("过滤侧不再匹配带方括号的形式", '"[QQ私聊"' not in his, "仍有旧写法")


# ---------------------------------------------------------------- bug 3
def test_cmd_retry_clears_seen_on_failure() -> None:
    """命令回复失败后，_cmd_seen_text 必须被复位，否则永远不会重试。"""
    import inspect

    from xtc_qq_bridge.bridge import core

    src = inspect.getsource(core)
    check("失败分支会 pop _cmd_seen_text",
          "_cmd_seen_text.pop(" in src, "找不到复位逻辑")
    check("复位在 _cmd_lock 保护下", "with self._cmd_lock:" in src)


# ---------------------------------------------------------------- bug 4
def test_deep_scan_uses_screens_limit() -> None:
    """深度扫描的滚动闸门必须用 screens_limit（被抬到 5），不是 _catchup_screens。"""
    import inspect

    from xtc_qq_bridge.bridge import core

    src = inspect.getsource(core)
    # 修复前这两行用的是 self._catchup_screens，导致 max(...,5) 是死代码
    bad = "if len(steps) >= self._catchup_screens:"
    check("滚动闸门不再用 _catchup_screens", bad not in src, "仍有死代码")
    check("滚动闸门用 screens_limit",
          "if len(steps) >= screens_limit:" in src, "找不到")
    check("媒体找标签的上限也用 screens_limit",
          "if len(steps) < screens_limit and self._scroll_probe(root):" in src)


# ---------------------------------------------------------------- bug 5
def test_webhook_checks_spawn_result() -> None:
    """动作类请求在并发名额满时必须回 503，不能丢弃后回 200。"""
    import inspect

    from xtc_qq_bridge import qq_webhook

    src = inspect.getsource(qq_webhook)
    # 四个动作（login/init/history/history_media）+ 转发，共 5 处 503
    n503 = src.count('_send(503, b"busy")')
    check("至少 5 处回 503（转发 + 4 个动作）", n503 >= 5, f"只有 {n503} 处")
    for action, task in (("login", "xtc-login"), ("init", "xtc-init"),
                         ("history", "xtc-history"),
                         ("history_media", "xtc-history-media")):
        # 每个动作的 _spawn 都要被 if not 包住
        check(f"{action} 检查了 _spawn 返回值",
              f"if not _spawn(" in src, "找不到检查")


# ---------------------------------------------------------------- bug 6
def test_backlog_cross_screen_dedup() -> None:
    """补发向上翻屏时，重叠区的同一条消息不能被收进 pending 两次。"""
    import inspect

    from xtc_qq_bridge.bridge import core

    src = inspect.getsource(core)
    check("补发里有跨屏去重集合", "seen_idents" in src, "找不到")
    check("重复的身份被跳过", "重叠区重复扫到" in src, "找不到跳过日志")
    check("去重发生在 append 之前",
          src.index("if ident_key in seen_idents:") < src.index("pending.append("),
          "顺序不对")


def test_scroll_step_is_partial_screen() -> None:
    """上翻只滚 0.7 屏 —— 这正是重叠区的来源，去重逻辑的前提。"""
    import inspect

    from xtc_qq_bridge.bridge import core

    src = inspect.getsource(core)
    check("滚动位移是 0.7 屏", "* 0.7)" in src, "找不到")


# ---------------------------------------------------------------- 结构
def test_all_fixes_present() -> None:
    """六处修复同时在位，防止只回滚了其中一两处。"""
    import inspect

    from xtc_qq_bridge import qq_webhook
    from xtc_qq_bridge.bridge import core, history
    from xtc_qq_bridge.utils import timelabel

    check("bug1 修在 timelabel", '"日" if m.group(1) == "天"' in inspect.getsource(timelabel))
    check("bug2 修在 history", "startswith(\"QQ私聊\")" in inspect.getsource(history))
    check("bug3 修在 core", "_cmd_seen_text.pop(" in inspect.getsource(core))
    check("bug4 修在 core", ">= screens_limit:" in inspect.getsource(core))
    check("bug5 修在 qq_webhook", inspect.getsource(qq_webhook).count('_send(503, b"busy")') >= 5)
    check("bug6 修在 core", "seen_idents" in inspect.getsource(core))


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                _fails.append(f"{name} 抛异常：{type(exc).__name__}: {exc}")

    if _fails:
        print(f"✗ rc 缺陷回归 {len(_fails)}/{_checks} 项失败：")
        for f in _fails:
            print(f"    - {f}")
        return 1
    print(f"✓ rc 缺陷回归 {_checks} 项检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
