import os

from dotenv import load_dotenv

load_dotenv()

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "你是一个有帮助的助手")

# 降级备份模型:主端点启动探测失败且备份三件套配齐时整体切换(进程内一次,
# 运行期不再回切——恢复主端点靠重启)。阿里云百炼兼容模式示例:
#   LLM_FALLBACK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
#   LLM_FALLBACK_MODEL=qwen-plus
LLM_FALLBACK_BASE_URL = os.environ.get("LLM_FALLBACK_BASE_URL", "")
LLM_FALLBACK_API_KEY = os.environ.get("LLM_FALLBACK_API_KEY", "")
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "")
# 主端点存活探测上限(秒);仅当备份配置齐时才发起探测,避免无谓启动延迟
LLM_PROBE_TIMEOUT = int(os.environ.get("LLM_PROBE_TIMEOUT", "6"))
# 首块慢于该值视为降级态并切备份:故障恢复期端点常处于"能吐数据但首块 2-4s、
# 后续真实调用挂死"的半活态(2026-10-15 实测),仅判存活会在抖动期误选主端点
LLM_PROBE_SLOW_MS = int(os.environ.get("LLM_PROBE_SLOW_MS", "2000"))


_LLM_ACTIVE: dict = {}


def _probe_llm_endpoint(base_url: str, api_key: str, model: str, timeout: int) -> bool:
    """最小**流式** chat completion 探测端点真实可用性。

    必须 stream=True 且在 LLM_PROBE_SLOW_MS 内读到真实 data 块才算存活:
    2026-10-15 实证 deepseek 故障期非流式/GET /models 均 200(0.2s),
    但 SSE 流只吐 ": keep-alive" 注释永不给内容——主生成链路(Agently 流式)
    必挂死;恢复期又会抖成"首块 2.3-4.4s 能到、真实调用仍挂死"的半活态,
    故加首块时延门槛。urllib 的 timeout 作用于每次 read,读不到数据即超时判死。
    """
    import json
    import time
    import urllib.request

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                return False
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    return (time.time() - t0) * 1000 <= LLM_PROBE_SLOW_MS
            return False  # 流正常结束但没任何数据块
    except Exception:
        return False


def resolve_llm_endpoint() -> dict:
    """解析本进程活动模型端点(base_url/model/api_key/via),进程内缓存一次。

    备份未配齐时不探测、直接沿用主端点;主端点故障且无备份时同样沿用主端点,
    错误在原请求路径暴露(与旧行为一致)。
    """
    if _LLM_ACTIVE:
        return _LLM_ACTIVE
    endpoint = {
        "base_url": LLM_BASE_URL,
        "model": LLM_MODEL,
        "api_key": LLM_API_KEY,
        "via": "primary",
    }
    fallback_ready = bool(
        LLM_FALLBACK_BASE_URL and LLM_FALLBACK_API_KEY and LLM_FALLBACK_MODEL
    )
    if fallback_ready and not _probe_llm_endpoint(
        LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_PROBE_TIMEOUT
    ):
        endpoint = {
            "base_url": LLM_FALLBACK_BASE_URL,
            "model": LLM_FALLBACK_MODEL,
            "api_key": LLM_FALLBACK_API_KEY,
            "via": "fallback",
        }
    _LLM_ACTIVE.update(endpoint)
    return _LLM_ACTIVE


# 关思考总开关:deepseek-flash 的隐藏 reasoning 在浅任务上实测生成 800+ token
# 挤占输出预算;qwen 混合思考同理。true 时按活动端点方言注入关闭参数
LLM_DISABLE_THINKING = os.environ.get("LLM_DISABLE_THINKING", "true").lower() == "true"


def thinking_off_options(base_url: str = "") -> dict:
    """指定端点的"关思考"请求参数(端点方言);开关关闭或未知端点返回 {}。

    方言按 base_url 识别而非 via 槽位(换端点不错发):
    deepseek={\"thinking\":{\"type\":\"disabled\"}},百炼 qwen={\"enable_thinking\":false};
    互发无效,发给不认识扩展参数的端点可能 400,未知端点一律不发。
    """
    if not LLM_DISABLE_THINKING:
        return {}
    url = (base_url or resolve_llm_endpoint()["base_url"]).lower()
    if "deepseek" in url:
        return {"thinking": {"type": "disabled"}}
    if "dashscope" in url:
        return {"enable_thinking": False}
    return {}


