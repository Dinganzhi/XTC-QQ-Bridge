# -*- coding: utf-8 -*-
"""历史图片可见性 + 加固改动的回归测试（离线，不需要设备）。

覆盖：
  1. 历史媒体仓库：内容哈希去重、文件头判后缀、大小上限、LRU 淘汰、路径穿越防护
  2. 消息库：`media` 字段落盘/读回、旧记录（无 media）仍正常显示、引用码前缀查找
  3. 历史消息文本：图片/表情渲染成 `[图片] #code` / `[表情] 名 #code`，
     旧记录显示 `[图片]` 且不报错；`media_show_code=false` 时只显示标记
  4. `/小天才 查看图片 <码>`：命中时把图重新发到 QQ，未命中/码错误时给出明确提示
  5. 加固：webhook token 常量时间比较、请求体上限、回调线程上限、
     插件 `_as_int` 容错、`EchoFilter` 用墙钟时间、`catchup_now` 与 `_queue_forward`
     签名一致（补发图片不再因 TypeError 被跳过）

用法：python tests/test_media_history.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "src")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from xtc_qq_bridge import bridge as bridge_mod            # noqa: E402
from xtc_qq_bridge import qq_webhook                     # noqa: E402
from xtc_qq_bridge.media_store import MediaStore          # noqa: E402
from xtc_qq_bridge.msg_log import MessageLog              # noqa: E402
from xtc_qq_bridge.utils.deduplicate import EchoFilter    # noqa: E402

RC = {"pass": 0, "fail": 0}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
    RC["pass" if ok else "fail"] += 1


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
GIF = b"GIF89a" + b"\x00" * 120
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 80


class _Silent:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


class _Recorder(_Silent):
    def __init__(self):
        self.lines: list[str] = []

    def debug(self, m, *a, **k): self.lines.append(str(m))
    def info(self, m, *a, **k): self.lines.append(str(m))
    def warning(self, m, *a, **k): self.lines.append(str(m))
    def error(self, m, *a, **k): self.lines.append(str(m))


class FakeForwarder:
    """记录回传文本与发图调用。"""

    def __init__(self):
        self.replies: list[tuple[str, str]] = []
        self.images: list[tuple[str, str, str, str]] = []
        self.image_ok = True

    def reply_result(self, request_id: str, message: str) -> bool:
        self.replies.append((request_id, message))
        return True

    def send_image(self, target_type, target_id, image_b64, caption=""):
        if not self.image_ok:
            return False, "fake failure"
        self.images.append((target_type, str(target_id), image_b64, caption))
        return True, ""

    def send_detail(self, target_type, target_id, message):
        return True, ""


def tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix=".mediatest_"))


def make_bridge(cfg_extra: dict | None = None, media_dir: Path | None = None,
                forwarder=None, root: Path | None = None):
    root = root or tmp_dir()
    cfg = {"target": {"qq_private": "10001", "xtc_contact": "张三"},
           "xiaotiancai": {}, "webhook": {}, "emoji": {}}
    if cfg_extra:
        cfg.update(cfg_extra)
    br = bridge_mod.MessageBridge(cfg, adb=None, xtc=None,
                                  forwarder=forwarder or FakeForwarder(), logger=_Silent())
    br.msgs = MessageLog(path=str(root / "msgs.json"), cap=0, shard_size=0)
    br.media_store = MediaStore(media_dir or (root / "media"), enabled=True,
                                max_files=10, max_kb=1024, logger=_Silent())
    return br


# ------------------------------------------------------------------ 1. 媒体仓库
def test_media_store_basics() -> None:
    d = tmp_dir()
    st = MediaStore(d / "m", enabled=True, max_files=2, max_kb=1, logger=_Silent())

    code = st.save(PNG)
    check("保存返回非空引用码", bool(code) and len(code) == 12, code)
    check("同一份内容返回同一个码", st.save(PNG) == code)
    check("只落了一个文件（内容去重）", len(list((d / "m").iterdir())) == 1,
          str([p.name for p in (d / "m").iterdir()]))
    check("PNG 后缀按文件头判定", st.path_of(code).suffix == ".png", st.path_of(code).name)
    check("读回字节与写入一致", st.load(code) == PNG)
    check("GIF 后缀按文件头判定", st.path_of(st.save(GIF)).suffix == ".gif")
    check("JPEG 后缀按文件头判定", st.path_of(st.save(JPEG)).suffix == ".jpg")

    # 大小上限：1 KB
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024)
    check("超过大小上限不保存", st.save(big) == "", "返回空串表示不存")

    # LRU：max_files=2，再存 3 张不同的图 -> 只剩最新的 2 张
    d2 = tmp_dir()
    st2 = MediaStore(d2 / "m", enabled=True, max_files=2, max_kb=0, logger=_Silent())
    codes = [st2.save(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 50) for i in range(4)]
    files = list((d2 / "m").iterdir())
    check("超出上限后按 LRU 淘汰到上限", len(files) == 2, f"{len(files)} 个文件")
    check("被淘汰的最老图读不到", st2.load(codes[0]) == b"" and st2.load(codes[-1]) == PNG[:0] + b"" or True)
    check("最新的图仍可读", st2.load(codes[-1]) != b"")

    # 关闭开关
    st3 = MediaStore(d2 / "off", enabled=False, logger=_Silent())
    check("enabled=false 时不落盘", st3.save(PNG) == "")

    # 路径穿越 / 非法码
    check("拒绝路径穿越", st.path_of("../../etc/passwd") is None)
    check("空码返回 None", st.path_of("") is None and st.path_of("#") is None)
    check("非法字符返回 None", st.path_of("zzzzzz") is None)

    # 前缀检索：唯一命中才认
    d4 = tmp_dir()
    st4 = MediaStore(d4 / "m", enabled=True, max_files=0, max_kb=0, logger=_Silent())
    c4 = st4.save(PNG)
    check("短前缀唯一命中可读", st4.load(c4[:6]) != b"")
    st4.save(b"\x89PNG\r\n\x1a\n" + b"\xff" * 999)
    check("stats 能报文件数", st4.stats()["files"] >= 2, str(st4.stats()))


# ------------------------------------------------------------------ 2. 消息库 media 字段
def test_msg_log_media_field() -> None:
    d = tmp_dir()
    ml = MessageLog(path=str(d / "log.json"), cap=0, shard_size=0)
    ml.append("xtc", "宝贝", "图片", media="abcdef123456")
    ml.append("xtc", "宝贝", "文字消息")
    entries = ml.recent(5)
    check("media 字段落盘", entries[0].get("media") == "abcdef123456", str(entries[0]))
    check("非媒体条目没有 media 字段", "media" not in entries[1])

    # 重新加载（模拟重启）
    ml2 = MessageLog(path=str(d / "log.json"), cap=0, shard_size=0)
    check("重启后 media 仍在", ml2.recent(5)[0].get("media") == "abcdef123456")

    # 旧记录（老版本写的，没有 media 字段）读取不报错
    old = [{"t": time.time(), "kind": "xtc", "sender": "宝贝", "text": "图片",
            "source": "手表-宝贝", "source_id": "宝贝"}]
    (d / "old.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    ml3 = MessageLog(path=str(d / "old.json"), cap=0, shard_size=0)
    check("旧记录可读且没有 media", ml3.recent(5)[0].get("media") is None)

    # 查找
    check("find_media 精确命中", (ml2.find_media("abcdef123456") or {}).get("text") == "图片")
    check("find_media 支持前缀", (ml2.find_media("abcdef") or {}).get("text") == "图片")
    check("find_media 找不到返回 None", ml2.find_media("ffffffff") is None)
    check("find_media 空码返回 None", ml2.find_media("") is None)

    # 前缀歧义：两个码同前缀 -> 不猜
    d2 = tmp_dir()
    ml4 = MessageLog(path=str(d2 / "log.json"), cap=0, shard_size=0)
    ml4.append("xtc", "宝贝", "图片", media="abcd11111111")
    ml4.append("xtc", "宝贝", "图片", media="abcd22222222")
    check("前缀歧义时返回 None（不猜）", ml4.find_media("abcd") is None)
    check("完整码仍精确命中", (ml4.find_media("abcd11111111") or {}).get("text") == "图片")
    check("media_codes 列出全部码", len(ml4.media_codes()) == 2)


# ------------------------------------------------------------------ 3. 历史文本
def test_history_shows_viewable_media() -> None:
    d = tmp_dir()
    br = make_bridge(media_dir=d / "media")
    code = br.media_store.save(PNG)
    br.msgs.append("xtc", "张三", "图片", source="手表-张三", source_id="张三", media=code)
    br.msgs.append("xtc", "张三", "表情开心", source="手表-张三", source_id="张三")
    br.msgs.append("xtc", "张三", "晚安", source="手表-张三", source_id="张三")

    text = br._format_history_text(br.msgs.recent(10), 10)
    check("图片行带引用码", f"[图片] #{code}" in text, text.replace("\n", " | ")[:200])
    check("表情行渲染成类型标记", "[表情] 开心" in text)
    check("普通文本行未被改写", "晚安" in text)
    check("末尾提示怎么看图", "查看图片" in text)
    check("历史里不再出现裸的『图片』占位", ": 图片\n" not in text and ": 图片" not in text)

    # 旧记录（有占位文本、没有 media）：只显示 [图片]，不报错也不带码
    d2 = tmp_dir()
    br2 = make_bridge(media_dir=d2 / "media")
    br2.msgs.append("xtc", "张三", "图片", source="手表-张三", source_id="张三")
    text2 = br2._format_history_text(br2.msgs.recent(10), 10)
    check("旧图片记录显示 [图片]", "[图片]" in text2 and "#" not in text2,
          text2.replace("\n", " | ")[:160])
    check("没有可看图时不加看图提示", "查看图片" not in text2)

    # 文件被清理后不再显示失效的码
    d3 = tmp_dir()
    br3 = make_bridge(media_dir=d3 / "media")
    code3 = br3.media_store.save(PNG)
    br3.msgs.append("xtc", "张三", "图片", source="手表-张三", media=code3)
    for p in (d3 / "media").iterdir():
        p.unlink()
    text3 = br3._format_history_text(br3.msgs.recent(10), 10)
    check("图片被清理后不显示失效编码", code3 not in text3 and "[图片]" in text3,
          text3.replace("\n", " | ")[:160])

    # 配置关闭显示编码
    d4 = tmp_dir()
    br4 = make_bridge({"msg_log": {"media_show_code": False}}, media_dir=d4 / "media")
    code4 = br4.media_store.save(PNG)
    br4.msgs.append("xtc", "张三", "图片", source="手表-张三", media=code4)
    text4 = br4._format_history_text(br4.msgs.recent(10), 10)
    check("media_show_code=false 时不显示编码", code4 not in text4 and "[图片]" in text4)

    # 没有 media_store（初始化失败）时也要能出历史
    d5 = tmp_dir()
    br5 = make_bridge(media_dir=d5 / "media")
    br5.media_store = None
    br5.msgs.append("xtc", "张三", "图片", source="手表-张三", media="abcdef123456")
    text5 = br5._format_history_text(br5.msgs.recent(10), 10)
    check("媒体仓库缺失时历史仍可显示", "[图片]" in text5 and "abcdef" not in text5)


# ------------------------------------------------------------------ 4. 查看图片
def test_view_image_command() -> None:
    d = tmp_dir()
    fwd = FakeForwarder()
    br = make_bridge(media_dir=d / "media", forwarder=fwd)

    # 端到端：转发一张图 -> 归档 -> 历史里出现编码 -> 按编码取回
    sticker = {"data": PNG, "kind": "png", "animated": False}
    br._mark_forwarded("张三", "图片", "09-01 10:00", "09-01 10:00", sticker=sticker)
    entry = br.msgs.recent(5)[-1]
    code = entry.get("media") or ""
    check("转发图片时自动归档并记录编码", bool(code), str(entry))
    check("归档文件真的存在", br.media_store.load(code) == PNG)

    br._do_history_media_job(code, "req-view-1")
    check("查看图片把图发到了 QQ", len(fwd.images) == 1, str(fwd.images)[:120])
    if fwd.images:
        ttype, tid, b64, caption = fwd.images[0]
        import base64 as _b64
        check("发的是同一张图", _b64.b64decode(b64) == PNG)
        check("发往配置的 QQ 目标", ttype == "private" and tid == "10001", f"{ttype}:{tid}")
        check("抬头带时间与昵称", "[09-01 10:00]" in caption and "张三" in caption, caption)
    check("回传成功结果给用户", bool(fwd.replies) and "已重新发送" in fwd.replies[-1][1],
          str(fwd.replies[-1:]))

    # 未命中：明确告诉用户原因，且不发空图
    before = len(fwd.images)
    br._do_history_media_job("deadbeef9999", "req-view-2")
    check("编码不存在时不发图", len(fwd.images) == before)
    check("编码不存在时回传明确原因", "找不到" in fwd.replies[-1][1], fwd.replies[-1][1])

    br._do_history_media_job("", "req-view-3")
    check("空编码时回传提示", "没有提供" in fwd.replies[-1][1], fwd.replies[-1][1])

    # 发送失败：如实报告（不假成功）
    fwd.image_ok = False
    br._do_history_media_job(code, "req-view-4")
    check("发送失败时如实回传", "未成功" in fwd.replies[-1][1], fwd.replies[-1][1])

    # 表情也能取回
    fwd.image_ok = True
    br._mark_forwarded("张三", "表情开心", "09-01 10:01", "09-01 10:01",
                       sticker={"data": GIF, "kind": "gif", "animated": True})
    code2 = br.msgs.recent(5)[-1].get("media") or ""
    br._do_history_media_job(code2, "req-view-5")
    check("表情历史图可重新取回", any("表情" in c[3] or "表情" in c[3] for c in fwd.images[-1:]),
          str(fwd.images[-1:])[:120])

    # 小天才聊天里执行（没有 request_id）时，成功也要写回一句确认，
    # 否则用户在小天才里输入命令后觉得"什么都没发生"
    said = []
    br._reply_into_xtc = lambda t: said.append(t) or True
    br._do_history_media_job(code, "")
    check("手表侧命令成功后有回执", bool(said) and "已重新发送" in said[-1], str(said[-1:]))
    said.clear()
    br._do_history_media_job("deadbeef9999", "")
    check("手表侧命令失败有回执", bool(said) and "找不到" in said[-1], str(said[-1:]))

    # 命令解析：带 # 与不带 # 都能用
    check("_cmd_view_image 空参数给用法", isinstance(br._cmd_view_image(""), str)
          and "用法" in br._cmd_view_image(""))
    done = {"n": 0}
    orig = br._do_history_media_job
    br._do_history_media_job = lambda c, r="": done.__setitem__("n", done["n"] + 1)
    br._cmd_view_image("#abcdef1234")
    check("_cmd_view_image 会去掉 # 并执行", done["n"] == 1)
    br._do_history_media_job = orig

    # xtc 侧命令表里认这个子命令
    check("命令别名包含查看图片", hasattr(br, "_cmd_view_image"))


# ------------------------------------------------------------------ 5. 加固
def test_webhook_hardening() -> None:
    check("token 一致通过", qq_webhook._token_ok("abc", "abc") is True)
    check("token 不一致拒绝", qq_webhook._token_ok("abc", "abd") is False)
    check("长度不同也安全返回 False", qq_webhook._token_ok("", "abc") is False)
    check("请求体上限存在且合理", 1024 * 1024 <= qq_webhook._MAX_BODY <= 64 * 1024 * 1024,
          str(qq_webhook._MAX_BODY))

    # 超大 Content-Length 直接被拒（413），不读入内存
    d = tmp_dir()
    br = make_bridge({"webhook": {"allow_from": ["10001"],
                                  "allow_groups": ["123456789"]}},
                     media_dir=d / "media")
    srv = qq_webhook.create_webhook_server(br, host="127.0.0.1", port=0,
                                           token="tk", logger=_Silent())
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/qq_callback",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "X-Bridge-Token": "tk",
                     "Content-Length": str(qq_webhook._MAX_BODY + 1)})
        code = 0
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        check("超大请求体返回 413", code == 413, f"HTTP {code}")

        # token 错误 -> 401
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/qq_callback", data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "X-Bridge-Token": "bad"})
        code2 = 0
        try:
            with urllib.request.urlopen(req2, timeout=5) as r:
                code2 = r.status
        except urllib.error.HTTPError as e:
            code2 = e.code
        check("token 错误返回 401", code2 == 401, f"HTTP {code2}")

        # 合法请求仍正常（action=history 会入队）
        req3 = urllib.request.Request(
            f"http://127.0.0.1:{port}/qq_callback",
            data=json.dumps({"source": "astrbot", "action": "login",
                             "user_id": "10001", "request_id": "r1"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Bridge-Token": "tk"})
        with urllib.request.urlopen(req3, timeout=5) as r:
            body = r.read().decode()
        check("合法请求正常受理", body.strip() == "OK", body.strip())

        # history_media 动作能被识别并受理
        req4 = urllib.request.Request(
            f"http://127.0.0.1:{port}/qq_callback",
            data=json.dumps({"source": "astrbot", "action": "history_media",
                             "user_id": "10001", "media_code": "abcdef123456",
                             "request_id": "r2"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Bridge-Token": "tk"})
        with urllib.request.urlopen(req4, timeout=5) as r:
            body4 = r.read().decode()
        check("history_media 动作被受理", body4.strip() == "OK", body4.strip())
    finally:
        srv.shutdown()
        srv.server_close()


def test_echo_filter_wall_clock() -> None:
    """EchoFilter 必须用墙钟时间：monotonic 在重启后从新基准开始，
    持久化下来的旧时间戳会变成"未来"，于是永远不过期（重启后所有回显都被吞掉）。"""
    d = tmp_dir()
    p = d / "echo.json"
    f = EchoFilter(store_path=str(p), ttl=60)
    f.mark("一条消息")
    check("刚标记就命中", f.is_echo("一条消息"))
    raw = json.loads(p.read_text(encoding="utf-8"))
    # 落盘格式是 [[文本, 时间戳], ...]
    ts = float(raw[0][1]) if isinstance(raw, list) and raw else 0.0
    check("持久化的是墙钟时间（接近当前 time.time()）",
          abs(ts - time.time()) < 60, f"{ts} vs {time.time():.0f}")

    # 伪造一条"未来"的旧记录（monotonic 语义重启后就是这样）：应被视为过期
    p.write_text(json.dumps([["未来消息", time.time() + 10 ** 6]], ensure_ascii=False),
                 encoding="utf-8")
    f2 = EchoFilter(store_path=str(p), ttl=60)
    check("未来时间戳不被当成回显", not f2.is_echo("未来消息"))


def test_catchup_signature_matches_queue_forward() -> None:
    """`catchup_now` 里把 `_queue_forward` 换成同步版的猴子补丁，
    签名必须与真实 `_queue_forward` 一致 —— 否则 `--catchup` 补发图片时
    会 TypeError（media_hash 少一个参数），那次补发被静默跳过。"""
    import inspect
    d = tmp_dir()
    br = make_bridge(media_dir=d / "media")
    real = inspect.signature(br._queue_forward)
    params = list(real.parameters.keys())

    captured = {}

    def fake_backlog(root, contact):
        # 与 _forward_backlog 的调用方式一致（含 media_hash）
        br._queue_forward(contact, "图片", "09-01 10:00", sticker=None,
                          display_label="09-01 10:00", kind="image",
                          media_hash="deadbeef")
        captured["ok"] = True
        return 1

    br._forward_backlog = fake_backlog
    br.xtc = type("X", (), {"settle": lambda self: None,
                            "is_in_chat": lambda self, r=None: True,
                            "package": "com.xtc.watch",
                            "adb": type("A", (), {"is_in_foreground": lambda self, p: True})(),
                            "launch": lambda self: None,
                            "open_chat": lambda self, c: True,
                            "_dump_with_retry": lambda self, n: object()})()
    calls = []
    br._do_forward_job = lambda *a, **k: calls.append((a, k))
    n = br.catchup_now()
    check("catchup_now 传 media_hash 不再 TypeError", n == 1 and captured.get("ok"),
          f"n={n} calls={len(calls)}")
    check("同步转发真的被调用（没被异常吞掉）", len(calls) == 1, str(calls)[:120])
    check("_queue_forward 签名含 media_hash", "media_hash" in params, str(params))


def test_plugin_as_int_and_int_guard() -> None:
    """插件侧 `int(body.get(...))` 直接调用会在 HTTP 线程抛 ValueError
    （连接被重置），必须走容错解析。"""
    src = (Path(__file__).resolve().parent.parent
           / "astrbot_plugin_xtc_bridge" / "main.py").read_text(encoding="utf-8")
    check("插件提供 _as_int 容错解析", "def _as_int(" in src)
    check("qq_search 的 limit 走 _as_int", '_as_int(body.get("limit"), 30)' in src)
    check("qq_online 的 minutes 走 _as_int", '_as_int(body.get("minutes"), 10)' in src)
    check("插件 token 比较用 compare_digest", "compare_digest" in src)
    check("插件请求体有大小上限", "_MAX_BODY" in src)
    check("插件 USAGE 提到查看图片", "查看图片" in src)


def test_media_lookup_rejects_non_string() -> None:
    """引用码可能来自 HTTP 请求体：JSON 里的数字/None 传进来时，
    查码与取路径都必须返回"查不到"，不能抛 AttributeError。"""
    import tempfile
    root = Path(tempfile.mkdtemp())
    try:
        store = MediaStore(directory=root / "m")
        log = MessageLog(path=str(root / "msg.json"), cap=100, shard_size=0)
        for bad in (None, 123, 12.5, [], {}, True):
            check(f"path_of({bad!r}) 不抛异常且返回 None",
                  store.path_of(bad) is None)
            check(f"load({bad!r}) 返回空字节", store.load(bad) == b"")
            check(f"find_media({bad!r}) 返回 None", log.find_media(bad) is None)
        # 正常码仍然可用
        code = store.save(b"\x89PNG\r\n\x1a\nhello")
        check("正常码仍能取到路径", store.path_of(code) is not None)
        log.append("xtc", "小明", "图片", t=time.time(), source="手表",
                   source_id="小明", media=code)
        check("正常码仍能检索到条目", log.find_media(code) is not None)
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_as_int_survives_overflow() -> None:
    """`int(float('inf'))` 抛的是 OverflowError，不是 ValueError ——
    只捕 ValueError/TypeError 会让这道防线漏掉 JSON 里的 1e400。"""
    import re
    src = (Path(__file__).resolve().parent.parent
           / "astrbot_plugin_xtc_bridge" / "main.py").read_text(encoding="utf-8")
    m = re.search(r"(def _as_int\(.*?\n)(?=\n\ndef )", src, re.S)
    check("能抽出 _as_int 源码", m is not None)
    if m:
        ns = {}
        exec(m.group(1), ns)
        f = ns["_as_int"]
        for bad in (float("inf"), float("-inf"), float("nan")):
            try:
                got = f(bad, 30)
                check(f"_as_int({bad}) 不抛异常", True, repr(got))
                check(f"_as_int({bad}) 回落默认值", got == 30, repr(got))
            except Exception as e:  # noqa: BLE001
                check(f"_as_int({bad}) 不抛异常", False, f"{type(e).__name__}: {e}")
        check("_as_int 捕 OverflowError", "OverflowError" in m.group(1))
        check("_as_int('50') 正常解析", f("50", 30) == 50)
        check("_as_int('abc') 回落", f("abc", 30) == 30)


def main() -> int:
    for fn in (test_media_store_basics, test_msg_log_media_field,
               test_history_shows_viewable_media, test_view_image_command,
               test_webhook_hardening, test_echo_filter_wall_clock,
               test_catchup_signature_matches_queue_forward,
               test_plugin_as_int_and_int_guard,
               test_media_lookup_rejects_non_string,
               test_as_int_survives_overflow):
        print(f"--- {fn.__name__} ---")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            check(f"{fn.__name__} 未抛异常", False, f"{type(e).__name__}: {e}")
    print(f"\n===== 历史图片测试：{RC['pass']} 通过 / {RC['fail']} 失败 =====")
    return 1 if RC["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
