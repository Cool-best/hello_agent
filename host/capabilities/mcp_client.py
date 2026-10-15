# -*- coding: utf-8 -*-
"""工具 MCP 服务器客户端:宿主侧挂载/直调入口(与 kb_client.py 同款哲学)。

- mcp_enabled():总开关 + 启动探测缓存;不可达按未启用处理(web/amap 不挂载);
- public_transport(chat_id):agent.async_use_mcp 的 transport,携带 x-chat-id
  供 MCP 服务器侧记录调用来源与限流;
- call_tool():fastmcp Client 直调(宿主 web_fetch wrapper、工具调试面板用),
  返回解包后的 result.data;异常抛给调用方归一化为错误文案。
"""
import json
import logging

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .. import config

logger = logging.getLogger(__name__)

# 启动时 health 检查结果缓存: True/False/None(未检查)
_available: bool | None = None


async def check_available() -> bool:
    """探测 MCP 服务器 /health 并缓存结果。启动时与 /api/tools 面板刷新时调用。"""
    global _available
    if not config.MCP_ENABLED:
        _available = False
        return False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{config.MCP_SERVER_URL}/health")
            _available = response.status_code == 200 and bool(response.json().get("ok"))
    except Exception as e:
        logger.warning("MCP 服务器健康检查失败: %s", e)
        _available = False
    return _available


def mcp_enabled() -> bool:
    """同步开关: 配置开启且启动探测可达。未探测(如探针脚本)按配置值。"""
    if not config.MCP_ENABLED:
        return False
    return _available if _available is not None else True


def public_transport(chat_id: str) -> dict:
    """模型可见工具的挂载 transport(公开 app),供 agent.async_use_mcp 使用。

    必须是 {"mcpServers": {...}} 完整形态(fastmcp MCPConfigTransport 只认这个);
    x-chat-id 随每次调用透传,供服务器侧记录调用来源与限流。
    """
    return {
        "mcpServers": {
            "agent-tools": {
                "url": f"{config.MCP_SERVER_URL}/mcp",
                "headers": {"x-chat-id": chat_id},
            }
        }
    }


def internal_transport(chat_id: str | None = None) -> StreamableHttpTransport:
    """web_fetch 抓取核心的直连 transport(内部 app,不挂给模型)。"""
    headers = {"x-chat-id": chat_id} if chat_id else {}
    return StreamableHttpTransport(
        f"{config.MCP_SERVER_URL}/mcp-internal", headers=headers
    )


async def call_tool(
    name: str, args: dict, chat_id: str | None = None, internal: bool = False
):
    """直调 MCP 工具,返回解包后的原始返回值;失败抛异常由调用方归一化。"""
    if internal:
        transport = internal_transport(chat_id)
    else:
        server_conf = public_transport(chat_id or "")["mcpServers"]["agent-tools"]
        transport = StreamableHttpTransport(
            server_conf["url"], headers=server_conf["headers"]
        )
    async with Client(transport) as client:
        result = await client.call_tool(name, args, timeout=config.MCP_TIMEOUT)
    # list/str 返回的 structured_content 可能是 {"result": ...} 包装或为空
    # (空时 text content 是 JSON 串),统一拆包成原始返回值
    data = result.data
    if data is None:
        sc = result.structured_content
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            data = sc["result"]
        elif sc is not None:
            data = sc
    if data is None:
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = text
    return data
