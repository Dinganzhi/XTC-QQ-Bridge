# -*- coding: utf-8 -*-
"""转发与补投：目标分发、失败补投、内容级护栏（原 bridge.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403


class ForwardingMixin:
    """转发与补投：目标分发、失败补投、内容级护栏"""

    def _recent_fwd_seen(self, contact, text: str) -> bool:
        """同一联系人 + 同一文本，最近是否已经转发过（默认 300 秒内不再重复转发）。

        为什么不能只靠"文本 + 时间标签"当身份：App 的分组时间标签**时有时无**
        （同一屏里同一条消息一会儿读得到 "14:49"、一会儿读不到），标签一变身份就变，
        于是一条失败消息会被当成"新消息"反复转发 —— 用户报的"只有一个群没发出去，
        却连着别的群和私信一起一直转发"就是这个。
        """
        if self._forward_content_guard <= 0:
            return False
        key = ("xtc", contact or "", text or "")
        now = time.monotonic()
        ts = self._recent_fwd.get(key)
        if ts is None:
            return False
        if now - ts > self._forward_content_guard:
            self._recent_fwd.pop(key, None)
            return False
        return True

    def _mark_recent_fwd(self, contact, text: str) -> None:
        if self._forward_content_guard <= 0:
            return
        now = time.monotonic()
        # 顺手清掉过期项，避免字典无限增长
        for k, ts in list(self._recent_fwd.items()):
            if now - ts > self._forward_content_guard:
                self._recent_fwd.pop(k, None)
        self._recent_fwd[("xtc", contact or "", text or "")] = now

    def _has_active_plan(self, contact, text: str) -> bool:
        """是否已有"同一联系人 + 同一文本"的补投计划在跑（有就别再发一遍整条）。"""
        with self._fwd_plan_lock:
            for key, plan in self._fwd_plan.items():
                if key[1] == (contact or "") and key[2] == (text or ""):
                    return True
        return False

    def _forward_suppressed(self, contact, text: str, label: str) -> bool:
        if self._is_media_placeholder(text):
            # 图片/表情的文本都是"图片""表情X"，按文本护栏会把**另一张**图片也挡掉
            # （实测：两张不同的照片，五分钟内第二张被判成"刚转发过"而漏发）。
            # 这类消息的重复判定交给内容哈希（见 _media_hash）。
            return False
        """这条消息现在要不要跳过：正在补投 / 刚转发过同一内容。"""
        if self._has_active_plan(contact, text):
            return True
        return self._recent_fwd_seen(contact, text)

    def _plan_key(self, contact, text: str, label: str) -> tuple:
        """补投计划的身份（**不含标签**）：标签会跳变，补投只认"谁 + 什么内容"。

        计划里单独记着这条消息的显示标签，所以补投出去的时间抬头依然是对的。
        """
        return ("xtc", contact or "", text or "")

    def _order_hold_active(self) -> bool:
        """有图片/表情在等原图、且等的时间还在 `order_hold_secs` 内 -> 后面的消息先压一下。

        为什么：那条图（在等原图）如果被后来的文字"插队"，QQ 里就是**错位**
        （用户实测）。压住的这段时间里消息不会丢：媒体就绪后由补发流程按顺序一起带出去；
        超过 `order_hold_secs` 就照旧先发，不让一条慢图把整条对话卡死。
        """
        if self._order_hold_secs <= 0 or not self._pending_media:
            return False
        now = time.monotonic()
        for st in self._pending_media.values():
            first = float(st.get("first_ts") or now)
            if now - first <= self._order_hold_secs:
                return True
        return False

    def _content_went_backwards(self, contact, text: str, label: str) -> bool:
        """同内容的消息"时间倒流"了 -> 判为重复（标签/时钟漂移的一种兜底）。

        为什么要这层：实测 10-15 16:23 转发过的表情，跨零点后 App 只给裸时间标签，
        被算成"10-16 16:23"（未来）-> 身份变了 -> 又发一遍。裸时间标签的问题已从根上修掉，
        这里再加一道：**同内容 + 时间比上次转发过的更早（超过 60 秒）= 同一条消息的漂移**。
        同一分钟里发两张一样的表情时间相等，不会被误判（这里要求"早 60 秒以上"）。
        """
        epoch = self._label_epoch(label) if label else None
        if epoch is None:
            return False
        last = self._fwd_clock.get((contact or "", text or ""))
        return last is not None and epoch < float(last) - 60.0

    def _remember_fwd_clock(self, contact, text: str, label: str) -> None:
        epoch = self._label_epoch(label) if label else None
        if epoch is None:
            return
        key = (contact or "", text or "")
        prev = self._fwd_clock.get(key)
        if prev is None or epoch > float(prev):
            self._fwd_clock[key] = float(epoch)
        while len(self._fwd_clock) > 200:      # 只留最近若干条，避免无限增长
            self._fwd_clock.pop(next(iter(self._fwd_clock)), None)

    def _stale_limit(self) -> float:
        """当前允许补发的最大年龄（秒）：0 = 不限。

        规则：取 `catchup_max_age` 与"距上次成功读界面已过去的时间 + 60 秒"的较大者 ——
        桥接中断（窗口失去焦点、容器休眠）期间错过的消息都在这个窗口内，不会被当成
        "旧消息"丢掉；而连续运行时仍然只补 `catchup_max_age` 以内的。
        """
        if self._catchup_max_age <= 0:
            return 0.0
        limit = self._catchup_max_age
        if self._last_ok_read_ts:
            gap = time.time() - float(self._last_ok_read_ts)
            if gap > 0:
                limit = max(limit, gap + 60.0)
        return limit

    def _stale_age(self, *labels) -> float | None:
        """这条消息是不是"太旧"了（超过 `catchup_max_age`）？返回年龄（秒）或 None。

        为什么要拦：实测手表 16:38 发的消息到 21:48 才出现在 App 里，桥接照发 —— 用户看到的
        是"九点四十八分才发了个四点半的消息"，而且这条**必然**排在已发过的更新消息后面（错位）。
        默认 30 分钟以内才转发；想全部都要就把 `xiaotiancai.catchup_max_age` 设 0。
        """
        limit = self._stale_limit()
        if limit <= 0:
            return None
        age: float | None = None
        for lb in labels:
            a = self._label_age(lb) if lb else None
            if a is not None:
                age = a if age is None else min(age, a)
        if age is not None and age > limit:
            return age
        return None

    @staticmethod
    def _media_hash(media: dict | None) -> str:
        """媒体的内容哈希（`img:<16 位>`）：**没有时间标签时也能稳定复现的身份**。

        为什么需要：照片气泡经常读不到时间标签，身份就会退回空串，同一条照片会被重复
        转发、时间也会变成转发时刻（实测 21:38 的照片在 23:07 又发一遍、抬头写成 23:07）。
        哈希只看图片内容，与标签无关，天然稳定。
        """
        if not media:
            return ""
        data = media.get("data")
        if not data:
            return ""
        try:
            return "img:" + hashlib.sha1(data).hexdigest()[:16]
        except Exception:  # noqa: BLE001
            return ""

    def _mark_forwarded(self, contact, text: str, label: str, disp: str,
                        media_hash: str = "") -> None:
        """记入长期历史 + 本地消息库（此后轮询不会再把它当成"库里没有"的消息）。"""
        self.history.mark("xtc", contact or "", text, label or "")
        if media_hash:
            # 同一张图片/表情无论标签怎么变，都不再重复转发
            self.history.mark("xtc", contact or "", text, media_hash)
        self._remember_fwd_clock(contact, text, label)
        epoch = self._label_epoch(disp)
        # 消息库去重必须按**这条消息的身份**（文本 + 时间），不能只按文本：
        # 所有照片的文本都是"图片"、同一个表情的名字也一样 —— 只按文本去重的话，
        # 第一条之后的所有图片/表情都会被当成"重复"而不入库，
        # 于是 /小天才 历史消息 里**看不到任何图片和表情**（用户实测）。
        if self._msg_stored(text, epoch):
            return
        self.msgs.append("xtc", contact or "", text, t=epoch,
                         source=self._xtc_source(contact), source_id=contact or "")

    def _msg_stored(self, text: str, epoch: float | None, window: float = 5.0) -> bool:
        """消息库里是否已经有"同文本 + 时间几乎相同"的这条（挡住同一条被标记两次）。

        窗口只给 5 秒：它只用来挡"同一条消息在重试路径里被重复标记"，
        **不按分钟去重** —— 同一分钟里发两张相同的表情、或两张照片，都是两条独立消息。
        """
        try:
            recent = self.msgs.recent(30)
        except Exception:  # noqa: BLE001 读不到就当没有，宁可多存一条也别丢
            return False
        t = text or ""
        for e in recent:
            if (e.get("text") or "") != t:
                continue
            if epoch is None:
                return True                      # 没有时间可用时退回"同文本即已存"
            try:
                if abs(float(e.get("t") or 0) - float(epoch)) <= window:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _schedule_fwd_plan(self, contact, text: str, label: str, disp: str,
                           sticker: dict | None, failed: list, kind: str = "") -> None:
        """登记/更新补投计划：**只记没发出去的目标**。"""
        if not failed:
            return
        if self._forward_max_tries <= 0:
            self._log("error", f"[转发] 有 {len(failed)} 个目标没发出去"
                               f"（{'、'.join(f'{t}:{i}' for t, i in failed)}）：{text[:24]!r}；"
                               "forward.retry_max_tries=0 -> 不补投（只报这一次）")
            return
        key = self._plan_key(contact, text, label)
        gap = self._forward_retry_backoff
        with self._fwd_plan_lock:
            plan = self._fwd_plan.get(key)
            if plan is None:
                plan = {"contact": contact, "text": text, "label": label, "disp": disp,
                        "sticker": sticker, "failed": list(failed), "tries": 0,
                        "kind": kind, "next_ts": time.monotonic() + gap}
                self._fwd_plan[key] = plan
            else:
                plan["failed"] = list(failed)
                plan["sticker"] = sticker or plan.get("sticker")
                plan["kind"] = kind or plan.get("kind") or ""
                plan["tries"] = 0
                plan["next_ts"] = time.monotonic() + gap
        self._log("warning", f"[转发] 有 {len(failed)} 个目标没发出去"
                             f"（{'、'.join(f'{t}:{i}' for t, i in failed)}）：{text[:24]!r}；"
                             f"{int(gap)} 秒后**只重试这些目标**（最多 {self._forward_max_tries} 次，"
                             "已经成功的私聊/群不会重发）")

    def _forward_retry_loop(self) -> None:
        """补投线程：按退避节奏只重试失败的目标，到上限就放弃并明确报错。

        为什么单独一个线程：补投是一次 HTTP（QQ 侧超时可能 30 秒），放在工作线程里会把
        用户新发的 QQ 消息一起堵住；补投**不碰界面**，所以它不需要界面锁。
        """
        while self.running:
            time.sleep(0.5)
            now = time.monotonic()
            with self._fwd_plan_lock:
                due = [k for k, p in self._fwd_plan.items()
                       if now >= float(p.get("next_ts") or 0.0)]
            for key in due:
                try:
                    self._retry_failed_targets(key)
                except Exception as e:  # noqa: BLE001 补投异常不能让线程退出
                    self._log("warning", f"[转发] 补投异常: {e}")

    def _retry_failed_targets(self, key: tuple) -> None:
        """补投一次：**只发上次失败的目标**；到上限就放弃（记历史，不再重试）。"""
        with self._fwd_plan_lock:
            plan = self._fwd_plan.get(key)
            if plan is None:
                return
            plan["tries"] = int(plan.get("tries") or 0) + 1
            # 先占位（成功会删掉、失败会重排），避免同一轮被取两次
            plan["next_ts"] = time.monotonic() + self._forward_retry_backoff * 4
            snapshot = dict(plan)
        text = snapshot.get("text") or ""
        failed = list(snapshot.get("failed") or [])
        tries = int(snapshot.get("tries") or 0)
        if tries > self._forward_max_tries:
            with self._fwd_plan_lock:
                self._fwd_plan.pop(key, None)
            # 放弃：记入历史，轮询不会再拿它重转（失败的目标不再尝试）
            self._mark_forwarded(snapshot.get("contact", ""), text, snapshot.get("label", ""),
                                 snapshot.get("disp", ""))
            self._log("error", f"[转发] 放弃补投（已试 {tries - 1} 次）: {text[:24]!r}；"
                               f"失败目标 {'、'.join(f'{t}:{i}' for t, i in failed)} 仍未发出，"
                               "已停止重试（不会再刷屏）")
            return
        ok, _sent, still_failed = self._forward(
            snapshot.get("contact", ""), text,
            snapshot.get("disp", "") or snapshot.get("label", ""),
            sticker=snapshot.get("sticker"), only=failed,
            kind=snapshot.get("kind") or "")
        if ok:
            with self._fwd_plan_lock:
                self._fwd_plan.pop(key, None)
            self._mark_forwarded(snapshot.get("contact", ""), text, snapshot.get("label", ""),
                                 snapshot.get("disp", ""))
            self._log("info", f"[转发] 补投成功（第 {tries} 次）: {text[:24]!r}")
            return
        with self._fwd_plan_lock:
            cur = self._fwd_plan.get(key)
            if cur is not None and still_failed:
                cur["failed"] = list(still_failed)
        self._log("warning", f"[转发] 补投仍失败（第 {tries}/{self._forward_max_tries} 次）: "
                             f"{text[:24]!r}；"
                             + (f"失败目标 {'、'.join(f'{t}:{i}' for t, i in still_failed)}"
                                if still_failed else "没有可重试的目标"))

    def _queue_confirm_xtc(self, message: str) -> None:
        """小天才聊天里的"发送成功：…"回执 -> 入**普通队列**（低优先级）。

        为什么不再同步做：它要在聊天页里点输入框/打字/点发送（占界面锁好几秒），
        同步做就会把"用户刚在 QQ 发的那条消息"一起堵在后面（实测堵过 20 秒）。
        """
        self._job_queue.put(("confirm", message))

    def _do_forward_job(self, contact: str, text: str, label: str,
                        sticker: dict | None = None, display_label: str = "",
                        kind: str = "", media_hash: str = "") -> None:
        """小天才 -> QQ 的**第一次**尝试（之后由补投线程只补失败的目标）。

        label 是身份标签（去重/历史都用它），display_label 只用于给 QQ 那条消息的抬头时间。
        kind 是这条消息的类型（sticker/image/text，按**气泡本身**判定）。
        三条纪律（用户实测过的坑）：
          * 已有补投计划 / 刚转发过同一内容 -> 直接跳过（标签跳变也不能骗过它）；
          * 有目标成功就立刻记历史：轮询不会再拿这条消息重转（私聊不会被刷屏）；
          * 失败的目标进补投计划：只补它们，退避 + 有上限，不再"每 5 秒一轮"。
        """
        disp = display_label or label
        if self._forward_suppressed(contact, text, label):
            self._log("debug", f"[转发] 这条正在补投或刚转发过，跳过: {text[:24]!r}")
            return
        # 打点必须在这里（"真的要发一次"那一刻）：轮询之后每 2~4 秒就会重新读到这条消息，
        # 靠它挡住"标签跳变 -> 被当成新消息 -> 整条重发"。
        self._mark_recent_fwd(contact, text)
        try:
            ok, sent, failed = self._forward(contact, text, disp, sticker=sticker, kind=kind)
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"转发异常: {e}")
            ok, sent, failed = False, [], []
        if ok:
            self._mark_forwarded(contact, text, label, disp, media_hash=media_hash)
            return
        if sent:
            # 部分成功：**立刻**记历史（否则标签一变就会被当成新消息，重发一遍私聊）
            self._mark_forwarded(contact, text, label, disp, media_hash=media_hash)
        self._schedule_fwd_plan(contact, text, label, disp, sticker, failed, kind=kind)

    def _warm_emoji_store(self) -> None:
        """后台预热表情包名字索引（失败无所谓，收到表情时会按需再建）。"""
        try:
            n = self._emoji_store.warm()
            self._log("debug", f"表情包索引预热完成：{n} 个名字")
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"表情包索引预热失败: {e}")

    def _capture_sticker(self, root, text: str,
                         near_epoch: float | None = None,
                         match_label: str = "", kind: str = "") -> dict | None:
        """取"要发到 QQ 的那张图"（表情包 **和** 照片都走这里）。

        表情包：
          1) **App 数据目录/图片缓存里的原文件**（`emoji_store`）：保真，动图保住动画，
             也不依赖气泡在屏幕上；挑哪张**由像素比对决定** —— 先抠下界面上这张气泡的
             截图当基准，再逐个候选比相似度，够像才发（根治"小猫流汗发成乌龟"）；
          2) 没有可信原图时，**就发这张气泡截图**（静止一帧，但一定是对的）；
          3) 连气泡都定位不到 -> 返回 None，调用方照样发"表情X"文字。

        照片（`text` == `图片`）：
          1) 缓存里的**原图**（大图；形状与气泡一致、写入时间就在消息时间附近才敢用）；
          2) 拿不到就发气泡截图（低清但一定对）—— 照片动辄 2000x3000，纯 Python 解不动
             （实测只解 DC 就要 37 秒），所以这条路不比像素，只认唯一候选。

        near_epoch：补发老消息时传"这条消息自己的时间"，好让缓存查找按消息时间去匹配
        （缓存文件是消息显示时写进来的），否则补发的贴图会退化成静态截图。
        """
        if not self._emoji_image:
            return None
        k = (kind or "").strip().lower()
        if k:
            is_image, is_sticker = (k == "image"), (k == "sticker")
        else:
            # 没有类型信息时退回旧推断（老调用方/脚本补的 kind 为空）
            is_image = (text or "").strip() == getattr(self.xtc, "IMAGE_TEXT", "图片")
            is_sticker = (text or "").strip().startswith("表情")
        if not (is_image or is_sticker):
            # 防呆：文字消息**绝不能**取图。以前实时路径对每条消息都调这里，而下面
            # `sticker_of_latest(root, "")` 会退化成"屏幕上最新那张贴纸"—— 于是发一条
            # "2" 时把上一条消息的表情包一起发了出去（用户报的"附带前面一条消息的表情包"）。
            self._log("debug", f"这条不是表情/图片消息（{text[:24]!r}），不取图")
            return None
        name = (text or "").strip()
        if name.startswith("表情"):
            name = name[len("表情"):].strip()
        # 有的贴纸名字自带扩展名（实机：'表情弹吉他.png'）——查表情包索引前先去掉
        name = re.sub(r"\.(png|gif|webp|jpe?g|apng)$", "", name, flags=re.I).strip()
        # 先在快照里定位气泡：① 拿它的形状去缓存里挑原图 ② 抠它的截图当核对基准/兜底
        def _locate(r):
            try:
                if is_image:
                    return self.xtc.image_of_latest(r, text, match_label=match_label)
                return (self.xtc.sticker_of_latest(r, text, match_label=match_label)
                        or self.xtc.sticker_of_latest(r, ""))
            except Exception as e:  # noqa: BLE001 定位失败不影响取原图
                self._log("debug", f"定位图片/表情气泡失败: {e}")
                return None

        item = _locate(root)
        # 滚动前的快照与位置：万一"滚动取完整图"失败（App 会自动滚回底部，重新定位失败），
        # 就退回这里取图 —— 宁可截半张，也不要让表情只剩一句"[表情] 名字"。
        root0, item0 = root, item
        ref0 = None            # 滚动前先抠一张备用（可能被裁，但内容一定是对的）
        # **气泡被消息列表裁掉时先滚进可视区**：实测贴边那条抠下来只有半张（原图比对
        # 0.78 不过 -> 发出去的是半张 png），往下滚一屏后同一张能到 0.99。
        # 抓完图按原路滚回去（列表停在中间会让轮询读不到新消息）。
        steps: list[int] = []
        vb = None
        if item is not None:
            try:
                vb = self.xtc.chat_view_bounds(root)
            except Exception as e:  # noqa: BLE001 拿不到可视区就按"没被裁"处理
                self._log("debug", f"读消息列表可视区失败（按未裁处理）: {e}")
                vb = None
        clipped0 = bool(item and vb and self.xtc.bubble_clipped(root, item["bounds"]))
        if clipped0:
            try:
                ref0 = self.xtc.capture_sticker(item["bounds"])
            except Exception as e:  # noqa: BLE001 抠图失败就只走滚动那条路
                self._log("debug", f"[媒体] 滚动前预抠图失败: {e}")
                ref0 = None
            if ref0 and imgtool.looks_blank(ref0):
                ref0 = None
        for _ in range(2):
            if not item or not vb or not self.xtc.bubble_clipped(root, item["bounds"]):
                break
            dy = int((vb[3] - vb[1]) * 0.3) or 40
            # 贴着上边 = 上面被裁 -> 内容往下拖；贴着下边 = 下面被裁 -> 内容往上拖
            step = dy if item["bounds"][1] <= vb[1] + 2 else -dy
            if not self.xtc.scroll_chat_by(vb, step):
                break
            steps.append(step)
            fresh = self.xtc._dump_with_retry(1)
            if fresh is None:
                break
            root = fresh
            item = _locate(root)
        if steps:
            self._log("info", f"[媒体] 这条气泡被消息列表裁到了，已滚动 {len(steps)} 次取完整图"
                              "（抓完会滚回底部）")
        if item is None and not is_image:
            # 表情气泡本来定位到了、滚动后却找不到了（App 自动滚回底部 / 被顶走）：
            # **不许按时间猜缓存**（实测这条路上猜出过一张 72x72 的小图），
            # 但可以先回到**滚动前那份快照**把界面上那张抠下来 —— 半张也强于没有图。
            if steps:
                for step in reversed(steps):
                    self.xtc.scroll_chat_by(vb, -step)
            if item0 is not None and ref0:
                # 用**滚动前就抠好的那一张**（那时坐标还是准的）：可能被裁掉一点，
                # 但内容一定是对的；绝不按旧坐标重新抠（列表已经动过了）。
                self._log("info", "[表情包] 滚动取完整图失败，改用滚动前抠下的那张"
                                  "（可能被裁掉一点，但内容是对的）")
                try:
                    return self._capture_media_now(root0, text, name, is_image, item0,
                                                   near_epoch=near_epoch, ref=ref0)
                except Exception as e:  # noqa: BLE001 兜底也失败就放弃
                    self._log("debug", f"[表情包] 用滚动前的图收尾失败: {e}")
            self._log("info", "[表情包] 没能取到这张表情的图，先不取图（稍后重试，避免发错）")
            return None
        try:
            return self._capture_media_now(root, text, name, is_image, item,
                                           near_epoch=near_epoch)
        finally:
            # 滚回原位：逆向、同样次数（列表停在中间会让轮询读不到新消息）
            for step in reversed(steps):
                self.xtc.scroll_chat_by(vb, -step)

    def _capture_media_now(self, root, text: str, name: str, is_image: bool, item,
                           near_epoch: float | None = None,
                           ref: bytes | None = None) -> dict | None:
        """真正取图（气泡已确保完整可见）。拆出来是为了让"滚动 -> 取图 -> 滚回"成对出现。"""
        aspect = None
        if item and item.get("bounds"):
            x1, y1, x2, y2 = item["bounds"]
            if y2 > y1:
                aspect = (x2 - x1) / (y2 - y1)
        # **先把界面上的气泡抠下来**：聊天会自动滚动（新消息/送达确认都会把列表顶上去），
        # 任何慢操作（建表情名索引 3~4 秒、扫缓存）之后再抠，坐标就对不上了 —— 实测
        # 晚几秒抠到的是别处的**空白占位图**，转发出去就是一张灰底问号（用户会以为发错了）。
        if ref is None and item and item.get("bounds"):
            need_ref = True
            if self._emoji_store is not None and name and not is_image:
                try:
                    # 索引已就绪才能"秒判"名字是否唯一；没就绪就直接截图（别为了省一次
                    # 截图去等 4 秒建索引，那样反而抠错位置）
                    if self._emoji_store.index_ready():
                        need_ref = not self._emoji_store.name_is_unique(name)
                except Exception as e:  # noqa: BLE001 判不出来就老老实实截图
                    self._log("debug", f"判断表情名是否唯一失败: {e}")
            if need_ref:
                try:
                    ref = self.xtc.capture_sticker(item.get("bounds"))
                except Exception as e:  # noqa: BLE001 抠图失败就走老路
                    self._log("debug", f"抠气泡截图失败: {e}")
                    ref = None
                if ref and imgtool.looks_blank(ref):
                    # 抠到空白/占位图：先重试一次（界面可能正在重绘）
                    retry = None
                    try:
                        retry = self.xtc.capture_sticker(item.get("bounds"))
                    except Exception as e:  # noqa: BLE001
                        self._log("debug", f"重抠气泡截图失败: {e}")
                    if retry and not imgtool.looks_blank(retry):
                        ref = retry
                    else:
                        dom, std = imgtool.content_stats(ref)
                        self._log("info", f"[表情包] 抠到的气泡像是空白/占位图（主色占比 {dom:.2f}、"
                                          f"亮度标准差 {std:.0f}）")
                        ref = None
        # ---------------- 照片（图片消息） ----------------
        if is_image:
            if self._emoji_photo and self._emoji_store is not None:
                try:
                    bw = item["bounds"][2] - item["bounds"][0] if item else 0
                    bh = item["bounds"][3] - item["bounds"][1] if item else 0
                    # allow_now=True（补发也一样）：App 显示图片时会把原图重新写进缓存，
                    # 所以"正在看的那张"必定刚写过；补发往往只是轮询晚了几秒而已。
                    # 真老的照片没有新鲜文件，自然落到"发气泡截图"兜底。
                    got = self._emoji_store.find_photo(near_epoch=near_epoch, aspect=aspect,
                                                       min_px=max(bw, bh))
                except Exception as e:  # noqa: BLE001 取不到就退回截图
                    self._log("debug", f"找照片原图失败（改用截图）: {e}")
                    got = None
                if got and got.get("data"):
                    self._log("info", f"[图片] 取自图片缓存原图：{got.get('kind', '?')} "
                                      f"{len(got['data'])} 字节（{got.get('w')}x{got.get('h')}）")
                    got["label"] = "图片"
                    return got
            if not item:
                self._log("info", f"[图片] 界面上没找到这条图片消息的气泡（{text!r}），按文字转发")
                return None
            if ref:
                self._log("info", f"[图片] 按气泡截图 {len(ref)} 字节（{item.get('bounds')}），"
                                  "随转发发给 QQ（缓存里没找到原图）")
                return {"data": ref, "kind": "png", "animated": False,
                        "source": "screenshot", "label": "图片"}
            self._log("info", f"[图片] 未能拿到这张照片（气泡 {item.get('bounds')}），按文字转发")
            return None
        # ---------------- 表情包 ----------------
        if self._emoji_from_data and self._emoji_store is not None:
            try:
                got = self._emoji_store.find(name, near_epoch=near_epoch,
                                             aspect=aspect, reference=ref,
                                             # 看得到气泡却没抠到可用截图 -> 只认能被证明的候选
                                             verified_only=bool(item) and ref is None)
            except Exception as e:  # noqa: BLE001 读原文件失败就走截图
                self._log("debug", f"读表情原文件失败（改用截图）: {e}")
                got = None
            if got and got.get("data"):
                anim = "动图" if got.get("animated") else "静态"
                where = "图片缓存" if got.get("source") == "cache" else "表情包文件"
                score = got.get("score")
                tag = f"，与界面比对 {score:.2f}" if isinstance(score, (int, float)) else ""
                self._log("info", f"[表情包] 取自{where}：{got.get('kind', '?')} {anim} "
                                  f"{len(got['data'])} 字节（{got.get('w')}x{got.get('h')}）{tag}")
                return got
        if ref:
            self._log("info", f"[表情包] 没有可信原图（{name!r}），发界面气泡截图 "
                              f"{len(ref)} 字节（静止一帧）")
            return {"data": ref, "kind": "png", "animated": False, "source": "screenshot"}
        if not item:
            self._log("info", f"[表情包] 界面上没找到这条表情的气泡（{text!r}），按文字转发")
            return None
        try:
            png = self.xtc.capture_sticker(item.get("bounds"))
            if png and not imgtool.looks_blank(png):
                self._log("info", f"[表情包] 按气泡截图 {len(png)} 字节（静态一帧）"
                                  f"（{item.get('bounds')}），随转发发给 QQ")
                return {"data": png, "kind": "png", "animated": False, "source": "screenshot"}
            self._log("info", f"[表情包] 抠到的气泡是空白/占位图（或截图失败），"
                              f"按文字转发（气泡 {item.get('bounds')}）")
            return None
        except Exception as e:  # noqa: BLE001 截图失败不影响文字转发
            self._log("warning", f"[表情包] 取图异常（按文字转发）: {e}")
            return None

