import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from host import config, events
from host.capabilities import kb_client, mcp_client, skill_registry
from host.channels.channel_base import InboundMessage
from host.channels.web_channel import WebSink
from host.pipeline import tool_debug
from host.pipeline.chat_pipeline import Pipeline, _turn_created_event, _turn_status_event
from host.state import session_store, workspace_store

# 长上下文评测(evals/,可选):发布副本不含评测模块,缺失时评测端点返回未安装提示
try:
    from evals import long_context
except ImportError:
    long_context = None

# 飞书操作台渠道(可选):host/channels/console_client.py/feishu_channel.py 存在且配置了
# CONSOLE_BASE_URL 时自动启用,否则按纯网页对话运行
try:
    from host.channels.console_client import ConsoleClient, WsListener
    from host.channels.feishu_channel import FeishuChannel

    CONSOLE_AVAILABLE = True
except ImportError:
    ConsoleClient = WsListener = FeishuChannel = None
    CONSOLE_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")
logger = logging.getLogger(__name__)

STATIC_FILE = Path(__file__).parent / "static" / "dashboard.html"


class SimulateBody(BaseModel):
    chat_id: str
    text: str
    seq: int | None = None


class ChatBody(BaseModel):
    text: str
    chat_id: str | None = None


class WorkspaceOpenBody(BaseModel):
    path: str
    name: str | None = None
    create: bool = False


class WorkspaceBindBody(BaseModel):
    chat_id: str


class FsMkdirBody(BaseModel):
    path: str
    name: str


class ToolCallBody(BaseModel):
    name: str
    args: dict = {}
    workspace_id: str | None = None


