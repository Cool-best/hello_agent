import asyncio
import os
import sys
import threading
from contextlib import asynccontextmanager
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, File, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import config
from .console import run_console
from .feishu_client import FeishuFetchError, FeishuService, demo_card
from .store import build_wire, recalled, store

feishu: FeishuService | None = None
main_loop: asyncio.AbstractEventLoop | None = None
ws_clients: set[WebSocket] = set()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    yield


app = FastAPI(title="飞书消息操作台", lifespan=lifespan)


# ---------- 长连接事件入口 ----------

def on_message(raw_event: dict):
    """长连接线程中的消息入口：去重入库 -> 终端打印 -> 推送 Web 面板。"""
    record = store.add(raw_event)
    if record is None:
        print("\n[飞书] 重复事件，已忽略")
        return
    print(
        f"\n[新消息 #{record['seq']}] {record['time']} | "
        f"来自 {record['sender_open_id']} | 类型 {record['msg_type']} | "
        f"内容: {record['content']}"
    )
    print(record["raw_json"])
    print("feishu> ", end="", flush=True)
    push({"type": "message", "data": record})


def on_card_action(raw: dict):
    """卡片按钮回调入口：终端打印 + 推送 Web 面板，toast 由 SDK 回调返回。"""
    event = raw.get("event", {})
    operator = (event.get("operator") or {}).get("open_id")
    value = (event.get("action") or {}).get("value")
    print(f"\n[卡片回调] 操作人 {operator} | value: {value}")
    print("feishu> ", end="", flush=True)
    push({"type": "card_action", "data": {"operator": operator, "value": value}})


def on_recall(raw: dict):
    """消息撤回事件入口：登记撤回状态 -> 标记内存消息 -> 终端打印 -> 推送 Web 面板。"""
    event = raw.get("event", {})
    message_id = event.get("message_id")
    if not message_id:
        return
    recalled.mark(
        message_id,
        event.get("chat_id"),
        event.get("recall_time"),
        event.get("recall_type"),
    )
    record = store.mark_recalled(message_id)
    print(f"\n[撤回] message_id={message_id} | recall_type={event.get('recall_type')}"
          + (f" | 对应消息 #{record['seq']}" if record else ""))
    print("feishu> ", end="", flush=True)
    push({"type": "recalled", "data": {"message_id": message_id, **recalled.get(message_id)}})


def push(payload: dict):
    if main_loop:
        asyncio.run_coroutine_threadsafe(broadcast(payload), main_loop)


async def broadcast(payload: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


# ---------- 页面与推送 ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/messages")
def list_messages():
    return store.list()


@app.get("/api/messages/{message_id}")
def get_message(message_id: str):
    """无状态实时代理：按 message_id 从飞书取单条消息，返回 wire dict。"""
    msg = feishu.fetch_message(message_id)
    if msg is None:
        return JSONResponse(status_code=404, content={"detail": "message not found"})
    return build_wire(msg)


@app.get("/api/recalled/{message_id}")
def get_recalled(message_id: str):
    """撤回状态查询：agent 处理消息期间轮询，recalled=true 应取消处理。"""
    rec = recalled.get(message_id)
    if rec is None:
        return {"message_id": message_id, "recalled": False}
    return {"message_id": message_id, "recalled": True, **rec}


@app.get("/api/file")
def get_file(
    message_id: str = Query(...),
    file_key: str | None = Query(None),
    res_type: str = Query("file"),
):
    """无状态实时代理：下载指定消息携带的附件，流式返回字节。

    显式传 file_key（如 post 内嵌图片的 image_key, res_type=image）时跳过消息体解析
    直接按 key 下载；未传时按消息自身附件解析，行为不变。
    """
    file_key = (file_key or "").strip() or None
    try:
        fileobj, name, mime, size = feishu.open_message_file(
            message_id, file_key=file_key, res_type=res_type
        )
    except LookupError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except FeishuFetchError as e:
        return JSONResponse(status_code=502, content={"detail": str(e)})
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
    if size:
        headers["Content-Length"] = str(size)

    def _iter_chunks(chunk_size: int = 64 * 1024):
        while True:
            chunk = fileobj.read(chunk_size)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        _iter_chunks(), media_type=mime or "application/octet-stream", headers=headers
    )


@app.get("/health")
def health():
    return feishu.health(message_count=len(store.list()))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)


# ---------- 文本 ----------

class ReplyBody(BaseModel):
    seq: int
    text: str


@app.post("/api/reply")
def reply(body: ReplyBody):
    record = store.get(body.seq)
    if not record:
        return {"success": False, "code": -1, "msg": f"消息 #{body.seq} 不存在"}
    return feishu.reply_text(record["message_id"], body.text)


class SendBody(BaseModel):
    open_id: str
    text: str


@app.post("/api/send")
def send(body: SendBody):
    return feishu.send_text(body.open_id, body.text)


# ---------- 卡片与流式 ----------

class SeqBody(BaseModel):
    seq: int


@app.post("/api/reply_card")
def reply_card(body: SeqBody):
    record = store.get(body.seq)
    if not record:
        return {"success": False, "code": -1, "msg": f"消息 #{body.seq} 不存在"}
    return feishu.reply_card(record["message_id"], demo_card())


