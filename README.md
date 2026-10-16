## 架构总览

渠道与核心解耦：agent 核心只面向渠道中立的抽象（`InboundMessage` 入、`ReplySink` 出），飞书/web 各自以适配器实现接入，互不知道对方存在。

```
main.py            入口 + FastAPI 路由 + lifespan 组装
host/              宿主侧模块包(2026-10-13 重构:原平铺模块按功能分包,仅 git mv + 改 import,无功能变更)
  config.py          环境变量配置
  events.py          SSE 事件总线(进程内发布订阅)
  token_estimator.py token 粗估
  pipeline/          生成管线
    chat_pipeline.py   agent 核心 Pipeline,只认 InboundMessage + ReplySink
    capability_resolver.py 能力装配 provider 链(workspace/MCP/kb/记忆/VL/skills)
    intent_gate.py     意图分类门(轻量结构化分类,默认关灰度)
    html_inline.py     轮后 HTML 产物自包含化
    tool_debug.py      工具目录与手动调试分发(主仪表盘 /api/tools、/api/tools/call)
  channels/          渠道适配
    channel_base.py    渠道抽象:InboundMessage / ReplySink 接口
    feishu_channel.py  飞书适配器:wire→InboundMessage、去重、补拉、卡片流式 sink、附件编排、图片意图判定(pending buffer)
    console_client.py  飞书操作台传输层 HTTP+WS(含消息查询/附件下载,纯传输不落盘)
    web_channel.py     web 渠道 sink(SSE 已覆盖展示,回复仅记日志)
  state/             会话状态
    session_store.py   会话/turn 内存存储(Turn.seq 为渠道透传的展示字段)
    workspace_store.py workspace 注册表 + chat_id↔workspace 绑定(落盘 workspaces.json)
    inbox.py           会话收件箱:附件落盘、幂等索引、命名净化(渠道中立)
  capabilities/      能力实现(渠道中立)
    mcp_client.py      MCP 服务器客户端:启动探测缓存、public/internal transport、call_tool 直调拆包
    kb_client.py       RAG 知识库客户端 + kb_search Action、[[ref:]] 引用渲染
    skill_registry.py  skill 扫描/依赖登记(onboard/clean)/脚本沙箱绑定(含 shell 窄适配器)
mcp_server/        【不在本仓库】工具 MCP 服务器(独立进程 :8003,独立项目需自行部署):公开/内部双 app、usage 调用记录;接入契约见「工具 MCP 服务器(外部依赖)」一节
kb_service/        独立 RAG 微服务(FastAPI :8002):RecordStore 向量检索、knowledge/ 扫描入库、管理 API
feishu_console/    飞书操作台(独立进程 :8000):飞书长连接收发、Web 面板、卡片/流式/文件代理,主服务经 CONSOLE_BASE_URL 以 HTTP+WS 连接
sandbox/           沙箱镜像构建(build.py)+ 依赖覆盖表(deps_overlay.yaml)
scripts/           openai_agent.py 旧版自研 agent 封装(已无引用,保留)、clean_history_pseudo_calls.py 一次性清洗脚本
static/            仪表盘前端
```

依赖方向：

```
main.py ──► host/channels/feishu_channel.py ──► host/channels/console_client.py(传输)
   │              │
   │              ▼
   ├──► host/pipeline/chat_pipeline.py ──► host/channels/channel_base.py(抽象)
   │              │                          ▲
   └──► host/channels/web_channel.py ────────┘
```


单轮处理流程（`_generate_and_reply` 外层编排）：

```
用户消息
   │
   ▼
① 意图分类门(intent_gate, 默认关)── chitchat/ambiguous → 重能力不挂载
   │  输出 IntentResult(intent, needs_file)   (workspace文件/web/kb_remember/脚本沙箱)
   │ task
   ▼
┌─ _run_generation(可重入) ─────────────────────────────────┐
│  按意图挂载能力(write_deliverable 工具恒挂, 语义在 docstring)│
│  → instructs 组装(交付流程 instruct 仅 needs_file 时注入,    │
│    无格式示例; 闲聊/纯问答轮完全不出现该模板)                 │
│  → 流式生成 → 伪调用清洗(全轮生效, 含形态 D)                 │
│  → rescue 内联文件落盘 → 引用渲染 → _GenerationOutcome       │
└───────────────────────────────────────────────────────────┘
   │
   ▼
② 验证重试环(仅读写 workspace): 承诺了产物(rescue 落盘 /
   write_file/write_deliverable 痕迹 / 声明正则) 且 outbox diff 为空?
   │                                    │
   │ 是, 且重试预算未用完                │ 否
   ▼                                    ▼
回灌 _VERIFY_FEEDBACK          ②.5 HTML 自包含化(html_inline):
重跑 _run_generation           本轮产物 HTML 的失效本地资源引用
(非流式, -rN 后缀, 上限 1 次)    按 skill 源内联成单文件(投递前)
   │                                    │
   │                                    ▼
   │                              ③ 事实合成层:
   │                              outbox diff + send_file 回执
   │                              → 系统脚注(📎 已发送 / ⚠️ 未生成)
   │                                    │
   └──────────┬─────────────────────────┘
              ▼
   展示文本 = 模型正文 + 系统脚注 → 覆写卡片/发出
   历史落盘只存模型正文(脚注不入历史)
```