def console_enabled() -> bool:
    return CONSOLE_AVAILABLE and bool(config.CONSOLE_BASE_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    config.apply_llm_settings(logger)
    # 项目内置 skills(全局)启动时安装一次;会话 workspace 内 skills 由 chat_pipeline 每轮增量安装
    skill_registry.install_project_skills()
    session_store.load()  # 恢复重启前的会话记录(仪表盘可见)
    # kb_service 启动探测: 不可达按未启用处理(与 WEB_ENABLED 缺 key 同款哲学)
    if config.KB_ENABLED:
        if await kb_client.check_available():
            logger.info("kb_service 已连接: %s", config.KB_SERVICE_URL)
        else:
            logger.warning(
                "KB_ENABLED=true 但 kb_service 不可达(%s),知识库按未启用处理",
                config.KB_SERVICE_URL,
            )
    # 工具 MCP 服务器启动探测: 不可达则 web/amap 能力不挂载(fail-soft)
    if config.MCP_ENABLED:
        if await mcp_client.check_available():
            logger.info("工具 MCP 服务器已连接: %s", config.MCP_SERVER_URL)
        else:
            logger.warning(
                "MCP_ENABLED=true 但 MCP 服务器不可达(%s),web/amap 能力按未启用处理",
                config.MCP_SERVER_URL,
            )

    pipeline = Pipeline()
    console = None
    feishu = None
    listener = None
    ws_task = None
    if console_enabled():
        console = ConsoleClient()
        feishu = FeishuChannel(console, pipeline)
        listener = WsListener(feishu.handle_wire)
    else:
        logger.info(
            "飞书渠道未启用(缺少渠道模块或未配置 CONSOLE_BASE_URL),按纯网页对话运行"
        )
    app.state.console = console
    app.state.pipeline = pipeline
    app.state.feishu = feishu
    app.state.listener = listener

    if console is not None:
        try:
            feishu.mark_existing_seen(await console.list_messages())
        except Exception as e:
            logger.warning("启动时拉取存量消息失败: %s", e)
        ws_task = asyncio.create_task(listener.run(feishu.catch_up))

    # 重启恢复告知:load 已把上次退出时仍在进行中的轮次落为 failed(标记
    # INTERRUPTED_ERROR);逐个通知来源渠道,避免用户对着冻结在"生成中"的
    # 卡片干等。已告知过的(error 带"(已告知)"后缀)不重复通知。
    for chat_id, turn in session_store.consume_interrupted_turns():
        logger.info("重启中断轮次: chat=%s turn=%s input=%.30s", chat_id, turn.id, turn.input)
        events.publish(_turn_status_event(chat_id, turn))
        if turn.source == "feishu" and console is not None and turn.seq is not None:
            notice = (
                "⚠️ 服务重启,你 "
                + time.strftime("%H:%M", time.localtime(turn.created_at))
                + f" 的消息「{turn.input[:30]}」处理被中断,请重新发送。"
            )
            try:
                await console.reply(turn.seq, notice)
            except Exception as e:  # noqa: BLE001 — 告知失败不影响启动
                logger.warning("重启中断告知发送失败(chat=%s): %s", chat_id, e)

    yield

    if listener is not None:
        await listener.stop()
    if ws_task is not None:
        ws_task.cancel()
    if console is not None:
        await console.close()


app = FastAPI(title="飞书自动对话 Agent 后台", lifespan=lifespan)


@app.get("/")
async def dashboard():
    return FileResponse(STATIC_FILE)


@app.get("/health")
async def health():
    listener = getattr(app.state, "listener", None)
    llm_ep = config.resolve_llm_endpoint()
    return {
        "status": "ok",
        "console_enabled": console_enabled(),
        "console_ws_connected": bool(listener and listener.connected),
        "kb_enabled": kb_client.kb_enabled(),
        "mcp_enabled": mcp_client.mcp_enabled(),
        "llm": {"model": llm_ep["model"], "base_url": llm_ep["base_url"], "via": llm_ep["via"]},
        "sessions": len(session_store.list_sessions()),
    }


@app.get("/sessions")
async def list_sessions():
    return session_store.list_sessions()


@app.delete("/sessions/{chat_id}")
async def clear_session(chat_id: str):
    cleared = session_store.clear(chat_id)
    if cleared:
        events.publish({"type": "session_cleared", "chat_id": chat_id})
    return {"cleared": cleared}


@app.get("/api/overview")
async def api_overview():
    data = session_store.snapshot()
    listener = getattr(app.state, "listener", None)
    data["console_enabled"] = console_enabled()
    data["console_ws_connected"] = bool(listener and listener.connected)
    data["kb"] = await kb_client.service_status()
    data["mcp"] = {"enabled": mcp_client.mcp_enabled(), "url": config.MCP_SERVER_URL}
    llm_ep = config.resolve_llm_endpoint()
    data["llm"] = {
        "model": llm_ep["model"],
        "base_url": llm_ep["base_url"],
        "via": llm_ep["via"],
    }
    for session in data["sessions"]:
        session["workspace"] = workspace_store.workspace_for_chat(session["chat_id"])
    # 重启后会话是内存态、已丢,但绑定关系落盘还在:为有绑定而无内存会话的
    # chat_id 合成占位条目,让仪表盘能看到"该 workspace 下有哪些会话"并可选中续聊
    existing = {s["chat_id"] for s in data["sessions"]}
    for chat_id in workspace_store.bound_chats():
        if chat_id in existing:
            continue
        workspace = workspace_store.workspace_for_chat(chat_id)
        if not workspace:
            continue
        data["sessions"].append(
            {
                "chat_id": chat_id,
                "status": "idle",
                "created_at": 0,
                "updated_at": 0,
                "turn_count": 0,
                "message_count": 0,
                "history": [],
                "turns": [],
                "workspace": workspace,
                "stub": True,
            }
        )
    data["workspaces"] = workspace_store.list_workspaces()
    return data


@app.get("/api/workspaces")
async def api_list_workspaces():
    return workspace_store.list_workspaces()


@app.post("/api/workspaces")
async def api_open_workspace(body: WorkspaceOpenBody):
    try:
        return workspace_store.open_workspace(body.path, body.name, body.create)
    except workspace_store.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/workspaces/{workspace_id}/bind")
async def api_bind_workspace(workspace_id: str, body: WorkspaceBindBody):
    chat_id = body.chat_id.strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id 不能为空")
    try:
        workspace = workspace_store.bind(chat_id, workspace_id)
    except workspace_store.WorkspaceError as e:
        raise HTTPException(status_code=404, detail=str(e))
    events.publish(
        {
            "type": "workspace_bound",
            "chat_id": chat_id,
            "workspace": workspace,
            "session": session_store.get_summary(chat_id),
        }
    )
    return {"ok": True, "chat_id": chat_id, "workspace": workspace}


@app.post("/api/sessions/{chat_id}/unbind")
async def api_unbind_workspace(chat_id: str):
    unbound = workspace_store.unbind(chat_id)
    if unbound:
        events.publish(
            {
                "type": "workspace_unbound",
                "chat_id": chat_id,
                "session": session_store.get_summary(chat_id),
            }
        )
    return {"unbound": unbound}


# workspace 文件下载:产物交付给 web 用户;resolve 后必须在 workspace 根内
@app.get("/api/workspaces/{workspace_id}/file")
async def api_workspace_file(workspace_id: str, path: str = ""):
    ws = workspace_store.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="workspace 不存在")
    raw = (path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path 不能为空")
    root = Path(ws["root"]).resolve()
    target = (root / raw.lstrip("/\\")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="路径越界")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {raw}")
    return FileResponse(target, filename=target.name)


# 目录浏览:服务端即用户本机,供仪表盘弹窗式选择 workspace 目录
@app.get("/api/fs/list")
async def api_fs_list(path: str = ""):
    raw = (path or "").strip()
    if not raw:
        drives = [
            f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")
        ]
        return {"path": "", "parent": None, "dirs": [{"name": d, "path": d} for d in drives]}
    p = Path(raw).expanduser().resolve()
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在: {raw}")
    try:
        entries = [e for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")]
    except PermissionError:
        raise HTTPException(status_code=400, detail=f"无权限访问: {raw}")
    entries.sort(key=lambda e: e.name.lower())
    return {
        "path": str(p),
        "parent": None if p.parent == p else str(p.parent),
        "dirs": [{"name": e.name, "path": str(e)} for e in entries[:500]],
    }


@app.post("/api/fs/mkdir")
async def api_fs_mkdir(body: FsMkdirBody):
    name = body.name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="文件夹名非法")
    base = Path(body.path).expanduser().resolve()
    if not base.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在: {body.path}")
    target = base / name
    try:
        target.mkdir(exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"创建失败: {e}")
    return {"name": name, "path": str(target)}


@app.get("/api/events")
async def api_events():
    queue = events.subscribe()

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
async def api_chat(body: ChatBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    chat_id = (body.chat_id or "").strip() or f"web-{uuid.uuid4().hex[:8]}"
    turn = session_store.create_turn(chat_id, None, "web", text)
    events.publish(_turn_created_event(chat_id, turn))
    asyncio.create_task(
        app.state.pipeline.handle_message(
            InboundMessage(chat_id=chat_id, text=text, source="web"),
            WebSink(),
            turn=turn,
        )
    )
    return {"ok": True, "chat_id": chat_id, "turn_id": turn.id}


# ── 长上下文会话评测(evals/long_context.py, 剧本回放 + 关键点断言) ──

@app.get("/api/eval/long_context")
async def api_eval_long_context():
    if long_context is None:
        return {"status": {"running": False}, "report": None, "history": [], "msg": "评测模块(evals/)未安装"}
    return {
        "status": long_context.status(),
        "report": long_context.latest_report(),
        "history": long_context.report_history(),
    }


@app.post("/api/eval/long_context/run")
async def api_eval_long_context_run():
    if long_context is None:
        return {"ok": False, "msg": "评测模块(evals/)未安装"}
    if long_context.status()["running"]:
        return {"ok": False, "msg": "评测进行中"}
    asyncio.create_task(long_context.run_all(app.state.pipeline))
    return {"ok": True}


# ── 工具手动调试(tool_debug.py,不走 LLM 直接调用,验证工具好坏) ──

@app.get("/api/tools")
async def api_tools(workspace_id: str | None = None):
    if config.MCP_ENABLED:
        # 刷新可达性缓存,让面板实时反映 MCP 服务器中途宕机/恢复
        await mcp_client.check_available()
    return {
        "tools": tool_debug.build_catalog(workspace_id or None),
        # MCP 工具的调试入口在 MCP 调试台(列表即真实注册表),面板据此放跳转链接
        "mcp_debug_url": config.MCP_SERVER_URL if config.MCP_ENABLED else None,
    }


@app.post("/api/tools/call")
async def api_tool_call(body: ToolCallBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    try:
        result = await tool_debug.call_tool(name, body.args, body.workspace_id)
    except tool_debug.ToolNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tool_debug.WorkspaceRequiredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.post("/dev/simulate")
async def dev_simulate(body: SimulateBody):
    feishu = getattr(app.state, "feishu", None)
    if feishu is None:
        raise HTTPException(status_code=400, detail="飞书渠道未启用,无法模拟")
    await feishu.handle_wire(
        {
            "seq": body.seq,
            "message_id": "",
            "chat_id": body.chat_id,
            "chat_type": "p2p",
            "msg_type": "text",
            "content": body.text,
            "sender_open_id": "dev",
        }
    )
    return {"ok": True}


if __name__ == "__main__":
    logger.info("agent 后台: http://localhost:%d", config.AGENT_PORT)
    uvicorn.run(app, host="0.0.0.0", port=config.AGENT_PORT)