def apply_llm_settings(logger=None) -> dict:
    """解析活动端点并写入 Agently 全局模型设置(main.py 宿主 / mcp_server 共用入口)"""
    from agently import Agently

    ep = resolve_llm_endpoint()
    Agently.set_settings(
        "plugins.ModelRequester.OpenAICompatible.base_url", ep["base_url"]
    )
    Agently.set_settings("plugins.ModelRequester.OpenAICompatible.model", ep["model"])
    Agently.set_settings(
        "plugins.ModelRequester.OpenAICompatible.auth", {"api_key": ep["api_key"]}
    )
    think_off = thinking_off_options(ep["base_url"])
    if think_off:
        Agently.set_settings(
            "plugins.ModelRequester.OpenAICompatible.request_options", think_off
        )
    if logger:
        logger.info(
            "LLM 端点: %s | model=%s | via=%s | 关思考: %s",
            ep["base_url"],
            ep["model"],
            ep["via"],
            f"开 {think_off}" if think_off else "关",
        )
    return ep

# 飞书操作台渠道:console_client.py/feishu_channel.py 存在且 CONSOLE_BASE_URL 非空时自动启用,
# 显式置空或删除渠道模块则按纯网页对话运行
CONSOLE_BASE_URL = os.environ.get("CONSOLE_BASE_URL", "http://localhost:8000").rstrip("/")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "8001"))

LLM_CONTEXT_WINDOW = int(os.environ.get("LLM_CONTEXT_WINDOW", "128000"))

# 短期记忆窗口:5 轮(10 条消息);更早的上下文由长期记忆(kb recall/kb_remember/
# 轮后自动抽取)承接,不靠拉长历史
HISTORY_MAX_MESSAGES = int(os.environ.get("HISTORY_MAX_MESSAGES", "10"))
DEDUP_TTL_SECONDS = 600
WS_RECONNECT_DELAY = 3

# workspace 试点:会话绑定目录后 agent 可对其进行文件操作;
# 只读试点期间保持 true,放开写权限时置 false
WORKSPACE_READ_ONLY = os.environ.get("WORKSPACE_READ_ONLY", "true").lower() == "true"
# 大文件分块写(2026-10-13 起,DELIVER_INSTRUCT 引导 ~3000 字符/块)会多耗
# 回路轮次:25KB 内容约 8-9 块,叠加读素材/校验,8 轮不够
ACTION_LOOP_MAX_ROUNDS = int(os.environ.get("ACTION_LOOP_MAX_ROUNDS", "12"))
# 整个 action loop(含全部模型轮次+工具执行)的预算;Node 工程型 skill 的
# 生成链(dashi-ppt: scaffold→渲染→chromium 导出)实测 300s 不够
# (2026-10-13 e2e:turn 2af7cdacd707 loop timeout 时模型已走到渲染段),900 起步
ACTION_LOOP_TIMEOUT = int(os.environ.get("ACTION_LOOP_TIMEOUT", "900"))  # 秒
# action 规划协议:structured_plan(提示词 JSON 计划;deepseek-flash 偶发退化
# 成 DSML 伪调用文本,靠 rescue+verify 重试兜底)/ native_tool_calls(API 原生
# 工具调用——2026-10-13 e2e 实证:当前 LLM 端点不支持 tools 参数,原生模式下
# 所有调用都退化成 DSML 文本,不可用;保留开关仅供将来换端点时再试)
ACTION_PROTOCOL = os.environ.get("ACTION_PROTOCOL", "structured_plan")

# workspace 注册表落盘文件;飞书会话自动供给目录的根目录
WORKSPACE_STORE_FILE = os.environ.get("WORKSPACE_STORE_FILE", "workspaces.json")
WORKSPACE_AUTO_ROOT = os.environ.get("WORKSPACE_AUTO_ROOT", "workspaces")

# 会话(history + turns)落盘文件;重启后仪表盘可看到重启前的会话记录
SESSION_STORE_FILE = os.environ.get("SESSION_STORE_FILE", "sessions.json")

