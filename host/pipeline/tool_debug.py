# -*- coding: utf-8 -*-
"""工具手动调试:目录构建 + 不走 LLM 的直接调用分发(仪表盘「工具调试」面板后端)。

面板分工(2026-10-12 起):本面板只调试宿主自实现工具——kb/记忆/workspace 文件/
write_deliverable,以及 web_fetch 宿主 wrapper(二进制物化 + 每轮限额是宿主行为);
web_search/amap_*/web_fetch 抓取核心等 MCP 工具的调试在 MCP 调试台(:8003,
列表即真实注册表,所见即实挂),/api/tools 响应带 mcp_debug_url 供面板跳转。

目录条目与正式链路(capability_resolver 的 provider 链)同源——工厂函数直接复用、
提示语引用同一批常量,保证"看到的即模型所见"。调用与正式链路完全隔离:
每次调试新建工厂实例/调试 agent,不碰任何会话状态与 turn 记录。

调试调用会真实访问外部 API(高德/搜索/知识库)并真实写 workspace/记忆,
这正是调试目的;信任假设与 /api/fs/*、/dev/simulate 一致(本机仪表盘用户)。
"""
import inspect
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from agently import Agently

from .. import config
from ..capabilities import kb_client, mcp_client, web_actions
from ..state import workspace_store
from . import capability_resolver

logger = logging.getLogger(__name__)

DEBUG_CHAT_ID = "__tool_debug__"

FILE_TOOL_NAMES = ("list_files", "read_file", "search_files", "write_file")

# 无任何已打开 workspace 时文件工具的静态兜底(签名与框架
# enable_task_workspace_file_actions 注册一致,Agently 4.1.4.7);
# 有可用 workspace 时一律以调试 agent 的 get_action_info() 为准
_FILE_TOOL_FALLBACK = {
    "list_files": [
        ("path", "str", "."),
        ("pattern", "str", "*"),
        ("max_results", "int", 200),
        ("include_hidden", "bool", False),
    ],
    "read_file": [("path", "str", None), ("max_bytes", "int", 20000), ("offset", "int", 0)],
    "search_files": [
        ("query", "str", None),
        ("path", "str", "."),
        ("pattern", "str", "*"),
        ("max_results", "int", 50),
        ("include_hidden", "bool", False),
    ],
    "write_file": [
        ("path", "str", None),
        ("content", "str", None),
        ("append", "bool", False),
        ("expected_sha256", "str", ""),
    ],
}


class ToolNotFoundError(Exception):
    pass


class WorkspaceRequiredError(Exception):
    pass


def _params_of(fn) -> list[dict]:
    """从 callable 签名生成参数表(与 Agently use_actions 同一信息源)。"""
    params = []
    for name, p in inspect.signature(fn).parameters.items():
        ann = p.annotation
        type_name = getattr(ann, "__name__", None) or (
            str(ann) if ann is not inspect.Parameter.empty else "str"
        )
        has_default = p.default is not inspect.Parameter.empty
        params.append(
            {
                "name": name,
                "type": type_name,
                "has_default": has_default,
                "default": p.default if has_default else None,
                "required": not has_default,
            }
        )
    return params


def _callable_entry(
    name,
    fn,
    group: str,
    group_label: str,
    instruct: str,
    enabled: bool,
    disabled_reason: str = "",
    needs_workspace: bool = False,
    note: str = "",
) -> dict:
    return {
        "name": name,
        "group": group,
        "group_label": group_label,
        "description": inspect.getdoc(fn) or "",
        "params": _params_of(fn),
        "instruct": instruct,
        "enabled": enabled,
        "disabled_reason": "" if enabled else disabled_reason,
        "needs_workspace": needs_workspace,
        "note": note,
    }


def _require_workspace(workspace_id: Optional[str]) -> dict:
    if not workspace_id:
        raise WorkspaceRequiredError("该工具需要选择一个已打开的 workspace")
    ws = workspace_store.get_workspace(workspace_id)
    if not ws:
        raise WorkspaceRequiredError(f"workspace 不存在: {workspace_id}")
    return ws