class StreamBody(BaseModel):
    seq: int
    text: str
    mode: str = "patch"  # patch = 方案A整卡覆盖；cardkit = 方案B官方流式


@app.post("/api/stream")
def stream(body: StreamBody):
    record = store.get(body.seq)
    if not record:
        return {"success": False, "code": -1, "msg": f"消息 #{body.seq} 不存在"}
    try:
        if body.mode == "patch":
            return feishu.stream_patch(record["message_id"], body.text)
        return feishu.stream_cardkit(record["chat_id"], record["message_id"], body.text)
    except Exception as e:
        return {"success": False, "code": -1, "msg": str(e)}


# ---------- 手动分段流式（begin -> append*N -> end） ----------

class StreamBeginBody(BaseModel):
    seq: int


@app.post("/api/stream_begin")
def stream_begin(body: StreamBeginBody):
    record = store.get(body.seq)
    if not record:
        return {"success": False, "code": -1, "msg": f"消息 #{body.seq} 不存在"}
    return feishu.stream_begin(record["chat_id"], record["message_id"])


class StreamAppendBody(BaseModel):
    stream_id: str
    delta: str


@app.post("/api/stream_append")
def stream_append(body: StreamAppendBody):
    return feishu.stream_append(body.stream_id, body.delta)


class StreamSetBody(BaseModel):
    stream_id: str
    content: str


@app.post("/api/stream_set")
def stream_set(body: StreamSetBody):
    return feishu.stream_set(body.stream_id, body.content)


class StreamEndBody(BaseModel):
    stream_id: str


@app.post("/api/stream_end")
def stream_end(body: StreamEndBody):
    return feishu.stream_end(body.stream_id)


# ---------- 文件 ----------

# 上传上限 20MB（飞书 im/v1/files 上限 30MB，留余量；超限由 agent 侧降级文本提示）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@app.post("/api/files/upload")
async def upload_file_api(file: UploadFile = File(...), file_name: str = Form(...)):
    """无状态代理：把文件字节上传到飞书，返回 file_key（不做任何缓存）。"""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        return {"success": False, "code": -1,
                "msg": f"文件超过 20MB 上限: {file.size} bytes"}
    try:
        file_key = feishu.upload_file(file_name, file.file)
        return {"success": True, "code": 0, "msg": "", "file_key": file_key}
    except Exception as e:
        return {"success": False, "code": -1, "msg": str(e)}


@app.post("/api/reply_file")
async def reply_file(request: Request):
    """回复文件消息，两种用法：

    - multipart(seq + file)：上传并回复（Web 面板旧路径）
    - JSON {"seq", "file_key"}：只回复已上传的 file_key（agent 代理路径）
    """
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        seq, file_key = body.get("seq"), body.get("file_key")
        if not seq or not file_key:
            return {"success": False, "code": -1, "msg": "缺少 seq 或 file_key"}
        record = store.get(seq)
        if not record:
            return {"success": False, "code": -1, "msg": f"消息 #{seq} 不存在"}
        return feishu.reply_file(record["message_id"], file_key)
    form = await request.form()
    seq = int(form["seq"])
    upload = form["file"]
    record = store.get(seq)
    if not record:
        return {"success": False, "code": -1, "msg": f"消息 #{seq} 不存在"}
    try:
        file_key = feishu.upload_file(upload.filename or "file", upload.file)
        return feishu.reply_file(record["message_id"], file_key)
    except Exception as e:
        return {"success": False, "code": -1, "msg": str(e)}


@app.post("/api/send_file")
async def send_file(open_id: str = Form(...), file: UploadFile = File(...)):
    try:
        file_key = feishu.upload_file(file.filename or "file", file.file)
        return feishu.send_file(open_id, file_key)
    except Exception as e:
        return {"success": False, "code": -1, "msg": str(e)}


@app.get("/api/download/{seq}")
def download(seq: int):
    record = store.get(seq)
    if not record or not record.get("file_key"):
        return {"success": False, "code": -1, "msg": f"消息 #{seq} 不存在或不含文件"}
    try:
        data, _ = feishu.download_file(
            record["message_id"], record["file_key"], record.get("res_type", "file")
        )
    except Exception as e:
        return {"success": False, "code": -1, "msg": str(e)}
    filename = record.get("file_name") or f"file_{seq}"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(filename)}"},
    )


def main():
    global feishu
    if not config.APP_ID or not config.APP_SECRET:
        print("请先填写凭证：编辑 config.py，或设置环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET")
        sys.exit(1)

    feishu = FeishuService(config.APP_ID, config.APP_SECRET, on_message, on_card_action,
                           on_recall=on_recall)
    print(f"[日志] 消息日志文件: {feishu.log_path}")
    threading.Thread(target=feishu.start_ws, daemon=True).start()
    threading.Thread(target=run_console, args=(store, feishu), daemon=True).start()

    print(f"[Web] 面板地址: http://localhost:{config.PORT}")
    uvicorn.run(app, host="127.0.0.1", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
