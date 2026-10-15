# 飞书消息手动操作台（Spec）

接收飞书消息（长连接模式，无需公网 IP），在终端和 Web 面板中查看完整消息信息，并手动回复。**不接 LLM**，所有动作由人手动触发。

支持能力：文本收发、**卡片消息**、**流式输出（两种方案）**、**发送文件**、**接收文件**、卡片按钮回调、**消息撤回感知**。

---

## 1. 运行原理

### 1.1 总体架构

```
                 ┌─────────────────── 本程序（python app.py）───────────────────┐
                 │                                                            │
  飞书用户  ⇄ 飞书服务器 ══长连接(WebSocket)══▶ FeishuService                    │
  （点按钮）        ◀══回调返回(toast/新卡片)══  （独立线程，阻塞运行）             │
                 │            │                                               │
                 │            ▼ on_message / on_card_action                    │
                 │      MessageStore（内存存储 + event_id 去重）                │
                 │            │                                               │
                 │     终端打印完整 JSON      asyncio 跨线程广播                 │
  操作者（你） ◀═══ 终端 REPL                  │                               │
  （命令交互） ═══▶ console.py                ▼                               │
                 │                    FastAPI（主线程，端口 8000）              │
  浏览器      ◀═══ HTTP / WebSocket ══▶  / /api/* /ws                          │
                 │                                                            │
                 │      回复/发送/卡片/文件 ──▶ lark.Client（REST）──▶ 飞书      │
                 └────────────────────────────────────────────────────────────┘
```

### 1.2 线程模型

| 线程 | 职责 |
|------|------|
| 主线程 | uvicorn 运行 FastAPI（Web 面板 + HTTP API + WebSocket 推送） |
| daemon 线程 A | `lark.ws.Client.start()` 长连接，阻塞接收消息事件与卡片回调 |
| daemon 线程 B | 终端 REPL（`input()` 阻塞读命令） |
| daemon 线程 C | 独立 asyncio 事件循环，承载 SDK 内置 `FeishuChannel` 的出站流式（`stream()` 是 async 接口） |

长连接线程收到事件后：去重入库 → 终端打印 → `asyncio.run_coroutine_threadsafe` 跨线程推送到 Web 面板的 WebSocket 连接。

### 1.3 关键机制说明

- **消息去重**：飞书会重推事件，按 `header.event_id` 幂等去重（`store.py`）
- **令牌管理**：`tenant_access_token` 有效期 2 小时，SDK 自动获取和刷新，程序不感知
- **序号机制**：每条入库消息分配递增序号 `#1, #2...`，终端命令和 Web API 都用序号定位消息，回复时取该消息的 `message_id`
- **消息日志**：每次启动在 `logs/` 下按时间戳新建 `feishu_YYYYMMDD_HHMMSS.log`（`msglog.py`）。收：`[IN]` 摘要行 + 完整原始事件 JSON（pretty）；发：`[OUT]` 单行，含结果（`result=ok` / `fail code=...`）与内容。注意日志含 app_id、open_id、消息正文，勿提交 git 或外发

### 1.4 四类高级能力的原理

**① 卡片消息**：本质是 `msg_type="interactive"` 的消息，content 为卡片 JSON。卡片里按钮被点击时，飞书通过长连接下发 `card.action.trigger` 回调，回调函数可以直接 `return` 一个 toast 或新卡片，实现"点按钮就地刷新"。

**② 流式输出 — 方案 A（PATCH 整卡覆盖）**：先发一张带 `"config": {"update_multi": true}` 的卡片，拿到 message_id，然后反复调用 `PATCH /im/v1/messages/:message_id`，每次用内容更长的完整卡片 JSON 整体覆盖，形成打字机效果。简单可靠，但每次传输整卡、频率受限（建议每秒 1~2 次）。

**③ 流式输出 — 方案 B（SDK 内置 FeishuChannel 流式）**：直接使用 lark-oapi 1.7+ 内置的 `FeishuChannel.stream(chat_id, {"markdown": producer})`。SDK 内部完成全套 CardKit 协议：懒建卡（首次 append 时才 create 卡片实体并发送）→ 内置节流（每 100ms 或攒够 50 字触发一次更新）→ 快照式整段内容更新 + 递增 `sequence` 保序 → producer 返回后自动调用 `finish_streaming_card` 关闭流式态。业务侧只需在 producer 里逐段 `await stream.append(chunk)`。注意 `FeishuChannel` 只用于出站（不 connect），入站仍由 ws.Client 负责，避免双连接重复收事件。