def _make_debug_agent(ws_root: str):
    """按 capability_resolver.WorkspaceProvider 同款挂载方式构建调试 agent(纯本地操作,不触发 LLM)。"""
    read_only = config.WORKSPACE_READ_ONLY
    agent = Agently.create_agent(name=f"tool-debug-{uuid.uuid4().hex[:8]}").instruct(
        config.SYSTEM_PROMPT
    )
    agent.use_task_workspace(ws_root, mode="read_only" if read_only else "read_write")
    # 与 capability_resolver.WorkspaceProvider 相同:execution 及其 context 的 workspace 副本需同步派生,
    # 否则文件 action 落到默认空 workspace
    tw = agent.agent.task_workspace._derive(execution_id=agent.id)
    agent.task_workspace = tw
    agent.execution_context.task_workspace = tw
    agent.enable_task_workspace_file_actions(
        read=True, write=not read_only, search=True, list_files=True
    )
    return agent


def _file_tool_entries(ws: Optional[dict]) -> list[dict]:
    """workspace 文件工具目录条目;有可用 workspace 时从调试 agent 实况提取。"""
    enabled = ws is not None
    reason = "" if enabled else "先在顶部选择一个已打开的 workspace"
    if ws is not None:
        try:
            info = _make_debug_agent(ws["root"]).action.get_action_info()
        except Exception as e:
            logger.warning("调试 agent 构建失败,文件工具用静态兜底: %s", e)
            info = None
        if info:
            entries = []
            for name in FILE_TOOL_NAMES:
                spec = info.get(name)
                if not spec:
                    continue
                required = set(spec.get("required_input_keys") or [])
                params = [
                    {
                        "name": k,
                        "type": getattr(v[0], "__name__", str(v[0])),
                        "description": str(v[1]) if len(v) > 1 else "",
                        "required": k in required,
                    }
                    for k, v in (spec.get("kwargs") or {}).items()
                ]
                entries.append(
                    {
                        "name": name,
                        "group": "workspace",
                        "group_label": "工作区文件",
                        "description": str(spec.get("desc") or ""),
                        "params": params,
                        "instruct": capability_resolver.WS_INBOX_INSTRUCT,
                        "enabled": enabled,
                        "disabled_reason": reason,
                        "needs_workspace": True,
                    }
                )
            if entries:
                return entries
    entries = []
    for name, params in _FILE_TOOL_FALLBACK.items():
        entries.append(
            {
                "name": name,
                "group": "workspace",
                "group_label": "工作区文件",
                "description": "框架内置文件工具(选择 workspace 后显示完整描述)",
                "params": [
                    {
                        "name": p_name,
                        "type": p_type,
                        "has_default": p_default is not None,
                        "default": p_default,
                        "required": p_default is None,
                    }
                    for p_name, p_type, p_default in params
                ],
                "instruct": capability_resolver.WS_INBOX_INSTRUCT,
                "enabled": enabled,
                "disabled_reason": reason,
                "needs_workspace": True,
            }
        )
    return entries


def build_catalog(workspace_id: Optional[str] = None) -> list[dict]:
    """工具目录:名称/描述(模型所见 docstring)/参数/挂载提示语/启用状态。"""
    entries: list[dict] = []

    # 面板分工(2026-10-12 起):web_search/amap_* 等 MCP 工具的调试在 MCP 调试台
    # (:8003,列表即真实注册表);本面板只保留宿主自实现工具。web_fetch 是唯一
    # "半宿主"条目:wrapper(二进制物化+每轮限额)在宿主,HTML 抓取委托 MCP 内部
    # 核心,故启用状态仍依赖 MCP 可达性;选中了 workspace 时二进制会真实落盘进去。
    mcp_ok = mcp_client.mcp_enabled()
    web_ok = mcp_ok and config.WEB_ENABLED
    web_reason = "MCP_ENABLED=false 或 MCP 服务器不可达" if not mcp_ok else "WEB_ENABLED=false"
    for name, fn in web_actions.make_web_actions(DEBUG_CHAT_ID).items():
        entries.append(
            _callable_entry(
                name, fn, "web", "网络", capability_resolver.WEB_INSTRUCT, web_ok, web_reason,
                note="宿主 wrapper:HTML 抓取委托 MCP 内部核心(抓取/渲染/缓存的单独调试在 MCP 调试台);"
                "二进制物化进所选 workspace,未选择时返回提示文案",
            )
        )

    kb_ok = kb_client.kb_enabled()
    kb_actions = kb_client.make_kb_actions(DEBUG_CHAT_ID)["actions"]
    for name, fn in kb_actions.items():
        entries.append(
            _callable_entry(
                name, fn, "kb", "知识库", capability_resolver.KB_INSTRUCT,
                kb_ok, "KB_ENABLED=false 或 kb_service 不可达",
            )
        )

    memory_ok = config.MEMORY_ENABLED and kb_client.kb_enabled()
    entries.append(
        _callable_entry(
            "kb_remember", capability_resolver.make_kb_remember(DEBUG_CHAT_ID, None),
            "memory", "长期记忆", capability_resolver.REMEMBER_INSTRUCT,
            memory_ok, "MEMORY_ENABLED=false 或 kb_service 不可达",
        )
    )

    ws = None
    if workspace_id:
        ws = workspace_store.get_workspace(workspace_id)
    ws_reason = "" if ws else "先在顶部选择一个已打开的 workspace"
    entries.append(
        _callable_entry(
            "write_deliverable",
            capability_resolver.make_write_deliverable(Path("__debug_outbox__")),
            "workspace", "工作区文件", capability_resolver.DELIVER_INSTRUCT,
            ws is not None and not config.WORKSPACE_READ_ONLY,
            ws_reason or "WORKSPACE_READ_ONLY=true(只读试点)",
            needs_workspace=True,
        )
    )
    entries.extend(_file_tool_entries(ws))
    return entries


