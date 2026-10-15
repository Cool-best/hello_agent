import asyncio
import json
import os
import threading
import time
import uuid
from typing import IO

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageRequest,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    P2ImMessageRecalledV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.channel import FeishuChannel
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from .msglog import MessageLogger
from .store import build_wire


class MessageNotFoundError(LookupError):
    """消息不存在（已撤回/无权限/不属于本 bot）。"""


class MessageNoAttachmentError(LookupError):
    """消息存在但不含附件。"""


class FeishuFetchError(RuntimeError):
    """操作台向飞书拉取失败。"""


def demo_card(text: str = "**演示卡片**：点击下方按钮试试回调") -> dict:
    """带一个按钮的演示卡片，update_multi 允许后续更新。"""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "演示卡片"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": text},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "点我试试"},
                    "type": "primary",
                    "value": {"cmd": "demo_button"},
                },
            ]
        },
    }


class FeishuService:
    """飞书长连接接收 + REST 回复/发送/卡片/文件封装。"""

    def __init__(self, app_id: str, app_secret: str, on_message, on_card_action=None,
                 on_recall=None, log_dir: str | None = None):
        self._on_message = on_message
        self._on_card_action = on_card_action
        self._on_recall = on_recall
        self._api = (
            lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        )
        builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
            self._handle_message
        )
        if on_card_action:
            builder = builder.register_p2_card_action_trigger(self._handle_card_action)
        if on_recall:
            builder = builder.register_p2_im_message_recalled_v1(self._handle_recalled)
        handler = builder.build()
        self._ws = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        # 方案B流式用 SDK 内置 FeishuChannel：只做出站（不 connect，入站仍走上面的 ws.Client）。
        # channel.stream 是 async 接口，给它一个独立事件循环线程承载。
        self._channel = FeishuChannel(
            app_id=app_id, app_secret=app_secret, log_level=lark.LogLevel.WARNING
        )
        self._aloop = asyncio.new_event_loop()
        threading.Thread(target=self._aloop.run_forever, daemon=True).start()
        # 健康检查用状态
        self._started_at = time.time()
        self._ws_state = "connecting"  # connecting/connected/reconnecting/failed
        self._ws_error: str | None = None
        self._last_message_at: float | None = None
        self._ws.on_reconnecting = self._on_ws_reconnecting
        self._ws.on_reconnected = self._on_ws_reconnected
        # 收发消息日志（每次启动一个文件）
        self._logger = MessageLogger(log_dir)
        self.log_path = self._logger.path
        # 手动分段流式会话：stream_id -> {queue, fut, created_at, text, chat_id}
        self._streams: dict[str, dict] = {}
        self._streams_lock = threading.Lock()

    def _log_out(self, action: str, res: dict, **fields):
        result = "ok" if res.get("success") else f"fail code={res.get('code')} {res.get('msg')}"
        self._logger.log("OUT", action, result=result, **fields)

    def _on_ws_reconnecting(self):
        self._ws_state = "reconnecting"

    def _on_ws_reconnected(self):
        self._ws_state = "connected"
        self._ws_error = None

    # ---------- 长连接 ----------

    def start_ws(self):
        """阻塞调用，需在独立线程中运行。"""
        print("[飞书] 正在建立长连接...")
        try:
            self._ws.start()
        except Exception as e:
            self._ws_state = "failed"
            self._ws_error = str(e)
            print(f"[飞书] 长连接启动失败（请检查 App ID / App Secret 是否正确）: {e}")

    def _handle_message(self, data: P2ImMessageReceiveV1) -> None:
        self._last_message_at = time.time()
        raw = json.loads(lark.JSON.marshal(data))
        event = raw.get("event", {})
        msg = event.get("message", {})
        sender = event.get("sender", {})
        self._logger.log(
            "IN", "message",
            block=json.dumps(raw, ensure_ascii=False, indent=2),
            message_id=msg.get("message_id"),
            chat_id=msg.get("chat_id"),
            msg_type=msg.get("message_type"),
            sender=(sender.get("sender_id") or {}).get("open_id"),
        )
        self._on_message(raw)

    def _handle_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        raw = json.loads(lark.JSON.marshal(data))
        event = raw.get("event", {})
        self._logger.log(
            "IN", "card_action",
            block=json.dumps(raw, ensure_ascii=False, indent=2),
            operator=(event.get("operator") or {}).get("open_id"),
            value=(event.get("action") or {}).get("value"),
        )
        if self._on_card_action:
            self._on_card_action(raw)
        resp = P2CardActionTriggerResponse()
        resp.toast = CallBackToast()
        resp.toast.type = "success"
        resp.toast.content = "操作台已收到按钮点击"
        return resp

    def _handle_recalled(self, data: P2ImMessageRecalledV1) -> None:
        raw = json.loads(lark.JSON.marshal(data))
        event = raw.get("event", {})
        self._logger.log(
            "IN", "recalled",
            block=json.dumps(raw, ensure_ascii=False, indent=2),
            message_id=event.get("message_id"),
            chat_id=event.get("chat_id"),
            recall_type=event.get("recall_type"),
        )
        if self._on_recall:
            self._on_recall(raw)

    # ---------- 通用结果 ----------

    @staticmethod
    def _result(resp) -> dict:
        out = {"success": resp.success(), "code": resp.code, "msg": resp.msg}
        data = getattr(resp, "data", None)
        if resp.success() and data:
            message_id = getattr(data, "message_id", None)
            card_id = getattr(data, "card_id", None)
            if message_id:
                out["message_id"] = message_id
            if card_id:
                out["card_id"] = card_id
        return out

    # ---------- 文本消息 ----------

    def reply_text(self, message_id: str, text: str) -> dict:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        res = self._result(self._api.im.v1.message.reply(req))
        self._log_out("reply_text", res, message_id=message_id, text=text)
        return res

    def send_text(self, open_id: str, text: str) -> dict:
        return self._send("open_id", open_id, "text", {"text": text})

    # ---------- 卡片消息 ----------

    def _send(self, receive_id_type: str, receive_id: str, msg_type: str, content: dict) -> dict:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        res = self._result(self._api.im.v1.message.create(req))
        self._log_out(f"send_{msg_type}", res, receive_id=receive_id, content=content)
        return res

    def _reply(self, message_id: str, msg_type: str, content: dict) -> dict:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        res = self._result(self._api.im.v1.message.reply(req))
        self._log_out(f"reply_{msg_type}", res, message_id=message_id, content=content)
        return res

    def reply_card(self, message_id: str, card: dict) -> dict:
        return self._reply(message_id, "interactive", card)

    def send_card(self, open_id: str, card: dict) -> dict:
        return self._send("open_id", open_id, "interactive", card)

    def update_card(self, message_id: str, card: dict) -> dict:
        """PATCH 整卡覆盖，卡片需带 config.update_multi=true。"""
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        return self._result(self._api.im.v1.message.patch(req))

    # ---------- 流式输出 ----------

    def stream_patch(self, message_id: str, full_text: str, interval: float = 0.15, step: int = 10) -> dict:
        """方案A：回复卡片后反复 PATCH 整卡，打字机效果。"""
        card = demo_card("")
        res = self.reply_card(message_id, card)
        if not res["success"]:
            return res
        mid = res["message_id"]
        last = res
        for i in range(step, len(full_text) + step, step):
            card["body"]["elements"][0]["content"] = full_text[:i]
            last = self.update_card(mid, card)
            time.sleep(interval)
        card["body"]["elements"][0]["content"] = full_text
        res = self.update_card(mid, card)
        self._log_out("stream_patch", res, message_id=message_id, text=full_text)
        return res

    def stream_cardkit(self, chat_id: str, message_id: str, full_text: str, interval: float = 0.02) -> dict:
        """方案B：SDK 内置 FeishuChannel.stream。

        内置节流（100ms/50字）、快照式整段内容更新、sequence 保序，
        结束时自动关闭 streaming_mode。producer 逐字 append 即可。
        """
        async def _run():
            async def producer(stream):
                for ch in full_text:
                    await stream.append(ch)
                    await asyncio.sleep(interval)

            return await self._channel.stream(
                chat_id, {"markdown": producer}, {"reply_to": message_id}
            )

        fut = asyncio.run_coroutine_threadsafe(_run(), self._aloop)
        try:
            result = fut.result(timeout=120)
        except Exception as e:
            # 超时/失败后协程仍挂在 _aloop 上会继续跑完整个 stream，必须取消
            fut.cancel()
            res = {"success": False, "code": -1, "msg": str(e)}
            self._log_out("stream_cardkit", res, chat_id=chat_id, text=full_text)
            return res
        res = {
            "success": result.success,
            "code": 0 if result.success else -1,
            "msg": "" if result.success else str(result.error),
            "message_id": result.message_id,
        }
        self._log_out("stream_cardkit", res, chat_id=chat_id, text=full_text)
        return res

    # ---------- 手动分段流式（begin/append/end） ----------

    _STREAM_IDLE_TIMEOUT = 600  # 秒，begin 后超过此时长未 end 的会话会被清理

    def _sweep_streams(self):
        """清理闲置超时或已异常的流会话（调用方须持有 _streams_lock）。"""
        now = time.time()
        for sid, sess in list(self._streams.items()):
            if sess["fut"].done() or now - sess["created_at"] > self._STREAM_IDLE_TIMEOUT:
                sess["fut"].cancel()
                del self._streams[sid]

    def stream_begin(self, chat_id: str, message_id: str) -> dict:
        """开一条流式会话：producer 阻塞在队列上，append/set 喂内容，end 收尾。

        队列元素为 (op, payload)：op = append / set / end（end 为哨兵）。
        """
        q: asyncio.Queue = asyncio.Queue()

        async def _run():
            async def producer(stream):
                while True:
                    op, payload = await q.get()
                    if op == "end":
                        return
                    if op == "set":
                        await stream.set_content(payload)
                    else:
                        await stream.append(payload)

            return await self._channel.stream(
                chat_id, {"markdown": producer}, {"reply_to": message_id}
            )

        fut = asyncio.run_coroutine_threadsafe(_run(), self._aloop)
        stream_id = uuid.uuid4().hex[:12]
        with self._streams_lock:
            self._sweep_streams()
            self._streams[stream_id] = {
                "queue": q, "fut": fut, "created_at": time.time(),
                "chat_id": chat_id, "text": "",
            }
        res = {"success": True, "code": 0, "msg": "", "stream_id": stream_id}
        self._log_out("stream_begin", res, chat_id=chat_id, message_id=message_id)
        return res

    def _get_live_stream(self, stream_id: str):
        """取出活跃会话；已中断的顺手清掉。返回 (sess, error_dict)。"""
        with self._streams_lock:
            sess = self._streams.get(stream_id)
        if sess is None:
            return None, {"success": False, "code": -1, "msg": f"流会话 {stream_id} 不存在或已结束"}
        if sess["fut"].done():
            with self._streams_lock:
                self._streams.pop(stream_id, None)
            exc = sess["fut"].exception()
            return None, {"success": False, "code": -1, "msg": f"流会话已中断: {exc}"}
        return sess, None

    def stream_append(self, stream_id: str, delta: str) -> dict:
        """向流式会话追加一段文本（非阻塞，SDK 内部节流后推到卡片）。"""
        sess, err = self._get_live_stream(stream_id)
        if err:
            return err
        sess["text"] += delta
        # asyncio.Queue 非线程安全，须切回 _aloop 线程投放
        self._aloop.call_soon_threadsafe(sess["queue"].put_nowait, ("append", delta))
        return {"success": True, "code": 0, "msg": "", "chars": len(sess["text"])}

    def stream_set(self, stream_id: str, content: str) -> dict:
        """整体替换流式卡片内容并立即刷新；后续 append 在新内容基础上衔接。"""
        sess, err = self._get_live_stream(stream_id)
        if err:
            return err
        sess["text"] = content
        self._aloop.call_soon_threadsafe(sess["queue"].put_nowait, ("set", content))
        return {"success": True, "code": 0, "msg": "", "chars": len(content)}

    def stream_end(self, stream_id: str) -> dict:
        """关闭流式会话：producer 收到哨兵返回，SDK 自动关闭 streaming_mode。"""
        with self._streams_lock:
            sess = self._streams.pop(stream_id, None)
        if sess is None:
            return {"success": False, "code": -1, "msg": f"流会话 {stream_id} 不存在或已结束"}
        self._aloop.call_soon_threadsafe(sess["queue"].put_nowait, ("end", None))
        try:
            result = sess["fut"].result(timeout=60)
        except Exception as e:
            sess["fut"].cancel()
            res = {"success": False, "code": -1, "msg": str(e)}
        else:
            res = {
                "success": result.success,
                "code": 0 if result.success else -1,
                "msg": "" if result.success else str(result.error),
                "message_id": result.message_id,
            }
        self._log_out("stream_end", res, chat_id=sess["chat_id"],
                      stream_id=stream_id, text=sess["text"])
        return res

    # ---------- 文件 ----------

    # 飞书 im/v1/files 的 file_type 只认这组枚举，按扩展名映射，映射不到用 stream
    _FILE_TYPE_BY_EXT = {
        "doc": "doc", "docx": "doc",
        "xls": "xls", "xlsx": "xls", "csv": "xls",
        "ppt": "ppt", "pptx": "ppt",
        "pdf": "pdf", "mp4": "mp4", "opus": "opus",
    }

    def upload_file(self, file_name: str, fileobj: IO) -> str:
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        file_type = self._FILE_TYPE_BY_EXT.get(ext, "stream")
        req = (
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type(file_type)
                .file_name(file_name)
                .file(fileobj)
                .build()
            )
            .build()
        )
        resp = self._api.im.v1.file.create(req)
        if not resp.success():
            self._logger.log("OUT", "upload_file", file_name=file_name,
                             result=f"fail code={resp.code} {resp.msg}")
            raise RuntimeError(f"上传文件失败: code={resp.code} msg={resp.msg}")
        self._logger.log("OUT", "upload_file", file_name=file_name,
                         file_key=resp.data.file_key, result="ok")
        return resp.data.file_key

    def reply_file(self, message_id: str, file_key: str) -> dict:
        return self._reply(message_id, "file", {"file_key": file_key})

    def send_file(self, open_id: str, file_key: str) -> dict:
        return self._send("open_id", open_id, "file", {"file_key": file_key})

    def download_file(self, message_id: str, file_key: str, res_type: str = "file") -> tuple[bytes, str]:
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(res_type)
            .build()
        )
        resp = self._api.im.v1.message_resource.get(req)
        if not resp.success():
            self._logger.log("OUT", "download_file", message_id=message_id, file_key=file_key,
                             result=f"fail code={resp.code} {resp.msg}")
            raise RuntimeError(f"下载文件失败: code={resp.code} msg={resp.msg}")
        self._logger.log("OUT", "download_file", message_id=message_id, file_key=file_key, result="ok")
        return resp.file.read(), (resp.file_name or file_key)

    # ---------- 按 message_id 实时查询（无状态代理，不经内存存储） ----------

    def fetch_message(self, message_id: str) -> dict | None:
        """实时从飞书取单条消息，返回原始消息 dict；查不到返回 None。"""
        req = GetMessageRequest.builder().message_id(message_id).build()
        resp = self._api.im.v1.message.get(req)
        if not resp.success():
            self._logger.log("OUT", "get_message", message_id=message_id,
                             result=f"fail code={resp.code} {resp.msg}")
            return None
        items = (resp.data and resp.data.items) or []
        if not items:
            self._logger.log("OUT", "get_message", message_id=message_id, result="fail empty items")
            return None
        self._logger.log("OUT", "get_message", message_id=message_id, result="ok")
        return json.loads(lark.JSON.marshal(items[0]))

    def open_message_file(
        self,
        message_id: str,
        file_key: str | None = None,
        res_type: str | None = None,
    ):
        """取消息并下载其附件。返回 (fileobj, name, mime, size)。

        消息不存在/无附件抛 LookupError（映射 404）；飞书拉取失败抛 FeishuFetchError（映射 502）。
        fileobj 为 SDK 返回的文件对象，可按块 read，调用方负责流式转发。
        显式传 file_key（如 post 内嵌图片的 image_key, res_type="image"）时跳过消息体
        附件解析，直接按 key 下载。
        """
        msg = self.fetch_message(message_id)
        if msg is None:
            raise MessageNotFoundError("message not found")
        wire = build_wire(msg)
        if file_key:
            info = {"file_key": file_key, "name": file_key, "mime": None, "size": None}
            res_type = res_type or "file"
        else:
            info = wire.get("file")
            if not info or not info.get("file_key"):
                raise MessageNoAttachmentError("message has no attachment")
            res_type = "image" if wire["msg_type"] == "image" else "file"
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(info["file_key"])
            .type(res_type)
            .build()
        )
        resp = self._api.im.v1.message_resource.get(req)
        if not resp.success():
            self._logger.log("OUT", "get_message_file", message_id=message_id,
                             file_key=info["file_key"], result=f"fail code={resp.code} {resp.msg}")
            raise FeishuFetchError(f"下载文件失败: code={resp.code} msg={resp.msg}")
        fileobj = resp.file
        name = resp.file_name or info["name"]
        size = info.get("size")
        if not size and hasattr(fileobj, "getbuffer"):
            size = fileobj.getbuffer().nbytes
        self._logger.log("OUT", "get_message_file", message_id=message_id,
                         file_key=info["file_key"], result="ok")
        return fileobj, name, info.get("mime"), size

    # ---------- 健康检查 ----------

    def health(self, message_count: int = 0) -> dict:
        """ws_connected 以 SDK 当前 _conn 为准；状态字段由重连钩子维护。"""
        ws_connected = getattr(self._ws, "_conn", None) is not None
        if ws_connected and self._ws_state == "connecting":
            self._ws_state = "connected"
        return {
            "status": "ok" if ws_connected else "degraded",
            "ws_connected": ws_connected,
            "ws_state": self._ws_state,
            "ws_error": self._ws_error,
            "uptime_s": round(time.time() - self._started_at, 1),
            "last_message_at": (
                time.strftime("%H:%M:%S", time.localtime(self._last_message_at))
                if self._last_message_at
                else None
            ),
            "messages": message_count,
            "active_streams": len(self._streams),
        }

    # ---------- 便捷组合 ----------

    def reply_local_file(self, message_id: str, path: str) -> dict:
        with open(path, "rb") as f:
            file_key = self.upload_file(os.path.basename(path), f)
        return self.reply_file(message_id, file_key)
