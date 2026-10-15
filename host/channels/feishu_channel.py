import json
import logging
import mimetypes
import time
from collections import OrderedDict
from typing import Optional

from .. import config, events
from ..state import inbox, session_store, workspace_store
from .channel_base import InboundMessage, ReplySink
from .console_client import ConsoleClient, MessageWithdrawnError

logger = logging.getLogger(__name__)


def _image_ref(saved: dict) -> dict:
    return {
        "path": saved["path"],
        "name": saved["name"],
        "saved_as": saved["saved_as"],
        "mime": saved.get("mime") or "",
    }


def _wire_ts(msg: dict) -> float:
    """取消息时间:wire 带 create_time/timestamp 则用(秒或毫秒),否则到达时间兜底。
    pending 关联必须用消息时间,避免 catch_up 补拉造成"旧图配新话"。"""
    raw = msg.get("create_time") or msg.get("timestamp")
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return time.time()
    if ts > 1e12:  # 毫秒时间戳
        ts /= 1000
    return ts


def _parse_post(content_raw: str) -> tuple[str, list[str]]:
    """解析飞书 post(富文本)消息体:拼接 text/a 段为纯文本、剔除 at 段、
    收集 img 段的 image_key。非 JSON 或结构不符时原样返回文本、无图片。"""
    try:
        obj = json.loads(content_raw)
    except (json.JSONDecodeError, TypeError):
        return (content_raw or "").strip(), []
    if not isinstance(obj, dict) or not isinstance(obj.get("content"), list):
        return (content_raw or "").strip(), []
    parts: list[str] = []
    if obj.get("title"):
        parts.append(str(obj["title"]))
    images: list[str] = []
    for para in obj["content"]:
        if not isinstance(para, list):
            continue
        seg_texts: list[str] = []
        for seg in para:
            if not isinstance(seg, dict):
                continue
            tag = seg.get("tag")
            if tag == "text":
                seg_texts.append(seg.get("text") or "")
            elif tag == "a":
                seg_texts.append(seg.get("text") or seg.get("href") or "")
            elif tag == "img":
                key = seg.get("image_key")
                if key:
                    images.append(key)
            # at 段(@某人)剔除,不进入文本
        line = "".join(seg_texts).strip()
        if line:
            parts.append(line)
    return "\n".join(parts).strip(), images


class FeishuSink(ReplySink):
    """飞书回复出口:纯文本回复 + 卡片流式输出。

    撤回感知:任一操作台调用命中「消息已撤回」(230011)即置 _recalled
    标记并按失败返回(不向 pipeline 抛异常);pipeline 经 recalled() 在
    检查点查询,标记命中或轮询命中都会中止本轮。
    """

    def __init__(
        self, console: ConsoleClient, seq: Optional[int], message_id: str = ""
    ):
        self._console = console
        self._seq = seq
        self._message_id = message_id or ""
        self._recalled = False

    async def _guarded(self, call, *args, **kwargs):
        """调用操作台接口;命中「消息已撤回」时记录标记并返回 None(按失败)。"""
        try:
            return await call(*args, **kwargs)
        except MessageWithdrawnError as e:
            self._recalled = True
            logger.info("[feishu] 消息已撤回,调用按失败处理: %s", e)
            return None

    async def recalled(self) -> bool:
        """源消息是否已被撤回:已命中标记直接 True,否则向操作台轮询一次。"""
        if self._recalled:
            return True
        if not self._message_id:
            return False
        self._recalled = await self._console.is_recalled(self._message_id)
        return self._recalled

    async def reply(self, text: str) -> bool:
        if self._seq is None:
            logger.info("[feishu] 无 seq,回复仅记录: %s", text)
            return True
        return bool(await self._guarded(self._console.reply, self._seq, text))

    async def reply_final(self, text: str) -> None:
        if self._seq is None:
            logger.info("[feishu] 无 seq,回复仅记录: %s", text)
            return
        ok = await self._guarded(self._console.stream_reply, self._seq, text, mode="cardkit")
        if not ok:
            await self._guarded(self._console.reply, self._seq, text)

    async def stream_begin(self) -> Optional[str]:
        if self._seq is None:
            return None
        return await self._guarded(self._console.stream_begin, self._seq)

    async def stream_append(self, stream_id: str, delta: str) -> bool:
        return bool(await self._guarded(self._console.stream_append, stream_id, delta))

    async def stream_set(self, stream_id: str, content: str) -> bool:
        return bool(await self._guarded(self._console.stream_set, stream_id, content))

    async def stream_end(self, stream_id: str) -> bool:
        return bool(await self._guarded(self._console.stream_end, stream_id))

    async def send_file(self, file_path: str, display_name: str) -> bool:
        """产物投递:操作台单接口上传+回复 file 消息;无 seq(补拉等)跳过。"""
        if self._seq is None:
            logger.info("[feishu] 无 seq,产物文件仅记录: %s", display_name)
            return False
        return bool(
            await self._guarded(self._console.send_file_reply, self._seq, file_path, display_name)
        )


