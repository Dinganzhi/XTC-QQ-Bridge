# -*- coding: utf-8 -*-
"""登录相关：登录页表单输入、账号密码登录、登录页判定（原 xiaotiancai.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .constants import *  # noqa: F401,F403


class LoginMixin:
    """登录相关：登录页表单输入、账号密码登录、登录页判定"""

    def _field_verifier(self, field_node=None):
        """生成 input_text 的校验回调工厂：必须**按行精确匹配**目标文本。

        为什么不用"输入框非空即成功"：登录页有多个 EditText，把手机号写进密码框、
        或残留上一次的内容时，非空判定会误报成功，导致后续逻辑"找不到控件"反复重输
        （用户遇到的密码被输好几遍）。这里要求某一行的文本恰好等于目标值；
        **密码框显示为掩码**（••••/圆点）时按"已清空且长度一致"判断，
        否则掩码文本永远不等于明文密码 -> 会被误判为没输进去而重复输入。
        """
        masked = self._is_masked_field(field_node)

        def _verify_text(expected: str):
            want = (expected or "").strip()

            def _v() -> bool:
                try:
                    root = self.adb.dump_ui(retries=1, delay=0.0)
                except AdbError:
                    return True  # 读不到界面时不阻塞流程
                rows = self._input_row_texts(root)
                if not rows:
                    return True
                for r in rows:
                    cur = (r or "").strip()
                    if cur == want:
                        return True
                    if masked and cur and self._is_mask_text(cur) and len(cur) == len(want):
                        return True
                return False

            return _v

        return _verify_text

    @staticmethod
    def _is_masked_field(node) -> bool:
        if node is None:
            return False
        try:
            if (node.get("password", "") or "").lower() == "true":
                return True
            hint = (node.get("text", "") or "") + (node.get("content-desc", "") or "")
            return "密码" in hint
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_mask_text(text: str) -> bool:
        """掩码文本判定：全部由圆点/星号/实心点构成（允许尾部空格）。

        注意：下面这串字符是**功能字符**（App 可能用其中任意一种渲染密码掩码），
        不是装饰符号，清理"特殊符号"时不要删；它们都是 GBK 可编码的。
        """
        s = (text or "").strip()
        if not s:
            return False
        return all(ch in "•·*＊●○□■" for ch in s)

    def _input_row_texts(self, root: ET.Element) -> list[str]:
        """当前界面上所有输入类控件的文本（EditText 优先，其次聚焦节点）。"""
        rows: list[str] = []
        for n in root.iter("node"):
            cls = n.get("class", "") or ""
            if cls.endswith("EditText"):
                rows.append(n.get("text", "") or "")
        if rows:
            return rows
        for n in root.iter("node"):
            if (n.get("focusable", "") or "") == "true" and (n.get("text", "") or ""):
                rows.append(n.get("text", "") or "")
        return rows

    def fill_login_form(self, edits: list, values: list) -> tuple[bool, list[str]]:
        """按行填入登录表单，返回 (是否全部成功, 最终各行文本)。

        每个字段：点击聚焦 -> 清空（必要时删两行，避免残留）-> 注入 -> 按行校验。
        校验失败只"清空重填一次"，绝不重复追加，因此不会出现"密码被输好几遍"。
        密码框显示为掩码时按长度校验（见 _field_verifier）。
        """
        final: list[str] = []
        for edit, value in zip(edits, values):
            if not value:
                continue
            ok_field = False
            for attempt in (1, 2):
                self.adb.tap_element(edit)
                time.sleep(1.2)
                self.adb.clear_text_field()   # 尽量清空前后文本（含掩码字段的残留）
                time.sleep(0.4)
                ok_field = self.adb.input_text(
                    value, verify=self._field_verifier(edit)(value), retries=2)
                time.sleep(0.4)
                final = self._input_row_texts(self._current_root())
                if self._field_looks_ok(edit, value, final):
                    ok_field = True
                    break
                self.log("warning",
                         f"第 {attempt} 次输入未确认（当前输入框内容={final!r}），清空后重试")
            if not ok_field:
                return False, final
        return True, final

    def _field_looks_ok(self, edit, value: str, rows: list[str]) -> bool:
        """最终判定：明文逐行精确匹配；掩码字段按"非空且长度等于目标长度"。"""
        want = (value or "").strip()
        if any((r or "").strip() == want for r in rows):
            return True
        if self._is_masked_field(edit):
            for r in rows:
                cur = (r or "").strip()
                if cur and self._is_mask_text(cur) and len(cur) == len(want):
                    return True
                if cur == want:
                    return True
        return False

    def _current_root(self) -> ET.Element:
        try:
            return self.adb.dump_ui(retries=1, delay=0.0)
        except AdbError:
            return ET.Element("hierarchy")

    def login(self, phone: str, password: str) -> str:
        """账密登录小天才（手机号 + 密码，非验证码）。

        返回状态：
        - 'already' 已经登录，无需操作
        - 'ok'      登录成功
        - 'risk'    触发安全验证，需要用户手动在模拟器操作
        - 'fail'    确认的账号/密码错误等（登录失败）
        - 'timeout' 等待超时/网络类临时问题（**不能当成"密码错误"**，稍后应重试）
        - 'error'   流程异常（未找到控件/未配置账密）

        重要：登录中的界面（"登录中/正在验证/请稍候"）**绝不判失败**——历史版本因为把
        泛化的"失败/错误"当成密码错误，导致"明明在登录中却提示登录失败"，随后还被
        当成"已处理"而不再重试。
        """
        if not phone or not password:
            self.log("error", "账密登录需要 config.yaml -> xiaotiancai.login.phone / password")
            return "error"
        # 只有明确重新读界面后仍显示未登录才会走登录流程（避免缓存/偶发 dump 失败误判）
        if self.is_logged_in(force=True):
            return "already"
        try:
            # 0) 确保 App 在前台 + 处理首启隐私弹窗/权限等（已在前台则完全不重启 App）
            self.launch()
            self._dismiss_blockers()
            # 1) 进入登录页（欢迎页 -> 点"注册/登录"）
            root = self._dump_fast()
            if not self._is_login_page(root):
                entry = self._first(
                    self.adb.find_element(root, text="注册/登录", text_contains=True),
                    self.adb.find_element(root, content_desc="注册/登录"))
                if entry is None:
                    entry = self._first(
                        self.adb.find_element(root, text="登录", text_contains=True),
                        self.adb.find_element(root, content_desc="登录"))
                if entry is not None:
                    self.log("debug", f"点登录入口 {entry.get('bounds')}")
                    self.adb.tap_element(entry)
                    time.sleep(self._delay)
                    root = self._dump_fast()
            # 2) 切到"账号密码登录"（短信登录页底部多段链接的最左段）
            if not self._is_account_login_page(root):
                switch = self._find_account_login_entry(root)
                if switch is not None:
                    self.log("debug", f"点账密入口 {switch.get('bounds')}")
                    self._tap_entry_left(switch)
                    time.sleep(self._delay)
                    root = self._dump_fast()
                else:
                    self.log("debug", "未找到账密入口（可能已在账密页）")
            # 3) 找手机号 + 密码两个输入框（确认确实在账密页，避免把密码写进短信页）
            edits = self.adb.find_elements(root, class_name="EditText")
            edits.sort(key=lambda n: (self._bounds(n) or (0, 0, 0, 0))[1])
            if len(edits) < 2:
                self.log("error", "未找到账密输入框（页面结构变化？运行 python tools/dump_ui.py 查看登录页）")
                return "error"
            self.log("debug",
                     f"账密页就绪：前台={self.current_activity()} 输入框={len(edits)}")
            # 4) 输入手机号、密码（点击聚焦 -> 清空 -> 注入 -> **按行精确校验**）
            ok_fill, final_rows = self.fill_login_form(
                [edits[0], edits[1]], [phone, password])
            if not ok_fill:
                self.log("error",
                         f"登录表单输入未确认：输入框内容={final_rows!r}"
                         "（运行 python tools/dump_ui.py 查看登录页控件）")
                return "error"
            # 5) 勾选协议：只有**明确未勾选**（checked=false）才点，
            #    属性缺失时不乱点（避免把已勾选的又取消，实机上会被"请勾选协议"拦住）
            root = self._dump_fast()
            cb = self._first(
                self.adb.find_element(root, class_name="CheckBox"),
                self.adb.find_element(root, resource_id="com.xtc.watch:id/cb_protocol"))
            if cb is not None and (cb.get("checked", "") or "").lower() == "false":
                self.log("debug", "勾选用户协议")
                self.adb.tap_element(cb)
                time.sleep(0.4)
            # 6) 点登录（先收起键盘——输入密码后键盘仍打开，会挡住/截获点击）
            #    未离开账密登录页说明点击被吞，重试最多 3 次；
            #    若被"请勾选协议"拦住，则补勾一次再点。
            self.adb.keyevent(4)
            time.sleep(0.5)
            tapped = False
            for attempt in range(1, 4):
                root = self._dump_fast()
                if self._needs_protocol_agree(root):
                    cb2 = self._first(
                        self.adb.find_element(root, class_name="CheckBox"),
                        self.adb.find_element(root, resource_id="com.xtc.watch:id/cb_protocol"))
                    if cb2 is not None:
                        self.log("info", "登录提示需要同意协议，补勾一次后重试")
                        self.adb.tap_element(cb2)
                        time.sleep(0.5)
                        root = self._dump_fast()
                btn = self._find_login_button(root)
                if btn is None:
                    break  # 页面已跳转（登录中/验证页/成功）
                self.log("debug", f"点登录按钮（第 {attempt} 次）{btn.get('bounds')}")
                self.adb.tap_element(btn)
                tapped = True
                time.sleep(1.5)
                act = self.current_activity()
                if "loginactivity" not in act.lower():
                    break  # 已离开账密登录页，请求已发出
                self.log("debug", "点击后仍在登录页，重试")
            if not tapped:
                self.log("error", "未找到登录按钮（页面结构变化？运行 dump_ui 查看）")
                return "error"
            return self._await_login_result()
        except AdbError as e:
            self.log("error", f"登录流程异常: {e}")
            return "error"

    def _await_login_result(self) -> str:
        """等待登录结果。**区分**"登录中"、"确证的失败"、"网络类临时问题"与"超时"。

        - 出现"登录中/正在验证/请稍候"等进度文案 -> 不判失败，继续等；
        - 明确账号/密码错误 -> 'fail'；
        - 网络类提示 -> 继续等到超时，返回 'timeout'（稍后重试，而不是当成密码错）；
        - 一直无法确证 -> 'timeout'。
        """
        try:
            wait = max(15.0, float(self.ui.get("login_timeout", 45) or 45))
        except (TypeError, ValueError):
            wait = 45.0
        hard_deadline = time.time() + wait + 30
        deadline = time.time() + wait
        progress_logged = False
        soft = ""
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                if self.is_logged_in(force=True):
                    self.log("info", "小天才账密登录成功")
                    return "ok"
                root = self._dump_fast()
                risk = self._detect_risk(root)
                if risk:
                    self.log("warning", f"登录触发安全验证（{risk}），需要用户手动操作")
                    return "risk"
                if self._detect_login_progress(root):
                    if not progress_logged:
                        self.log("info", "登录进行中（界面提示登录中/验证中），继续等待结果")
                        progress_logged = True
                    deadline = min(hard_deadline, max(deadline, time.time() + 15))
                    continue
                soft = self._detect_soft_error(root) or soft
                err = self._detect_login_error(root)
                if err:
                    self.log("error", f"登录失败（明确错误）: {err}")
                    return "fail"
            except AdbError:
                continue
        if soft:
            self.log("warning", f"登录未在时限内完成（疑似临时问题：{soft}）——稍后会自动重试")
        else:
            self.log("warning", "登录结果超时未确定（界面既没有成功也没有明确错误）——稍后会自动重试")
        return "timeout"

    def _is_login_page(self, root: ET.Element) -> bool:
        """当前页面是否为登录页（短信/账密均可）。
        以输入框/获取验证码等登录页专属元素为准——欢迎页虽有"注册/登录"文案，但不是登录页。"""
        if self.adb.find_elements(root, class_name="EditText"):
            return True
        texts = "".join(n.get("text", "") for n in root.iter("node"))
        return "获取验证码" in texts or "短信验证码登录" in texts

    def _is_account_login_page(self, root: ET.Element) -> bool:
        """是否为账密登录页：至少两个 EditText（手机号+密码）。"""
        return len(self.adb.find_elements(root, class_name="EditText")) >= 2

    def _find_account_login_entry(self, root: ET.Element):
        """切到账密登录的入口（短信页底部文案 '账号密码登录' 等）。"""
        return self._first(
            self.adb.find_element(root, text="账号密码登录", text_contains=True),
            self.adb.find_element(root, content_desc="账号密码登录"))

    def _tap_entry_left(self, node) -> None:
        """多段链接（'账号密码登录｜注册｜忘记密码…'）点最左段；普通节点点中心。"""
        b = self._bounds(node)
        if b is None:
            self.adb.tap_element(node)
            return
        x1, y1, x2, y2 = b
        text = node.get("text", "")
        if "｜" in text or "|" in text:
            self.adb.tap(x1 + int((x2 - x1) * 0.2), (y1 + y2) // 2)
        else:
            self.adb.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def _find_login_button(self, root: ET.Element):
        """登录按钮：精确文本"登录" -> id 末段是 login/btn_login 的控件 -> content-desc。

        实机（WSA + 小天才）实测：登录按钮是**无文本的可点击 RelativeLayout**
        （`rl_login_login`，clickable=true）。旧实现要求 class 含 "Text"/"Button"，
        于是"表单填好了却说未找到登录按钮"。现在只要 id 命中且**可点击**就认。
        （仍不能用"id 含 login"——tv_login_area_title / tv_login_bottom 等标签也含 login。）
        """
        n = self.adb.find_element(root, text="登录")
        if n is not None:
            return n
        for node in root.iter("node"):
            tail = self._id_tail(node).lower()
            if not (tail.endswith("login") or "login_btn" in tail or "btn_login" in tail):
                continue
            cls = node.get("class", "")
            if ("Text" in cls or "Button" in cls
                    or (node.get("clickable", "") or "") == "true"):
                return node
        return self._first(
            self.adb.find_element(root, content_desc="登录"),
            None)

    @staticmethod
    def _needs_protocol_agree(root: ET.Element) -> bool:
        """是否提示"需要勾选同意协议"（实机上未勾选时点登录会这样拦）。"""
        joined = "".join((n.get("text") or "") for n in root.iter("node"))
        return ("协议" in joined) and any(w in joined for w in ("请勾选", "请先", "未勾选", "勾选同意"))

    def _detect_risk(self, root: ET.Element) -> str:
        """检测安全验证（账号风险）界面，返回命中的标记文案；无则返回 ''。
        注意：不要用 "验证码" 这类宽泛词——登录页底部固定有"短信验证码登录"文案会误判。"""
        markers = self.ui.get("risk_markers",
                              ["安全验证", "风险", "滑块", "拖动滑块", "图形验证",
                               "完成验证", "请完成验证", "滑动验证"])
        texts = [n.get("text", "") for n in root.iter("node")]
        joined = "".join(texts)
        for m in markers:
            if m in joined:
                return m
        return ""

    def _detect_login_progress(self, root: ET.Element) -> str:
        """检测"登录正在进行"的界面文案；命中返回文案，否则 ''。

        这类文案存在时**绝不能**判失败（用户报告的"登录中却提示登录失败"）。
        """
        markers = self.ui.get("login_progress_markers", list(_DEFAULT_LOGIN_PROGRESS))
        for n in root.iter("node"):
            blob = (n.get("text", "") or "") + (n.get("content-desc", "") or "")
            if not blob:
                continue
            for m in markers:
                if m and m in blob:
                    return m
        return ""

    def _detect_login_error(self, root: ET.Element) -> str:
        """检测**确证的**登录失败提示（账号/密码错误等）；不确定时返回 ''。

        规则（修复"登录中误报失败"）：
        1. 出现"登录中/正在验证/请稍候"等进度文案 -> 一律返回 ''（交给调用方继续等待）；
        2. 强标记（密码错误 / 账号不存在 / 次数过多 …）命中即返回；
        3. 配置里的弱标记（"失败"/"错误"/"不存在"这类泛化词）只有出现在**短文本节点**
           （toast/对话框文案，<=30 字）且**不含网络类词**时才采纳——
           这样"网络连接失败""加载失败，请重试"之类的临时问题不会被当成密码错误。
        """
        if self._detect_login_progress(root):
            return ""
        joined = "".join((n.get("text", "") or "") for n in root.iter("node"))
        joined += "".join((n.get("content-desc", "") or "") for n in root.iter("node"))
        for m in _STRONG_LOGIN_ERRORS:
            if m in joined:
                return m
        markers = self.ui.get("login_error_markers",
                              ["密码错误", "账号不存在", "手机号不存在", "错误", "失败", "次数过多"])
        for m in markers:
            if not m or m not in joined:
                continue
            if m in _STRONG_LOGIN_ERRORS:
                return m
            if self._weak_error_context(root, m):
                return m
        return ""

    def _weak_error_context(self, root: ET.Element, marker: str) -> bool:
        """泛化错误词（失败/错误/不存在）是否出现在可信的"短提示"节点里。"""
        for n in root.iter("node"):
            blob = ((n.get("text", "") or "") + (n.get("content-desc", "") or "")).strip()
            if not blob or marker not in blob or len(blob) > 30:
                continue
            if any(w in blob for w in _NET_WORDS) or any(w in blob for w in ("网络", "超时", "连接", "服务器")):
                continue  # 网络类 = 临时问题，不是密码错误
            cls = n.get("class", "") or ""
            if "Button" in cls or "EditText" in cls:
                continue  # 按钮/输入框上的文字不算错误提示
            return True
        return False

    def _detect_soft_error(self, root: ET.Element) -> str:
        """检测网络类/临时性提示（登录超时后用来说明原因，不作为失败依据）。"""
        for n in root.iter("node"):
            blob = ((n.get("text", "") or "") + (n.get("content-desc", "") or "")).strip()
            if not blob or len(blob) > 40:
                continue
            if any(w in blob for w in _NET_WORDS) or any(w in blob for w in ("网络", "超时", "服务器繁忙")):
                return blob
        return ""

