import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Optional
from urllib.parse import unquote

import httpx
import websockets

from .. import config

logger = logging.getLogger(__name__)


def _filename_from_disposition(disposition: str) -> str:
    """解析 Content-Disposition 的 filename*=/filename=,取不到返回空串。"""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.IGNORECASE)
    return m.group(1).strip() if m else ""


class MessageWithdrawnError(RuntimeError):
    """回复目标消息已被用户撤回(飞书 code=230011);作为本轮中止信号传播。"""


def _raise_if_withdrawn(path: str, data: Optional[dict]) -> None:
    """操作台返回体若是「消息已撤回」错误,抛 MessageWithdrawnError。"""
    if not isinstance(data, dict) or data.get("success"):
        return
    if data.get("code") == 230011 or "withdrawn" in str(data.get("msg") or "").lower():
        raise MessageWithdrawnError(
            f"{path}: code={data.get('code')} msg={data.get('msg')}"
        )


class ConsoleClient:
    def __init__(self, base_url: str = config.CONSOLE_BASE_URL):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def close(self):
        await self._client.aclose()

    async def list_messages(self) -> list[dict]:
        resp = await self._client.get("/api/messages")
        resp.raise_for_status()
        return resp.json()

    async def get_message(self, message_id: str) -> Optional[dict]:
        """按 message_id 查单条消息(wire 同构);404/异常返回 None。"""
        try:
            resp = await self._client.get(f"/api/messages/{message_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("message") if isinstance(data, dict) and "message" in data else data
        except Exception as e:
            logger.error("get_message(%s) 调用失败: %s", message_id, e)
            return None

    async def download_file(
        self,
        message_id: str,
        max_bytes: int,
        file_key: Optional[str] = None,
        res_type: Optional[str] = None,
    ) -> Optional[tuple[dict, bytes]]:
        """流式下载消息附件,超 max_bytes 中止;返回 (meta, content),失败 None。
        纯传输:不碰文件系统,文件名取自 Content-Disposition。
        file_key/res_type 用于 post 内嵌图片(image_key + res_type="image")。"""
        try:
            params = {"message_id": message_id}
            if file_key:
                params["file_key"] = file_key
                params["res_type"] = res_type or "file"
            async with self._client.stream(
                "GET", "/api/file", params=params
            ) as resp:
                if resp.status_code != 200:
                    logger.warning(
                        "download_file(%s) 返回 %s", message_id, resp.status_code
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        logger.warning(
                            "download_file(%s) 超过上限 %d 字节,中止", message_id, max_bytes
                        )
                        return None
                    chunks.append(chunk)
                meta = {
                    "name": _filename_from_disposition(
                        resp.headers.get("content-disposition", "")
                    ),
                    "mime": resp.headers.get("content-type", ""),
                    "size": total,
                }
                return meta, b"".join(chunks)
        except Exception as e:
            logger.error("download_file(%s) 调用失败: %s", message_id, e)
            return None

    async def send_file_reply(self, seq: int, file_path: str, file_name: str) -> bool:
        """产物投递(操作台单接口):multipart 上传文件并回复 file 消息到 seq 对应消息。"""
        try:
            with open(file_path, "rb") as f:
                resp = await self._client.post(
                    "/api/reply_file",
                    data={"seq": str(seq)},
                    files={"file": (file_name, f)},
                )
            resp.raise_for_status()
            data = resp.json()
        except MessageWithdrawnError:
            raise
        except Exception as e:
            logger.error("send_file_reply(%s) 调用失败: %s", file_name, e)
            return False
        _raise_if_withdrawn("/api/reply_file", data)
        if not data.get("success"):
            logger.error(
                "/api/reply_file 返回失败: code=%s msg=%s",
                data.get("code"),
                data.get("msg"),
            )
            return False
        return True

    async def is_recalled(self, message_id: str) -> bool:
        """查询消息是否已被用户撤回(操作台 /api/recalled/{message_id})。

        查询失败/消息不存在按未撤回处理(fail-open),网络抖动不误杀正常回复。
        """
        if not message_id:
            return False
        try:
            resp = await self._client.get(f"/api/recalled/{message_id}")
            resp.raise_for_status()
            return bool(resp.json().get("recalled"))
        except Exception as e:  # noqa: BLE001 — fail-open
            logger.warning("撤回状态查询失败(%s),按未撤回处理: %s", message_id, e)
            return False

    async def reply(self, seq: int, text: str) -> bool:
        return await self._post("/api/reply", {"seq": seq, "text": text})

    async def stream_reply(self, seq: int, text: str, mode: str = "cardkit") -> bool:
        return await self._post("/api/stream", {"seq": seq, "text": text, "mode": mode})

    async def stream_begin(self, seq: int) -> Optional[str]:
        data = await self._post_json("/api/stream_begin", {"seq": seq})
        if data and data.get("success"):
            return data.get("stream_id")
        if data is not None:
            _raise_if_withdrawn("/api/stream_begin", data)
            logger.error(
                "/api/stream_begin 返回失败: code=%s msg=%s",
                data.get("code"),
                data.get("msg"),
            )
        return None

    async def stream_append(self, stream_id: str, delta: str) -> bool:
        return await self._post(
            "/api/stream_append", {"stream_id": stream_id, "delta": delta}
        )

    async def stream_end(self, stream_id: str) -> bool:
        return await self._post("/api/stream_end", {"stream_id": stream_id})

    async def stream_set(self, stream_id: str, content: str) -> bool:
        return await self._post(
            "/api/stream_set", {"stream_id": stream_id, "content": content}
        )

    async def _post(self, path: str, body: dict) -> bool:
        data = await self._post_json(path, body)
        if data is None:
            return False
        _raise_if_withdrawn(path, data)
        if not data.get("success"):
            logger.error(
                "%s 返回失败: code=%s msg=%s", path, data.get("code"), data.get("msg")
            )
            return False
        return True

    async def _post_json(self, path: str, body: dict) -> Optional[dict]:
        try:
            resp = await self._client.post(path, json=body)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("%s 调用失败: %s", path, e)
            return None


class WsListener:
    def __init__(self, on_message: Callable[[dict], Awaitable[None]]):
        self._on_message = on_message
        self._connected = False
        self._stopped = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    async def stop(self):
        self._stopped.set()

    async def run(self, catch_up: Callable[[], Awaitable[None]]):
        url = f"{config.ws_url()}/ws"
        first = True
        while not self._stopped.is_set():
            try:
                async with websockets.connect(url) as ws:
                    self._connected = True
                    logger.info("已连接操作台 WS: %s", url)
                    if first:
                        first = False
                    else:
                        await catch_up()
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "WS 连接断开: %s,%ds 后重连", e, config.WS_RECONNECT_DELAY
                )
            finally:
                self._connected = False
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=config.WS_RECONNECT_DELAY
                )
            except asyncio.TimeoutError:
                pass

    async def _dispatch(self, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("WS 收到无法解析的消息: %s", raw[:200])
            return
        if payload.get("type") == "message":
            await self._on_message(payload.get("data") or {})
