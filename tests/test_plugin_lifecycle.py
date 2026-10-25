"""插件请求生命周期离线测试；假 AstrBot 宿主，不连接 QQ 或设备。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    """加载完整插件源码，仅替代未安装的 AstrBot 宿主 API。"""
    names = (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.core",
        "astrbot.core.message", "astrbot.core.message.message_event_result",
        "astrbot.core.star", "astrbot.core.star.filter", "astrbot.core.star.filter.command",
    )
    modules = {name: types.ModuleType(name) for name in names}

    class Star:
        def __init__(self, context):
            self.context = context

    class Chain:
        def message(self, text):
            return self

    modules["astrbot.api"].star = types.SimpleNamespace(Star=Star, Context=object)
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda *a, **k: lambda fn: fn,
        event_message_type=lambda *a, **k: lambda fn: fn,
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )
    modules["astrbot.core.message.message_event_result"].MessageChain = Chain
    modules["astrbot.core.star.filter.command"].GreedyStr = str
    spec = importlib.util.spec_from_file_location(
        "xtc_plugin_lifecycle_test", ROOT / "astrbot_plugin_xtc_bridge/main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


PLUGIN = load_plugin()


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = PLUGIN.Main(types.SimpleNamespace())
        self.loop = asyncio.get_running_loop()
        self.plugin._loop = self.loop
        self.event = types.SimpleNamespace(
            get_sender_id=lambda: "10001", get_group_id=lambda: "")
        self.tasks = []
        self.loop_errors = []
        self.old_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda loop, context: self.loop_errors.append(context))

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for future in self.plugin._pending_replies.values():
            future.cancel()
        self.plugin._pending_replies.clear()
        # 清理旧实现未追踪的测试协程，避免一个用例污染后续用例。
        remaining = [task for task in asyncio.all_tasks()
                     if task is not asyncio.current_task()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        self.loop.set_exception_handler(self.old_handler)

    async def waiting_request(self):
        started = asyncio.Event()
        request = {}

        async def forward(payload):
            request.update(payload)
            started.set()
            return "OK"

        self.plugin._forward_to_python = forward
        task = self.spawn(self.plugin._post_and_wait(self.event, action="init"))
        await started.wait()
        await asyncio.sleep(0)
        return task, request["request_id"]

    async def test_result_before_post_returns_is_preserved(self):
        async def forward(payload):
            self.plugin._resolve_request(payload["request_id"], "completed")
            await asyncio.sleep(0)
            return "OK"

        self.plugin._forward_to_python = forward
        result = await self.plugin._post_and_wait(self.event, action="auto_login", timeout=0.01)
        self.assertEqual("completed", result)
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_ordinary_result_from_http_thread(self):
        task, request_id = await self.waiting_request()
        await asyncio.to_thread(self.plugin._resolve_request, request_id, "completed")
        self.assertEqual("completed", await asyncio.wait_for(task, 0.2))
        self.assertEqual({}, self.plugin._pending_replies)
        self.assertEqual([], self.loop_errors)

    async def test_duplicate_results_complete_once_without_loop_error(self):
        task, request_id = await self.waiting_request()
        self.plugin._resolve_request(request_id, "first")
        self.plugin._resolve_request(request_id, "duplicate")
        self.assertEqual("first", await asyncio.wait_for(task, 0.2))
        await asyncio.sleep(0)
        self.assertEqual([], self.loop_errors)

    async def test_cancel_between_callback_schedule_and_execution(self):
        future = self.loop.create_future()
        self.plugin._pending_replies["late"] = future
        self.plugin._resolve_request("late", "completed")
        future.cancel()
        await asyncio.sleep(0)
        self.assertEqual([], self.loop_errors)

    async def test_rejections_and_transport_errors_cleanup(self):
        for response, expected in (("IGNORED", "未受理"), ("ERROR: offline", "未连接")):
            with self.subTest(response=response):
                self.plugin._forward_to_python = AsyncMock(return_value=response)
                result = await self.plugin._post_and_wait(self.event)
                self.assertIn(expected, result)
                self.assertEqual({}, self.plugin._pending_replies)

    async def test_empty_ack_is_rejected_without_waiting_for_result(self):
        self.plugin._forward_to_python = AsyncMock(return_value="")
        result = await self.plugin._post_and_wait(self.event, timeout=0.001)
        self.assertIsNotNone(result)
        self.assertIn("未受理", result)
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_unexpected_post_exception_cleanup(self):
        self.plugin._forward_to_python = AsyncMock(side_effect=OSError("offline"))
        with self.assertRaises(OSError):
            await self.plugin._post_and_wait(self.event)
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_timeout_and_late_result_cleanup(self):
        payloads = []

        async def forward(payload):
            payloads.append(payload)
            return "OK"

        self.plugin._forward_to_python = forward
        self.assertIsNone(await self.plugin._post_and_wait(self.event, timeout=0.001))
        self.plugin._resolve_request(payloads[0]["request_id"], "too late")
        await asyncio.sleep(0)
        self.assertEqual({}, self.plugin._pending_replies)
        self.assertEqual([], self.loop_errors)

    async def test_cancellation_while_waiting_cleanup(self):
        task, _ = await self.waiting_request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_cancellation_during_post_cleanup(self):
        started = asyncio.Event()

        async def forward(payload):
            started.set()
            await asyncio.Event().wait()

        self.plugin._forward_to_python = forward
        task = self.spawn(self.plugin._post_and_wait(self.event))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_termination_releases_result_waiter(self):
        task, _ = await self.waiting_request()
        await self.plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_termination_cancels_inflight_post(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def forward(payload):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.plugin._forward_to_python = forward
        task = self.spawn(self.plugin._post_and_wait(self.event))
        await started.wait()
        await self.plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        self.assertTrue(cancelled.is_set())
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_termination_keeps_event_loop_responsive(self):
        gate = threading.Event()
        server = types.SimpleNamespace(
            shutdown=lambda: gate.wait(0.2), server_close=lambda: None)
        self.plugin._httpd = server

        async def release():
            await asyncio.sleep(0.005)
            gate.set()

        release_task = self.spawn(release())
        await self.plugin.terminate()
        self.assertTrue(gate.is_set(), "shutdown blocked the AstrBot event loop")
        await release_task
        self.assertIsNone(self.plugin._httpd)

    async def test_query_timeout_cancels_underlying_work(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def query():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        result = await asyncio.to_thread(self.plugin._run_qq_query, query, 0.01)
        self.assertFalse(result["ok"])
        # 超时那一刻协程可能还没被事件循环调度（取决于循环忙闲）：
        # 既没开始执行、也就谈不上“继续运行”，同样满足要求。
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        if started.is_set():
            for _ in range(100):
                if cancelled.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(cancelled.is_set(), "timed-out QQ query kept running")

    async def test_termination_cancels_outbound_send(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def send(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.plugin._do_send = send
        future = self.plugin._on_forward_request("private", "10001", "hello")
        await started.wait()
        await self.plugin.terminate()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(future.cancelled())
        self.assertTrue(cancelled.is_set())

    async def test_stopped_plugin_rejects_forward_instead_of_queueing(self):
        await self.plugin.terminate()
        future = self.plugin._on_forward_request("private", "10001", "hello")
        self.assertIsNotNone(future)
        self.assertTrue(future.done())
        with self.assertRaises(RuntimeError):
            future.result()
        self.assertEqual([], self.plugin._pending)

    async def test_stopped_plugin_rejects_new_command(self):
        await self.plugin.terminate()
        self.plugin._forward_to_python = AsyncMock(return_value="OK")
        result = await self.plugin._post_and_wait(self.event, timeout=0.001)
        self.assertIn("停止", result)
        self.plugin._forward_to_python.assert_not_awaited()

    async def test_preinitialize_queue_keeps_payload_and_flushes(self):
        self.plugin._loop = None
        self.assertIsNone(self.plugin._on_forward_request("group", "20001", "caption", "image"))
        self.plugin._start_http = lambda: None
        self.plugin._do_send = AsyncMock(return_value=(True, ""))
        await self.plugin.initialize()
        for _ in range(4):
            await asyncio.sleep(0)
        self.plugin._do_send.assert_awaited_once_with("group", "20001", "caption", "image")
        self.assertEqual([], self.plugin._pending)

    async def test_unknown_request_is_ignored(self):
        self.plugin._resolve_request("unknown", "completed")
        await asyncio.sleep(0)
        self.assertEqual([], self.loop_errors)
        self.assertEqual({}, self.plugin._pending_replies)

    async def test_http_result_roundtrip_before_post_returns(self):
        self.plugin.config = {"http_port": 0, "token": "offline-test-token"}
        await self.plugin.initialize()
        port = self.plugin._httpd.server_address[1]

        async def forward(payload):
            response = await asyncio.to_thread(
                self.plugin._http_post, f"http://127.0.0.1:{port}/api/result",
                {"request_id": payload["request_id"], "message": "http-completed"},
                "offline-test-token")
            self.assertTrue(json.loads(response)["ok"])
            return "OK"

        self.plugin._forward_to_python = forward
        try:
            result = await self.plugin._post_and_wait(self.event, timeout=0.05)
            self.assertEqual("http-completed", result)
            self.assertEqual([], self.loop_errors)
        finally:
            await self.plugin.terminate()

    async def test_initialize_failure_leaves_plugin_stopped(self):
        def fail():
            raise OSError("address already in use")

        self.plugin._start_http = fail
        with self.assertRaises(OSError):
            await self.plugin.initialize()
        self.assertIsNone(self.plugin._loop)
        self.assertTrue(self.plugin._stopping)

    async def test_stop_clears_preinitialize_queue(self):
        self.plugin._loop = None
        self.plugin._on_forward_request("private", "10001", "pending")
        await self.plugin.terminate()
        self.assertEqual([], self.plugin._pending)

    async def test_completed_http_tasks_leave_tracking_set(self):
        self.plugin._do_send = AsyncMock(return_value=(True, ""))
        future = self.plugin._on_forward_request("private", "10001", "hello")
        self.assertEqual((True, ""), await asyncio.wrap_future(future))
        self.assertEqual(set(), self.plugin._http_futures)

    async def test_concurrent_early_duplicate_results_do_not_cross_requests(self):
        async def forward(payload):
            self.plugin._resolve_request(payload["request_id"], payload["message"])
            self.plugin._resolve_request(payload["request_id"], "duplicate")
            await asyncio.sleep(0)
            return "OK"

        self.plugin._forward_to_python = forward
        expected = [f"result-{i}" for i in range(100)]
        results = await asyncio.gather(*(
            self.plugin._post_and_wait(self.event, message=text, timeout=2)
            for text in expected))
        self.assertEqual(expected, results)
        self.assertEqual({}, self.plugin._pending_replies)
        self.assertEqual({}, self.plugin._pending_posts)
        self.assertEqual([], self.loop_errors)

    async def test_plugin_can_restart_listener_after_termination(self):
        self.plugin.config = {"http_port": 0}
        for _ in range(3):
            await self.plugin.initialize()
            self.assertFalse(self.plugin._stopping)
            self.assertIs(self.loop, self.plugin._loop)
            await self.plugin.terminate()
            self.assertIsNone(self.plugin._httpd)
            self.assertIsNone(self.plugin._loop)
            self.assertEqual(set(), self.plugin._http_futures)


    async def test_qq_query_timeout_reports_timeout_not_error(self) -> None:
        """查询超时要回“QQ 查询超时”，而不是被当成异常（3.10 上两类超时不同源）。"""
        res = await asyncio.to_thread(
            self.plugin._run_qq_query, lambda: asyncio.sleep(2), 0.05)
        self.assertFalse(res.get("ok"))
        self.assertIn("超时", str(res.get("error")), res)
        self.assertNotIn("异常", str(res.get("error")), res)

class ForwardHandlerTests(unittest.TestCase):
    def test_forward_timeout_cancels_queued_send(self):
        class Future:
            cancelled = False

            def result(self, timeout):
                raise TimeoutError

            def cancel(self):
                self.cancelled = True

        future = Future()
        handler = PLUGIN._HttpHandler.__new__(PLUGIN._HttpHandler)
        handler.path = "/api/forward"
        handler.plugin = types.SimpleNamespace(_on_forward_request=lambda *a: future)
        handler._auth = lambda: True
        handler._read_json = lambda: {"target_id": "10001", "message": "hello"}
        replies = []
        handler._json = lambda body, code=200: replies.append(body)
        handler.do_POST()
        self.assertFalse(replies[0]["ok"])
        self.assertTrue(future.cancelled, "send continued after its HTTP caller timed out")


if __name__ == "__main__":
    unittest.main()

