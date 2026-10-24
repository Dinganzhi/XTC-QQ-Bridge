# -*- coding: utf-8 -*-
"""登录与自动登录开关、初始化（原 bridge.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403


class LoginMixin:
    """登录与自动登录开关、初始化"""

    def login_xiaotiancai(self, request_id: str = "") -> str:
        """执行账密登录（入队，由工作线程串行执行，保证与其他发送任务的顺序）。
        有 request_id 时把结果回传给插件（原会话引用+@ 回复发送人）；否则走 _notify。"""
        if self._login_inflight:
            self._log("info", "[自动登录] 已有一次登录任务在执行/排队，跳过重复触发")
            if request_id:
                self._login_reply(request_id, "小天才登录已在进行中，请稍候")
            return "inflight"
        self._login_inflight = True
        self._job_queue.put(("login", request_id))
        return "queued"

    def _has_credentials(self) -> tuple[str, str]:
        acc = (self.cfg.get("xiaotiancai") or {}).get("login") or {}
        return str(acc.get("phone", "")).strip(), str(acc.get("password", "")).strip()

    def _do_login_job(self, request_id: str) -> None:
        phone, password = self._has_credentials()
        if not phone or not password:
            self._login_inflight = False
            msg = "未配置手机号/密码（config.yaml -> xiaotiancai.login.phone/password）"
            self._login_reply(request_id, "小天才登录：" + msg)
            return
        try:
            with self._op_lock:
                status = self.xtc.login(phone, password)
        except Exception as e:  # noqa: BLE001
            self._log("error", f"自动登录异常: {e}")
            self._login_reply(request_id, "小天才自动登录出错，请手动检查目标 Android 环境")
            self._login_not_before = time.monotonic() + self._login_retry_interval
            return
        finally:
            self._login_inflight = False
        now = time.monotonic()
        if status == "risk":
            self._pending_login_notify = True  # 等待用户手动完成安全验证
            self._login_not_before = now + self._login_retry_after_risk
            self._login_reply(request_id, "需要安全验证：请手动打开小天才 App 所在窗口完成验证")
            self._notify_once("risk", "小天才登录触发安全验证，请手动打开对应窗口完成验证")
        elif status == "fail":
            self._pending_login_notify = True
            self._login_not_before = now + self._login_retry_after_fail
            self._login_reply(request_id, "登录失败（账号或密码错误等），请检查配置或手动登录")
            self._notify_once("fail", "小天才自动登录失败（账号或密码错误等）")
        elif status == "timeout":
            # 超时/网络类临时问题：**不算失败**，按较短间隔自动重试
            self._login_not_before = now + self._login_retry_interval
            self._login_reply(request_id, "登录未在时限内完成（界面一直显示登录中或网络较慢），稍后自动重试")
            self._notify_once("timeout", "小天才自动登录暂未成功（登录中/网络较慢），稍后会自动重试")
        elif status == "error":
            self._login_not_before = now + self._login_retry_interval
            self._login_reply(request_id, "登录出错（控件未找到），请运行 tools/dump_ui.py 查看登录页")
        elif status == "ok":
            self._login_not_before = now + self._login_check_interval
            self._login_reply(request_id, "小天才登录成功")
        elif status == "already":
            self._login_not_before = now + self._login_check_interval
            self._login_reply(request_id, "小天才已登录，无需重复登录")

    def _login_reply(self, request_id: str, message: str) -> None:
        """登录结果回复：有 request_id 回传插件（引用+@）；否则 _notify 兜底。"""
        if request_id:
            try:
                ok = self.forwarder.reply_result(request_id, message)
                self._log("info" if ok else "error",
                          f"[登录结果] {message}（回传{'成功' if ok else '失败'}）")
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"登录结果回传异常: {e}")
        else:
            self._notify("小天才登录：" + message)

    def toggle_auto_login(self, request_id: str = "") -> str:
        """/小天才 自动登录：切换自动登录检测开关，结果回传插件。"""
        self._auto_login_enabled = not self._auto_login_enabled
        state = "已开启（每 10 分钟检测，未登录自动登录）" if self._auto_login_enabled else "已关闭"
        msg = f"自动登录检测{state}"
        if request_id:
            try:
                self.forwarder.reply_result(request_id, msg)
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"自动登录开关回传异常: {e}")
        else:
            self._notify("小天才" + msg)
        self._log("info", f"[自动登录] {msg}")
        return msg

    def init_xiaotiancai(self, request_id: str = "") -> str:
        """/小天才 初始化：入队执行界面状态检测与恢复。"""
        self._job_queue.put(("init", request_id))
        return "queued"

    def _do_init_job(self, request_id: str) -> None:
        """检测并恢复界面状态（**按需执行**，不做无意义的重启/点击）：
        清理弹窗 -> 只在前台不对时启动 -> 只在未登录时登录 -> 只在不在聊天页时进入
        -> 只在输入框有残留时清空。"""
        msgs: list[str] = []
        try:
            acc = (self.cfg.get("xiaotiancai") or {}).get("login") or {}
            phone = str(acc.get("phone", "")).strip()
            password = str(acc.get("password", "")).strip()
            contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
            with self._op_lock:
                # 1) 弹窗清理（一次多轮）
                if self.xtc.settle():
                    msgs.append("已清理弹窗")
                # 2) App 前台：已经在前台就完全不启动（避免"已经启动还反复启动"）
                if self.xtc.adb.is_in_foreground(self.xtc.package):
                    msgs.append("App 已在前台")
                else:
                    launched = False
                    for _ in range(2):
                        if self.xtc.launch():
                            launched = True
                            break
                        time.sleep(1.5)
                    msgs.append("启动" + ("OK" if launched else "失败"))
                self.xtc.settle()
                # 3) 登录态：明确已登录就跳过；无法判断时**不猜**，只报告
                state = self.xtc.login_state(force=True)
                if state == LOGIN_LOGGED_IN:
                    msgs.append("已登录")
                elif state == LOGIN_NOT_LOGGED_IN and phone and password:
                    status = self.xtc.login(phone, password)
                    msgs.append("登录：" + self._login_status_text(status))
                elif state == LOGIN_NOT_LOGGED_IN:
                    msgs.append("未登录（未配置账密，请手动登录）")
                else:
                    msgs.append("无法确认登录态（App 不在前台或界面读不到，未做登录动作）")
                # 4) 聊天页：已经在聊天页就不导航
                if self.xtc.is_in_chat():
                    msgs.append("已在聊天页")
                elif contact:
                    chat_ok = False
                    for _ in range(3):
                        if self.xtc.open_chat(contact):
                            chat_ok = True
                            break
                        self.xtc.settle()
                        time.sleep(1.0)
                    msgs.append("已进入聊天" if chat_ok else "未进入聊天")
                else:
                    msgs.append("未配置联系人")
                # 5) 文字模式/输入框：只有真的有残留才清空
                if self.xtc.is_in_chat():
                    msgs.append(self.xtc.ensure_input_clean())
                    # 6) 顺手学一次"发送按钮坐标"：先手打字要用它，学完**重启后的第一条**
                    #    QQ 消息也能 ~1 秒内把文字打进去（只注入一个探针字符随即清空，不发消息）
                    if self.xtc.learn_send_point():
                        msgs.append("已记录发送按钮坐标")
            reply = "初始化完成：" + "，".join(msgs)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"初始化异常: {e}")
            reply = "初始化失败：" + str(e)
        self._log("info", f"[初始化] {reply}")
        if request_id:
            try:
                self.forwarder.reply_result(request_id, reply)
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"初始化回传异常: {e}")
        else:
            self._notify(reply)

