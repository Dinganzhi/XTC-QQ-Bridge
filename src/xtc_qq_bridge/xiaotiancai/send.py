# -*- coding: utf-8 -*-
"""发送相关：发送消息、发送判定、坐标缓存与预输入（原 xiaotiancai.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .constants import *  # noqa: F401,F403


class SendMixin:
    """发送相关：发送消息、发送判定、坐标缓存与预输入"""

    def chat_input_text(self) -> str:
        """读取聊天输入框当前文本；读不到返回 ''。"""
        try:
            root = self._dump_fast()
        except AdbError:
            return ""
        edit = self._find_input(root)
        if edit is None:
            return ""
        return self._input_text_of(edit)

    def input_verifier(self, text: str):
        """给 adb.input_text 用的校验回调：输入框里已出现目标文本，或发送按钮已出现
        （小天才 App 只在输入框有内容时才显示发送按钮）即视为注入成功。"""
        needle = (text or "").strip()

        def _verify() -> bool:
            try:
                root = self._dump_fast()
                self._last_verify_root = root        # 供 _send_once 复用，省一次 dump
            except AdbError:
                return True  # dump 失败无法判定，不阻塞发送流程
            edit = self._find_input(root)
            if edit is not None:
                cur = self._input_text_of(edit)
                if needle and needle in cur:
                    return True
                if cur.strip():
                    # 输入框里有内容但和目标不一致（例如剪贴板粘错），
                    # 说明注入通道是通的，由调用方清空重输，不在这里当作失败
                    self.log("warning", f"输入框内容与预期不一致: {cur[:40]!r}")
                    return True
                return False
            # 没有输入框（切走了/语音模式）：退而看发送按钮是否出现
            return self._find_send(root) is not None

        return _verify

    def _clear_chat_input(self, root: ET.Element | None = None) -> None:
        """清空聊天输入框，避免上一次失败残留的内容被拼在新消息前面。
        root 传入时复用调用方的界面快照，少 dump 一次。"""
        if root is None:
            try:
                root = self._dump_fast()
            except AdbError:
                return
        edit = self._find_input(root)
        if edit is None:
            return
        cur = self._input_text_of(edit).strip()
        if not cur:
            return
        self.adb.tap_element(edit)
        time.sleep(self._delay)
        self.adb.clear_text_field()
        time.sleep(0.3)
        left = self.chat_input_text().strip()
        if left:
            for _ in range(len(left) + 5):   # 兜底：逐字符删除
                self.adb.keyevent(67)
            time.sleep(0.2)

    def _send_fail_markers(self) -> tuple:
        return tuple(self.ui.get("send_fail_markers",
                                 ["发送失败", "发送不成功", "未发送", "网络异常", "网络不可用",
                                  "发送异常", "重发", "无法发送"]))

    def _fail_signature(self, root: ET.Element) -> list[str]:
        """界面上"发送失败"类提示的文本清单（用于发送前后对比，识别**新出现**的失败提示）。"""
        markers = self._send_fail_markers()
        out: list[str] = []
        for n in root.iter("node"):
            blob = ((n.get("text") or "") + " " + (n.get("content-desc") or "")).strip()
            if not blob:
                continue
            if any(m in blob for m in markers):
                out.append(blob)
            elif _SEND_FAIL_TITLE in blob and ("失败" in blob or "重发" in blob):
                out.append(blob)
        return out

    @staticmethod
    def _counter(items) -> dict:
        out: dict = {}
        for it in items:
            out[it] = out.get(it, 0) + 1
        return out

    def _own_bubble_counter(self, root: ET.Element) -> dict:
        """界面上"自己发的"消息气泡文本计数（用于确认新气泡真的出现了）。"""
        try:
            items = self._chat_bubbles(root, include_own=True)
        except Exception:  # noqa: BLE001 解析失败不影响发送
            return {}
        return self._counter(it["text"] for it in items if it["is_own"])

    def _has_new_own_bubble(self, root: ET.Element, text: str, baseline: dict) -> bool:
        """发送后是否出现了包含目标文本的**新**己方气泡（避免上次同文本误判）。"""
        needle = (text or "").strip()
        if not needle:
            return False
        first_line = needle.splitlines()[0].strip()[:20] or needle[:20]
        after = self._own_bubble_counter(root)
        for blob, cnt in after.items():
            if blob.startswith(self._send_fail_markers()) or self._is_system_msg(blob):
                continue
            if (needle in blob or (first_line and first_line in blob)) \
                    and cnt > baseline.get(blob, 0):
                return True
        return False

    def send_message(self, text: str) -> bool:
        """发送消息并**确认真实结果**（不再"发失败也报成功"）。

        返回 True 仅当能确认消息已交出去：
        - 出现包含该文本的**新**己方气泡；或
        - 输入框已不再包含该文本，且没有出现新的"发送失败"类提示。
        读不到界面、找不到输入框、出现新的失败提示 -> 返回 False（调用方如实回报失败）。

        重试策略：只有"输入框里仍留着这段文本"（点击发送没生效）才安全重发，
        最多 xiaotiancai.ui.send_retries 轮；无法确认时绝不重发（避免重复消息）。
        """
        if not text or not text.strip():
            return False
        last_reason = ""
        for attempt in range(1, self._send_retries + 1):
            ok, reason = self._send_once(text)
            if ok:
                return True
            last_reason = reason
            self.log("warning", f"发送未确认（{reason}）"
                                + (f"，准备重试 {attempt}/{self._send_retries}" if attempt < self._send_retries else ""))
            if "输入框仍留有内容" not in reason:
                break  # 其他情况重发有重复风险，直接如实报失败
            time.sleep(self._delay)
        self.log("error", f"发送失败：{last_reason or '未知原因'}"
                          "（请检查小天才 App 聊天窗口与手表网络）")
        return False

    def _send_once(self, text: str) -> tuple[bool, str]:
        """执行一轮"读界面 -> 清残留 -> 注入 -> 点发送 -> 确认"。返回 (是否确认成功, 说明)。

        速度（WSA 上一次 uiautomator dump 约 3 秒，所以**数 dump 次数**才是关键）：
        - 发送前的基线优先复用轮询刚 dump 的那份快照（少 1 次）；
        - 布局没变时走**快路径**：直接广播注入 + 点缓存的发送按钮坐标，
          再用一次 dump 同时确认"注入成功 + 发送成功"（少 1 次）；
        - 快路径不成立/没确认成功时退回"注入并校验"的稳妥流程（首次发送、布局变了、
          或注入没生效时走这条），结果照样如实判定。
        """
        try:
            # 0) 先确保屏幕是亮的：WSA 大约每分钟就会把虚拟屏睡一次（stayOn 也挡不住），
            #    息屏时 dump 必失败（null root node），整条发送就要多花 10 秒以上。
            #    这一步只花 ~0.2 秒，能把"发送撞上息屏"的概率压到很低。
            try:
                self.adb.wake_if_asleep()
            except Exception:  # noqa: BLE001 唤醒失败也照常往下走
                pass
            # 1) 发送前基线：优先复用很新的界面快照（轮询每 2 秒 dump 一次）
            root = self.recent_snapshot()
            if root is None or self._find_input(root) is None:
                root = self._dump_with_retry(2)   # 读不到界面（界面不空闲）时自动收键盘重试
                if root is None or self._find_input(root) is None:
                    # 找不到输入框：可能有弹窗盖住了聊天页，先清理再重读界面
                    if self._dismiss_blockers():
                        root = self._dump_with_retry(1)
            if root is None:
                return False, "界面读取失败，无法确认聊天页状态"
            input_node = self._find_input(root)
            if input_node is None:
                # 语音模式没有输入框：单击左侧图标切到文字模式
                if self._switch_to_text_mode(root):
                    self.log("info", "已在语音模式，切换到文字输入")
                    time.sleep(self._delay)
                    try:
                        root = self._dump_fast()
                    except AdbError:
                        return False, "界面读取失败（切换文字模式后）"
                    input_node = self._find_input(root)
            if input_node is None:
                return False, "未找到输入框（请确认当前在聊天页）"
            # 发送前基线：旧的失败提示 / 已有己方气泡（用于识别"新出现"的失败与新气泡）
            input_bounds = self._bounds(input_node)
            base_fail = self._fail_signature(root)
            base_own = self._own_bubble_counter(root)
            self._clear_chat_input(root)
            self._focus_input(input_node)
            # 2) 快路径：布局与上次一致 -> 直接用缓存的发送按钮坐标
            point = self._cached_send_point(input_bounds) if self._fast_send else None
            lost_focus = False                 # 快路径失败时可能点开了别的面板 -> 需要重新聚焦
            if point is not None and self.adb.input_text_plain(text):
                time.sleep(0.25)
                self.adb.tap(point[0], point[1])
                ok, why = self._confirm_sent(text, base_fail, base_own)
                if ok:
                    return True, why
                # 只有"输入框仍留有内容"才能安全重来（说明没发出去）；
                # 其它情况（气泡/界面不明）不重试，免得把消息发两遍。
                if "输入框仍留有内容" not in why:
                    return False, why
                self.log("debug", f"快路径发送未确认（{why}），改用带校验的流程重试")
                self._send_cache = None
                # 盲点可能点到了别的控件（例如打开了表情面板）：先清一遍弹层，
                # 再重读界面按残留处理，避免拼在新消息前面。
                self._dismiss_blockers()
                try:
                    root = self._dump_fast()
                    fresh_input = self._find_input(root)
                    if fresh_input is not None:
                        self._clear_chat_input(root)
                        input_node = fresh_input
                        input_bounds = self._bounds(fresh_input)
                        lost_focus = True          # 中间动过界面 -> 稳妥流程要重新聚焦
                except AdbError:
                    pass
            elif point is not None:
                self.log("debug", "纯注入广播未发出，改用带校验的注入流程")
            # 3) 稳妥路径：注入并校验（这次 dump 同时用来找发送按钮），点完再确认
            if lost_focus:
                # 中间没有动过界面时**不重复聚焦**（那是一次 tap + 等待，白花 ~0.5 秒）
                self._focus_input(input_node)
            if not self.adb.input_text(text, verify=self.input_verifier(text)):
                # 注入失败最常见的原因是"输入框没拿到输入连接"（点一下没生效、
                # 输入法没挂上）。这时反复发 ADBKeyBoard 广播是没用的（没人接收），
                # 必须重新点击输入框拿到焦点后再注入一次。
                self.log("warning", "文本注入未生效，重新聚焦输入框后再注入一次")
                try:
                    fresh = self._dump_fast()
                except AdbError:
                    fresh = root
                self._focus_input(self._find_input(fresh) or input_node)
                if not self.adb.input_text(text, verify=self.input_verifier(text)):
                    return False, "文本注入失败（输入框未收到内容）"
            time.sleep(0.3)
            # 复用注入校验时那次界面快照来找发送按钮，省掉一次 dump（WSA 上约 3 秒）
            send_root = self._last_verify_root
            self._last_verify_root = None
            if send_root is None:
                try:
                    send_root = self._dump_fast()
                except AdbError:
                    send_root = root
            send_node = self._find_send(send_root)
            if send_node is None:
                # 快照可能是息屏/过渡帧里读到的（没有发送按钮）：先唤醒再重读一次，
                # 别拿旧快照去点，也别糊里糊涂按回车（实测这一下能省一整轮 10 秒）。
                try:
                    if self.adb.wake_if_asleep():
                        again = self._dump_with_retry(1)
                        if again is not None:
                            send_root = again
                            send_node = self._find_send(send_root)
                except Exception:  # noqa: BLE001 重读失败就按原逻辑兜底
                    pass
            if send_node is None:
                self.log("warning", "未找到发送按钮，改用回车发送")
                self.adb.keyevent(66)  # KEYCODE_ENTER
            else:
                self._tap_send(send_node, send_root)
                self._remember_send_point(input_bounds, send_node, send_root)
            return self._confirm_sent(text, base_fail, base_own)
        except AdbError as e:
            return False, f"ADB 异常: {e}"

    @staticmethod
    def _same_bounds(a, b, tol: int = 12) -> bool:
        """两处控件位置是否基本一致（允许 tol 像素误差）。"""
        if not a or not b or len(a) != 4 or len(b) != 4:
            return False
        try:
            return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))
        except (TypeError, ValueError):
            return False

    def _cached_send_point(self, input_bounds):
        """缓存的发送按钮可点坐标；**仅当输入框位置与缓存时一致**才返回

        为什么要这个判断：WSA 窗口能被缩放/最大化，布局一变，缓存下来的绝对坐标
        可能落到别的控件上（例如"更多"）。所以：
          * 输入框位置一致 -> 直接用缓存坐标（最稳）；
          * 只有**输入框高度**变了（多行文字 / 键盘收起）-> 按"相对输入框右下角的偏移"
            换算（发送按钮就在输入框右下角外侧），实测多行消息就属于这种，以前会直接
            放弃快路径、退回两次 dump 的稳妥流程；
          * 宽度也变了（窗口被缩放）或偏移离谱 -> 返回 None，退回稳妥流程，宁可慢也不乱点。

        已知残余风险（可接受）：位置比对用的是**最近快照**，所以"刚缩放完窗口、还没
        轮到下一次轮询 dump 就发消息"这一种情况仍可能按旧坐标点一次；点完的确认 dump
        会发现没发出去（文字还在框里），随后**就着快照补点一次**（`_retap_send`），
        仍不行才清缓存退回稳妥流程 —— 最坏是多花一次 dump，不会静默发错。
        """
        cache = self._send_cache
        if not cache or not input_bounds:
            return None
        point = cache.get("point")
        cached_in = cache.get("input")
        if not point or not cached_in:
            return None
        try:
            w, h = self.adb.get_screen_size()
        except Exception:  # noqa: BLE001 拿不到屏幕尺寸就不冒险
            return None
        if self._same_bounds(cached_in, input_bounds):
            x, y = int(point[0]), int(point[1])
            return (x, y) if (0 <= x <= int(w) and 0 <= y <= int(h)) else None
        # 输入框位置对不上：**先看是不是"输入框变高了"**（多行文字 / 键盘收起）。
        # 发送按钮固定在输入框右下角外侧，所以按"相对输入框右下角的偏移"换算仍然准；
        # 但窗口被缩放时**宽度**也会变，那种情况必须放弃（绝对坐标会落到别的控件上）。
        cx1, cy1, cx2, cy2 = (int(v) for v in cached_in)
        ix1, iy1, ix2, iy2 = (int(v) for v in input_bounds)
        cw, iw = max(1, cx2 - cx1), max(1, ix2 - ix1)
        if abs(iw - cw) > 0.25 * cw:
            return None                        # 宽度都变了 = 窗口被缩放，别猜
        dx = int(point[0]) - cx2                # 按钮相对输入框右边
        dy = int(point[1]) - cy2                # 按钮相对输入框下边（一般是个小负数）
        if not (-24 <= dx <= 200 and -90 <= dy <= 60):
            return None                        # 偏移太离谱 = 布局不是这一种，别猜
        x, y = ix2 + dx, iy2 + dy
        if not (0 <= x <= int(w) and 0 <= y <= int(h)):
            return None
        self.log("debug", f"输入框高度变了（{cy2 - cy1} -> {iy2 - iy1}），"
                          f"按相对偏移换算发送按钮坐标 -> ({x}, {y})")
        return (x, y)

    def _remember_send_point(self, input_bounds, send_node, root: ET.Element) -> None:
        """记下这次点成功的发送按钮坐标 + 输入框坐标，供下次"先手打字"用。"""
        try:
            target = send_node
            if str(send_node.get("clickable", "")).lower() != "true":
                target = self._clickable_ancestor(send_node, root) or send_node
            center = self.adb.node_center(target) or self.adb.node_center(send_node)
            if center and input_bounds:
                x1, y1, x2, y2 = (int(v) for v in input_bounds)
                self._send_cache = {
                    "input": (x1, y1, x2, y2),
                    "point": (int(center[0]), int(center[1])),
                    "input_point": ((x1 + x2) // 2, (y1 + y2) // 2),
                }
                self._save_send_cache()   # 落盘：重启后第一条消息也能走"先手打字"
        except Exception as e:  # noqa: BLE001 缓存失败不影响发送
            self.log("debug", f"发送按钮坐标缓存失败: {e}")

    def chat_title(self, root: ET.Element | None = None) -> str:
        """当前聊天页的标题（联系人名）。用于确认"现在打开的确实是目标联系人"。"""
        try:
            if root is None:
                root = self._dump_fast()
            for n in root.iter("node"):
                if self._id_tail(n) in ("tv_titleBar_title", "tv_title", "chat_title"):
                    t = (n.get("text") or "").strip()
                    if t:
                        return t
                    d = (n.get("content-desc") or "").strip()
                    if d.startswith("和") and d.endswith("的聊天"):
                        return d[1:-3].strip()
        except Exception:  # noqa: BLE001 标题读不到就算了（调用方据此不放行快发）
            pass
        return ""

    def blind_send_ready(self) -> bool:
        """能不能"先手打字"：需要上次成功发送留下的坐标缓存 + 一份可用的界面快照。

        坐标缓存由**上一次真正发成功**的发送留下（重启后第一条走稳妥流程），
        快照用轮询刚 dump 的那份（只为拿"发送前基线"，不做任何 UI 操作）。
        `ui.blind_send: false` 可彻底关掉这条快路径。
        """
        if not self._blind_enabled:
            return False
        cache = self._send_cache or {}
        if not cache.get("point") or not cache.get("input_point"):
            return False
        return self.recent_snapshot() is not None

    def begin_blind_send(self, text: str) -> tuple[bool, str]:
        """先手把文字打进去（**不占桥接的操作锁、不读界面**）：文字 ~1 秒内出现在输入框。

        为什么快：稳妥流程要先等轮询那次 ~3.8 秒的 dump（操作锁）再自己读一次界面，
        文字才被注入；这里直接用缓存坐标点输入框 -> 广播注入 -> 点缓存坐标的发送按钮。
        正确性靠两点保证：
          * 调用方（桥接）只在"最近一次轮询确认过就是目标聊天页"时才走这条（含标题校验）；
          * 随后的 `end_blind_send()` 会用一次 dump 复核，失败就退回稳妥流程 + 清缓存。
        """
        cache = self._send_cache or {}
        point_in = cache.get("input_point")
        point_send = cache.get("point")
        if not point_in or not point_send:
            return False, "还没有发送坐标缓存（先走稳妥流程建立）"
        base = self.recent_snapshot()
        if base is None:
            return False, "没有可用的界面快照（拿不到发送前基线）"
        self._blind = {
            "text": text,
            "base_fail": self._fail_signature(base),
            "base_own": self._own_bubble_counter(base),
            "retryable": False,
        }
        try:
            # 屏可能刚睡过去（WSA 随宿主窗口状态休眠虚拟屏）：先确认点亮，
            # 否则点下去是打在 WSA 主屏的空壳上、文字进不了输入框（白费一轮复核）。
            try:
                self.adb.wake_if_asleep()
            except Exception:  # noqa: BLE001 唤醒失败也照常试
                pass
            self.adb.tap(point_in[0], point_in[1])          # 点输入框拿焦点
            time.sleep(min(self._delay, 0.4))
            if not self.adb.input_text_plain(text):         # 广播注入（不校验）
                self._blind = None
                return False, "纯注入广播未发出"
            time.sleep(0.15)
            self.adb.tap(point_send[0], point_send[1])      # 点发送（缓存坐标）
            self._blind["retryable"] = True
            return True, ""
        except AdbError as e:
            self._blind = None
            return False, f"ADB 异常: {e}"

    def end_blind_send(self, text: str) -> tuple[bool, str, bool]:
        """复核"先手打字"的结果：返回 (是否确认发出, 说明, 失败时能否安全重发)。

        用一次 dump 同时判断：出现新己方气泡（最强证据）/ 出现新失败提示 /
        输入框仍留有内容（没发出去，可安全重发）/ 输入框已清空且无新失败提示。

        文字还留在输入框里时**先就着快照补点一次发送**（见 `_retap_send`）：实测那多半只是
        那一下没点中（输入框变高了/按钮位置变了），补点一次就出去了 —— 比回退到
        "清空 + 重新注入 + 重新找按钮"的稳妥流程快 5~8 秒。
        """
        blind = self._blind or {}
        self._blind = None
        if not blind:
            return False, "没有待复核的先手发送", False
        base_fail = blind.get("base_fail") or []
        base_own = blind.get("base_own") or {}
        try:
            ok, why = self._confirm_sent(text, base_fail, base_own)
            retryable = "输入框仍留有内容" in (why or "")
            if not ok and retryable:
                ok, why = self._retap_send(text, base_fail, base_own)
                retryable = (not ok) and ("输入框仍留有内容" in (why or ""))
        except AdbError as e:
            return False, f"复核失败: {e}", False
        if not ok and retryable:
            # 补点还是没发出去（文字仍在输入框里）：清掉缓存坐标，让稳妥流程重来
            self._send_cache = None
            self._save_send_cache()
        return ok, why, retryable

    def _load_send_cache(self) -> None:
        """从磁盘恢复上次学到的发送按钮坐标（拿不到就照旧现场学）。"""
        if not self._cache_path:
            return
        try:
            import json
            import os
            if not os.path.exists(self._cache_path):
                return
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            inp = tuple(data.get("input") or ())
            pt = tuple(data.get("point") or ())
            ip = tuple(data.get("input_point") or ())
            if len(inp) == 4 and len(pt) == 2:
                self._send_cache = {
                    "input": tuple(int(v) for v in inp),
                    "point": tuple(int(v) for v in pt),
                    "input_point": (tuple(int(v) for v in ip) if len(ip) == 2
                                    else ((inp[0] + inp[2]) // 2, (inp[1] + inp[3]) // 2)),
                }
                self.log("debug", f"已恢复发送按钮坐标缓存: {data}")
        except Exception as e:  # noqa: BLE001 缓存坏了不影响功能（大不了重新学）
            self.log("debug", f"读取发送按钮坐标缓存失败: {e}")

    def _save_send_cache(self) -> None:
        """把发送按钮坐标缓存落盘（下次启动直接用；用前仍会校验输入框位置/屏幕尺寸）。"""
        if not self._cache_path:
            return
        try:
            import json
            import os
            data = {
                "input": [int(v) for v in self._send_cache["input"]],
                "point": [int(v) for v in self._send_cache["point"]],
                "input_point": [int(v) for v in (self._send_cache.get("input_point") or ())],
            }
            os.makedirs(os.path.dirname(self._cache_path) or ".", exist_ok=True)
            tmp = self._cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._cache_path)
        except Exception as e:  # noqa: BLE001 落盘失败不影响功能
            self.log("debug", f"写入发送按钮坐标缓存失败: {e}")

    def learn_send_point(self, probe: str = "a") -> bool:
        """启动时用一次"探针注入"学出发送按钮坐标（不会发出任何消息）。

        为什么需要：先手打字靠**发送按钮的坐标**，而它只在输入框有内容时才出现 ——
        不先学一次的话，**重启后的第一条** QQ 消息只能走稳妥流程（两次 dump）。
        这里往输入框注入一个探针字符 -> dump 一次 -> 记下输入框与发送按钮坐标 ->
        立刻清空输入框。全程只输入、不点发送，所以不会有任何消息被发出去。
        """
        try:
            # 探针注入可能因为"输入框刚点、输入法还没挂上"而白注一次（实机踩过：启动时
            # 学坐标失败 -> 重启后的第一条只能走稳妥流程，多花十几秒），所以重试一轮。
            for attempt in range(2):
                if self._learn_send_point_once(probe):
                    return True
                self.log("debug", f"第 {attempt + 1} 次学习发送按钮坐标未成功，重试一次")
                time.sleep(0.5)
            self.log("debug", "未能学出发送按钮坐标（首次发送仍走稳妥流程）")
            return False
        except Exception as e:  # noqa: BLE001 学不到就退回稳妥流程
            self.log("debug", f"学习发送按钮坐标失败: {e}")
            return False

    def _learn_send_point_once(self, probe: str = "a") -> bool:
        """学一次发送按钮坐标（只注入探针、不点发送，学完立刻清空输入框）。"""
        try:
            root = self.recent_snapshot() or self._dump_with_retry(1)
            edit = self._find_input(root) if root is not None else None
            if edit is None:
                return False
            bounds = self._bounds(edit)
            self._focus_input(edit)
            if not self.adb.input_text_plain(probe):
                return False
            time.sleep(0.25)
            snap = self._dump_with_retry(1)          # 有内容了 -> 发送按钮出现
            if snap is None:
                return False
            # 探针没进输入框（刚启动时输入法常常还没挂上，实机踩过）：改用**带校验的完整
            # 策略链**再试一次（含剪贴板兜底）。学不到坐标不影响正确性，只是第一条会慢一些。
            cur = self._find_input(snap)
            if cur is not None and probe not in (self._input_text_of(cur) or ""):
                self.log("debug", "探针未进入输入框，改用带校验的注入流程再试一次")
                if not self.adb.input_text(probe, verify=self.input_verifier(probe)):
                    self._clear_chat_input(self._dump_with_retry(1) or snap)
                    return False
                snap = self._last_verify_root or self._dump_with_retry(1) or snap
                self._last_verify_root = None
            send_node = self._find_send(snap)
            if send_node is not None and bounds:
                self._remember_send_point(bounds, send_node, snap)
            # 清掉探针字符：绝不留内容在输入框里（否则会被拼进下一条消息）
            self._clear_chat_input(self._dump_with_retry(1) or snap)
            done = self._send_cache is not None
            if done:
                self.log("info", "已学出发送按钮坐标（预输入可用）")
            return done
        except Exception as e:  # noqa: BLE001 学不到就退回稳妥流程
            self.log("debug", f"学习发送按钮坐标失败: {e}")
            return False

    def _focus_input(self, edit) -> None:
        """点击输入框让它获得焦点，并等软键盘/输入连接就绪。

        输入框没有焦点时，ADBKeyBoard 的广播会"发出去了但没人接收"，
        表现就是"文本注入失败（输入框未收到内容）"。
        """
        if edit is None:
            return
        self.adb.tap_element(edit)
        time.sleep(self._delay)

    def _confirm_sent(self, text: str, base_fail: list, base_own: dict) -> tuple[bool, str]:
        """发送后确认（通常只 dump 一次，约 3 秒）。返回 (是否确认发出, 说明)。

        第一次 dump 前先等 `_confirm_delay`：点发送后 App 要一点时间才把气泡画出来，
        等太短会白 dump 一次（WSA 上一次 ~3 秒，白等就变成"发一条要十几秒"）。
        后面几次只在前一次证据不足时才继续。

        每次 dump 的**结果与耗时**都记下来：多花了一次 dump 就在 INFO 里写一行说明，
        "到底慢在哪一步"从此不用猜（实测最常见的就是第一次 dump 撞上界面还没稳定而失败）。
        """
        last = "界面读取失败"
        base_fail_cnt = self._counter(base_fail)
        notes: list[str] = []
        for i in range(3):
            time.sleep(self._confirm_delay if i == 0 else 0.5)
            t0 = time.monotonic()
            try:
                root = self._dump_fast()
            except AdbError as e:
                last = f"界面读取失败({e})"
                notes.append(f"第{i + 1}次 dump 失败（{time.monotonic() - t0:.1f}s）")
                continue
            self._last_confirm_root = root      # 供"补点一次发送"复用，省一次 dump
            # 新出现的"发送失败"提示 = 明确失败
            now_fail = self._counter(self._fail_signature(root))
            new_fail = [k for k, v in now_fail.items() if v > base_fail_cnt.get(k, 0)]
            if new_fail:
                return False, f"出现发送失败提示（{new_fail[0][:30]}）"
            if self._has_new_own_bubble(root, text, base_own):
                return True, self._confirm_note("已出现己方消息气泡", i, notes)
            edit = self._find_input(root)
            if edit is None:
                last = "未找到输入框，无法确认"
                notes.append(f"第{i + 1}次没读到输入框（{time.monotonic() - t0:.1f}s）")
                continue
            cur = self._input_text_of(edit)
            needle = (text or "").strip()
            if needle and cur and needle in cur:
                return False, "输入框仍留有内容"
            # 输入框已清空且没有新的失败提示：视为已发出（App 点发送后立即清空输入框）
            return True, self._confirm_note("输入框已清空且无失败提示", i, notes)
        return False, last + ("（" + "；".join(notes) + "）" if notes else "")

    def _confirm_note(self, why: str, attempt: int, notes: list) -> str:
        """确认成功时，若不止 dump 了一次就写一行 INFO（便于用户/排查看"慢在哪"）。"""
        if attempt <= 0 and not notes:
            return why
        detail = f"（用了 {attempt + 1} 次 dump"
        if notes:
            detail += "：" + "；".join(notes)
        detail += "）"
        self.log("info", f"发送确认{detail}")
        return why + detail

    def _retap_send(self, text: str, base_fail: list, base_own: dict) -> tuple[bool, str]:
        """复核发现"文字还留在输入框里"（那次点发送没生效）时：**就着刚才那份快照里的
        发送按钮再点一次**，然后重新确认。

        为什么这么做：回退到稳妥流程要"清空输入框 -> 重新聚焦 -> 重新注入 -> 重新找按钮 ->
        再确认"（多 2~3 次 dump，实测多花 5~8 秒）。实测文字已经在框里、只是那一下没点中，
        直接补点一次就发出去了（顺手把发送按钮的新坐标记进缓存，下次盲打更准）。
        """
        root = self._last_confirm_root
        if root is None:
            return False, "没有可复用的界面快照"
        node = self._find_send(root)
        if node is None:
            return False, "复核快照里没有发送按钮"
        edit = self._find_input(root)
        self._tap_send(node, root)
        if edit is not None:
            self._remember_send_point(self._bounds(edit), node, root)
        self.log("info", "文字仍留在输入框（本次点击未命中），依据现有界面快照补点发送一次")
        return self._confirm_sent(text, base_fail, base_own)

    def _switch_to_text_mode(self, root: ET.Element) -> bool:
        """语音模式 -> 文字模式：单击 iv_left_img_view（实测单击即可切换）。"""
        for n in root.iter("node"):
            if self._id_tail(n) in _SWITCH_TO_TEXT_IDS:
                self.adb.tap_element(n)
                return True
        return False

    def _input_text_of(self, edit) -> str:
        """输入框的**真实**文本：把空输入框显示的占位提示（hint）当成空串。

        实机（WSA + 小天才）测到：输入框为空时 dump 出的 text 是 "发送文字"，
        旧逻辑会把它当作用户残留内容，于是反复清空/影响发送确认判定。
        """
        t = (edit.get("text", "") or "") if edit is not None else ""
        hints = tuple(self.ui.get("input_hint_texts", _DEFAULT_INPUT_HINTS))
        return "" if t.strip() in hints else t

    def _find_input(self, root: ET.Element):
        rid = self.ui.get("input_resource_id", "")
        if rid:
            n = self.adb.find_element(root, resource_id=rid)
            if n is not None:
                return n
        # 特征 id 优先（实测 et_chat_text_content）
        for n in root.iter("node"):
            if self._id_tail(n) == "et_chat_text_content":
                return n
        nodes = self.adb.find_elements(root, class_name="EditText")
        if nodes:
            # 取最靠下的 EditText，通常是聊天输入框
            n = max(nodes, key=lambda n: (self._bounds(n) or (0, 0, 0, 0))[3])
            return n
        return None

    def _find_send(self, root: ET.Element):
        """找"发送"控件。必须**只**认真正的发送控件，不能误认消息状态图标。

        实机（WSA + 小天才，1366x768）实测：
        - 输入框有内容时：`tv_send_view`（TextView text=发送，本身 clickable=false，
          真正可点的是它的父 `fl_chat_text_send`）；
        - 输入框为空时：**没有** tv_send_view；此时旧的 content-desc 子串匹配会命中
          `iv_chat_msg_item_state`（每条消息右侧的"发送中/发送失败"状态图标），
          于是"没输入内容也以为发送按钮存在"——注入其实没成功却继续点它，
          表现为"莫名其妙发送失败"。所以这里排除状态图标，且 desc 必须完全相等。
        """
        rid = self.ui.get("send_resource_id", "")
        if rid:
            n = self.adb.find_element(root, resource_id=rid)
            if n is not None:
                return n
        # 1) 已知发送控件 id（不依赖文案，最稳）
        for n in root.iter("node"):
            tail = self._id_tail(n).lower()
            if any(x in tail for x in _SEND_EXCLUDE_IDS):
                continue
            if any(x in tail for x in _SEND_ID_HINTS):
                return n
        # 2) 精确文本"发送"（不能用 text_contains：会命中输入框"发送文字"提示）
        for t in self.ui.get("send_texts", ["发送"]):
            n = self.adb.find_element(root, text=t)
            if n is not None:
                return n
        # 3) 最后才看 content-desc，且必须完全相等 + 不是消息状态图标
        for t in self.ui.get("send_texts", ["发送"]):
            n = self.adb.find_element(root, content_desc=t, content_desc_contains=False)
            if n is not None and not any(
                    x in self._id_tail(n).lower() for x in _SEND_EXCLUDE_IDS):
                return n
        return None

    def _tap_send(self, send_node, root: ET.Element) -> None:
        """点发送：优先点它自己，不可点则点最近的可点祖先（实测 tv_send_view
        的 clickable=false，父 fl_chat_text_send 才接收点击）。"""
        target = send_node
        try:
            if str(send_node.get("clickable", "")).lower() != "true":
                target = self._clickable_ancestor(send_node, root) or send_node
        except Exception:  # noqa: BLE001 拿不到祖先就点自己
            target = send_node
        self.adb.tap_element(target)

