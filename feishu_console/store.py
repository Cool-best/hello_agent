import json
import mimetypes
import threading
from datetime import datetime


def build_wire(msg: dict, sender: dict | None = None, seq: int | None = None) -> dict:
    """把飞书消息统一整理为 wire dict。

    兼容两种来源：长连接事件的 event.message（message_type + content）
    和 GET /im/v1/messages/:id 返回的消息体（msg_type + body.content）。
    """
    msg_type = msg.get("message_type") or msg.get("msg_type")
    content_raw = (msg.get("body") or {}).get("content") or msg.get("content") or "{}"
    try:
        content_obj = json.loads(content_raw)
    except (json.JSONDecodeError, AttributeError, TypeError):
        content_obj = {}

    record = {
        "seq": seq,
        "message_id": msg.get("message_id"),
        "chat_id": msg.get("chat_id"),
        "chat_type": msg.get("chat_type"),
        "msg_type": msg_type,
        # 回复/引用关系：被回复消息的 message_id，无则为 None
        "parent_id": msg.get("parent_id"),
        "sender_open_id": ((sender or msg.get("sender") or {}).get("sender_id") or {}).get("open_id"),
        "content": content_obj.get("text", content_raw) if isinstance(content_obj, dict) else content_raw,
    }
    # 文件/图片/视频类消息：提取资源 key 和文件名，便于下载
    if msg_type in ("file", "media", "image") and isinstance(content_obj, dict):
        if msg_type == "image":
            file_key = content_obj.get("image_key") or content_obj.get("file_key")
        else:
            file_key = content_obj.get("file_key") or content_obj.get("image_key")
        name = content_obj.get("file_name") or (
            f"{file_key}.png" if msg_type == "image" and file_key else f"{msg_type}_{msg.get('message_id')}"
        )
        record["file_key"] = file_key
        record["file_name"] = name
        record["res_type"] = "image" if msg_type == "image" else "file"
        record["content"] = f"[{msg_type}] {name}"
        # 结构化的附件信息（agent 下载用），size 消息体里没有时置 None
        if msg_type in ("file", "image"):
            mime = (
                content_obj.get("mime")
                or mimetypes.guess_type(name)[0]
                or ("image/png" if msg_type == "image" else "application/octet-stream")
            )
            record["file"] = {
                "file_key": file_key,
                "name": name,
                "size": content_obj.get("size"),
                "mime": mime,
            }
    return record


class MessageStore:
    """内存消息存储：递增序号 + event_id 去重（飞书会重推事件）。"""

    def __init__(self):
        self._messages = []
        self._seen_event_ids = set()
        self._lock = threading.Lock()

    def add(self, event: dict) -> dict | None:
        header = event.get("header", {})
        event_id = header.get("event_id")
        with self._lock:
            if event_id and event_id in self._seen_event_ids:
                return None
            if event_id:
                self._seen_event_ids.add(event_id)

            e = event.get("event", {})
            record = build_wire(e.get("message", {}), sender=e.get("sender"), seq=len(self._messages) + 1)
            record["event_id"] = event_id
            record["raw_json"] = json.dumps(event, ensure_ascii=False, indent=2)
            record["time"] = datetime.now().strftime("%H:%M:%S")
            self._messages.append(record)
            return record

    def list(self) -> list[dict]:
        with self._lock:
            return list(self._messages)

    def get(self, seq: int) -> dict | None:
        with self._lock:
            for m in self._messages:
                if m["seq"] == seq:
                    return m
        return None

    def mark_recalled(self, message_id: str) -> dict | None:
        """给内存中对应消息记录置 recalled 标记；找不到返回 None。"""
        with self._lock:
            for m in self._messages:
                if m.get("message_id") == message_id:
                    m["recalled"] = True
                    return m
        return None


store = MessageStore()


class RecallRegistry:
    """撤回事件登记簿：独立于消息存储，撤回事件可能指向 store 中没有的消息。"""

    def __init__(self):
        self._records: dict[str, dict] = {}
        self._lock = threading.Lock()

    def mark(self, message_id: str, chat_id: str, recall_time: str, recall_type: str):
        with self._lock:
            self._records[message_id] = {
                "chat_id": chat_id,
                "recall_time": recall_time,
                "recall_type": recall_type,
            }

    def get(self, message_id: str) -> dict | None:
        with self._lock:
            rec = self._records.get(message_id)
            return dict(rec) if rec else None


recalled = RecallRegistry()