class FeishuChannel:
    """飞书渠道适配器:wire 协议解析、消息去重、序号追踪与断线补拉。"""

    def __init__(self, console: ConsoleClient, pipeline):
        self._console = console
        self._pipeline = pipeline
        self._seen_message_ids: OrderedDict[str, float] = OrderedDict()
        self._max_seen_seq = 0
        # 群聊图片 pending buffer:(chat_id, sender_open_id) -> [PendingImage]
        # PendingImage 只有元数据(message_id/file_info/ts,零字节);
        # 命中软规则才懒下载,过期惰性丢弃——不相关图片不下载、不落盘、不触发生成
        self._pending_images: dict[tuple[str, str], list[dict]] = {}

    async def handle_wire(self, msg: dict):
        message_id = msg.get("message_id") or ""
        if message_id and self._is_duplicate(message_id):
            logger.info("重复消息,忽略: %s", message_id)
            return
        seq = msg.get("seq")
        if isinstance(seq, int):
            self._max_seen_seq = max(self._max_seen_seq, seq)

        chat_id = msg.get("chat_id") or ""
        if not chat_id:
            logger.warning("消息缺少 chat_id,忽略: %s", msg)
            return

        sender_open_id = msg.get("sender_open_id") or ""
        source = "web" if sender_open_id in ("dev", "web") else "feishu"
        chat_type = msg.get("chat_type") or ""
        if source == "feishu":
            self._ensure_workspace(chat_id, chat_type, sender_open_id)

        msg_type = msg.get("msg_type") or ""
        text = (msg.get("content") or "").strip()
        parent_id = msg.get("parent_id") or None
        attachment = None
        images: Optional[list[dict]] = None
        if source == "feishu":
            if msg_type == "file":
                file_info = msg.get("file") or {}
                attachment = await self._fetch_attachment(
                    chat_id, message_id, file_info
                )
                if not text:
                    text = file_info.get("name") or message_id
            elif msg_type == "image":
                if chat_type == "p2p":
                    # 私聊图片 = 明确发给 agent:下载落盘 + 本轮 VL;
                    # text 保持用户配文(可能为空),由核心决定默认问题
                    saved = await self._fetch_attachment(
                        chat_id, message_id, msg.get("file") or {}
                    )
                    if saved:
                        attachment = saved
                        images = [_image_ref(saved)]
                else:
                    # 群聊图片只登记 pending 元数据,等待指令信号
                    self._pending_add(
                        chat_id,
                        sender_open_id,
                        message_id,
                        msg.get("file") or {},
                        _wire_ts(msg),
                    )
                    logger.info(
                        "群聊图片登记 pending: chat=%s sender=%s message=%s",
                        chat_id, sender_open_id, message_id,
                    )
                    return
            elif msg_type == "post":
                # 富文本:wire content 即 post body 原始 JSON,agent 侧解析;
                # 图文同消息 = 显式意图(群聊仅 @bot 推送),不进 pending
                text, image_keys = _parse_post(msg.get("content") or "")
                if image_keys:
                    saved_list = await self._fetch_post_images(
                        chat_id, message_id, image_keys
                    )
                    if saved_list:
                        images = [_image_ref(s) for s in saved_list]
            elif msg_type == "text":
                if parent_id:
                    # 硬规则:回复引用图片/图文消息 = 显式意图
                    hint, ref_saved = await self._resolve_reference(
                        chat_id, parent_id
                    )
                    if ref_saved:
                        images = [_image_ref(s) for s in ref_saved]
                    if hint:
                        text = f"{hint}\n{text}"
                if images is None and chat_type != "p2p":
                    # 软规则:同 sender TTL 内先发图后发文本,关联其 pending 图
                    pendings = self._pending_pop(
                        chat_id, sender_open_id, _wire_ts(msg)
                    )
                    saved_list = []
                    for p in pendings:
                        saved = inbox.find_saved(
                            chat_id, p["message_id"]
                        ) or await self._fetch_attachment(
                            chat_id, p["message_id"], p["file_info"]
                        )
                        if saved:
                            saved_list.append(saved)
                    if saved_list:
                        images = [_image_ref(s) for s in saved_list]
                        names = ", ".join(
                            f"inbox/{s['saved_as']}" for s in saved_list
                        )
                        text = (
                            f"[用户发送了 {len(saved_list)} 张图片,"
                            f"已保存到工作区 {names}]\n{text}"
                        )

        inbound = InboundMessage(
            chat_id=chat_id,
            text=text,
            source=source,
            msg_type=msg_type,
            ref=seq if isinstance(seq, int) else None,
            parent_id=parent_id,
            message_id=message_id,
            sender=sender_open_id or None,
            attachment=attachment,
            images=images,
        )
        await self._pipeline.handle_message(
            inbound, FeishuSink(self._console, inbound.ref, message_id)
        )

    def _pending_add(
        self,
        chat_id: str,
        sender_open_id: str,
        message_id: str,
        file_info: dict,
        ts: float,
    ):
        self._pending_images.setdefault((chat_id, sender_open_id), []).append(
            {"message_id": message_id, "file_info": file_info, "ts": ts}
        )
        self._pending_gc(ts)

    def _pending_pop(
        self, chat_id: str, sender_open_id: str, ref_ts: float
    ) -> list[dict]:
        """取出同 sender 在 TTL 窗口内(ref_ts 之前)的 pending 图并消费;
        无命中不动 buffer(等 GC)。"""
        key = (chat_id, sender_open_id)
        entries = self._pending_images.get(key)
        if not entries:
            return []
        fresh = [
            e
            for e in entries
            if 0 <= ref_ts - e["ts"] <= config.IMAGE_PENDING_TTL
        ]
        if not fresh:
            return []
        self._pending_images.pop(key, None)
        return fresh

    def _pending_gc(self, ref_ts: float):
        """以最新消息时间为基准惰性清理过期项——与 pending 关联用同一时间基准,
        不混用本地时钟(wire 带 create_time 时两者可能不同源)。"""
        for key, entries in list(self._pending_images.items()):
            entries = [
                e for e in entries if ref_ts - e["ts"] <= config.IMAGE_PENDING_TTL
            ]
            if entries:
                self._pending_images[key] = entries
            else:
                del self._pending_images[key]

    async def _fetch_attachment(
        self, chat_id: str, message_id: str, file_info: dict
    ) -> Optional[dict]:
        """下载消息附件并落盘到会话 workspace 的 inbox/,失败返回 None。"""
        downloaded = await self._console.download_file(
            message_id, config.FILE_MAX_BYTES
        )
        if not downloaded:
            logger.warning("附件下载失败,message_id=%s", message_id)
            return None
        meta, content = downloaded
        name = file_info.get("name") or meta["name"] or message_id
        mime = file_info.get("mime") or meta["mime"]
        saved = inbox.save_attachment(chat_id, message_id, name, content, mime)
        if saved:
            logger.info(
                "附件已保存: chat=%s message=%s -> inbox/%s (%d 字节)",
                chat_id, message_id, saved["saved_as"], saved["size"],
            )
        return saved

    async def _fetch_post_images(
        self, chat_id: str, message_id: str, image_keys: list[str]
    ) -> list[dict]:
        """下载 post 内嵌图片并落盘 inbox/。复合索引键 message_id:image_key,
        多图各自幂等;单图失败跳过,返回成功列表。"""
        saved_list = []
        for key in image_keys:
            if not key:
                continue
            index_key = f"{message_id}:{key}"
            saved = inbox.find_saved(chat_id, index_key)
            if not saved:
                downloaded = await self._console.download_file(
                    message_id, config.FILE_MAX_BYTES,
                    file_key=key, res_type="image",
                )
                if not downloaded:
                    logger.warning(
                        "post 图片下载失败: message=%s key=%s", message_id, key
                    )
                    continue
                meta, content = downloaded
                mime = (meta["mime"] or "").split(";")[0].strip()
                # post 的 img 段必是图片;mime 缺失或泛化时按 png 命名,避免 .bin
                if not mime or mime == "application/octet-stream":
                    mime = "image/png"
                ext = mimetypes.guess_extension(mime) or ".png"
                saved = inbox.save_attachment(
                    chat_id, index_key, f"img_{key[-8:]}{ext}", content, mime
                )
            if saved:
                logger.info(
                    "post 图片已保存: chat=%s message=%s key=%s -> inbox/%s",
                    chat_id, message_id, key, saved["saved_as"],
                )
                saved_list.append(saved)
        return saved_list

    async def _resolve_reference(
        self, chat_id: str, parent_id: str
    ) -> tuple[str, list[dict]]:
        """B 路径:文本消息回复了另一条消息时,解析被引用消息。
        返回 (注入到用户输入前的提示行, 需 VL 的已落盘图片列表);
        无法解析时返回 ("", [])。"""
        saved = inbox.find_saved(chat_id, parent_id)
        if saved:
            kind = "图片" if (saved.get("mime") or "").startswith("image/") else "文件"
            hint = (
                f"[用户引用了{kind} {saved['name']},"
                f"已保存到工作区 inbox/{saved['saved_as']}]"
            )
            return hint, [saved] if kind == "图片" else []
        parent = await self._console.get_message(parent_id)
        if not parent:
            return "", []
        p_type = parent.get("msg_type") or ""
        if p_type == "post":
            p_text, p_images = _parse_post(parent.get("content") or "")
            if p_images:
                saved_list = await self._fetch_post_images(
                    chat_id, parent_id, p_images
                )
                if not saved_list:
                    return "", []
                names = ", ".join(f"inbox/{s['saved_as']}" for s in saved_list)
                label = p_text.replace("\n", " ")
                label = label[:30] + ("…" if len(label) > 30 else "")
                return (
                    f"[用户引用了图文消息「{label}」,"
                    f"{len(saved_list)} 张图片已保存到工作区 {names}]"
                ), saved_list
            if p_text:
                label = p_text[:80] + ("…" if len(p_text) > 80 else "")
                return f"[用户引用了消息:「{label}」]", []
            return "", []
        if p_type not in ("file", "image"):
            return "", []
        saved = await self._fetch_attachment(
            chat_id, parent_id, parent.get("file") or {}
        )
        if not saved:
            return "", []
        kind = "图片" if (saved.get("mime") or "").startswith("image/") else "文件"
        hint = (
            f"[用户引用了{kind} {saved['name']},"
            f"已保存到工作区 inbox/{saved['saved_as']}]"
        )
        return hint, [saved] if kind == "图片" else []

    def _ensure_workspace(self, chat_id: str, chat_type: str, sender_open_id: str):
        """飞书会话自动供给 workspace:私聊按用户 open_id、群聊按群 chat_id,
        对用户无感知;已绑定(含仪表盘手动绑定)的不覆盖。"""
        if workspace_store.workspace_for_chat(chat_id) is not None:
            return
        try:
            if chat_type == "p2p" and sender_open_id:
                key = f"user-{sender_open_id}"
                name = f"飞书私聊 {sender_open_id[-6:]}"
            else:
                key = f"group-{chat_id}"
                name = f"飞书群 {chat_id[-6:]}"
            workspace = workspace_store.ensure_auto_workspace(key, name)
            workspace_store.bind(chat_id, workspace["id"])
            events.publish(
                {
                    "type": "workspace_bound",
                    "chat_id": chat_id,
                    "workspace": workspace,
                    "session": session_store.get_summary(chat_id),
                }
            )
            logger.info("飞书会话 %s 自动绑定 workspace %s", chat_id, workspace["root"])
        except Exception as e:
            logger.error("飞书会话 %s 自动供给 workspace 失败: %s", chat_id, e)

    async def catch_up(self):
        try:
            messages = await self._console.list_messages()
        except Exception as e:
            logger.error("补拉消息失败: %s", e)
            return
        for msg in messages:
            seq = msg.get("seq")
            if isinstance(seq, int) and seq > self._max_seen_seq:
                logger.info("补拉处理消息 #%s", seq)
                await self.handle_wire(msg)

    def mark_existing_seen(self, messages: list[dict]):
        for msg in messages:
            seq = msg.get("seq")
            if isinstance(seq, int):
                self._max_seen_seq = max(self._max_seen_seq, seq)
            message_id = msg.get("message_id") or ""
            if message_id:
                self._remember(message_id)

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.monotonic()
        while self._seen_message_ids:
            _, ts = next(iter(self._seen_message_ids.items()))
            if now - ts > config.DEDUP_TTL_SECONDS:
                self._seen_message_ids.popitem(last=False)
            else:
                break
        if message_id in self._seen_message_ids:
            return True
        self._remember(message_id)
        return False

    def _remember(self, message_id: str):
        self._seen_message_ids[message_id] = time.monotonic()
