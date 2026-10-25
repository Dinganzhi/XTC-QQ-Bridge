# -*- coding: utf-8 -*-
"""状态判定、生命周期与界面判定：App 状态、界面 dump、启动与恢复（原 xiaotiancai.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .constants import *  # noqa: F401,F403


class StateMixin:
    """状态判定、生命周期与界面判定：App 状态、界面 dump、启动与恢复"""

    def app_state_with_root(self, attempts: int = 2) -> tuple:
        """读一次界面并判定状态，返回 (state, root)。

        state 取值见 STATE_* 常量。无法读取界面时 root 可能为 None。
        """
        root = self._dump_with_retry(attempts)
        return self.app_state(root), root

    @staticmethod
    def _tree_is_empty(root: ET.Element) -> bool:
        """界面快照是不是"空的"（UI 还没渲染好/dump 到了空树）。

        不能用"节点数 < 3"来判断：登录页有时只有两三个控件，会被误判成无法读取。
        真正的空树是所有节点都没有文本、描述、id、也不可点击。
        """
        for n in root.iter("node"):
            if (n.get("text") or "").strip() or (n.get("content-desc") or "").strip():
                return False
            if (n.get("resource-id") or "").strip():
                return False
            if str(n.get("clickable", "")).lower() == "true":
                return False
        return True

    def app_state(self, root: ET.Element | None = None) -> str:
        """判定"App 现在到底处于什么状态"，供桥接做状态机与日志。

        为什么需要它：以前轮询只问"在不在聊天页"，不在就去 open_chat，
        于是在登录页/别的页面上反复找联系人，报一堆"找不到联系人"，
        既没有判断状态也没法对症处理。

        顺序：先看前台（不在前台时界面根本不是小天才的）-> 再看能不能读到界面
        -> 登录/安全验证页 -> 聊天页 -> 消息列表 -> 其它页面。
        """
        try:
            if not self.adb.is_in_foreground(self.package):
                return self.STATE_BACKGROUND
        except AdbError:
            return self.STATE_BLIND
        if root is None:
            root = self._dump_with_retry(1)
        if root is None or self._tree_is_empty(root):
            return self.STATE_BLIND
        try:
            # 弹窗优先：弹窗盖在上面时，dump 出来的往往只有弹窗本身（下面的聊天页
            # 无法读取），若先判聊天页会得出错误结论，所以先认弹窗、把它关掉再说。
            if self._looks_like_popup(root, self.current_activity()):
                return self.STATE_POPUP
            if self.looks_like_chat_page(root):
                return self.STATE_CHAT
            if self._looks_like_login_page(root):
                return self.STATE_LOGIN
            if self.looks_like_message_list(root):
                return self.STATE_LIST
        except Exception:  # noqa: BLE001 判定异常按"无法读取"处理
            return self.STATE_BLIND
        return self.STATE_OTHER

    def _dump_fast(self) -> ET.Element:
        """交互路径的快速 UI dump（2 次尝试、很短的等待）。

        比默认 dump（2 次 / 0.8s）快，又比"只试 1 次"稳——单次 uiautomator 偶发失败
        时不至于把整轮发送/登录直接判成失败。

        每次成功都记进"最近快照"（见 recent_snapshot）：轮询每 2 秒就会 dump 一次，
        发送等交互可以直接复用那份，省掉一次 ~3 秒的 dump。
        """
        root = self.adb.dump_ui(retries=2, delay=0.3)
        self._snapshot = root
        self._snapshot_ts = time.monotonic()
        return root

    def recent_snapshot(self, max_age: float | None = None) -> ET.Element | None:
        """最近一次界面快照（默认 max_age = ui.snapshot_reuse，秒），过期返回 None。

        只用于"基线/控件位置"这类容错信息（例如发送前记下已有气泡数、输入框位置），
        调用方拿到后仍会做自己的判断；过期就无法取得，自己 dump 即可。
        """
        if self._snapshot is None:
            return None
        age = self._snapshot_reuse if max_age is None else max_age
        if age <= 0:
            return None
        if time.monotonic() - self._snapshot_ts > age:
            return None
        return self._snapshot

    def _dump_with_retry(self, attempts: int = 2) -> ET.Element | None:
        """读界面并容错：失败时（收键盘 / 找回窗口）再试，仍失败返回 None。

        为什么要收键盘：软键盘/光标闪烁是 uiautomator "could not get idle state"
        的常见原因之一，收起后再 dump 往往就成功了。
        为什么要"找回窗口"：容器窗口被最小化/关闭或息屏后，Android 侧
        `mCurrentFocus=null`，uiautomator 会一直报 "null root node"——这时先唤醒屏幕、
        再重新拉起 App，通常就能恢复读取。
        日志节流：同一类失败 30 秒内只打一次 warning，其余降级为 debug（避免重复输出）。
        """
        for i in range(max(1, attempts)):
            try:
                return self._dump_fast()
            except AdbError as e:
                now = time.monotonic()
                level = "warning" if now - self._last_dump_warn >= 30 else "debug"
                self._last_dump_warn = now
                self.log(level, f"读取界面失败（第 {i + 1}/{attempts} 次）: {e}")
                if i + 1 >= attempts:
                    return None
                self._recover_window()
                time.sleep(0.8)
        return None

    def _recover_window(self) -> bool:
        """把"窗口"找回来：先收键盘，再（必要时）唤醒屏幕 + 重新拉起 App。

        返回是否执行了恢复动作。没有焦点窗口时（容器窗口被最小化/关闭、息屏），
        `launch()` 因为 Activity 仍是 resumed 会直接跳过，所以这里直接调 adb 拉起。
        """
        acted = False
        try:
            self.close_keyboard()
        except Exception:  # noqa: BLE001 个别实现没有 ime_shown
            pass
        try:
            # 两种情况都要把窗口找回来：
            #   ① 没有任何窗口有焦点（mCurrentFocus=null，见实时日志）
            #   ② 前台是**别的 App**（例如 容器 主屏 com.microsoft.windows.homeapp，
            #      用户日志里就是这个场景）——此时 dump 同样会失败
            foreign = False
            if self.adb.has_focus_window():
                foreign = not self.adb.is_in_foreground(self.package)
            needs = (not self.adb.has_focus_window()) or foreign
            if needs:
                why = "前台是其它窗口" if foreign else "没有任何窗口获得焦点"
                self.log("info", f"{why}（可能是 容器窗口被最小化/关闭、屏幕未点亮，"
                                 f"或 App 没在前台），正在唤醒屏幕并重新拉起小天才 App ...")
                if self.adb.screen_on() is not True:
                    self.adb.wake_up()
                self.adb.invalidate_focus()
                self.adb.launch_app(self.package, self.main_activity, wait=8.0, attempts=1)
                self.invalidate_login_state()
                acted = True
        except AdbError as e:
            self.log("debug", f"找回窗口失败: {e}")
        return acted

    def launch(self) -> bool:
        """确保小天才 App 在前台；**已在前台时不做任何启动动作**（避免"定死操作"）。

        设备上 `am start -n` 偶发失败，adb_controller.launch_app() 内部已做多策略
        启动 + 前台轮询；这里只负责前置判断、重试与日志。
        """
        if self.adb.is_in_foreground(self.package):
            self.log("debug", "小天才 App 已在前台，跳过启动")
            return True
        self.log("info", f"小天才 App 不在前台，启动: {self.package}")
        try:
            installed = self.adb.package_installed(self.package)
        except AdbError as e:
            # 无法读取包列表 ≠ 没安装（ADB offline 时命令会失败）。别说成"未安装"误导排障。
            self.log("warning", f"无法确认小天才 App 是否已安装（ADB 连接异常）: {e}")
            return False
        if not installed:
            self.log("error", f"设备上没有安装 {self.package}（请先在模拟器/容器里安装并登录小天才 App）")
            return False
        last = ""
        for attempt in range(1, 4):
            try:
                used = self.adb.launch_app(self.package, self.main_activity)
                if used and self.adb.is_in_foreground(self.package):
                    self._login_state = None
                    return True
                last = f"activity={used or '(未解析)'} 前台={self.current_activity() or '(未知)'}"
            except AdbError as e:
                last = str(e)
            self.log("warning", f"启动小天才未确认（第 {attempt}/3 次）：{last}")
            time.sleep(1.5 * attempt)
        self.log("error", f"启动失败: {last}")
        return False

    def current_activity(self) -> str:
        """当前 **Activity**（忽略输入法/弹窗窗口）。"""
        return self.adb.get_current_activity() or ""

    def current_window(self) -> str:
        """当前最上层窗口（含输入法/弹窗），用于弹窗判断。"""
        return self.adb.get_current_focus() or ""

    def login_state(self, force: bool = False, root: ET.Element | None = None) -> str:
        """判断登录态，返回 'logged_in' / 'not_logged_in' / 'unknown'。

        **以界面为准，Activity 名只作兜底**（用户反馈过"明明已登录却提示未登录"）：
        - App 不在前台 / 界面无法读取（息屏、锁屏、dump 失败）→ 'unknown'
          （绝不当成"未登录"，否则会去点登录按钮、甚至误触发自动登录）；
        - 界面像登录页（密码框 / 获取验证码 / 短信验证码登录 / login_markers）→ 'not_logged_in'；
        - 界面像主界面（聊天页 / 消息列表 / 微聊·我的 等）→ 'logged_in'；
        - 都不像时才看 Activity 名：含 welcome/register/signin 或以 LoginActivity 结尾 → 'not_logged_in'，
          否则按已登录处理（保守，避免谎报未登录）。

        force=False 时结果按 login_state_ttl 秒缓存（轮询每 2s 调用，缓存可省掉大量 dump）；
        root 传入时复用调用方已经 dump 好的界面，不再重复 dump。
        """
        if not force and root is None and self._login_state is not None:
            ts, val = self._login_state
            if self._login_state_ttl > 0 and (time.monotonic() - ts) < self._login_state_ttl:
                return val
        act = self.current_activity()
        if not act.startswith(self.package):
            # 无法取得 Activity 信息，或 App 真的不在前台：无法判断登录态
            return self._remember_login(self.LOGIN_UNKNOWN)
        if root is None:
            try:
                root = self.adb.dump_ui(retries=2, delay=0.4)
            except AdbError:
                # 无法读取界面（息屏/空闲性/安全窗口）→ 无法判断，不缓存为"未登录"
                return self.LOGIN_UNKNOWN
        # 首启隐私协议弹窗 = 尚未进入 App
        joined = "".join(n.get("text", "") or "" for n in root.iter("node"))
        if "温馨提示" in joined and ("同意" in joined or "不同意" in joined):
            return self._remember_login(self.NOT_LOGGED_IN)
        if self._looks_like_login_page(root):
            return self._remember_login(self.NOT_LOGGED_IN)
        if self._looks_like_main_ui(root):
            return self._remember_login(self.LOGGED_IN)
        # 界面读到了但两种都不像：用 Activity 名兜底（只认明确的登录/欢迎页）
        low = act.lower()
        if any(m in low for m in ("welcome", "register", "signin")) or low.endswith("loginactivity"):
            return self._remember_login(self.NOT_LOGGED_IN)
        return self._remember_login(self.LOGGED_IN)

    def is_logged_in(self, force: bool = False, root: ET.Element | None = None) -> bool:
        """是否已登录（True 仅当**明确**判定为已登录；无法判断时为 False）。

        需要区分"未登录"和"无法判断"的场景请直接用 login_state()。
        """
        return self.login_state(force=force, root=root) == self.LOGGED_IN

    def _looks_like_main_ui(self, root: ET.Element) -> bool:
        """主界面证据：聊天页（输入栏/消息气泡）、消息列表行、微聊/我的 等入口。

        这是修复"已登录却提示未登录"的关键：**登录后界面有这些特征就认为已登录**，
        不再因为 Activity 名里带 login（如 AccountVerifyLoginActivity）而误判。
        """
        if self.looks_like_chat_page(root):
            return True
        if self._row_anchors(root):                     # 消息列表有"联系人+预览"行
            return True
        markers = set(self.ui.get("main_ui_markers",
                                  ["微聊", "消息", "我的", "联系人", "聊天", "设置", "发现"]))
        texts = {(n.get("text") or "").strip() for n in root.iter("node")}
        hits = {t for t in texts if t in markers}
        return len(hits) >= 2

    def _remember_login(self, state: str) -> str:
        self._login_state = (time.monotonic(), state)
        return state

    def invalidate_login_state(self) -> None:
        self._login_state = None

    def _looks_like_login_page(self, root: ET.Element) -> bool:
        """登录页判定：只认登录页**专属**特征，避免把已登录界面里的"登录"字样误判。

        专属特征：
        1) 密码输入框（EditText 的 password="true"，或提示文案含"密码"）；
        2) 短信登录页元素（获取验证码 / 短信验证码登录）；
        3) 配置的 login_markers（默认只含「注册/登录」「立即登录」这类强标记）。
        """
        texts = [(n.get("text", "") or "") for n in root.iter("node")]
        hint = "".join(texts) + "".join(
            (n.get("content-desc", "") or "") for n in root.iter("node"))
        for marker in ("获取验证码", "短信验证码登录", "验证码登录"):
            if marker in hint:
                return True
        for n in root.iter("node"):
            if not (n.get("class", "") or "").endswith("EditText"):
                continue
            if (n.get("password", "") or "").lower() == "true":
                return True
            if "密码" in (n.get("text", "") or "") or "密码" in (n.get("content-desc", "") or ""):
                return True
        for marker in self.ui.get("login_markers", ["注册/登录", "立即登录"]):
            if not marker:
                continue
            if any(marker in (t or "") for t in texts):
                return True
        return False

    def require_login(self, root: ET.Element | None = None) -> bool:
        """能否读消息：只有**明确未登录**才算不能读。

        注意：'unknown'（App 不在前台 / 界面无法读取，例如息屏）不再当成"未登录"重复输出日志，
        也不会（在桥接层）触发自动登录 —— 这正是"已经登录了还提示未登录"的修复点之一。
        """
        state = self.login_state(root=root)
        if state == self.NOT_LOGGED_IN:
            if not self._warned_not_login:
                self.log("warning", "小天才 App 未登录家长账号：请在目标 Android 环境"
                                    "（模拟器/容器/Waydroid/真机）里完成登录（登录前无法读取/发送消息）")
                self._warned_not_login = True
            return False
        if state == self.LOGIN_UNKNOWN:
            self.log("debug", "无法确认登录态（App 不在前台或界面暂时无法读取），本轮跳过读取")
            return False
        self._warned_not_login = False
        return True

    def _activity_is_chat(self, act: str) -> bool:
        """前台 Activity 名是否就是聊天页。

        旧实现只认 `endswith("chatactivity")`，机型/版本一换（ChattingActivity、
        带子类后缀等）就漏判，于是"人已经在聊天页却以为在主页"。
        后来改成"含 chat 且不含列表/首页类字样"，但那段判断用的是**整个组件名**——
        实机上报的是 `com.xtc.wechat.view.chatlist.ChatActivity`：**包名里的
        `chatlist` 含 "list"**，于是聊天页永远被排除，`is_in_chat()` 每次都要再
        dump 一次界面（设备上一次 ~3 秒，发送链路空等 3 秒）。现在只拿**类名**
        （最后一段）做判断，`ui.activity_chat_exclude` 也是按类名匹配。
        """
        a = (act or "").lower()
        if not a or not a.startswith(self.package.lower()):
            return False
        cls = a.split("/")[-1].rsplit(".", 1)[-1]      # ...chatlist.ChatActivity -> chatactivity
        if any(w in cls for w in self.ui.get("activity_chat_exclude",
                                             list(_ACTIVITY_CHAT_EXCLUDE))):
            return False
        return "chat" in cls

    def _find_chat_bar(self, root: ET.Element):
        """聊天输入栏 / 聊天页特征控件。

        先精确匹配已知 id，再按"包含"匹配（`send_view`/`chat_msg_item`/`record_button` …），
        这样即使输入栏 id 变了、或当前是语音模式，也能认出"这就是聊天页"。
        """
        hints = tuple(self.ui.get("chat_id_hints", list(_CHAT_ID_HINTS)))
        fallback = None
        for n in root.iter("node"):
            tail = self._id_tail(n).lower()
            if not tail:
                continue
            if tail in _CHAT_IDS:
                return n
            if fallback is None and any(h in tail for h in hints):
                fallback = n
        return fallback

    def looks_like_chat_page(self, root: ET.Element) -> bool:
        """聊天页判定（只看界面）：输入栏特征或**消息气泡**出现即算。

        消息气泡（chat_msg_item_*）只在聊天页出现，是最可靠的信号之一；
        聊天列表页只有 tv_chat_dialog_* 行。
        """
        if self._find_chat_bar(root) is not None:
            return True
        for n in root.iter("node"):
            tail = self._id_tail(n).lower()
            if "chat_msg_item" in tail or tail in ("tv_chat_msg_item_date",):
                return True
        return False

    def looks_like_message_list(self, root: ET.Element) -> bool:
        """是否像"消息列表"页（用于找不到联系人时给出准确诊断）。"""
        names = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        previews = set(self.ui.get("contact_preview_ids", list(_CONTACT_PREVIEW_IDS)))
        rows = 0
        for n in root.iter("node"):
            tail = self._id_tail(n)
            if tail in previews:
                rows += 1
            elif tail in names:
                rows += 1
        return rows >= 2

    def is_in_chat(self, root: ET.Element | None = None) -> bool:
        """聊天窗口判定。

        快路径：前台 Activity 名就是聊天页（见 _activity_is_chat）；
        兜底：按界面特征确认（输入栏 / 消息气泡），避免 Activity 名误判或漏判。
        root 传入时复用调用方已经 dump 好的界面，避免重复 dump。"""
        if self._activity_is_chat(self.current_activity()):
            return True
        if root is not None:
            return self.looks_like_chat_page(root)
        try:
            root = self._dump_fast()
        except AdbError:
            return False
        return self.looks_like_chat_page(root)

    def ensure_input_clean(self) -> str:
        """确保文字模式（有输入框），并清空输入框内容。返回状态描述。"""
        try:
            root = self._dump_fast()
            edit = self._find_input(root)
            if edit is None:
                if self._switch_to_text_mode(root):
                    time.sleep(self._delay)
                    root = self._dump_fast()
                    edit = self._find_input(root)
            if edit is None:
                return "未找到输入框（可能不在聊天页或界面异常）"
            # 已经空了就不要重复点键盘（少一次点击/40 次按键）；占位提示文案算"空"
            if self._input_text_of(edit).strip():
                self.adb.tap_element(edit)
                time.sleep(self._delay)
                for _ in range(40):
                    self.adb.keyevent(67)  # 清空可能残留的文字
                return "文字模式已就绪，输入框已清空"
            return "文字模式已就绪（输入框本来就是空的）"
        except AdbError as e:
            return f"输入框处理失败: {e}"

    def recover(self, contact: str = "") -> str:
        """"随机应变"的界面自愈：清弹窗 -> 必要时启动 App -> 检查登录 -> 回到聊天页。

        与旧的"每次都从头启动一遍"不同：每一步都先判断当前状态，只做缺的那一步。
        返回一句可读的状态说明（供日志/初始化命令使用）。
        """
        steps: list[str] = []
        try:
            if self.settle():
                steps.append("已清理弹窗")
            if not self.adb.is_in_foreground(self.package):
                ok = self.launch()
                steps.append("已启动 App" if ok else "启动失败")
                if not ok:
                    return "，".join(steps)
                self.settle()
            state = self.login_state(force=True)
            if state == self.NOT_LOGGED_IN:
                steps.append("未登录（等待自动登录/手动登录）")
                return "，".join(steps)
            if state == self.LOGIN_UNKNOWN:
                steps.append("无法确认登录态（界面无法读取/App 不在前台），未做登录动作")
                return "，".join(steps)
            if contact and not self.is_in_chat():
                if self.open_chat(contact):
                    steps.append("已回到聊天页")
                else:
                    steps.append("未能回到聊天页")
            return "，".join(steps) or "界面正常"
        except AdbError as e:
            return f"界面恢复异常: {e}"

    def keyboard_visible(self) -> bool:
        """软键盘是否弹出（dumpsys input_method 判断）。"""
        try:
            return self.adb.ime_shown()
        except AdbError:
            return False

    def close_keyboard(self) -> bool:
        """收起软键盘（若可见）。返回是否执行了收起动作。"""
        try:
            if self.keyboard_visible():
                self.adb.keyevent(4)
                time.sleep(0.8)
                return True
        except AdbError:
            pass
        return False