def _convert(value, annotation):
    """表单字符串按签名注解转型;非字符串(前端已给正确 JSON 类型)原样透传。"""
    if not isinstance(value, str):
        return value
    if annotation is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if annotation is int:
        return int(value)
    if annotation is float:
        return float(value)
    return value


def _coerce(fn, args: dict) -> dict:
    sig = inspect.signature(fn)
    coerced = {}
    for key, value in (args or {}).items():
        param = sig.parameters.get(key)
        if param is None:
            raise ValueError(f"未知参数: {key}")
        coerced[key] = _convert(value, param.annotation)
    return coerced


def _coerce_by_types(args: dict, types: dict) -> dict:
    coerced = {}
    for key, value in (args or {}).items():
        if key not in types:
            raise ValueError(f"未知参数: {key}")
        coerced[key] = _convert(value, types[key])
    return coerced


async def _dispatch(name: str, args: dict, workspace_id: Optional[str]):
    if name in FILE_TOOL_NAMES:
        ws = _require_workspace(workspace_id)
        agent = _make_debug_agent(ws["root"])
        spec = agent.action.get_action_info().get(name)
        if spec is None:
            raise ToolNotFoundError(f"文件工具不可用: {name}")
        types = {k: v[0] for k, v in (spec.get("kwargs") or {}).items()}
        return await agent.action.async_call_action(name, _coerce_by_types(args, types))
    if name == "write_deliverable":
        ws = _require_workspace(workspace_id)
        fn = capability_resolver.make_write_deliverable(Path(ws["root"]) / "outbox")
        return await fn(**_coerce(fn, args))
    # web_fetch 宿主 wrapper:与模型同款全路径(MCP 内部抓取 + 二进制物化);
    # 选中 workspace 时物化真实落盘到其 downloads/,未选中则返回"需绑定 workspace"提示
    if name == "web_fetch":
        if not config.WEB_ENABLED:
            raise ToolNotFoundError("web 组已禁用(WEB_ENABLED=false),web_fetch 宿主 wrapper 不可用")
        ws = workspace_store.get_workspace(workspace_id) if workspace_id else None
        fn = web_actions.make_web_actions(
            DEBUG_CHAT_ID, materialize_root=Path(ws["root"]) if ws else None
        )["web_fetch"]
        return await fn(**_coerce(fn, args))
    factories: dict = {}
    factories.update(kb_client.make_kb_actions(DEBUG_CHAT_ID)["actions"])
    factories["kb_remember"] = capability_resolver.make_kb_remember(DEBUG_CHAT_ID, None)
    fn = factories.get(name)
    if fn is None:
        raise ToolNotFoundError(
            f"未知工具: {name}(web_search/amap_* 等 MCP 工具请前往 MCP 调试台 {config.MCP_SERVER_URL} 调试)"
        )
    return await fn(**_coerce(fn, args))


async def call_tool(name: str, args: dict, workspace_id: Optional[str] = None) -> dict:
    """手动调用一个工具,返回 {ok, result|error, elapsed_ms};异常不抛到路由层。"""
    started = time.monotonic()
    try:
        result = await _dispatch(name, args or {}, workspace_id)
    except (ToolNotFoundError, WorkspaceRequiredError):
        raise
    except Exception as e:
        logger.warning("工具调试调用失败: %s(%s): %s", name, args, e)
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    try:
        payload = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        payload = str(result)
    logger.info("工具调试调用: %s(%s) 成功", name, args)
    return {
        "ok": True,
        "result": payload,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
