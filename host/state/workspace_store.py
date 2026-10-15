import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """路径非法或 workspace 不存在时抛出,message 可直接作为 API 错误详情。"""


_workspaces: dict[str, dict] = {}
_roots: dict[str, str] = {}  # resolve 后的绝对路径 -> workspace_id,用于去重
_bindings: dict[str, str] = {}  # chat_id -> workspace_id


def _save():
    data = {"workspaces": list(_workspaces.values()), "bindings": dict(_bindings)}
    try:
        Path(config.WORKSPACE_STORE_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as e:
        logger.error("workspace 注册表落盘失败: %s", e)


def _load():
    store = Path(config.WORKSPACE_STORE_FILE)
    if not store.is_file():
        return
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("workspace 注册表读取失败,按空表启动: %s", e)
        return
    for w in data.get("workspaces", []):
        root = w.get("root")
        ws_id = w.get("id")
        # 目录已被删除的注册项跳过,其绑定关系一并丢弃
        if not ws_id or not root or not Path(root).is_dir():
            continue
        _workspaces[ws_id] = w
        _roots[root] = ws_id
    for chat_id, ws_id in data.get("bindings", {}).items():
        if ws_id in _workspaces:
            _bindings[chat_id] = ws_id


_load()


def open_workspace(path: str, name: Optional[str] = None, create: bool = False) -> dict:
    raw = (path or "").strip()
    if not raw:
        raise WorkspaceError("path 不能为空")
    root = Path(raw).expanduser()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise WorkspaceError(f"目录不存在: {raw}")
    resolved = str(root.resolve())

    existing_id = _roots.get(resolved)
    if existing_id is not None:
        return _workspaces[existing_id]

    workspace = {
        "id": uuid.uuid4().hex[:8],
        "name": (name or "").strip() or Path(resolved).name,
        "root": resolved,
        "created_at": time.time(),
    }
    _workspaces[workspace["id"]] = workspace
    _roots[resolved] = workspace["id"]
    _save()
    return workspace


def ensure_auto_workspace(key: str, name: str) -> dict:
    """渠道自动供给:<WORKSPACE_AUTO_ROOT>/<key> 下按需建目录并注册,幂等。"""
    return open_workspace(str(Path(config.WORKSPACE_AUTO_ROOT) / key), name=name, create=True)


def list_workspaces() -> list[dict]:
    result = []
    for w in sorted(_workspaces.values(), key=lambda w: w["created_at"], reverse=True):
        chat_ids = sorted(c for c, wid in _bindings.items() if wid == w["id"])
        result.append({**w, "chat_ids": chat_ids})
    return result


def bound_chats() -> list[str]:
    """所有有绑定的 chat_id(含内存中暂无会话的,用于重启后恢复展示)。"""
    return sorted(_bindings)


def get_workspace(workspace_id: str) -> Optional[dict]:
    return _workspaces.get(workspace_id)


def bind(chat_id: str, workspace_id: str) -> dict:
    workspace = _workspaces.get(workspace_id)
    if workspace is None:
        raise WorkspaceError(f"workspace 不存在: {workspace_id}")
    _bindings[chat_id] = workspace_id
    _save()
    return workspace


def unbind(chat_id: str) -> bool:
    if _bindings.pop(chat_id, None) is None:
        return False
    _save()
    return True


def workspace_for_chat(chat_id: str) -> Optional[dict]:
    workspace_id = _bindings.get(chat_id)
    return _workspaces.get(workspace_id) if workspace_id else None


def root_for_chat(chat_id: str) -> Optional[str]:
    workspace = workspace_for_chat(chat_id)
    return workspace["root"] if workspace else None
