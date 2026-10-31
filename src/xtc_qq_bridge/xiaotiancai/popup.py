# -*- coding: utf-8 -*-
"""弹窗处理：通用弹窗识别、关闭、权限与更新提示（原 xiaotiancai.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .constants import *  # noqa: F401,F403


class PopupMixin:
    """弹窗处理：通用弹窗识别、关闭、权限与更新提示"""

    def dismiss_blockers(self) -> bool:
        """公开别名：多轮清理所有可识别的弹窗/遮挡，直到界面干净。"""
        return self.settle()

    def settle(self, max_passes: int = 0) -> bool:
        """多轮弹窗清理：权限/无响应/更新/网络/警告/通用对话框/BACK 兜底。
        任何一轮处理了内容就继续下一轮，直到界面干净或达到轮数上限。

        `max_passes` 默认取 `xiaotiancai.popup.max_passes`（6）。轮数上限之外还要
        加一道"原地踏步"闸门：`_popup_tries` 计数只保护通用弹窗兜底那一个分支，其余
        分支都是"点一下就返回 True"，于是同一个点不掉的弹窗（动画未结束/点击被吞/
        点了"稍后再说"又弹回来）会把 6 轮全部用完 —— 实测每轮一次 dumpsys + 一次
        ~3 秒的 dump，合计把操作锁占住约 20 秒，期间 QQ→手表的发送全部排队。
        """
        try:
            max_passes = int(max_passes or self.popup.get("max_passes", 6) or 6)
        except (TypeError, ValueError):
            max_passes = 6
        max_passes = max(1, min(max_passes, 20))
        try:
            delay = max(0.0, float(self.popup.get("pass_delay", 0.5)))
        except (TypeError, ValueError):
            delay = 0.5
        handled_any = False
        last_sig: str | None = None
        i = -1
        for i in range(max_passes):
            if self._dismiss_blockers():
                handled_any = True
                # `_dismiss_blockers` 每轮都会 dump 一次，因此这里的快照是"本轮点击之前"
                # 的界面：两轮完全一样 = 上一次点击毫无效果，再点也点不掉，直接收手。
                sig = self._snapshot_signature()
                if sig is not None and sig == last_sig:
                    self.log("debug", "弹窗点了没有变化（同一个遮挡反复出现），停止重复点击")
                    break
                last_sig = sig
                time.sleep(delay)
                continue
            break
        if handled_any:
            self.log("debug", f"弹窗清理完成（共处理 {i + 1} 轮内的可识别遮挡）")
        return handled_any

    def _snapshot_signature(self) -> str | None:
        """当前界面快照的指纹（用于判断"点了没反应"）；拿不到快照返回 None。"""
        try:
            snap = getattr(self, "_snapshot", None)
            if snap is None:
                return None
            return f"{len(ET.tostring(snap))}:{hash(ET.tostring(snap))}"
        except Exception:  # noqa: BLE001 指纹算不出来就不做这道判断
            return None

    def _dismiss_blockers(self) -> bool:
        """处理单轮可识别的弹窗/遮挡，返回是否处理过。

        覆盖（按优先级）：系统权限 -> 应用无响应/崩溃 -> 通话面板 -> 隐私协议 ->
        更新/评价/公告类（点"以后再说"）-> 网络类（点"重试"，带冷却）->
        小天才警告弹窗 -> 关闭类按钮（id/desc）-> 通用对话框文本按钮 ->
        **通用弹窗兜底（负向按钮/返回键，认结构不认文案）** -> 弹窗窗口 BACK 兜底。

        注意：普通页面的 NAF 节点（图片等）不算遮挡，绝不能按 BACK（会把 App 退到桌面）；
        关闭类按钮只在"弹窗特征"成立时才点，避免误关正常页面。
        """
        focus = self.current_activity()
        low_focus = focus.lower()
        try:
            # 1) 系统权限弹窗（前台/一次性/始终允许等，不同镜像 id 与文案都试）
            if "permissioncontroller" in low_focus or "packageinstaller" in low_focus:
                root = self._dump_fast()
                for rid in _PERMISSION_ALLOW_IDS:
                    btn = self.adb.find_element(root, resource_id=rid)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("info", "已允许系统权限弹窗请求")
                        return True
                for t in _PERMISSION_TEXTS:
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("info", f"已点击权限弹窗按钮: {t}")
                        return True
                return False
            root = self._dump_fast()
            texts_all = "".join((n.get("text") or "") for n in root.iter("node"))
            descs_all = "".join((n.get("content-desc") or "") for n in root.iter("node"))
            # 弹窗判定（认结构/Activity 名，不认文案）；不是弹窗就把"同一弹窗重试计数"清零
            popup = self._looks_like_popup(root, focus)
            if not popup:
                self._popup_key, self._popup_tries = "", 0
            # 2) 应用无响应/崩溃弹窗：优先"等待"（不杀进程），否则关闭
            if any(w in texts_all for w in _ANR_WORDS):
                for t in self.ui.get("anr_wait_texts", list(_ANR_WAIT)):
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("warning", "检测到应用无响应弹窗，已点「等待」继续")
                        return True
                for t in ("确定", "关闭应用", "知道了"):
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None:
                        self.adb.tap_element(btn)
                        self.log("warning", "检测到应用无响应弹窗，已关闭提示")
                        return True
            # 3) 通话面板弹层（视频通话/拨打电话 + 取消；误触 "+" 菜单时出现）
            if "视频通话" in texts_all and "拨打电话" in texts_all:
                cancel = self._first(
                    self.adb.find_element(root, resource_id="com.xtc.watch:id/tv_cancel"),
                    self.adb.find_element(root, text="取消"))
                if cancel is not None:
                    self.adb.tap_element(cancel)
                    self.log("info", "已关闭通话面板弹层")
                    return True
            # 4) 隐私协议/首启温馨提示（点"同意"；必须先于更新/关闭类，否则会点成"不同意"）
            title = self.adb.find_element(root, resource_id="com.xtc.watch:id/tv_title")
            if title is not None and "温馨提示" in title.get("text", ""):
                sure = self.adb.find_element(root, resource_id="com.xtc.watch:id/btn_sure")
                if sure is not None:
                    self.adb.tap_element(sure)
                    self.log("info", "已同意隐私协议（首启弹窗）")
                    return True
            # 5) 已知的"跳过/稍后"类弹窗：点配置里的跳过按钮。
            #    命中条件放宽到"只要是弹窗就试"——小天才的"升级提醒"标题是"升级提醒"、
            #    按钮是"不更新"，以前靠 _UPDATE_WORDS 匹配碰运气，现在结构认出来就够了；
            #    按钮文案仍必须是白名单里的（绝不点"立即安装/立即更新"）。
            if popup or any(w in texts_all for w in _UPDATE_WORDS) \
                    or any(w in descs_all for w in _UPDATE_WORDS):
                for t in self.ui.get("popup_skip_texts", list(_SKIP_TEXTS)):
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None and not self._popup_blocked(btn) \
                            and self._looks_like_dialog(root, focus):
                        self.adb.tap_element(btn)
                        self.log("info", f"已跳过更新/活动类弹窗（点「{t}」）")
                        return True
            # 6) 网络类临时弹窗：带冷却地点"重试"，否则关掉提示
            if any(w in texts_all for w in _NET_WORDS):
                if self._looks_like_dialog(root, focus) and \
                        time.monotonic() - self._net_retry_ts >= 30:
                    for t in _NET_RETRY:
                        btn = self.adb.find_element(root, text=t)
                        if btn is not None:
                            self._net_retry_ts = time.monotonic()
                            self.adb.tap_element(btn)
                            self.log("info", f"网络异常弹窗：已点「{t}」重新尝试")
                            return True
                for t in _NET_DISMISS:
                    btn = self.adb.find_element(root, text=t)
                    if btn is not None and self._looks_like_dialog(root, focus):
                        self.adb.tap_element(btn)
                        self.log("info", f"已关闭网络提示弹窗（点「{t}」）")
                        return True
            # 7) 小天才警告弹窗（取消 / 关闭）
            btn = self.adb.find_element(root, resource_id=_CANCEL_DIALOG_ID)
            if btn is not None:
                self.adb.tap_element(btn)
                self.log("info", "已关闭小天才弹窗")
                return True
            # 8) 关闭类按钮：resource-id 末段命中即点（这些 id 只出现在弹窗/浮层上）
            for n in root.iter("node"):
                tail = self._id_tail(n).lower()
                if tail and tail in _CLOSE_ID_TAILS:
                    self.adb.tap_element(n)
                    self.log("info", f"已点击关闭按钮（id={self._id_tail(n)}）")
                    return True
            # 9) content-desc 关闭类：仅当当前确实像弹窗窗口时
            if self._looks_like_dialog(root, focus):
                for n in root.iter("node"):
                    blob = ((n.get("content-desc") or "") + (n.get("text") or "")).strip()
                    if blob and any(d in blob for d in _CLOSE_DESCS) and len(blob) <= 8:
                        self.adb.tap_element(n)
                        self.log("info", f"已点击弹窗关闭控件（{blob}）")
                        return True
            # 10) 通用对话框文本按钮（按需取确认类或取消类，避免误关）
            clicked = self._tap_any_dialog_button(root, focus)
            if clicked:
                return True
            # 10.5) 通用弹窗兜底（小天才"升级提醒"这类**自定义 Activity 弹窗**：
            #       com.xtc.watch/...popup.activity.CustomActivity14，按钮是
            #       btn_left="不更新" / btn_right="立即安装"）。
            #       以前只认 PopupWindow/Dialog 和固定文案，认不出它，于是弹窗永远
            #       关不掉、消息一直无法读取。这里按"结构"处理，与具体文案无关：
            #       先点负向按钮（不更新/稍后/取消），找不到就按返回键；绝不点正向按钮。
            if popup:
                key = self._popup_signature(root, focus)
                if key != self._popup_key:
                    self._popup_key, self._popup_tries = key, 0
                self._popup_tries += 1
                title = self._popup_title(root) or focus or "(无标题)"
                if self._popup_tries == 1:
                    neg = self._find_popup_negative(root)
                    if neg is not None:
                        label = (neg.get("text") or "").strip() or self._id_tail(neg)
                        self.adb.tap_element(neg)
                        self.log("info", f"已关闭弹窗「{title}」（点「{label}」，未点确认/安装类按钮）")
                        return True
                    self.adb.keyevent(4)
                    self.log("info", f"弹窗「{title}」没有可点的负向按钮，按返回键关闭")
                    return True
                if self._popup_tries == 2:
                    self.adb.keyevent(4)
                    self.log("warning", f"弹窗「{title}」仍在，直接按返回键关闭")
                    return True
                # 点过负向按钮、也按过返回键都关不掉：不再反复点，交给人工（10 分钟提醒一次）
                self._warn_throttled(
                    f"popup_stuck:{key[:48]}",
                    f"弹窗「{title}」无法自动关闭（已尝试负向按钮与返回键）；"
                    "可在 config.yaml -> xiaotiancai.ui.popup_skip_texts 里加上它的跳过按钮文案"
                    "（例如「不更新」「稍后」）", interval=600.0)
                return False
            # 11) BACK 兜底：仅当前台是独立弹窗/对话框窗口时（如 PopupWindow），
            #     普通 Activity 页面即使有 NAF 节点也不按返回，防止 App 退到桌面。
            if "popupwindow" in low_focus or "dialog" in low_focus:
                self.adb.keyevent(4)
                self.log("info", "检测到弹窗窗口，按返回键关闭")
                return True
        except AdbError:
            pass
        return False

    def _looks_like_popup(self, root: ET.Element, focus: str = "") -> bool:
        """是不是"弹窗盖在界面上"（自定义 Activity 弹窗 / Dialog / PopupWindow）。

        判定必须**保守**：误判会把正常页面当弹窗去点按钮/按返回键，把 App 退到桌面。
        强证据（任一成立才算）：
        * 前台 Activity 名里带 popup/dialog/alert（小天才是 ...popup.activity.CustomActivity14）；
        * 同时存在左/右两个对话按钮（btn_left + btn_right，聊天页/列表页不会有）；
        * 存在弹窗容器（ll_center 等）且容器里有对话按钮或"标题+说明"。
        """
        low = (focus or "").lower()
        if any(k in low for k in _POPUP_ACTIVITY_HINTS):
            return True
        tails = {self._id_tail(nd).lower() for nd in root.iter("node")}
        if "btn_left" in tails and "btn_right" in tails:
            return True
        if tails & set(_POPUP_CONTAINER_TAILS):
            buttons = tails & (set(_POPUP_NEG_TAILS) | set(_POPUP_POS_TAILS))
            has_desc = bool(tails & {"desc", "tv_desc", "dialog_desc", "alert_desc"})
            has_title = bool(tails & set(_POPUP_TITLE_TAILS))
            if buttons or (has_title and has_desc):
                return True
        return False

    def _popup_title(self, root: ET.Element) -> str:
        """弹窗标题（用于日志，让用户一眼看出是哪个弹窗）。"""
        for rid in _POPUP_TITLE_TAILS:
            nd = self.adb.find_element(root, resource_id=f"{self.package}:id/{rid}")
            if nd is not None and (nd.get("text") or "").strip():
                return (nd.get("text") or "").strip()[:24]
        return ""

    def _popup_signature(self, root: ET.Element, focus: str = "") -> str:
        """弹窗指纹：Activity + 标题 + 说明前 24 字。用来判断"是不是同一个弹窗"，
        从而限制同一弹窗的重试次数（否则会一直点/一直按返回）。"""
        desc = ""
        for rid in ("desc", "tv_desc", "dialog_desc", "alert_desc", "message"):
            nd = self.adb.find_element(root, resource_id=f"{self.package}:id/{rid}")
            if nd is not None and (nd.get("text") or "").strip():
                desc = (nd.get("text") or "").strip()[:24]
                break
        return f"{focus}|{self._popup_title(root)}|{desc}"

    def _popup_block_texts(self) -> tuple:
        """禁点按钮文案（config -> xiaotiancai.ui.popup_block_texts 可覆盖）。"""
        val = self.ui.get("popup_block_texts")
        if isinstance(val, (list, tuple)) and val:
            return tuple(str(v) for v in val)
        return _POPUP_BLOCK_TEXTS

    def _popup_blocked(self, node) -> bool:
        """这个控件是不是"危险按钮"（点了会真的下载/安装/更新/重启）。

        判定顺序：id 末段是正向按钮 -> 禁点；文案命中禁点词 -> 禁点；
        文案带否定词（不/稍后/取消…）-> 安全。
        **禁点词必须先于否定词**：`_POPUP_SAFE_WORDS` 里有单字 "不"/"暂"/"去"，
        而危险按钮的文案经常同时含它们（"不同意并安装"、"暂不安装，去升级"）。
        先判否定词就会把这类按钮当成安全按钮、真的点下去 —— 一旦触发安装/升级，
        App 版本一变界面结构全废，整条桥接随之失效。宁可少点一次（不处理弹窗），
        也不能点错一次。
        """
        if node is None:
            return True
        if self._id_tail(node).lower() in _POPUP_POS_TAILS:
            return True
        text = (node.get("text") or "").strip()
        if not text:
            return False
        if any(w in text for w in self._popup_block_texts()):
            return True
        return False

    def _find_popup_negative(self, root: ET.Element):
        """找弹窗里"安全的负向按钮"（不更新/稍后/取消），找不到返回 None。

        优先按 id（btn_left/btn_cancel…，小天才会话框的约定是左负右正），
        再按配置/默认的跳过文案。**永远不会返回"立即安装"这类正向按钮。**
        """
        for rid in _POPUP_NEG_TAILS:
            nd = self.adb.find_element(root, resource_id=f"{self.package}:id/{rid}")
            # clickable 属性缺失时按"可点"处理（dump 里通常有，测试构造的树里可能没有）
            if nd is not None and str(nd.get("clickable", "true")).lower() != "false" \
                    and not self._popup_blocked(nd):
                return nd
        for t in self.ui.get("popup_skip_texts", list(_SKIP_TEXTS)):
            nd = self.adb.find_element(root, text=t)
            if nd is not None and not self._popup_blocked(nd):
                return nd
        return None

    def _looks_like_dialog(self, root: ET.Element, focus: str = "") -> bool:
        """是否像"弹窗/对话框"场景：独立弹窗窗口、对话框标题控件、
        或界面上同时出现多个对话框按钮文本。用于给"点关闭/跳过"类动作兜底证据，
        避免在正常聊天/列表页误点。"""
        if self._looks_like_popup(root, focus):
            return True
        low = (focus or "").lower()
        if "popupwindow" in low or "dialog" in low:
            return True
        for n in root.iter("node"):
            if self._id_tail(n).lower() in ("tv_title", "dialog_title", "btn_sure",
                                            "btn_cancel", "tv_cancel", "alert_title"):
                return True
        keys = ("同意", "确定", "知道了", "好的", "确认", "允许", "取消", "关闭", "不同意",
                "以后再说", "稍后再说", "重试")
        hit = 0
        for n in root.iter("node"):
            t = (n.get("text") or "").strip()
            if t in keys:
                hit += 1
                if hit >= 2:
                    return True
        return False

    def _tap_any_dialog_button(self, root: ET.Element, focus: str = "") -> bool:
        """点通用对话框按钮。优先"跳过/取消"类（避免误触下载、更新、支付），
        其次确认类。仅当界面存在"对话框特征"时才动作，避免误点正常界面按钮。"""
        if not self._looks_like_dialog(root, focus):
            return False
        skip_keys = ("以后再说", "稍后再说", "暂不更新", "稍后更新", "下次再说", "暂不升级",
                     "取消", "关闭", "不同意", "暂不", "忽略")
        confirm_keys = ("同意", "确定", "知道了", "好的", "确认", "允许")
        for key in skip_keys:
            n = self.adb.find_element(root, text=key)
            if n is not None and not self._popup_blocked(n):
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        for key in confirm_keys:
            n = self.adb.find_element(root, text=key)
            if n is not None and not self._popup_blocked(n):
                self.adb.tap_element(n)
                self.log("info", f"已点击对话框按钮: {key}")
                return True
        return False