# 飞书附件下载上限
FILE_MAX_BYTES = int(os.environ.get("FILE_MAX_BYTES", str(20 * 1024 * 1024)))  # 20MB

# 产物文件投递上限(飞书 im/v1/files 上限 30MB,留余量);超限不投递、降级文本提示
FILE_DELIVERY_MAX_BYTES = int(
    os.environ.get("FILE_DELIVERY_MAX_BYTES", str(20 * 1024 * 1024))
)  # 20MB

# 单轮投递附件数护栏:产物超过该数量时不再逐发,全部收拢成一个 zip 投递
# (dashi-ppt 类目录树产物实测可刷 200+ 附件);含 index.html 的产物目录
# 不受此限——先按站点目录收拢(只投 index.html + 整树 zip),仍超限才全量打包
DELIVERY_MAX_ATTACHMENTS = int(os.environ.get("DELIVERY_MAX_ATTACHMENTS", "10"))

# 视觉(VL)模块:总开关默认关,切到 VL 模型(如 deepseek-flash)时显式置 true;
# false 时图片退回文本兜底行为,不挂 image attachment,纯文本模型安全
VISION_ENABLED = os.environ.get("VISION_ENABLED", "false").lower() == "true"
IMAGE_DETAIL = os.environ.get("IMAGE_DETAIL", "auto")  # low | high | auto
IMAGE_PENDING_TTL = int(os.environ.get("IMAGE_PENDING_TTL", "120"))  # 群聊图片软关联时间窗(秒)

# Skills(渐进式披露):项目内置全局 skills 目录 + 会话 workspace 内 skills 子目录;
# model_decision = 每轮一次小选择请求按卡片选;required = 跳过选择全量注入
SKILLS_DIR = os.environ.get("SKILLS_DIR", "skills")
WORKSPACE_SKILLS_DIR = os.environ.get("WORKSPACE_SKILLS_DIR", "skills")
SKILL_MODE = os.environ.get("SKILL_MODE", "model_decision")  # model_decision | required

# skill scripts 的 Docker 沙箱执行:仅项目内置 skill、仅被选中时绑定脚本 Action;
# 默认关,需本机 Docker 可用且已预拉 python:3.12-slim(strict profile 不自动拉)
SKILL_SCRIPTS_ENABLED = os.environ.get("SKILL_SCRIPTS_ENABLED", "false").lower() == "true"
# 单个脚本/shell action 的执行超时;Node 工程链(渲染+chromium 导出)实测分钟级,
# 60s 会在 render 段被杀(2026-10-13 e2e 实证),默认 300
SKILL_SCRIPT_TIMEOUT = int(os.environ.get("SKILL_SCRIPT_TIMEOUT", "300"))  # 秒
# 选中 skill 的 SKILL.md 作为 required TaskContext 内容投递给 model_request 消费者,
# 框架默认预算(max_chars=12000/max_block_chars=6000)装不下大 skill 会直接 fail closed
# (pptx SKILL.md ~20.8KB,2026-10-12 实测报 "Required TaskContext content could not be
# delivered");此处放宽,超出时按 lossy_digest 降级摘要投递而不是报错
SKILL_CONTEXT_MAX_CHARS = int(os.environ.get("SKILL_CONTEXT_MAX_CHARS", "48000"))
SKILL_CONTEXT_MAX_BLOCK_CHARS = int(os.environ.get("SKILL_CONTEXT_MAX_BLOCK_CHARS", "24000"))
# 运行镜像:strict profile image_pull_policy=never,必须本地预拉/预构建;
# skill frontmatter 可声明 sandbox_image 或 sandbox_python_deps(派生 agent-skill-auto:<hash>)覆盖
SKILL_SCRIPT_IMAGE = os.environ.get("SKILL_SCRIPT_IMAGE", "python:3.12-slim")
# python -m sandbox.build 的构建基座与 pip 索引
# (基座官方源不通时走 daocloud;pip 索引 2026-10-12 起默认官方源——
#  清华镜像对容器出口 403,docx 时代可用的 pypi.tuna.tsinghua.edu.cn 已不可用)
SKILL_SCRIPT_BASE_IMAGE = os.environ.get(
    "SKILL_SCRIPT_BASE_IMAGE", "docker.m.daocloud.io/library/python:3.12-slim"
)
SKILL_SCRIPT_PIP_INDEX = os.environ.get(
    "SKILL_SCRIPT_PIP_INDEX", "https://pypi.org/simple"
)
# 容器内 workspace 挂载放开读写(受信任项目级 skill 脚本可写回产物,约定写 outbox/);
# 要求 WORKSPACE_READ_ONLY=false,否则 read_write grant 签发被拒、按脚本执行失败降级
SKILL_SCRIPT_WORKSPACE_WRITE = (
    os.environ.get("SKILL_SCRIPT_WORKSPACE_WRITE", "false").lower() == "true"
)