## 调试界面
### agent仪表盘
<img width="1368" height="761" alt="image" src="https://github.com/user-attachments/assets/3fdf4a79-1280-46d2-9ee5-7afb3191cfa9" />

### 飞书仪表盘
<img width="945" height="968" alt="image" src="https://github.com/user-attachments/assets/4c5bef5c-c6a4-4bcc-bbb7-3d1fe64a4b56" />

### RAG仪表盘
详见代码运行


## 手动操作清单（人需要跑的命令，集中维护）

服务代码里不会自动执行、需要人手动跑的命令统一登记在这里；


### 常驻服务（开发/部署都要起，顺序无关）

```bash
scripts/svc.sh start|stop|restart|status   # 一键启停三服务(推荐):自动等端口监听、pidfile 记真实 Windows PID、
                                           # 停时按 pidfile→端口兜底(手动起的也能停);日志 .dev_ws/logs/<name>.log。
                                           # 启动顺序 feishu_console→kb_service→main:main 的 lifespan 探测(kb_enabled/
                                           # mcp_enabled)是一次性的,下游没就绪它探到会按未启用处理,必须最后起。
                                           # 工具 MCP 服务器(8003)为外部独立项目,不在本仓库,如需联网/高德工具
                                           # 请单独部署并在 main 之前启动,见「工具 MCP 服务器(外部依赖)」
```

等价的手动命令（svc.sh 内部就是这三条）：

```bash
<py> -m feishu_console.app    # 飞书操作台 :8000(前置:.env 配好 FEISHU_APP_ID/FEISHU_APP_SECRET)
<py> main.py                  # 主服务 :8001(仪表盘 / 渠道 / pipeline)
<py> -m kb_service.main       # RAG 微服务 :8002
# 外部组件(不在本仓库): 工具 MCP 服务器 :8003,部署后配置 MCP_SERVER_URL 指向它
```

重启前必须确认旧进程已死（Windows SO_REUSEADDR 允许同端口双绑，旧进程不死会分流请求到旧代码）：`scripts/svc.sh stop` 或 `netstat -ano | grep :800x` → `taskkill //PID <pid> //F`（Git Bash 的 `kill` 只认 MSYS pid，停不了别的会话起的 Windows 进程，svc.sh 已内置此坑处理）。

### 一次性环境准备

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim   # skill 沙箱 base(image_pull_policy=never,必须本地预拉;官方源不通时走 daocloud)
docker build -t agent-skill-python:3.12 sandbox/python      # 自定义运行时(预装 python-docx 等,.env SKILL_SCRIPT_IMAGE 已指向它)
```

可选 `.env` 项（改后重启部署了浏览工具核心的 MCP 服务器进程；发布副本宿主不 import web_actions）：

```bash
WEB_FETCH_GITHUB_MIRRORS=https://ghproxy.net/,https://gh-proxy.com/   # GitHub 直连不通时经镜像回退(本网络实测 github.com/codeload 直连超时;第三方代理,供应链风险自担)
WEB_FETCH_MAX_BYTES=104857600   # 下载大仓库 zip 时调高(默认 20MB;如 dashi-ppt-skill 仓库约 100MB)
```

### 变更触发（做了对应改动才需要跑）

```bash
<py> -m host.capabilities.skill_registry onboard   # 新增 skill 后跑一次:AST 推导 scripts 第三方依赖 → 人确认(y/i/N)写 sandbox/deps_overlay.yaml → 确认后顺带构建镜像(--yes 免确认, --no-build 只登记)
<py> -m sandbox.build            # 改了 frontmatter sandbox_python_deps / deps_overlay.yaml 后跑一次(幂等,缺镜像才构建;前置:Docker Desktop 运行中)
<py> -m host.capabilities.skill_registry clean     # 删除 skill 源目录后,顺手清理库缓存惰性垃圾(可选,误删无害——只清 .agently/skill-library,不碰 skills/ 源目录)
```


