# -*- coding: utf-8 -*-
"""命令去重与 xtc 命令辅助（原 bridge.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403
from ..version import about_text


class CommandMixin:
    """命令去重与 xtc 命令辅助"""

    def _load_cmd_done(self) -> None:
        """读取已执行命令身份 (side, text, time_label)。"""
        try:
            data = json.loads(Path(self._cmd_done_file).read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._cmd_done = {tuple(x) for x in data if isinstance(x, list) and len(x) == 3}
        except Exception:  # noqa: BLE001 文件缺失/损坏不致命
            self._cmd_done = set()

    def _cmd_done_has(self, side: str, text: str, label: str) -> bool:
        with self._cmd_lock:
            return (side, text, label) in self._cmd_done

    def _cmd_done_add(self, side: str, text: str, label: str) -> None:
        with self._cmd_lock:
            self._cmd_done.add((side, text, label))
            if len(self._cmd_done) > 300:  # 修剪上限
                self._cmd_done = set(list(self._cmd_done)[-300:])
            try:
                Path(self._cmd_done_file).parent.mkdir(parents=True, exist_ok=True)
                tmp = self._cmd_done_file + ".tmp"
                Path(tmp).write_text(
                    json.dumps([list(x) for x in self._cmd_done], ensure_ascii=False),
                    encoding="utf-8")
                Path(tmp).replace(self._cmd_done_file)
            except Exception:  # noqa: BLE001 写盘失败不影响运行
                pass

    def _cmd_text_handled(self, side: str, text: str, time_label: str) -> bool:
        """这条命令（同侧同文本同标签）是否已经处理过——用于轮询时跳过重复触发。"""
        key = (side, (text or "").strip())
        label = (time_label or "").strip()
        with self._cmd_lock:
            if self._cmd_seen_text.get(key) == label:
                return True
            return (side, (text or "").strip(), label) in self._cmd_done

    def _maybe_xtc_cmd(self, side: str, text: str, time_label: str) -> None:
        """命令去重入队（手表侧/家长侧输入共用）。

        三层去重，彻底解决"同一条命令被反复执行/反复回复"：
        - pending：已入队未完成 -> 不再重复入队；
        - done（持久化）：身份相同（侧+文本+时间标签）-> 不再执行，重启也不重复；
        - **seen_text（会话级）**：同一侧的同一条命令文本，只要 App 时间标签没变，
          就不再处理（含"桥接自己发出去的回复/结果"被读回的情况）。
          标签变了说明是用户新输入的一条（哪怕文本一样），仍会执行。
        """
        label = (time_label or "").strip()
        key = (side, text)
        with self._cmd_lock:
            if text in self._cmd_pending:
                # 仍在队列里/正在执行：同一次输入不重复入队；
                # 若是用户新发的一条（标签变了），把标签更新为新值，
                # 这样执行成功后 done 记录的是最新身份，不会因旧标签被反复触发。
                if label and self._cmd_pending[text][1] != label:
                    self._cmd_pending[text] = (side, label)
                return
            if self._cmd_seen_text.get(key) == label:
                return
            if (side, text, label) in self._cmd_done:
                self._cmd_seen_text[key] = label
                return
            self._cmd_seen_text[key] = label
            if len(self._cmd_seen_text) > 200:
                recent = list(self._cmd_seen_text.items())[-200:]
                self._cmd_seen_text = dict(recent)
        self._cmd_pending[text] = (side, label)
        self._log("info", f"[xtc命令] 收到: {text}")
        self._job_queue.put(("cmd", text))

    def _do_cmd_job(self, text: str) -> bool:
        """执行 xtc 侧命令。返回是否成功回复（失败时调用方复位去重，允许下轮重试）。
        语法错误 / 无法识别的子命令 / 缺参数 -> 一律回复完整帮助列表。"""
        raw = (text or "").strip()
        if not self._xtc_cmd_prefix or not raw.startswith(self._xtc_cmd_prefix):
            return True  # 非命令消息不应到任务里（轮询已过滤）
        rest = raw[len(self._xtc_cmd_prefix):].strip()
        if not rest or rest in ("帮助", "help", "-h", "?"):
            return self._reply_into_xtc(self._xtc_usage())
        sub, _, args = rest.partition(" ")
        sub = sub.strip()
        args = args.strip()
        reply = None
        try:
            if sub in ("历史消息", "history"):
                reply = self._cmd_history(args)      # None = 已同步执行并写回聊天
            elif sub in ("搜索", "search"):
                reply = self._cmd_search(args)
            elif sub in ("在线人数", "online"):
                reply = self._cmd_online(args)
            elif sub in ("提醒", "remind"):
                reply = self._cmd_remind(args)
            elif sub in ("关于", "about", "版本", "version"):
                reply = about_text()
            else:
                reply = self._xtc_usage()            # 无法识别 -> 帮助列表
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"xtc 命令执行异常: {e}")
            reply = self._xtc_usage()
        if reply is None:
            return True
        return self._reply_into_xtc(reply)

    def _cmd_history(self, args: str):
        """参数：[条数] [来源]。返回 None=已执行（写回聊天）；str=帮助列表（参数错误）。"""
        n = 20
        source = ""
        parts = (args or "").split()
        if parts:
            if parts[0].isdigit():
                n = int(parts[0])
                if not 1 <= n <= 100:
                    return self._xtc_usage()
                parts = parts[1:]
            else:
                n = 20
            if parts:
                source = " ".join(parts).strip()
        # 已处于工作线程：直接同步执行历史读取任务（结果写入小天才聊天）
        self._do_history_job(n, "", into_chat=True, source=source)
        return None

    def _cmd_search(self, args: str) -> str:
        if not args:
            return self._xtc_usage()
        allow_from, allow_groups = self._qq_scope()
        try:
            resp = self.forwarder.qq_search(args, allow_from, allow_groups)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"QQ 搜索调用异常: {e}")
            resp = None
        if not resp:
            return "QQ 搜索失败：AstrBot 插件未连接（请确认 AstrBot 与插件已运行）"
        if not resp.get("ok"):
            return f"QQ 搜索失败：{resp.get('error') or '未知错误'}"
        people = resp.get("people") or []
        if not people:
            return f"未找到昵称含「{args}」的人（白名单私聊/群聊）"
        now = time.time()
        lines = ["搜索结果："]
        for p in people:
            name = p.get("name") or p.get("qq") or "?"
            qq = p.get("qq") or ""
            if p.get("scope") == "group":
                head = f"群聊 {p.get('session_name') or p.get('session_id')}(群{p.get('session_id')}) {name}(QQ{qq})"
            else:
                head = f"私聊 {name}(QQ{qq})"
            ts = p.get("last_ts")
            if ts:
                try:
                    lines.append(f"{head}：上次发送 {self._fmt_dt(ts)}，距现在 {self._fmt_ago(ts, now)}")
                except Exception:  # noqa: BLE001
                    lines.append(f"{head}：上次发送时间未知")
            else:
                lines.append(f"{head}：未发送过任何消息")
        limit = int((resp.get("limit") or 0) or 0)
        if limit and len(people) >= limit:
            lines.append(f"（结果过多，仅显示前 {limit} 条）")
        return "\n".join(lines)

    def _cmd_online(self, args: str) -> str:
        try:
            minutes = int(args or "")
        except ValueError:
            return self._xtc_usage()
        if not 1 <= minutes <= 60:
            return self._xtc_usage()
        allow_from, allow_groups = self._qq_scope()
        try:
            resp = self.forwarder.qq_online(minutes, allow_from, allow_groups)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"QQ 在线人数调用异常: {e}")
            resp = None
        if not resp:
            return "在线人数查询失败：AstrBot 插件未连接（请确认 AstrBot 与插件已运行）"
        if not resp.get("ok"):
            return f"在线人数查询失败：{resp.get('error') or '未知错误'}"
        total = int(resp.get("total") or 0)
        lines = [f"在线人数（最近 {minutes} 分钟，白名单QQ会话）：{total} 人"]
        for s in resp.get("sessions") or []:
            if s.get("kind") == "private":
                lines.append(f"私聊：{s.get('count')} 人")
            else:
                sid = s.get("session_id") or ""
                name = s.get("session_name") or sid
                lines.append(f"群聊 {name}({sid})：{s.get('count')} 人")
        return "\n".join(lines)

    def _cmd_remind(self, args: str) -> str:
        parts = args.split(None, 2) if args else []
        if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            return self._xtc_usage()  # 参数缺失/非数字 -> 帮助列表
        group_id, qq_id = parts[0], parts[1]
        content = parts[2] if len(parts) > 2 else ""
        allow_from, allow_groups = self._qq_scope()
        if group_id not in allow_groups:
            return f"群 {group_id} 不在白名单（webhook.allow_groups），无法提醒"
        try:
            resp = self.forwarder.qq_remind(group_id, qq_id, content)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"QQ 提醒调用异常: {e}")
            resp = None
        if not resp:
            return "提醒失败：AstrBot 插件未连接（请确认 AstrBot 与插件已运行）"
        if resp.get("ok"):
            return f"已提醒 QQ{qq_id}（群 {group_id}）" + (f"：{content}" if content else "")
        return f"提醒失败：{resp.get('error') or '未知错误'}"

    def _reply_into_xtc(self, text: str) -> bool:
        """把命令结果写进小天才聊天（家长侧消息，转发路径按"自己发的"忽略，不会回传 QQ）。
        实测聊天输入支持换行：多行文本（帮助列表/历史消息等）按原样作为一条消息发送，
        与 QQ 帮助信息的排版一致；超长截断。"""
        msg = (text or "").strip()
        if not msg:
            return False
        if len(msg) > 900:
            msg = msg[:900] + "（过长截断）"
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "回复小天才需要 config.yaml -> target.xtc_contact")
            return False
        with self._op_lock:
            if not self.xtc.is_in_chat():
                self.xtc.dismiss_blockers()
                if not self.xtc.open_chat(contact):
                    self._log("error", "命令结果写入失败：无法进入小天才聊天窗口")
                    return False
            self.echo.mark(msg)  # 本桥发出的消息，防止（理论上）被读回
            try:
                ok = self.xtc.send_message(msg)
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"命令结果写入异常: {e}")
                ok = False
        if ok:
            self._log("info", f"[xtc命令回复] {msg[:150]!r}...")
        else:
            self._log("error", "[xtc命令回复] 发送失败（请检查小天才聊天窗口状态）")
        return ok

    def _qq_scope(self) -> tuple[list[str], list[str]]:
        """白名单范围：webhook.allow_from（私聊）+ allow_groups（群聊）。"""
        wh = self.cfg.get("webhook") or {}
        allow_from = [str(x) for x in (wh.get("allow_from") or [])]
        allow_groups = [str(x) for x in (wh.get("allow_groups") or [])]
        return allow_from, allow_groups

    @staticmethod
    def _fmt_dt(ts) -> str:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")

    @staticmethod
    def _fmt_ago(ts, now: float | None = None) -> str:
        now = time.time() if now is None else now
        sec = max(0, now - float(ts))
        if sec < 60:
            return "刚刚"
        m = int(sec // 60)
        if m < 60:
            return f"{m} 分钟"
        h = int(m // 60)
        if h < 24:
            return f"{h} 小时"
        return f"{int(h // 24)} 天"

    @staticmethod
    def _login_status_text(status: str) -> str:
        return {
            "already": "已登录",
            "ok": "成功",
            "risk": "需安全验证（请手动完成）",
            "fail": "失败（账号或密码错误等）",
            "timeout": "超时/网络较慢（稍后自动重试）",
            "error": "出错（控件未找到）",
        }.get(status, status)

    def _login_check_loop(self) -> None:
        """检测登录态，未登录则自动账密登录（可被 /小天才 自动登录 关闭）。

        节奏（修复"自动登录不生效"）：
        - 启动后约 5s 立即检测一次；
        - 未登录且配置了账密 -> 立即触发登录，并按结果设置下次可尝试时间；
        - 登录成功 -> 按 login_check_interval（默认 600s）复查；
        - 登录超时/网络问题 -> login_retry_interval（默认 120s）后自动重试；
        - 明确的账号密码错误 -> login_retry_after_fail（默认 1800s）后重试；
        - 触发安全验证 -> login_retry_after_risk（默认 900s）后重试（等用户手动完成）。
        """
        time.sleep(5)  # 等 App 启动稳定，避免误判未登录
        while self.running:
            try:
                if not self._auto_login_enabled:
                    time.sleep(10)
                    continue
                now = time.monotonic()
                if self._login_inflight or now < self._login_not_before:
                    time.sleep(5)
                    continue
                if not self._op_lock.acquire(blocking=False):
                    time.sleep(5)
                    continue
                try:
                    state = self.xtc.login_state(force=True)
                finally:
                    self._op_lock.release()
                action = self._auto_login_decision(state)
                if action == "none":
                    if state == LOGIN_UNKNOWN:
                        # 无法判断（App 不在前台 / 息屏 / 界面无法读取）：**不去登录**，
                        # 只稍后再看。旧实现把这种情况当成"未登录"，于是已登录也报未登录。
                        self._log_once("login_unknown",
                                       "无法确认小天才登录态（App 不在前台或界面暂时无法读取），"
                                       "跳过本轮自动登录")
                        self._login_not_before = now + 60
                    else:
                        if self._pending_login_notify:
                            self._pending_login_notify = False
                            self._notify("小天才已重新登录，桥接继续运行")
                        self._login_not_before = now + self._login_check_interval
                        self._log("debug", "小天才登录态正常")
                    time.sleep(10)
                    continue
                if action == "no_cred":
                    if not self._warned_no_cred:
                        self._warned_no_cred = True
                        self._log("warning",
                                  "检测到小天才未登录，但没有配置账密"
                                  "（config.yaml -> xiaotiancai.login.phone / password）"
                                  "-> 自动登录不会生效，请手动登录或补齐配置")
                    self._login_not_before = now + self._login_check_interval
                    time.sleep(10)
                    continue
                self._log("info", "检测到小天才未登录，触发自动登录（手机号+密码）...")
                self.login_xiaotiancai()
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"自动登录检测异常: {e}")
            time.sleep(5)

    def _auto_login_decision(self, state: str) -> str:
        """根据登录态决定自动登录动作（纯函数，便于测试）：

        - 'logged_in'      -> none（什么都不做）
        - 'unknown'        -> none（**不能**当成未登录，否则会误触发登录流程）
        - 'not_logged_in'  -> login（已配置账密）/ no_cred（未配置账密，提示一次）
        """
        if state in (LOGIN_LOGGED_IN, LOGIN_UNKNOWN):
            return "none"
        phone, password = self._has_credentials()
        return "login" if (phone and password) else "no_cred"

    def _log_once(self, key: str, text: str, interval: float = 600.0) -> None:
        """同类日志按 key 节流（避免每 5 秒重复输出）。"""
        now = time.monotonic()
        if now - self._log_seen.get(key, float("-inf")) < interval:
            return
        self._log_seen[key] = now
        self._log("info", text)

    def _log_state(self, state: str) -> None:
        """状态变化时记一条 INFO；长时间停在非聊天状态则每 10 分钟提醒一次。

        目的：日志里能一眼看出"当时到底处于什么状态"，而不是满屏重复的失败告警
        （用户反馈"不能判断当前状态，还频繁提示找不到联系人"）。
        """
        text = self.xtc.STATE_TEXT.get(state, state)
        if state != self._last_state:
            self._last_state = state
            self._log("info", f"小天才状态: {text}")
            # 从现在起算 10 分钟，避免"状态没变"时立刻又提醒一次
            self._log_seen[f"stuck:{state}"] = time.monotonic()
            return
        if state not in (self.xtc.STATE_CHAT,):
            self._log_once(f"stuck:{state}", f"小天才状态持续为「{text}」", interval=600.0)

    def _notify_once(self, key: str, text: str, interval: float = 600.0) -> None:
        """同一类通知在 interval 秒内只发一次（避免重复输出）。"""
        now = time.monotonic()
        if now - self._notify_seen.get(key, float("-inf")) < interval:
            self._log("debug", f"[通知去重] {text}")
            return
        self._notify_seen[key] = now
        self._notify(text)

    def _notify(self, text: str) -> None:
        """发送登录相关通知到 QQ（target.notify_qq，缺省用 qq_private 第一个）。"""
        target = self._notify_target()
        if not target:
            self._log("info", f"[通知占位] {text}")
            return
        try:
            ok = self.forwarder.send("private", target, text)
            if ok:
                self._log("info", f"[通知] private:{target} <- {text}")
            else:
                self._log("error", f"[通知失败] private:{target} <- {text}")
        except Exception as e:  # noqa: BLE001
            self._log("error", f"通知发送异常: {e}")

    def _notify_target(self) -> str:
        t = self.cfg.get("target") or {}
        v = t.get("notify_qq")
        if v:
            return str(v[0]) if isinstance(v, (list, tuple)) else str(v)
        for mtype, tid in self._qq_targets():
            if mtype == "private":
                return tid
        return ""

    def _log(self, level: str, msg: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

