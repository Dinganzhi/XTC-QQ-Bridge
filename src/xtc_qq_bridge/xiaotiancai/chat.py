# -*- coding: utf-8 -*-
"""聊天相关：打开会话、联系人查找、读取消息、历史与命令轮询（原 xiaotiancai.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .constants import *  # noqa: F401,F403
from ..utils import timelabel


def _norm_name(text: str) -> str:
    """比较用归一化：去空白（含全角空格）、统一小写。"""
    return re.sub(r"[\s\u3000]+", "", (text or "")).casefold()


class ChatMixin:
    """聊天相关：打开会话、联系人查找、读取消息、历史与命令轮询"""

    def _own_side_ratio(self) -> float:
        """右侧（自己发的气泡）的起始比例：默认 0.55，可用 `xiaotiancai.ui.own_side_ratio` 调整。

        横屏/平板/折叠屏/不同分辨率机型上聊天栏的位置不一样，固定 0.55 会把己方气泡判成
        对方发来的（表现为：家长自己说的话被当成孩子消息转发回 QQ，形成回声）。
        """
        try:
            return max(0.30, min(0.90, float(self.ui.get("own_side_ratio", 0.55))))
        except (TypeError, ValueError):
            return 0.55

    def _chat_screen_w(self, root) -> int:
        """屏幕宽度：优先用**本次 dump 自己的几何**，取不到才去问 adb。

        为什么不用 `adb.get_screen_size()`：`wm size` 查询失败时控制器会回落到内置的
        1920x1080（旧版本还会把这个兜底值永久缓存），于是 1366x768 / 1080x1920 的设备上
        阈值被算成 1056，而右对齐的己方气泡中心往往只有 800~1000 —— 己方消息被当成
        对方发来的，转发回 QQ 就成了回声。
        """
        try:
            w = 0
            for n in root.iter("node"):
                b = self._bounds(n)
                if b and b[2] > w:
                    w = max(w, b[2])
            if w >= 200:
                return int(w)
        except Exception:  # noqa: BLE001 几何解析失败就退回 adb 查询
            pass
        try:
            return int(self.adb.get_screen_size()[0])
        except Exception:  # noqa: BLE001
            return 1920

    @staticmethod
    def _id_tail(node) -> str:
        """resource-id 的末段（如 com.xtc.watch:id/tv_send_view -> tv_send_view）。"""
        rid = node.get("resource-id", "") or ""
        return rid.split("/")[-1]

    @staticmethod
    def _first(*nodes):
        """返回第一个非 None 节点（Element 真值判断已弃用，禁止用 or 链）。"""
        for n in nodes:
            if n is not None:
                return n
        return None

    def open_chat(self, contact: str) -> bool:
        if not contact:
            self.log("error", "open_chat 缺少联系人昵称（config.yaml -> target.xtc_contact）")
            return False
        if self.is_in_chat():
            return True
        # 0) App 不在前台（例如 容器 停在主屏/别的窗口）-> 先把它拉到前台，
        #    否则会在 容器 主屏上找联系人，然后报"找不到联系人"（实机测试发现的坑）。
        if not self.adb.is_in_foreground(self.package):
            # 0.1) 先排除"只是息屏/窗口没焦点"：容器 睡过去时 App 同样看起来不在前台，
            #      但窗口还在、Activity 还是 resumed 的 —— 叫醒即可，不必重新拉起 App
            #      （实测"重新拉起"要 20 秒，唤醒只要 ~1.5 秒）。
            if self.adb.wake_if_asleep() and self.adb.is_in_foreground(self.package):
                self.log("info", "容器息屏导致 App 不在前台，唤醒后已回到前台（无需重启 App）")
                self._dismiss_blockers()
                if self.is_in_chat():
                    return True
            else:
                self.log("info", f"小天才 App 不在前台（当前={self.current_activity() or '未知'}），先启动它")
                if not self.launch():
                    self.log("warning", "打开聊天失败：小天才 App 未能拉到前台")
                    return False
                self._dismiss_blockers()
                if self.is_in_chat():
                    return True
        self._dismiss_blockers()
        # 读到界面之前先查看登录态：未登录时没必要去找联系人
        # （实机测试：未登录时在登录页反复找联系人会空等 40 多秒）
        if self.login_state(force=True) == self.NOT_LOGGED_IN:
            self.last_open_reason = (f"小天才 App 未登录（当前在登录页），"
                                     f"登录后才能进入与 {contact!r} 的聊天")
            self._warn_throttled("open_chat_not_logged_in", "打开聊天失败：" + self.last_open_reason)
            return False
        # 读界面（失败自动收键盘重试；仍失败就本轮放弃，交给外层稍后重试，
        # 不再抛出 "cat: ... No such file" 这种误导性 ERROR）
        root = self._dump_with_retry(2)
        if root is None:
            self.last_open_reason = (f"连续无法读取界面控件（联系人 {contact}），稍后会自动重试"
                                     "（若反复出现，多为界面一直不空闲，"
                                     "可调大 adb.dump_retries / adb.dump_delay）")
            self._warn_throttled("open_chat_no_dump", "打开聊天暂缓：" + self.last_open_reason)
            return False
        if len(list(root.iter("node"))) < 3:
            # 刚登录完/页面切换中的空树：别照着它去"找联系人"，那只会得出错结论
            self.last_open_reason = "界面还在切换/加载（只读到空树）"
            self.log("info", f"打开聊天暂缓：{self.last_open_reason}，稍后自动重试")
            return False
        try:
            # 进来先确认一次：万一 Activity 名不认识，界面特征也能证明"已经在聊天页"
            if self.looks_like_chat_page(root):
                return True
            # 消息列表通常就是首页（"微聊"）：先在当前页找，找不到再切 Tab / 滑动查找
            node = self._find_contact_node(root, contact)
            if node is None:
                root = self._switch_to_message_tab(root) or root
                node = self._find_contact_node(root, contact)
            if node is None and self.looks_like_message_list(root):
                # 联系人可能在屏幕外：列表页才滑动（避免在别的页面误滑）
                node, _visible = self._scroll_find_contact(contact, root)
            if node is None:
                visible = self._visible_contact_names(root)
                if not self.adb.is_in_foreground(self.package):
                    page = f"小天才 App 不在前台（当前={self.current_activity() or '未知'}）"
                elif self.login_state(force=True) == self.NOT_LOGGED_IN:
                    page = "小天才 App 未登录（当前在登录页），登录后才能进入聊天"
                elif visible:
                    page = "消息列表"
                else:
                    page = f"当前页面不像消息列表（前台={self.current_activity() or '未知'}）"
                self.last_open_reason = (
                    f"找不到联系人 {contact!r}：{page}；"
                    f"当前可见联系人: {'、'.join(visible) or '(无)'}"
                    "（可修改 config.yaml -> target.xtc_contact 或 ui.contact_name_ids）")
                # 轮询每 2 秒一次，这类失败会连续发生 -> 同因 5 分钟只报一次
                self._warn_throttled("open_chat_not_found", self.last_open_reason)
                return False
            self.adb.tap_element(node)

            # 等待进入聊天；若误入别的页面/弹窗遮挡，先清弹窗再点一次
            deadline = time.time() + 12
            reclick = 0
            while time.time() < deadline:
                if self.is_in_chat():
                    return True
                act = self.current_activity()
                if (not act.startswith(self.package)) or "WatchMsg" in act or reclick < 2:
                    if self._dismiss_blockers():
                        time.sleep(0.5)
                    if reclick < 2:
                        root = self._dump_with_retry(1)   # 无法读取不影响本轮继续等待
                        if root is not None:
                            node = self._find_contact_node(root, contact)
                            if node is not None:
                                self.adb.tap_element(node)   # 弹窗关掉后重新点联系人
                        reclick += 1
                time.sleep(0.8)
            self.log("warning", f"点击联系人 {contact} 后未检测到聊天窗口（可能 App 界面有弹窗）")
            return False
        except AdbError as e:
            self.log("warning", f"打开聊天失败: {e}")
            return False


    def _contact_candidates(self, contact: str) -> list[str]:
        """联系人名的候选写法：原名/去空格；"张三(爸爸)" 这类再取括号前的部分。"""
        raw = (contact or "").strip()
        cands = [raw]
        for sep in ("(", "（", "[", "【", "<", "《", "-", "_", "|"):
            if sep in raw:
                head = raw.split(sep, 1)[0].strip()
                if head:
                    cands.append(head)
        cands += [c.replace(" ", "").replace("\u3000", "") for c in list(cands)]
        out: list[str] = []
        for c in cands:
            if c and c not in out:
                out.append(c)
        return out

    @staticmethod
    def _name_matches(row_name: str, candidates: list[str]) -> bool:
        """行内名字与候选名是否算同一个：全等，或（双方都不太短时）互相包含。"""
        n = _norm_name(row_name)
        if not n:
            return False
        for c in candidates:
            nc = _norm_name(c)
            if not nc:
                continue
            if n == nc:
                return True
            short, long_ = (n, nc) if len(n) <= len(nc) else (nc, n)
            if len(short) >= 2 and short in long_:
                return True
        return False

    def _clickable_ancestor(self, node, root: ET.Element, max_up: int = 5):
        """往上找可点击的祖先（列表行本身往往才是可点击节点）。"""
        cur = node
        for _ in range(max_up):
            if (cur.get("clickable", "") or "") == "true":
                return cur
            parent = self._parent(cur, root)
            if parent is None:
                break
            cur = parent
        return None

    def _visible_contact_names(self, root: ET.Element) -> list[str]:
        """当前界面上的联系人名（诊断用：找不到时告诉用户"我看到了谁"）。

        先按已知 id 取；再按"预览行"结构取（不同版本联系人名控件的 id 可能不一样，
        只认 id 会让诊断信息变成"空"，反而误导排查）。
        """
        ids = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        out: list[str] = []
        for n in root.iter("node"):
            if self._id_tail(n) in ids:
                t = (n.get("text") or "").strip()
                if t and t not in out:
                    out.append(t)
        if not out:
            for anchor in self._row_anchors(root):
                name = self._row_name_of(anchor, root)
                if name and name not in out:
                    out.append(name)
        return out

    def _row_anchors(self, root: ET.Element) -> list:
        """消息列表行的锚点节点（"最后一条消息预览"）。"""
        previews = set(self.ui.get("contact_preview_ids", list(_CONTACT_PREVIEW_IDS)))
        return [n for n in root.iter("node") if self._id_tail(n) in previews]

    def _row_name_of(self, anchor, root: ET.Element) -> str:
        """从"预览"节点所在的行里取出联系人名。

        ① 同行里已知的联系人名 id；② 退化为"行内位于预览上方、最靠上的文本"
        （列表行结构：名字在上、预览在下），这样即使 id 变了也能匹配/诊断。
        """
        parent = self._parent(anchor, root)
        if parent is None:
            return ""
        name_ids = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        for n in parent.iter("node"):
            if self._id_tail(n) in name_ids and (n.get("text") or "").strip():
                return (n.get("text") or "").strip()
        preview_text = (anchor.get("text") or "").strip()
        anchor_b = self._bounds(anchor)
        best = None
        best_y = None
        for n in parent.iter("node"):
            t = (n.get("text") or "").strip()
            if not t or t == preview_text:
                continue
            b = self._bounds(n)
            if b is None:
                continue
            if anchor_b is not None and b[3] > anchor_b[1]:
                continue                      # 在预览下方的不算（多为时间/未读角标）
            if best is None or (best_y is not None and b[1] < best_y):
                best, best_y = n, b[1]
        return ((best.get("text") or "").strip() if best is not None else "")

    def _find_contact_node(self, root: ET.Element, contact: str):
        """在消息列表里找联系人节点。

        顺序：① 列表行的联系人名节点（id 命中，最可靠，支持别名/空格差异）
              ② 按"预览行"结构找（联系人名 id 与预期不同也能命中）
              ③ 任意节点的 text / content-desc 包含匹配
        找到后尽量返回**可点击的祖先**（点行本身才稳定，点小文字有时无效）。
        """
        candidates = self._contact_candidates(contact)
        name_ids = set(self.ui.get("contact_name_ids", list(_CONTACT_NAME_IDS)))
        for n in root.iter("node"):
            if self._id_tail(n) not in name_ids:
                continue
            if self._name_matches(n.get("text", ""), candidates):
                return self._clickable_ancestor(n, root) or n
        for anchor in self._row_anchors(root):
            if self._name_matches(self._row_name_of(anchor, root), candidates):
                return self._clickable_ancestor(anchor, root) or anchor
        for c in candidates:
            n = self._first(
                self.adb.find_element(root, text=c, text_contains=True),
                self.adb.find_element(root, content_desc=c))
            if n is not None:
                return self._clickable_ancestor(n, root) or n
        return None

    def _switch_to_message_tab(self, root: ET.Element):
        """切到"微聊/消息"页；成功返回新的界面快照，否则 None。

        文案可通过 ui.message_tab_text / ui.message_tab_texts 配置（不同版本叫法不同）。
        """
        texts = self.ui.get("message_tab_texts") or [self.ui.get("message_tab_text", "微聊")]
        for t in [x for x in texts if x]:
            tab = self._first(
                self.adb.find_element(root, text=t),
                self.adb.find_element(root, content_desc=t))
            if tab is None:
                continue
            self.log("debug", f"切换到 {t} 页")
            self.adb.tap_element(tab)
            time.sleep(self._delay)
            return self._dump_with_retry(2)
        return None

    def _scroll_find_contact(self, contact: str, root: ET.Element,
                             max_swipes: int = 3) -> tuple:
        """联系人可能在屏幕外：在消息列表里向上滑几次找它。返回 (节点或None, 见过的名字)。

        只在"确实是消息列表"时调用（调用方已判断），避免在别的页面在非消息列表页面误滚动。
        """
        seen = self._visible_contact_names(root)
        try:
            w, h = self.adb.get_screen_size()
        except AdbError:
            return None, seen
        for i in range(max_swipes):
            try:
                self.adb.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 300)
            except Exception as e:  # noqa: BLE001 个别实现/假设备没有 swipe 时不影响查找
                self.log("debug", f"滑动查找联系人失败（跳过）: {e}")
                break
            time.sleep(0.5)
            cur = self._dump_with_retry(1)
            if cur is None:
                break
            for name in self._visible_contact_names(cur):
                if name not in seen:
                    seen.append(name)
            node = self._find_contact_node(cur, contact)
            if node is not None:
                self.log("info", f"向上滑动 {i + 1} 次后在列表里找到联系人 {contact!r}")
                return node, seen
        return None, seen

    def message_kind(self, root: ET.Element, text: str) -> str:
        """这条消息到底是什么：`'sticker'`（气泡是表情图）/ `'image'`（照片）/ `'text'`。

        为什么不能只看文本前缀：用户实测手打了一条内容就是"表情"的**文本**消息，
        旧逻辑按 `text.startswith("表情")` 把它当表情包，等了 10 分钟没有图可发，
        最后发出一条**没有图、只有"[表情]"的"空气消息"**。
        这里只认**气泡本身的类型**（`_chat_bubbles` 里给 sticker/image 打的标记）。
        """
        t = (text or "").strip()
        if not t:
            return "text"
        if t == IMAGE_TEXT:
            return "image"                 # "图片"这个文本本身就是 App 给照片的占位文本
        if not t.startswith("表情"):
            return "text"
        try:
            items = self._chat_bubbles(root, include_own=False)
        except Exception as e:  # noqa: BLE001 解析失败按文本处理（宁可当文本，也别发空气消息）
            self.log("debug", f"判断消息类型失败（按文本处理）: {e}")
            return "text"
        for it in reversed(items):         # 从新到旧找同一条
            it_text = (it.get("text") or "").strip()
            if not it_text:
                continue
            # 文本可能因为 desc 的写法有一两个字的差别（例如名字没读全），
            # 这里对"表情"类消息放宽到包含关系；非表情文本必须完全一致。
            same = (it_text == t) or (t.startswith("表情") and it_text.startswith("表情")
                                      and (t in it_text or it_text in t))
            if not same:
                continue
            if it.get("sticker"):
                return "sticker"
            if it.get("image"):
                return "image"
        return "text"                      # 界面上的同文本气泡不是图 -> 只是手打的文本

    def get_latest_message(self, root: ET.Element | None = None):
        """返回 (contact, text, time_label, own_text, own_recent)；无法确定时
        (None, None, "", "", [])。

        root: 可传入调用方已经读好的界面快照（轮询层会先 app_state 判状态再复用），
              省掉一次 uiautomator dump——设备上单次 dump 要 3 秒左右。

        own_text = 最新一条"自己发的"消息文本；
        own_recent = 最近若干条自己发的消息 [(text, time_label)]（新->旧，供 xtc 侧
        命令检测；命令可能被送达确认等新消息盖过，需扫最近几条；时间标签用于区分
        同文本的再次输入）。复用同一次 dump，避免额外开 uiautomator dump。不抛异常。

        只在聊天窗口内读取——列表预览无法可靠判断发送方（家长侧手动发送的消息
        也会出现在预览里），会被误当成对方消息转发。轮询层负责确保聊天窗口已打开。"""
        try:
            if not self.require_login(root):
                return (None, None, "", "", [])
            # 一次 dump 同时用于登录态判断与消息解析（登录态有缓存，避免重复 dump）；
            # 无法读取界面（界面一直不空闲等）→ 返回空，交给轮询层稍后重试/自愈，
            # 不抛异常也不刷 ERROR 日志。
            root = root if root is not None else self._dump_with_retry(2)
            if root is None:
                return (None, None, "", "", [])
            if self.is_in_chat(root):
                contact, text, time_label = self._latest_in_chat(root)
                kind0 = getattr(self, "_last_latest_kind", "")
                # 偶发的"不完整界面快照"：聊天里有消息，但一个时间标签都没有。
                # 这时直接转发会退化成"当前时间"（用户报告过 8 点的消息被按当前时间
                # 转发）。重读一两次通常就有标签了；20 秒内只补救一次，避免拖慢轮询。
                used_retry = False
                if (text and not time_label and self._date_count(root) == 0
                        and time.monotonic() - self._last_label_retry >= 20):
                    self._last_label_retry = time.monotonic()
                    for _ in range(2):
                        time.sleep(0.4)
                        again = self._dump_with_retry(1)
                        if again is None or not self.is_in_chat(again):
                            break
                        c2, t2, l2 = self._latest_in_chat(again)
                        if l2:
                            root = again                     # 用更完整的那次快照
                            contact, text, time_label = (c2 or contact), t2, l2
                            used_retry = True
                            break
                if not used_retry:
                    # 重读没被采用：类型必须回到**第一次解析**的结果，
                    # 否则 text 和 kind 来自不同的快照（表情会被当文本）。
                    self._last_latest_kind = kind0
                own_recent = self._own_texts_in_chat(root)
                return (contact, text, time_label,
                        own_recent[0][0] if own_recent else "", own_recent)
            return (None, None, "", "", [])  # 不在聊天页不读列表（防误转发家长侧消息）
        except Exception as e:  # noqa: BLE001 读取失败不致命
            self.log("warning", f"读取消息异常: {e}")
            return (None, None, "", "", [])

    @staticmethod
    def _is_image_bubble(cls: str, desc_body: str, bounds) -> bool:
        """这个气泡是不是"图片（照片）消息"。

        实测：class=ImageView、text 为空、content-desc 形如
        `童武洋发的消息,图片消息，双击查看`；图片还没下载完时 desc 里可能还没有
        "图片消息"几个字（只剩发送方），所以"空的说明 + 够大的 ImageView 气泡"
        也算图片 —— 否则这张照片会被**静默丢弃**（用户报的"图片无法发送、
        控制台一行都没有"）。
        表情（desc_body 以"表情"开头）与语音等其它 ImageView 气泡（desc_body 是
        "语音"这类文字）都不会被误判；尺寸门槛用来排除头像/小图标。
        """
        if "ImageView" not in (cls or ""):
            return False
        body = (desc_body or "").strip()
        if body.startswith("表情"):
            return False
        if body and "图片" not in body and "照片" not in body:
            return False
        if not bounds:
            return False
        w, h = bounds[2] - bounds[0], bounds[3] - bounds[1]
        return w >= 60 and h >= 60

    def _latest_in_chat(self, root: ET.Element):
        """聊天窗口内：取最新一条"别人发来的"消息，返回 (contact, text, time_label)。

        识别依据（真机实测）：
        - 消息气泡 id=chat_msg_item_content（文本为 TextView，表情/语音为 ImageView，text 可能为空）；
        - content-desc 标注发送方：'童武洋发的消息,内容'（手表发） vs '你发的消息,内容'（自己发，跳过）；
        - **双重判断**：desc 标注为主，气泡左右位置为辅（自己发=右侧、别人发=左侧），
          desc 缺失时按位置判断（右侧=自己发跳过）；
        - 时间标签取气泡上方最近的日期节点（tv_chat_msg_item_date，如 "06:56" / "昨天 23:42"）。
        注意：最新消息可能被输入栏遮挡（y 超出输入栏顶部），因此不做输入栏边界裁剪。
        """
        screen_w = self._chat_screen_w(root)
        filter_own = bool(self.ui.get("filter_own_bubbles", True))
        junk = set(self.ui.get("chat_junk_texts", _DEFAULT_JUNK))
        # 收集时间标签（按 y 排序，供气泡取最近上方标签）；压缩 dump 里退化成从
        # 父容器 desc 还原（见 _time_labels），否则时间会全变成"当前时间"
        dates = self._time_labels(root)
        # 发送失败提示条（网络异常等）：下方带该提示的气泡 = 未送达，跳过
        fail_hints = []
        for n in root.iter("node"):
            if self._id_tail(n) in _TIP_IDS:
                b = self._bounds(n)
                if b:
                    fail_hints.append((b[1], b[3]))
        candidates = []
        for n in root.iter("node"):
            if self._id_tail(n) != "chat_msg_item_content":
                continue  # 只关心消息气泡
            t = n.get("text", "").strip()
            desc = n.get("content-desc", "")
            cls = n.get("class", "") or ""
            b = self._bounds(n)
            if b is None:
                continue
            # 下方带"发送失败"提示的气泡 = 未送达（手动发送失败等），跳过
            if any(fh_top - 60 <= b[3] <= fh_top + 30 for fh_top, _ in fail_hints):
                continue
            center_x = (b[0] + b[2]) / 2
            if "发的消息" in desc:
                # App 标注了发送方：'XX发的消息,内容'（别人） / '你发的消息,内容'（自己）
                if desc.startswith("你发的"):
                    continue
                if not t and "," in desc:
                    t = desc.split(",", 1)[1].strip()  # 表情/语音等从 desc 取内容类型
            else:
                # 无标注：按左右位置判断（自己发=右侧跳过）
                if t in junk:
                    continue
                if filter_own and center_x > screen_w * self._own_side_ratio():
                    continue  # 右侧气泡 = 自己发的消息
            # 图片消息（照片）：气泡是 ImageView、没有文字，desc 里写"图片消息，双击查看"
            # （图片还没下完时 desc 可能只有"XX发的消息"）。以前这类消息 text 为空 -> 被
            # 静默丢弃，控制台一行日志都没有（用户报的"图片无法发送、也没提示"）。
            kind = self._bubble_kind(cls, n.get("text", ""), desc, b)
            if kind == "image":
                t = IMAGE_TEXT
            elif kind == "sticker" and not t:
                t = desc.split(",", 1)[1].strip() if "," in desc else "表情"
            if not t:
                continue
            # 桥接系统提示（如"发送成功/发送失败"送达确认）一律不转发，防止循环
            if self._is_system_msg(t):
                continue
            candidates.append((n, t, b[3], kind))
        if not candidates:
            self._last_latest_kind = ""
            return (None, None, "")
        n, t, y_bottom, kind = max(candidates, key=lambda c: c[2])
        # 本次"最新一条"的类型：桥接据此决定要不要取图/加 [表情] 标记。
        # 关键点：这是**同一次解析**里得出的，不靠事后拿文本再猜一遍
        # （实测事后猜会把表情判成文本，只发出"表情"两个字）。
        self._last_latest_kind = kind
        # 归属判定要知道**所有**气泡（含自己发的），所以单独收集一份
        all_bubbles = [x for x in root.iter("node")
                       if self._id_tail(x) == "chat_msg_item_content"
                       and self._bounds(x) is not None]
        return (None, t, self._label_for_bubble(n, dates, all_bubbles))

    def latest_kind(self) -> str:
        """最近一次 `get_latest_message()` 读到的那条消息的类型：sticker/image/text。"""
        return getattr(self, "_last_latest_kind", "") or "text"

    def _label_for_bubble(self, bubble, dates: list, all_bubbles: list | None = None) -> str:
        """取**这条消息自己的**时间（dates = [(top, bottom, text)]，按 top 升序）。

        小天才只在一个"时间组"的第一条消息上方画一个时间标签，组内后续消息
        **没有**自己的标签。所以规则是两条：
        ① 找位于该气泡上方、最靠下的那个标签；
        ② 这个标签必须**真的属于它**——标签下方第一条气泡必须是它自己。
           否则说明标签是上面那条消息的，本条没有自己的时间，返回 ""。

        为什么必须做 ②：没有它就会出现"三条消息全是 23:47"——实测日志
            [09-20 23:47] test / 我去？ / 晚安
        三条不同消息（检测时刻 23:50、23:52、23:53）都被安上了组首的 23:47。
        返回 "" 时上层用"当前时间"，对刚收到的消息就是它自己的到达时间，
        每条各不相同（这也是 09-15 老版本的行为）。
        """
        b = self._bounds(bubble)
        if not b or not dates:
            return ""
        top = b[1]
        chosen = None
        for d_top, d_bottom, d_text in reversed(dates):   # dates 按 top 升序
            if d_bottom <= top + 5:                       # 标签底边在气泡上方
                chosen = (d_top, d_bottom, d_text)
                break
        if chosen is None:
            return ""
        if not all_bubbles:            # 无法取得其它气泡时退回旧行为
            return chosen[2]
        label_bottom = chosen[1]
        below = [ob[1] for ob in (self._bounds(x) for x in all_bubbles)
                 if ob is not None and ob[1] >= label_bottom - 5]
        if below and min(below) != top:
            return ""                  # 这个标签是它上面那条消息的
        return chosen[2]

    def _date_count(self, root: ET.Element) -> int:
        return len(self._time_labels(root))

    @staticmethod
    def _cn_numeral(token: str) -> str:
        """还原 app 无障碍文案里的"中文式"数字：十1->11、十9->19、2十->20、4十6->46。

        实机 ll_chat_top_layout 的 content-desc 就是这种写法（'十1点十9分' 对应 11:19）。
        认不出来返回 ""（宁可不给，也不给错的时间）。
        """
        token = (token or "").strip()
        if not token:
            return ""
        if "十" not in token:
            return token if token.isdigit() else ""
        if token == "十":
            return "10"
        head, _, tail = token.partition("十")
        if head and not head.isdigit():
            return ""
        tens = int(head) if head else 1
        if not tail:
            return str(tens * 10)
        if not tail.isdigit():
            return ""
        return str(tens * 10 + int(tail))

    @classmethod
    def _label_from_desc(cls, desc: str) -> str:
        """把时间标签的 content-desc 还原成 "HH:MM"（认不出来返回 ""）。

        为什么需要：设备上默认的 `--compressed` dump **不含** tv_chat_msg_item_date
        节点（实测同一屏：压缩版 0 个时间标签，完整版 3 个），只剩父容器
        ll_chat_top_layout 的 content-desc；不还原的话每条消息的时间都会退化成
        "当前时间"（用户报的"消息时间不对"）。
        """
        s = (desc or "").strip()
        if not s:
            return ""
        m = re.match(r"^([0-9十]{1,3})点(?:([0-9十]{1,3})分?)?$", s)
        if not m:
            return ""
        hh = cls._cn_numeral(m.group(1))
        mm = cls._cn_numeral(m.group(2)) if m.group(2) else "0"
        if not hh or not mm:
            return ""
        hi, mi = int(hh), int(mm)
        if not (0 <= hi <= 23 and 0 <= mi <= 59):
            return ""
        return f"{hi:02d}:{mi:02d}"

    def _time_labels(self, root: ET.Element) -> list[tuple[int, int, str]]:
        """聊天页里的时间标签 [(top, bottom, text)]，按从上到下排序。

        首选 tv_chat_msg_item_date 的文本（完整 dump 里有，形如 "11:19"/"昨天 23:42"）；
        没有这些节点时（压缩 dump）退化成从 ll_chat_top_layout 的 content-desc 还原。
        """
        labels = []
        for n in root.iter("node"):
            if self._id_tail(n) != "tv_chat_msg_item_date":
                continue
            b = self._bounds(n)
            t = (n.get("text") or "").strip()
            if b and t:
                labels.append((b[1], b[3], t))
        if labels:
            return sorted(labels)
        for n in root.iter("node"):
            if self._id_tail(n) != "ll_chat_top_layout":
                continue
            b = self._bounds(n)
            t = self._label_from_desc(n.get("content-desc") or "")
            if b and t:
                labels.append((b[1], b[3], t))
        return sorted(labels)

    def _bubble_kind(self, cls: str, raw_text: str, desc: str, bounds) -> str:
        """这条气泡是什么：`'sticker'` / `'image'` / `'text'`（只按气泡本身判断）。

        为什么要有这一个统一入口：实时路径（`_latest_in_chat`）与补发路径
        （`_chat_bubbles`）必须使用**同一套**判定，否则同一条消息在两条路径上会被算成
        不同类型 —— 实测就是这样把表情当成文本发了出去（只发出"表情"两个字、
        没有 `[表情]` 前缀、也没有图）。
        """
        body = ""
        if "," in (desc or ""):
            body = desc.split(",", 1)[1].strip()
        raw = (raw_text or "").strip()
        if body.startswith("表情") and not raw:
            return "sticker"
        # 判图片时沿用旧行为：气泡没有文本就用 desc 内容当说明，
        # 否则"语音"这类 ImageView 气泡（desc 非空、但不含"图片"）会被当成照片。
        if self._is_image_bubble(cls, raw or body, bounds):
            return "image"
        return "text"

    def _chat_bubbles(self, root: ET.Element, include_own: bool = False) -> list[dict]:
        """解析一次 UI dump 的聊天消息气泡，按屏幕从上到下（旧->新）排序。

        识别逻辑与 _latest_in_chat 一致（气泡 id=chat_msg_item_content）：
        - content-desc 标注发送方（'XX发的消息'/'你发的消息'）优先；无标注按左右位置
          （右侧=自己发）；
        - 下方带发送失败提示（网络异常）的气泡剔除；
        - 垃圾 UI 文案 / 桥接系统提示（发送成功 等前缀）剔除；
        - include_own=False 时跳过自己发的消息。
        返回 [{text, is_own, contact, time_label, y_bottom}]。
        """
        screen_w = self._chat_screen_w(root)
        filter_own = bool(self.ui.get("filter_own_bubbles", True))
        junk = set(self.ui.get("chat_junk_texts", _DEFAULT_JUNK))
        dates: list[tuple[int, int, str]] = self._time_labels(root)
        fail_hints: list[tuple[int, int]] = []
        for n in root.iter("node"):
            if self._id_tail(n) in _TIP_IDS:
                b = self._bounds(n)
                if b:
                    fail_hints.append((b[1], b[3]))
        out: list[dict] = []
        all_bubbles = [x for x in root.iter("node")
                       if self._id_tail(x) == "chat_msg_item_content"
                       and self._bounds(x) is not None]
        for n in root.iter("node"):
            if self._id_tail(n) != "chat_msg_item_content":
                continue
            t = (n.get("text", "") or "").strip()
            desc = (n.get("content-desc", "") or "").strip()
            cls = n.get("class", "") or ""
            b = self._bounds(n)
            if b is None:
                continue
            if any(fh_top - 60 <= b[3] <= fh_top + 30 for fh_top, _ in fail_hints):
                continue  # 气泡下方是发送失败提示 = 未送达
            center_x = (b[0] + b[2]) / 2
            is_own = False
            contact = ""
            desc_body = ""
            if "发的消息" in desc:
                if desc.startswith("你发的"):
                    is_own = True
                else:
                    contact = desc.split("发的消息", 1)[0].strip()
                if "," in desc:
                    desc_body = desc.split(",", 1)[1].strip()
                if not t and desc_body:
                    t = desc_body                      # 表情/语音等从 desc 取类型
            else:
                # 无标注：右侧气泡 = 自己发的消息。
                # `is_own` **只按位置算**，不受 filter_own_bubbles 影响：这个开关的语义是
                # "要不要把自己发的气泡也收进来"（由调用方的 include_own 决定过滤），
                # 而不是"不判断谁发的"。此前写成 `filter_own and ...`，于是关掉开关后
                # is_own 恒为 False，`_own_texts_in_chat` 返回空 —— 家长侧在小天才里
                # 输入的 `/小天才 …` 命令整体失效。
                is_own = center_x > screen_w * self._own_side_ratio()
            # 类型只按**气泡本身**判断（见 _bubble_kind）：表情/照片/文本三选一。
            # 注意传的是气泡**原始** text（`t` 可能已经被 desc 内容替换过）。
            kind = self._bubble_kind(cls, n.get("text", ""), desc, b)
            is_sticker = kind == "sticker"
            is_image = kind == "image"
            if is_image:
                t = IMAGE_TEXT
            elif is_sticker and not t:
                t = desc_body or "表情"
            if not t:
                continue
            # 垃圾文案（"表情"/"语音"/"发送"…）只是**输入栏按钮**的文字：
            # ① 表情/图片气泡一定是真实消息，永不按垃圾文案丢弃；
            # ② 带发送方标注（`XX发的消息,…`）的气泡也是真实消息 ——
            #    实测手打了一句内容就是"表情"的文本，就是被这条规则悄悄丢弃的，
            #    于是"表情"只能靠猜类型，最后发出没有图的两个字。
            if t in junk and not (is_sticker or is_image) and not desc:
                continue
            if self._is_system_msg(t):
                continue
            if is_own and not include_own:
                continue
            time_label = ""
            for d_top, d_bottom, d_text in reversed(dates):  # 取气泡上方最近的日期标签
                if d_bottom <= b[1] + 5:
                    time_label = d_text
                    break
            # 身份标签（`own_label`）：**这条消息自己的**时间，取法和实时路径
            # （`_latest_in_chat` -> `_label_for_bubble`）完全一致。补发与实时两条路径
            # 必须用同源同格式的身份，否则同一条消息会被各转发一次（实测踩过）。
            own_label = self._label_for_bubble(n, dates, all_bubbles)
            out.append({"text": t, "is_own": is_own, "contact": contact,
                        "time_label": time_label, "own_label": own_label, "y_bottom": b[3],
                        "sticker": is_sticker, "image": is_image, "kind": kind,
                        "bounds": b})
        out.sort(key=lambda it: it["y_bottom"])
        return out

    def image_of_latest(self, root: ET.Element, text: str = "",
                        match_label: str = "") -> dict | None:
        """最新一条**对方发来的图片（照片）**气泡信息；没有则 None（给桥接取图用）。

        match_label：只认身份/显示标签与它一致的那条（补发重试用 —— 那时光标可能已经
        滚到别的照片上，"最新一条图片"未必还是原来那条）。
        """
        try:
            items = self._chat_bubbles(root, include_own=False)
        except Exception as e:  # noqa: BLE001 判定失败不影响文字转发
            self.log("debug", f"解析图片气泡失败: {e}")
            return None
        return self._pick_bubble(items, "image", text, match_label)

    def sticker_of_latest(self, root: ET.Element, text: str = "",
                          match_label: str = "") -> dict | None:
        """最新一条**对方发来的表情**气泡信息 {text, bounds, time_label}；没有则 None。

        给桥接用：轮询读到"表情X"时，据此拿到气泡位置，把贴纸原样截图发给 QQ
        （见 capture_sticker）。text 传了就按文本匹配那一条（避免读到别的表情）。
        """
        try:
            items = self._chat_bubbles(root, include_own=False)
        except Exception as e:  # noqa: BLE001 判定失败不影响文字转发
            self.log("debug", f"解析表情气泡失败: {e}")
            return None
        return self._pick_bubble(items, "sticker", text, match_label)

    def _pick_bubble(self, items: list[dict], flag: str, text: str, match_label: str = ""):
        """按 flag(image/sticker) + 文本 + 标签，从旧到新取**最后一条**命中的气泡。

        `match_label` 可能是**绝对**标签（补发路径传的是 `10-19 21:30`），而界面上的标签
        是相对的（`昨天 21:30`）—— 两边都归一化成绝对时间再比，否则永远匹配不上
        （实测：图片因此被判成"界面上未找到气泡"，补发失败）。

        归一化用的是 `utils.timelabel`（与桥接层同一份实现）：此前这里调的是
        `self._abs_time_label`，而该方法只存在于桥接的 mixin 上，`Xiaotiancai` 根本没有它
        —— 只要带了时间标签就抛 AttributeError，并被上层 `except Exception` 吞成
        "界面上找不到气泡"，连带跳过后面所有兜底（照片/表情因此退化成纯文字）。
        """
        want = (text or "").strip()
        want_lbl = (match_label or "").strip()
        want_abs = timelabel.abs_label(want_lbl) if want_lbl else ""
        hit = None
        for it in items:                      # 已按 y 从小到大（旧->新）
            if not it.get(flag):
                continue
            if want and it.get("text") != want:
                continue
            if want_lbl:
                cands = [str(it.get("own_label") or "").strip(),
                         str(it.get("time_label") or "").strip()]
                matched = False
                for c in cands:
                    if not c:
                        continue
                    if c == want_lbl or (want_abs and
                                         (timelabel.abs_label(c) or c) == want_abs):
                        matched = True
                        break
                if not matched:
                    continue
            hit = it
        return hit

    def chat_view_bounds(self, root: ET.Element):
        """消息列表的**可视区** bounds（被列表裁掉的气泡会贴着它的边）。"""
        for n in root.iter("node"):
            if self._id_tail(n) in ("rv_chat_msg_chat", "rv_chat_msg", "lv_chat_msg"):
                b = self._bounds(n)
                if b:
                    return b
        for n in root.iter("node"):
            if (n.get("class") or "").endswith("RecyclerView"):
                b = self._bounds(n)
                if b:
                    return b
        return None

    def bubble_clipped(self, root: ET.Element, bounds, tol: int = 2) -> bool:
        """气泡是不是被消息列表裁到了（贴着可视区上/下边 = 有内容被裁掉）。

        为什么必须判：抠图是按气泡 bounds 裁的 —— 被裁掉一半时抠下来的就是**半张图**，
        拿去和原图比像素必然不过（实测 0.78 < 0.80），最后发给 QQ 的就是那半张截图
        （用户报的"表情只有一小半、还是 png"）。
        """
        vb = self.chat_view_bounds(root)
        if not vb or not bounds:
            return False
        _x1, y1, _x2, y2 = bounds
        _vx1, vy1, _vx2, vy2 = vb
        if (y2 - y1) >= (vy2 - vy1):
            return True                        # 比可视区还高：怎么都在裁
        return (y1 <= vy1 + tol) or (y2 >= vy2 - tol)

    def scroll_chat_by(self, vb, dy: int, duration: int = 300) -> bool:
        """把消息列表按 dy 拖动：正数 = 内容往下（看更早的），负数 = 内容往上（看更新的）。"""
        if not vb:
            return False
        cx = (vb[0] + vb[2]) // 2
        vh = max(20, vb[3] - vb[1])
        if dy > 0:
            y0 = vb[1] + vh // 4
        else:
            y0 = vb[3] - vh // 4
        try:
            self.adb.swipe(cx, y0, cx, y0 + int(dy), duration)
        except Exception as e:  # noqa: BLE001 滑动失败就算了（调用方会退避/等重试）
            self.log("debug", f"滚动消息列表失败: {e}")
            return False
        time.sleep(0.45)
        return True

    def capture_sticker(self, bounds) -> bytes | None:
        """把表情气泡那块**截图抠成 PNG**（失败返回 None，调用方退回发文字）。

        为什么截图而不是拿原图：贴纸是 App 内部资源（可能在 so/apk 里、也可能是网络图），
        ADB 无法取得；而贴纸在屏幕上就是它本身，按气泡区域裁剪即得原图。
        只裁气泡本身的区域（不加内边距）：实机实测 ImageView 的 bounds 正好是贴纸范围。

        截图偶发失败（刚唤醒/界面正在重绘）时**重试一次**：实机遇到过"明明气泡就在屏幕上，
        却因为这一次 screencap 没成功而整条退化成了文字"。
        """
        if not bounds:
            return None
        for attempt in (1, 2):
            try:
                w, h = self.adb.get_screen_size()
                x1, y1, x2, y2 = (int(v) for v in bounds)
                if x2 - x1 < 12 or y2 - y1 < 12:
                    self.log("debug", f"表情气泡太小，放弃截图: {bounds}")
                    return None
                if x2 <= 0 or y2 <= 0 or x1 >= w or y1 >= h:
                    self.log("debug", f"表情气泡不在屏幕内，放弃截图: {bounds}")
                    return None
                png = self.adb.screencap_crop_png((max(0, x1), max(0, y1),
                                                   min(w, x2), min(h, y2)))
                if png and len(png) >= 200:
                    return png
                self.log("debug", f"表情截图内容异常（{len(png or b'')} 字节）"
                                  + ("，重试一次" if attempt == 1 else ""))
            except Exception as e:  # noqa: BLE001 截图失败不影响文字转发
                self.log("debug", f"表情截图失败（第 {attempt} 次）: {e}")
            if attempt == 1:
                time.sleep(0.4)
        return None

    def _own_texts_in_chat(self, root: ET.Element, limit: int = 8) -> list[tuple[str, str]]:
        """聊天页内最近若干条"自己发的"消息（新->旧，系统/垃圾已过滤）。
        返回 [(text, time_label)]。命令可能被送达确认等后续消息盖过（不再是
        "最新一条"），检测时扫最近几条；时间标签用于区分同文本的再次输入。"""
        items = self._chat_bubbles(root, include_own=True)
        return [(it["text"], it["time_label"]) for it in reversed(items)
                if it["is_own"]][:max(1, limit)]

    def get_chat_history(self, count: int = 20,
                         skip_own_prefixes: tuple = ()) -> list[dict]:
        """聊天窗口内向上滚动，读取最近 count 条对话消息（不含系统/送达确认等）。

        返回 list[dict]，按时间从旧到新：
          {text, is_own, contact, time_label}
        - contact: 对方（手表侧）发送方名字；无标注为 ""
        - is_own:   是否家长侧（自己）发的消息（含从 QQ 转发进来的消息）
        - time_label: 气泡上方的 App 日期标签（如 "06:56" / "昨天 23:42"）
        skip_own_prefixes: 自己发的、以这些前缀开头的消息（如 xtc 命令）不计入历史。
        滚动到顶部或连续两屏无新消息即停止。必须在聊天页调用（调用方保证），
        键盘需已收起（否则滚动区域被遮挡）。失败返回 []。
        """
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        prefixes = tuple(p for p in (skip_own_prefixes or ()) if p)

        def skipped(it: dict) -> bool:
            return bool(it["is_own"] and prefixes
                        and it["text"].startswith(prefixes))
        try:
            screen_h = self.adb.get_screen_size()[1]
            x = self.adb.get_screen_size()[0] // 2
        except AdbError:
            return []
        # 去重只按 (文本, 发送方) —— 时间标签会随滚动移出屏幕而消失，
        # 若把标签并入 key，同一条消息跨屏会被重复统计。
        seen_prev: set[tuple[str, bool]] = set()
        oldest_first: list[dict] = []  # 时间从旧到新（更旧的新内容整体排在前面）
        empty_streak = 0
        max_screens = min(40, 2 + count)
        try:
            for _ in range(max_screens):
                root = self.adb.dump_ui()
                items = self._chat_bubbles(root, include_own=True)
                new_items: list[dict] = []
                for it in items:
                    if skipped(it):
                        continue
                    key = (it["text"], it["is_own"])
                    if key in seen_prev:
                        continue
                    new_items.append(it)
                for it in items:
                    if skipped(it):
                        continue
                    seen_prev.add((it["text"], it["is_own"]))
                if new_items:
                    # 向下翻页 = 露出更早内容（从屏幕上方进入）-> 整体比已收集内容更旧
                    oldest_first[0:0] = new_items
                    empty_streak = 0
                    if len(oldest_first) >= count:
                        break
                else:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break  # 已到顶部 / 没有更多历史
                # 真机实测：手指自屏幕中上滑到下方 = 看更早消息
                self.adb.swipe(x, int(screen_h * 0.25), x, int(screen_h * 0.85), 350)
                time.sleep(1.0)
            return oldest_first[-count:]
        except Exception as e:  # noqa: BLE001 读取历史失败不致命
            self.log("warning", f"读取历史消息异常: {e}")
            return oldest_first[-count:]

    def _is_system_msg(self, text: str) -> bool:
        """桥接系统提示消息（送达确认等）按前缀识别，防止被当成接收消息转发。

        空前缀要过滤掉：`str.startswith("")` 恒为真，会把所有消息都当成系统提示。
        """
        prefixes = self.ui.get("system_msg_prefixes", ["发送成功", "发送失败"])
        return any(str(p).strip() and str(text).startswith(str(p)) for p in prefixes)

    @staticmethod
    def _parent(node, root: ET.Element):
        for p in root.iter("node"):
            if node in list(p):
                return p
        return None

    @staticmethod
    def _bounds(node) -> tuple[int, int, int, int] | None:
        return ADBController.node_bounds(node) if node is not None else None