# Web 联网能力(web_search/web_fetch Action):总开关默认关,需配搜索 API key;
# 开启但缺 key 时按未启用处理(validate 记 warning),行为与关闭一致
WEB_ENABLED = os.environ.get("WEB_ENABLED", "false").lower() == "true"
WEB_SEARCH_PROVIDER = os.environ.get("WEB_SEARCH_PROVIDER", "bocha")  # 搜索适配器注册表键
# 主源异常时的降级链(逗号分隔,如 "baidu,bing");空结果不级联,仅失败时切换
WEB_SEARCH_FALLBACK = os.environ.get("WEB_SEARCH_FALLBACK", "")
WEB_SEARCH_API_KEY = os.environ.get("WEB_SEARCH_API_KEY", "")
WEB_SEARCH_TIMEOUT = int(os.environ.get("WEB_SEARCH_TIMEOUT", "10"))  # 秒
WEB_SEARCH_MAX_RESULTS = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "30"))  # 宽检索默认 30 条(DeepSeek 式:一次铺开多来源候选再精选 fetch)
WEB_FETCH_TIMEOUT = int(os.environ.get("WEB_FETCH_TIMEOUT", "15"))  # 秒
WEB_FETCH_MAX_CHARS = int(os.environ.get("WEB_FETCH_MAX_CHARS", "2200"))  # 正文截断(单页结果 JSON 须 < 框架 8KB externalize 阈值,免 artifact 追加轮)
WEB_FETCH_BATCH_CHARS = int(os.environ.get("WEB_FETCH_BATCH_CHARS", "500"))  # 批量模式每页摘要截断(5 页总量同样须 < 8KB)
WEB_FETCH_MAX_BYTES = int(
    os.environ.get("WEB_FETCH_MAX_BYTES", str(20 * 1024 * 1024))
)  # 20MB,流式下载超限即断
WEB_MAX_CALLS_PER_TURN = int(os.environ.get("WEB_MAX_CALLS_PER_TURN", "8"))  # 单 turn search+fetch 合计限额
WEB_FETCH_BATCH_MAX = int(os.environ.get("WEB_FETCH_BATCH_MAX", "5"))  # web_fetch 列表参数上限
WEB_FETCH_CACHE_TTL = int(os.environ.get("WEB_FETCH_CACHE_TTL", "300"))  # 秒,同 URL 抓取结果缓存
# GitHub 镜像回退(2026-10-13 起):GitHub 系域名直连网络类失败时按顺序重试的镜像前缀
# (如 https://ghproxy.net/);逗号分隔,默认空=不回退。第三方代理,供应链风险自担。
WEB_FETCH_GITHUB_MIRRORS = [
    s.strip()
    for s in os.environ.get("WEB_FETCH_GITHUB_MIRRORS", "").split(",")
    if s.strip()
]
# 搜索结果增强:多源并行查询合并
# 搜索结果 LLM 剔除过滤(2026-10-14 起):仅剔除不相关/营销号/旧闻/非正文页,
# 保真不改写;失败/超时/剔除后不足 MIN_KEEP 条一律 fail-open 返回原始列表
WEB_SEARCH_REFINE = os.environ.get("WEB_SEARCH_REFINE", "true").lower() == "true"
WEB_SEARCH_REFINE_TIMEOUT = int(os.environ.get("WEB_SEARCH_REFINE_TIMEOUT", "8"))  # 秒
WEB_SEARCH_REFINE_MIN_KEEP = int(os.environ.get("WEB_SEARCH_REFINE_MIN_KEEP", "3"))
# 浏览器渲染兜底(web_fetch 分层抓取):静态抽取薄内容时用 headless Chromium 渲染重取。
# playwright 包或 chromium 二进制缺失时自动退回纯静态(一次性 warning),行为与旧版一致
WEB_RENDER_FALLBACK = os.environ.get("WEB_RENDER_FALLBACK", "true").lower() == "true"
WEB_RENDER_TIMEOUT = int(os.environ.get("WEB_RENDER_TIMEOUT", "20"))  # 秒,单次渲染上限
WEB_RENDER_MAX_CONCURRENCY = int(os.environ.get("WEB_RENDER_MAX_CONCURRENCY", "2"))
# 逗号分隔域名:渲染也没文本价值的页面(如 WebGL 地图 canvas)直接跳过渲染
WEB_RENDER_EXCLUDE_DOMAINS = os.environ.get("WEB_RENDER_EXCLUDE_DOMAINS", "")
# 已知 JS 站点跳过静态尝试直接渲染,省一次无效往返;默认空,建议从 usage.jsonl 数据养成
WEB_RENDER_FORCE_DOMAINS = os.environ.get("WEB_RENDER_FORCE_DOMAINS", "")