**④ 文件收发**：
- 发送 = 两步：`POST /im/v1/files` 上传文件得到 `file_key` → 发送 `msg_type="file"`、`content={"file_key": ...}` 的消息（图片走 `/im/v1/images` 得 `image_key`）
- 接收 = 消息事件 `msg_type` 为 `file/image/media` 时 content 内含 `file_key`/`image_key` 和 `file_name`，再调 `GET /im/v1/messages/:message_id/resources/:file_key?type=file` 下载二进制内容

---

## 2. 飞书开放平台配置（一次性）

在 [open.feishu.cn](https://open.feishu.cn) 的企业自建应用中确认，**改完必须发布新版本**：

1. **应用能力**：添加"机器人"
2. **事件订阅**：订阅方式选 **"使用长连接接收事件"**，添加事件 **"接收消息 v2.0"（im.message.receive_v1）** 和 **"消息撤回"（im.message.recalled_v1）**
3. **回调订阅**：添加 **"卡片回传交互"（card.action.trigger）**（点卡片按钮要用；与事件订阅是两个入口）
4. **权限管理**：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message:send_as_bot`（以应用的身份发消息）
   - `im:resource`（获取消息中的资源文件 —— 接收/下载文件要用）
5. **版本管理与发布**：创建版本并发布

---

## 3. 安装与启动

```bash
pip install -r requirements.txt
```

凭证（二选一）：编辑 `config.py`，或设置环境变量：

```bash
export FEISHU_APP_ID=cli_xxxx
export FEISHU_APP_SECRET=xxxx
```

启动：

```bash
python app.py
```

- 终端进入交互控制台；浏览器打开 `http://localhost:8000`

---

## 4. 使用方式

### 4.1 终端控制台命令

| 命令 | 说明 |
|------|------|
| `list` | 列出已收到的消息 |
| `show <序号>` | 打印该消息完整原始 JSON |
| `reply <序号> <文本>` | 回复文本 |
| `send <open_id> <文本>` | 主动发文本 |
| `card <序号>` | 回复一张演示卡片（带按钮，点击触发回调） |
| `stream <序号> <文本>` | **方案A**：回复卡片后 PATCH 整卡逐字流式更新 |
| `stream2 <序号> <文本>` | **方案B**：SDK 内置 FeishuChannel 流式（节流 + 平滑打印 + 自动收尾） |
| `file <序号> <本地路径>` | 上传本地文件并作为文件消息回复 |
| `download <序号> [保存目录]` | 下载收到的文件/图片（默认存到 `downloads/`） |
| `help` / `quit` | 帮助 / 退出 |

### 4.2 Web 面板（`http://localhost:8000`）

- **左侧消息列表**：实时推送；文件类消息显示文件名和「下载」链接
- **消息详情**：完整字段（点击复制）+ 原始 JSON
- **回复区**：文本回复 / 回复演示卡片 / 流式回复（PATCH）/ 流式回复（CardKit）/ 选择本地文件上传回复
- **主动发送区**：按 open_id 发文本或文件
- **操作日志**：发送结果、卡片按钮点击事件实时显示

### 4.3 本程序 HTTP API

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/` | - | Web 面板页面 |
| GET | `/api/messages` | - | 已收消息列表（内存近期消息，重启后为空） |
| GET | `/api/messages/{message_id}` | - | 无状态实时代理：按 message_id 从飞书取单条消息，返回 wire dict；查不到返回 404 `{"detail": "message not found"}` |
| GET | `/api/recalled/{message_id}` | - | 撤回状态查询：固定 200，已撤回返回 `{"recalled": true, "recall_type", "recall_time", "chat_id"}`，未撤回（含未知 id）返回 `{"recalled": false}`。agent 处理消息期间轮询，`recalled=true` 应取消处理 |
| GET | `/api/file` | `message_id`（必填）；`file_key`、`res_type`（可选） | 无状态实时代理：流式下载该消息的附件（`Content-Disposition` 带原始文件名）。默认解析消息体附件；显式传 `file_key`（如 post 内嵌图片的 `image_key`，配 `res_type=image`）则跳过解析直接按 key 下载。404=消息不存在或无附件，502=向飞书拉取失败 |
| GET | `/health` | - | 健康检查（见下方说明） |
| WS | `/ws` | - | 实时推送新消息与卡片回调 |
| POST | `/api/reply` | `{seq, text}` | 回复文本 |
| POST | `/api/send` | `{open_id, text}` | 主动发文本 |
| POST | `/api/reply_card` | `{seq}` | 回复演示卡片 |
| POST | `/api/stream` | `{seq, text, mode}` | 流式回复；`mode` = `patch` 或 `cardkit` |
| POST | `/api/stream_begin` | `{seq}` | 开手动流式会话，返回 `stream_id` |
| POST | `/api/stream_append` | `{stream_id, delta}` | 向会话追加一段文本（非阻塞） |
| POST | `/api/stream_set` | `{stream_id, content}` | 整体替换卡片内容并立即刷新；后续 append 在新内容基础上衔接 |
| POST | `/api/stream_end` | `{stream_id}` | 关闭会话（关闭 streaming_mode） |
| POST | `/api/reply_file` | multipart: `seq` + 文件；或 JSON `{seq, file_key}` | 上传并回复文件消息 / 只回复已上传的 file_key |
| POST | `/api/send_file` | `open_id` + 文件（multipart） | 上传并主动发文件消息 |
| POST | `/api/files/upload` | `file` + `file_name`（multipart） | 无状态代理：上传文件到飞书（`file_type` 按扩展名映射，未知用 `stream`），返回 `file_key`；上限 20MB，超限返回 `success: false` |
| GET | `/api/download/{seq}` | - | 下载收到的文件/图片 |

所有 POST 返回 `{"success": bool, "code": int, "msg": str, ...}`，`code`/`msg` 为飞书原始返回，便于排错。

消息 wire dict（WS 推送、`GET /api/messages`、`GET /api/messages/{message_id}` 共用结构）：

```json
{
  "seq": 3,                    // 内存序号；实时代理取单条时为 null
  "message_id": "om_xxx",
  "chat_id": "oc_xxx",
  "chat_type": "p2p",
  "msg_type": "text",
  "content": "消息文本",         // file/image 时为 "[file] 文件名" 展示文本
  "sender_open_id": "ou_xxx",
  "parent_id": "om_yyy",       // 回复/引用的被回复消息 id，无则为 null
  "recalled": true,            // 仅该消息后来被撤回时携带（内存消息被撤回事件命中时打标）
  "file": {                    // 仅 msg_type 为 file/image 时携带
    "file_key": "file_v3_xxx",
    "name": "requirements.txt",
    "size": 12345,             // 消息体不含 size 时为 null
    "mime": "text/plain"       // 由文件名推断；image 兜底 image/png
  }
}
```

`/health` 返回示例（供其他项目探活）：

```json
{
  "status": "ok",            // ok=长连接正常；degraded=未连接
  "ws_connected": true,      // 以 SDK 当前连接对象为准
  "ws_state": "connected",   // connecting/connected/reconnecting/failed
  "ws_error": null,          // 启动失败时的错误信息
  "uptime_s": 123.4,
  "last_message_at": "21:57:15",
  "messages": 6
}
```

---

## 5. 样例代码

### 5.1 发送一张卡片（回复指定消息）

```python
card = {
    "schema": "2.0",
    "config": {"update_multi": True},  # 允许后续更新这张卡
    "header": {"title": {"tag": "plain_text", "content": "处理结果"}},
    "body": {"elements": [{"tag": "markdown", "content": "**等待输入…**"}]},
}
feishu.reply_card(message_id, card)   # msg_type="interactive"
```

### 5.2 卡片按钮回调

```python
handler = (lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(on_message)
    .register_p2_card_action_trigger(on_card_action)   # 按钮点击
    .build())

def on_card_action(data) -> P2CardActionTriggerResponse:
    operator = data.event.operator.open_id      # 谁点的
    value = data.event.action.value             # 按钮 value 字段
    resp = P2CardActionTriggerResponse()
    resp.toast = CallBackToast({"type": "success", "content": f"收到: {value}"})
    return resp                                 # return 卡片对象则就地刷新
```

### 5.3 流式输出 — 方案 A（PATCH 整卡）

```python
card["body"]["elements"][0]["content"] = ""
result = feishu.reply_card(message_id, card)
mid = result["message_id"]
for i in range(1, len(full_text) + 1):
    card["body"]["elements"][0]["content"] = full_text[:i]
    feishu.update_card(mid, card)      # PATCH /im/v1/messages/:mid，整卡覆盖
    time.sleep(0.1)                    # 必须节流，每秒 1~2 次为宜
```

### 5.4 流式输出 — 方案 B（SDK 内置 FeishuChannel）

```python
async def producer(stream):
    for ch in full_text:
        await stream.append(ch)      # SDK 内置节流：100ms 或攒够 50 字刷一次
        await asyncio.sleep(0.02)
    # producer 返回后 SDK 自动 drain 并 finish_streaming_card（关闭流式态）

result = await channel.stream(chat_id, {"markdown": producer}, {"reply_to": message_id})
# 本程序封装为同步接口：feishu.stream_cardkit(chat_id, message_id, full_text)
```

### 5.4b 手动分段流式（begin / append / end）

适合文本由外部系统分段产出的场景（如 LLM 逐段回调）。HTTP 调用序列：

```bash
# 1) 开流：对消息 #3 建流式卡片（首段 append 时才真正建卡）
curl -X POST localhost:8000/api/stream_begin -H "Content-Type: application/json" -d '{"seq": 3}'
# -> {"success": true, "stream_id": "a1b2c3d4e5f6", ...}

# 2) 追加任意多段（SDK 内部节流：100ms 或攒够 50 字刷一次卡片）
curl -X POST localhost:8000/api/stream_append -H "Content-Type: application/json" \
     -d '{"stream_id": "a1b2c3d4e5f6", "delta": "第一段内容..."}'

# 2b) 整体替换：卡片内容立即换成新文本，之后 append 在新内容基础上继续
curl -X POST localhost:8000/api/stream_set -H "Content-Type: application/json" \
     -d '{"stream_id": "a1b2c3d4e5f6", "content": "完整新内容"}'

# 3) 收尾：关闭 streaming_mode，返回最终 message_id
curl -X POST localhost:8000/api/stream_end -H "Content-Type: application/json" -d '{"stream_id": "a1b2c3d4e5f6"}'
```

注意：`stream_id` 会话闲置超过 10 分钟未 end 会被自动清理；begin 后不 append 直接 end 是空卡片，属于合法操作。

### 5.5 发送文件

```python
file_key = feishu.upload_file("demo.pdf", open("demo.pdf", "rb"))  # POST /im/v1/files
feishu.reply_file(message_id, file_key)    # msg_type="file", content={"file_key": ...}
```

### 5.6 接收文件

```python
# 消息事件 msg_type="file" 时，content = {"file_key": "...", "file_name": "..."}
data = feishu.download_file(message_id, file_key)   # GET .../resources/:file_key?type=file
with open(file_name, "wb") as f:
    f.write(data)
```

### 5.7 消息撤回感知（agent 取消处理）

订阅 `im.message.recalled_v1` 事件后，机器人所在会话内任何消息被撤回都会推送到本程序并登记。
agent 处理耗时任务期间轮询查询接口，发现已撤回即取消：

```python
import requests

def is_recalled(message_id: str) -> bool:
    r = requests.get(f"http://localhost:8000/api/recalled/{message_id}").json()
    return r["recalled"]

# 处理循环里周期性检查（如 LLM 每生成一段、或回复前最终确认一次）
if is_recalled(msg["message_id"]):
    return  # 消息已被撤回，放弃处理与回复
```

撤回事件只带 `message_id` / `chat_id` / `recall_time` / `recall_type`，**不含被撤回消息的内容**；
`recall_type` 取值：`message_owner`（发送者）、`group_owner`（群主）、`group_manager`（群管理员）、`enterprise_manager`（企业管理员）。

---

## 6. 常见问题

| 现象 | 排查 |
|------|------|
| 长连接报 `app_id is invalid` | 凭证填错 / 应用被删除 |
| 收不到消息 | 事件订阅没加 `im.message.receive_v1`，或改了配置没发布版本 |
| 收不到撤回事件 | 事件订阅没加 `im.message.recalled_v1`，或改了配置没发布版本 |
| 发消息 `99991672` 类权限错误 | 权限没开通或没发布；机器人不在群里（群聊需先把机器人拉进群） |
| 点按钮没反应 | 回调订阅没加 `card.action.trigger` |
| 下载文件报权限错 | 缺 `im:resource` 权限 |
| 刷卡片报错 | 卡片没带 `update_multi: true`；PATCH 太频繁被限流（加大 sleep） |
| CardKit 报 `11311 printFrequencyMs or printStep is nil` | 手搓卡片时 `streaming_config` 里 `print_frequency_ms` 和 `print_step` 必须成对出现；本程序方案 B 走 SDK 内置流式，不传该字段，用服务端默认平滑参数 |
