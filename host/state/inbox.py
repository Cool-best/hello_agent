"""会话收件箱:附件落盘 + 幂等索引(渠道中立)。

精确定位键是 message_id,不是文件名:同名文件各自落盘(_dedupe 加 (1) 后缀),
.index.json 按 message_id 独立记录;引用回复按 parent_id(=被引用消息的 message_id)
查索引或回源拉取,同名文件不会串。post 内嵌图片用 `message_id:image_key` 复合键,
一条多图消息内各图独立幂等。索引只是幂等缓存,miss 不等于失败——重启后
重新引用旧文件消息会重新走下载落盘。
"""
import json
import logging
import re
from pathlib import Path
from typing import Optional

from . import workspace_store

logger = logging.getLogger(__name__)

INBOX_DIR = "inbox"
_INDEX_FILE = ".index.json"


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return name or "unnamed"


def _dedupe(inbox: Path, name: str) -> str:
    if not (inbox / name).exists():
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while (inbox / f"{stem}({i}){suffix}").exists():
        i += 1
    return f"{stem}({i}){suffix}"


def _load_index(inbox: Path) -> dict:
    index = inbox / _INDEX_FILE
    if not index.is_file():
        return {}
    try:
        return json.loads(index.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("收件箱索引读取失败,按空索引处理: %s", e)
        return {}


def _save_index(inbox: Path, index: dict):
    (inbox / _INDEX_FILE).write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def find_saved(chat_id: str, message_id: str) -> Optional[dict]:
    """查该会话收件箱里是否已保存过这条消息的附件。"""
    root = workspace_store.root_for_chat(chat_id)
    if not root:
        return None
    index = _load_index(Path(root) / INBOX_DIR)
    saved_as = index.get(message_id)
    if saved_as and (Path(root) / INBOX_DIR / saved_as).is_file():
        target = Path(root) / INBOX_DIR / saved_as
        return {
            "name": saved_as,
            "saved_as": saved_as,
            "path": str(target),
            "size": target.stat().st_size,
            "mime": "",
        }
    return None


def save_attachment(
    chat_id: str,
    message_id: str,
    name: str,
    content: bytes,
    mime: str = "",
) -> Optional[dict]:
    """把附件写入该会话 workspace 的 inbox/,幂等(message_id 索引命中直接复用)。

    会话未绑定 workspace 时返回 None,由渠道决定兜底话术。
    """
    root = workspace_store.root_for_chat(chat_id)
    if not root:
        logger.warning("会话 %s 未绑定 workspace,附件 %s 无法保存", chat_id, name)
        return None
    inbox = Path(root) / INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)

    index = _load_index(inbox)
    saved_as = index.get(message_id)
    if saved_as and (inbox / saved_as).is_file():
        return {
            "name": name,
            "saved_as": saved_as,
            "path": str(inbox / saved_as),
            "size": (inbox / saved_as).stat().st_size,
            "mime": mime,
        }

    saved_as = _dedupe(inbox, _sanitize(name))
    target = inbox / saved_as
    try:
        target.write_bytes(content)
        index[message_id] = saved_as
        _save_index(inbox, index)
    except OSError as e:
        logger.error("附件写入失败 %s: %s", target, e)
        target.unlink(missing_ok=True)
        return None
    return {
        "name": name,
        "saved_as": saved_as,
        "path": str(target),
        "size": len(content),
        "mime": mime,
    }