# 高德地图能力(amap_weather/amap_route/amap_poi Action):总开关默认关;
# Web 服务 API 无 key 全不可用,开启但缺 key 按未启用处理(validate 记 warning)
AMAP_ENABLED = os.environ.get("AMAP_ENABLED", "false").lower() == "true"
AMAP_API_KEY = os.environ.get("AMAP_API_KEY", "")
AMAP_TIMEOUT = int(os.environ.get("AMAP_TIMEOUT", "10"))  # 秒
AMAP_MAX_CALLS_PER_TURN = int(os.environ.get("AMAP_MAX_CALLS_PER_TURN", "6"))

# RAG 知识库(kb_service 独立微服务): 总开关默认关;
# 开启但启动探测不可达时按未启用处理(记 warning), 行为与关闭一致
KB_ENABLED = os.environ.get("KB_ENABLED", "false").lower() == "true"
KB_SERVICE_URL = os.environ.get("KB_SERVICE_URL", "http://127.0.0.1:8002").rstrip("/")
KB_TIMEOUT = int(os.environ.get("KB_TIMEOUT", "10"))  # 检索/入库超时(秒)
KB_MAX_RESULTS = int(os.environ.get("KB_MAX_RESULTS", "5"))  # 单次检索返回上限

# 工具 MCP 服务器(mcp_server 独立进程): web_search/web_fetch(抓取核心)/amap_*
# 的模型侧承载;总开关默认开,启动探测不可达时按未启用处理(web/amap 不挂载,记 warning)。
# 公开 app 挂 {URL}/mcp(模型可见工具),内部 app 挂 {URL}/mcp-internal(web_fetch 抓取核心,
# 供宿主 wrapper 与调试界面直调);调用记录与限流在 MCP 服务器侧(chat 滑动窗口)。
MCP_ENABLED = os.environ.get("MCP_ENABLED", "true").lower() == "true"
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8003").rstrip("/")
MCP_SERVER_PORT = int(os.environ.get("MCP_SERVER_PORT", "8003"))
MCP_TIMEOUT = int(os.environ.get("MCP_TIMEOUT", "30"))  # 秒,单次工具调用上限(含抓取)
MCP_RATE_LIMIT_WINDOW = int(os.environ.get("MCP_RATE_LIMIT_WINDOW", "300"))  # 秒,滑动窗口
MCP_USAGE_MAX_BYTES = int(
    os.environ.get("MCP_USAGE_MAX_BYTES", str(10 * 1024 * 1024))
)  # usage.jsonl 超量滚动保留尾部

# 跨会话长期记忆(复用 kb_service, collection="memory"): 总开关默认关,依赖 KB_ENABLED;
# 显式记忆走 kb_remember Action(主通道),自动抽取默认关(轮后异步、低置信度丢弃)
MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "false").lower() == "true"
MEMORY_AUTO_EXTRACT = os.environ.get("MEMORY_AUTO_EXTRACT", "false").lower() == "true"
MEMORY_MIN_CONFIDENCE = float(os.environ.get("MEMORY_MIN_CONFIDENCE", "0.8"))
MEMORY_MAX_RECALL = int(os.environ.get("MEMORY_MAX_RECALL", "5"))  # 每轮召回注入上限
# 过程状态类记忆(任务进行中/未完成/待处理)的存活天数:过期后召回过滤
# (不物理删除)——此类状态仅短期有效,长期留存会把过期任务当事实注入
MEMORY_PROCESS_TTL_DAYS = int(os.environ.get("MEMORY_PROCESS_TTL_DAYS", "3"))

# 生成可靠性(验证重试环 + 事实合成层): 模型声称交付但 outbox 无真实产物时
# 回灌反馈有界重试; 交付事实由系统脚注陈述(模型被 instruct 剥夺自述权)
VERIFY_MAX_RETRIES = int(os.environ.get("VERIFY_MAX_RETRIES", "1"))
FACT_FOOTER_ENABLED = os.environ.get("FACT_FOOTER_ENABLED", "true").lower() == "true"

# 意图分类门(生成可靠性 ① 层): chitchat/ambiguous 轮不挂重能力(文件写/web/记忆写);
# 默认关(灰度),开启后每轮 +1 次小请求; 分类失败 fail-open 为 task
INTENT_GATE_ENABLED = os.environ.get("INTENT_GATE_ENABLED", "false").lower() == "true"
INTENT_GATE_TIMEOUT = int(os.environ.get("INTENT_GATE_TIMEOUT", "5"))  # 秒


def ws_url() -> str:
    base = CONSOLE_BASE_URL
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def validate():
    missing = [
        name
        for name, value in (
            ("LLM_BASE_URL", LLM_BASE_URL),
            ("LLM_API_KEY", LLM_API_KEY),
            ("LLM_MODEL", LLM_MODEL),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)},请参考 .env.example 配置 .env")
    fallback_partial = [
        name
        for name, value in (
            ("LLM_FALLBACK_BASE_URL", LLM_FALLBACK_BASE_URL),
            ("LLM_FALLBACK_API_KEY", LLM_FALLBACK_API_KEY),
            ("LLM_FALLBACK_MODEL", LLM_FALLBACK_MODEL),
        )
        if not value
    ]
    if 0 < len(fallback_partial) < 3:
        import logging

        logging.getLogger(__name__).warning(
            "降级备份模型配置不完整(缺 %s),主端点故障时无法切换", ", ".join(fallback_partial)
        )
    if VISION_ENABLED:
        import logging

        logging.getLogger(__name__).info(
            "VISION_ENABLED=true: 图片将以 VL 方式发给模型 %s,请确认该模型支持视觉输入",
            LLM_MODEL,
        )
    if MCP_ENABLED and not WEB_ENABLED and not AMAP_ENABLED:
        import logging

        logging.getLogger(__name__).warning(
            "MCP_ENABLED=true 但 WEB_ENABLED 与 AMAP_ENABLED 均为 false,"
            "MCP 服务器可启动但不注册任何工具,联网/高德能力整体不生效"
        )
    if WEB_ENABLED and not WEB_SEARCH_API_KEY:
        # key 型源(博查)不可用;链路里全是 key 型源时联网能力整体不生效
        keyless = {"baidu", "bing"}
        chain = [WEB_SEARCH_PROVIDER] + [
            n.strip() for n in WEB_SEARCH_FALLBACK.split(",") if n.strip()
        ]
        if not any(n in keyless for n in chain):
            import logging

            logging.getLogger(__name__).warning(
                "WEB_ENABLED=true 但 WEB_SEARCH_API_KEY 为空且降级链无免 key 源"
                "(baidu/bing),联网能力按未启用处理"
            )
    if AMAP_ENABLED and not AMAP_API_KEY:
        import logging

        logging.getLogger(__name__).warning(
            "AMAP_ENABLED=true 但 AMAP_API_KEY 为空,高德能力按未启用处理"
        )
    if WEB_RENDER_FALLBACK:
        try:
            import playwright  # noqa: F401
        except ImportError:
            import logging

            logging.getLogger(__name__).warning(
                "WEB_RENDER_FALLBACK=true 但未安装 playwright,渲染兜底不生效,"
                "web_fetch 退回纯静态抓取(pip install playwright && playwright install chromium)"
            )
