import asyncio
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from .. import config, events, token_estimator
from ..capabilities import kb_client, skill_registry
from ..channels.channel_base import InboundMessage, ReplySink
from ..state import inbox, session_store, workspace_store
from . import capability_resolver, html_inline, intent_gate
from .capability_resolver import _active_action_turns
from agently import Agently
from agently.base import event_center

logger = logging.getLogger(__name__)

# Agently action 回路的外部化触发有两条独立路径(ActionArtifactManager,
# finalize_action_result):
#   ① 结果 JSON 超 8000B(硬编码字面量,_safe_json_size(data) > 8000);
#   ② instruction-heavy 判定——executor_type∈{browse,search,...} 或
#     kwargs 含 query/url/cmd/code/sql 等键(_INSTRUCTION_HEAVY_KWARGS),
#     无条件外部化,不看结果大小。
# 命中后模型看到 full_value_available 就会追加 read_action_artifact 轮读全文
# (弱模型每次都读,实测每轮 3-4s)。
# 防线:对 ① 是我们自己的输出预算(web_fetch 单页 2200 字符 / 批量每页 500 字符 /
# web_search 窄检索 120 字符摘要、宽检索 snippet 截 60 字,全部控制在 8000B 以内);
# 对 ② 直接把 query/url 从 heavy kwargs 集合移除——本项目的 web_search/web_fetch/
# kb_search 输出已做尺寸预算,不属于框架预设的"重指令工具"(bash/python/sql),
# 无代码执行类 action,移除无影响(2026-10-15 实证:74B 小结果带 query kwarg
# 也产生 2 个 artifact_refs,移除后归 0)。
# 类属性补丁同时覆盖 digest/recall 路径的预览阈值;升级 agently 时需复核此处。
try:
    from agently.core.operation.Action.ActionArtifactManager import (
        ActionArtifactManager,
    )

    ActionArtifactManager._MODEL_VISIBLE_RECORD_MAX_BYTES = 32768
    ActionArtifactManager._MODEL_VISIBLE_RESULT_PREVIEW_MAX_BYTES = 24576
    ActionArtifactManager._INSTRUCTION_HEAVY_KWARGS -= {"query", "url"}
except Exception as e:  # 框架结构变动时静默退回默认(只是慢,不坏)
    logger.warning("action 预览阈值放宽失败(agently 内部结构变动?): %s", e)

# skill 脚本沙箱的瞬时抖动重试(2026-10-15):框架每次 action 执行前现场探针选
# provider(docker version / image inspect 子进程,且初筛+复核两轮),Windows
# Docker Desktop 命名管道抖动会把单次调用判死成 provider_unavailable——模型随即
# 降级手写产物冒充交付(实证:html-deck render 失败一次,手写 6KB 裸 HTML 顶替
# 脚本渲染的 68KB 工作台,验证环只查产物存在性不查丰富度,照样投递)。
# 探针失败发生在容器启动前、零副作用,天然幂等,故仅对 provider_unavailable
# 错误码做一次退避重放;执行期失败(脚本非幂等)与其他错误码绝不重试。
_EXEC_RESOURCE_RETRY_DELAY = 3.0  # 秒
try:
    from agently.core.operation.ExecutionResource.ExecutionResource import (
        ExecutionResourceError,
        ExecutionResourceManager,
    )

    _orig_async_ensure = ExecutionResourceManager.async_ensure

    async def _async_ensure_with_probe_retry(self, *args, **kwargs):
        try:
            return await _orig_async_ensure(self, *args, **kwargs)
        except ExecutionResourceError as e:
            if getattr(e, "code", "") != "execution_resource.provider_unavailable":
                raise
            logger.warning(
                "执行资源探针判定无可用 provider,%.0fs 后重试一次(疑似瞬时抖动): %s",
                _EXEC_RESOURCE_RETRY_DELAY,
                e,
            )
            await asyncio.sleep(_EXEC_RESOURCE_RETRY_DELAY)
            return await _orig_async_ensure(self, *args, **kwargs)

    ExecutionResourceManager.async_ensure = _async_ensure_with_probe_retry
except Exception as e:  # 框架结构变动时静默退回默认(抖动裸露给模型,不坏)
    logger.warning("执行资源探针重试补丁失败(agently 内部结构变动?): %s", e)

PROGRESS_PUSH_INTERVAL = 0.1
ACTION_STEP_DETAIL_MAX = 200
RECALL_CHECK_INTERVAL = 3.0  # 生成中轮询源消息撤回状态的间隔(秒)

# 轮后自动记忆抽取(.dev_ws/probe_memory_extract.py 探针验证过结构稳定性)
MEMORY_EXTRACT_INSTRUCT = (
    "你是记忆抽取器。从这一轮对话中提取值得长期记住的事实或用户偏好"
    "(项目决定、人员分工、日期约定、用户明确要求记住的内容)。"
    "不要提取: 闲聊、一次性任务、问候、秘密信息。"
    "每条记忆给出 confidence(0-1): 用户明确要求记住=0.9-1.0, 明确陈述的事实=0.8-0.9, "
    "推测性的=0.5以下。没有值得记住的内容就返回空列表。"
    "每条记忆再给出 ttl_days(整数天数): 任务进度类过程状态(进行中、未完成、"
    "待处理、尚未交付等,只在短期内有效)填 3; 长期事实与用户偏好填 0(永不过期)。"
)
MEMORY_OUTPUT_SCHEMA = {
    "memories": [
        {
            "summary": "str, 一句话事实陈述",
            "confidence": "float, 0.0-1.0",
            "ttl_days": "int, 过程状态类=3, 长期事实/偏好=0",
        }
    ]
}

# 过程状态关键词兜底:模型漏标 ttl 时按文本特征补刀(保真优先,误标只是提前过期)
_MEMORY_PROCESS_STATE_RE = re.compile(
    r"进行中|未完成|待处理|待交付|尚未|未交付|正在做|待补|暂缓"
)


def _brief_kwargs(kwargs) -> str:
    if not isinstance(kwargs, dict):
        return ""
    return ", ".join(f"{k}={str(v)[:60]}" for k, v in kwargs.items())


# ── LLM 请求提示词观测(仪表盘) ─────────────────────────────
# 框架 ModelRequestRunner 每次模型请求前发 prompt.built 运行时事件,载荷含完整
# prompt_messages。按 agent_name(chat-<chat_id>-<turn_id>[-rN],见
# _run_generation)关联到 turn,脱敏截断后推给仪表盘——多步执行时能看到
# "当前这次请求给模型喂了什么"。intent/intent-* 等非会话请求不匹配正则,天然过滤。
_LLM_AGENT_NAME_RE = re.compile(r"^chat-(.+)-([0-9a-f]{12})(?:-r\d+)?$")
_LLM_PROMPT_MSG_MAX = 1500  # 单条历史消息展示上限(字符)
_LLM_PROMPT_MAIN_MAX = 8000  # 末条主提示词(instruct+input+action 结果)展示上限
_LLM_PROMPT_TOTAL_MAX = 20000  # 单次请求整体展示上限(字符)


def _flatten_message(msg) -> tuple[str, str]:
    """单条 prompt message → (role, 展示文本);rich content 的图片等大对象换占位说明。"""
    role = str(msg.get("role") or "?")
    content = msg.get("content")
    if isinstance(content, list):
        segs = []
        for part in content:
            if not isinstance(part, dict):
                segs.append(str(part))
                continue
            if part.get("type") == "text":
                segs.append(str(part.get("text") or ""))
            else:
                raw = json.dumps(part, ensure_ascii=False)
                segs.append(
                    f"[{part.get('type') or '附件'} 内容,约 {len(raw) // 1024} KB]"
                )
        content = "\n".join(s for s in segs if s)
    return role, str(content or "")


def _summarize_prompt_messages(messages) -> tuple[list[dict], int]:
    """prompt_messages → 仪表盘展示用的 [{role, content}],超长截断。

    首尾保留(2026-10-14 修复):Agently 的 to_messages 把全部 instruct(SYSTEM_PROMPT+
    能力提示语)与当前输入/action 结果拼进**最后一条** user 消息(main prompt),
    系统提示不在 system 角色里;旧策略从前往后截断,历史先吃光总预算,末条主提示词
    被整体省略,面板只剩历史(实证:action 回路第 3 次请求 20.8k 字全是历史)。
    现末条(主提示词)给独立大预算并永远保留,首条(system,如有)保留,
    中间历史从后往前填充,放不下打省略标记。
    """
    flat = (
        [_flatten_message(m) for m in messages if isinstance(m, dict)]
        if isinstance(messages, list)
        else []
    )
    if not flat:
        return [], 0

    def _cap(text: str, limit: int) -> str:
        return (
            text[:limit] + f" …(截断,原 {len(text)} 字)" if len(text) > limit else text
        )

    n = len(flat)
    # 末条是主提示词(instruct+input+action 结果),单条预算放宽
    tail_role, tail_text = flat[-1]
    tail = {"role": tail_role, "content": _cap(tail_text, _LLM_PROMPT_MAIN_MAX)}
    budget = _LLM_PROMPT_TOTAL_MAX - len(tail["content"])
    head = None
    if n > 1:
        head_role, head_text = flat[0]
        head = {"role": head_role, "content": _cap(head_text, _LLM_PROMPT_MSG_MAX)}
        budget -= len(head["content"])
    # 中间历史从后往前(新的优先)填充剩余预算
    middle: list[dict] = []
    omitted = 0
    for i, (role, text) in enumerate(reversed(flat[1:-1] if head else flat[:-1])):
        capped = _cap(text, _LLM_PROMPT_MSG_MAX)
        if budget - len(capped) < 0:
            # 中间历史保持连续性:放不下即整体省略更早的部分,不跳序拼接
            omitted = len(flat) - (2 if head else 1) - i
            break
        middle.append({"role": role, "content": capped})
        budget -= len(capped)
    middle.reverse()
    if omitted:
        middle.insert(
            0,
            {
                "role": "system",
                "content": f"…(中间省略 {omitted} 条历史消息,超出展示上限)",
            },
        )
    out = ([head] if head else []) + middle + [tail]
    return out, sum(len(m["content"]) for m in out)


def _llm_prompt_event_hook(event):
    payload = event.payload if isinstance(event.payload, dict) else {}
    m = _LLM_AGENT_NAME_RE.match(str(payload.get("agent_name") or ""))
    if not m:
        return
    messages, total = _summarize_prompt_messages(payload.get("prompt_messages"))
    events.publish(
        {
            "type": "llm_prompt",
            "chat_id": m.group(1),
            "turn_id": m.group(2),
            "attempt": payload.get("attempt_index"),
            "messages": messages,
            "total_chars": total,
            "ts": time.time(),
        }
    )


event_center.register_hook(
    _llm_prompt_event_hook,
    event_types=["prompt.built"],
    hook_name="chat_pipeline_llm_prompt",
)


async def _push_step_line(live: dict, line: str):
    """把 action 进展行追加进飞书流式卡片;失败置 broken,后续进展不再尝试。"""
    try:
        ok = await live["sink"].stream_append(live["stream_id"], line)
    except Exception:  # noqa: BLE001 — 进展推送失败不影响主链路
        ok = False
    if not ok:
        live["broken"] = True


# 上卡脱敏:step detail 面向仪表盘排查保留完整路径,推飞书卡片(对外)前一律
# 抹掉宿主机绝对路径(2026-10-13 实证:workspace 挂载 step 把
# D:\AI\...\workspaces\user-ou_<32hex> 推给了用户)
_HOST_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s,;,。]*")


def _redact_host_paths(text: str) -> str:
    return _HOST_PATH_RE.sub("<本机路径>", text)


# 准备阶段 step → 飞书卡片行图标。这些 step(意图分类/workspace 挂载/能力挂载/
# 脚本绑定)都发生在 stream_begin 建卡之前,飞书侧原本完全无感知(仅仪表盘可见);
# 建卡后由 _run_generation 把已有此类 step 一次性补推上卡。
_SETUP_STEP_ICONS = {
    "intent": "🎯",
    "workspace": "📁",
    "mcp": "🌐",
    "kb": "📚",
    "skills_scripts": "🧰",
    "skills_scripts_failed": "⚠️",
}


# 工具"假成功"识别:web/amap/kb 等工具把可预期失败按结构化字符串返回(不抛异常),
# action 层 ok=True,直接展示会全部显示"成功"(2026-10-12 实证:8 次全败的下载
# 轮在仪表盘/飞书全显示成功)。completed 事件里对结果内容做错误形态检测。
_ERROR_RESULT_PREFIXES = (
    "错误:",
    "抓取失败",
    "搜索失败",
    "不支持的内容类型",
    "页面未抽取",
    "响应内容为空",
    "文件保存失败",
    "文件过大",
    "知识库检索失败",
    "天气查询失败",
    "路径规划失败",
    "POI 检索失败",
    "地址解析失败",
)


def _result_failure(result) -> Optional[str]:
    """识别"ok 但结果内容是错误"的假成功,返回失败摘要;非失败返回 None。"""
    if isinstance(result, str):
        text = result.strip()
        if text.startswith(_ERROR_RESULT_PREFIXES):
            return text
        return None
    if isinstance(result, dict):
        # 框架 MCPActionExecutor 把 isError 映射为 {"error": text};宿主批量物化单项
        # 失败也是 {ok: False, error}
        err = result.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
        return None
    if isinstance(result, list) and result:
        fails = [
            str(item.get("error")).strip()
            for item in result
            if isinstance(item, dict) and item.get("ok") is False and item.get("error")
        ]
        if not fails:
            return None
        if len(fails) == len(result):
            return f"全部 {len(result)} 项失败: {fails[0]}"
        return f"{len(fails)}/{len(result)} 项失败: {fails[0]}"
    return None


def _action_event_hook(event):
    """把 Agently Action 回路的 started/completed 事件转成 turn.steps 实时推给仪表盘;
    started/failed 同时追加进飞书流式卡片(正文开始输出时卡片被正文覆盖,progress 仅工具期可见)。"""
    payload = event.payload if isinstance(event.payload, dict) else {}
    agent_name = payload.get("agent_name")
    if not agent_name and event.run is not None:
        agent_name = getattr(event.run, "agent_name", None)
    entry = _active_action_turns.get(agent_name)
    if entry is None:
        return
    pipeline, chat_id, turn, live = entry
    name = payload.get("action_name") or "unknown"
    not_achieved: Optional[str] = None
    if event.event_type == "action.started":
        command = payload.get("command") or {}
        detail = f"执行 {name}({_brief_kwargs(command.get('action_input'))})"
        # 结构化规划的 purpose 字段是模型"这次调用想收集/验证什么"的思考摘要,
        # 拼进 detail 让仪表盘能看到工具调用背后的意图;
        # 模型没给时框架会兜底填 "Use {action_id}"(normalize_action_call),属噪声,过滤
        purpose = str(command.get("purpose") or "").strip()
        if purpose and purpose != f"Use {name}":
            detail += f" ← {purpose[:80]}"
        if name not in turn.actions_seen:
            turn.actions_seen.append(name)
    else:
        record = payload.get("record") or {}
        brief = _brief_kwargs(record.get("kwargs"))
        if record.get("ok"):
            not_achieved = _result_failure(record.get("result", record.get("data")))
        if not record.get("ok"):
            error_text = str(record.get("error") or "")
            detail = f"{name}({brief}) → 失败: {error_text[:80]}"
            if "No eligible ExecutionResourceProvider" in error_text:
                # 框架错误串不含 provider 探针 reason,失败当刻补一次主动诊断留证
                logger.warning(
                    "action %s 无可用执行资源,当刻诊断: %s",
                    name,
                    skill_registry.diagnose_execution_unavailable(),
                )
        elif not_achieved is not None:
            detail = f"{name}({brief}) → 未达成: {not_achieved[:80]}"
        else:
            detail = f"{name}({brief}) → 成功"
    detail = detail[:ACTION_STEP_DETAIL_MAX]
    pipeline._add_step(chat_id, turn, "action", detail)
    # 进展实时上卡片:推 started(带 purpose)、failed 和"假成功"(completed 但结果
    # 是错误文案);纯 completed 属噪声不推。
    # web/评测 sink 的 stream_begin 返回 None → live["stream_id"] 为空,天然跳过
    if (
        (
            event.event_type in ("action.started", "action.failed")
            or not_achieved is not None
        )
        and live
        and live.get("stream_id")
        and live.get("sink")
        and not live.get("broken")
    ):
        icon = "🔧" if event.event_type == "action.started" else "⚠️"
        try:
            asyncio.run_coroutine_threadsafe(
                _push_step_line(live, f"\n{icon} {_redact_host_paths(detail)}\n"),
                live["loop"],
            )
        except RuntimeError:  # 事件循环已关闭(进程退出中),丢弃
            pass


event_center.register_hook(
    _action_event_hook,
    event_types=["action.started", "action.completed", "action.failed"],
    hook_name="chat_pipeline_action_steps",
)


def _turn_created_event(chat_id: str, turn: session_store.Turn) -> dict:
    return {
        "type": "turn_created",
        "chat_id": chat_id,
        "session": session_store.get_summary(chat_id),
        "turn": turn.to_dict(),
    }


def _turn_status_event(chat_id: str, turn: session_store.Turn) -> dict:
    return {
        "type": "turn_status",
        "chat_id": chat_id,
        "session": session_store.get_summary(chat_id),
        "turn": turn.to_dict(),
    }


def _step_event(chat_id: str, turn: session_store.Turn, step: dict) -> dict:
    return {"type": "step", "chat_id": chat_id, "turn_id": turn.id, "step": step}


def _snapshot_outbox(ws_root: str) -> dict[str, tuple[int, float]]:
    """outbox/ 快照:相对路径 -> (size, mtime);目录不存在按空。"""
    outbox = Path(ws_root) / "outbox"
    snap: dict[str, tuple[int, float]] = {}
    if not outbox.is_dir():
        return snap
    for p in outbox.rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(Path(ws_root))).replace("\\", "/")] = (
                st.st_size,
                st.st_mtime,
            )
    return snap


def _diff_outbox(before: dict[str, tuple[int, float]], ws_root: str) -> list[dict]:
    """与快照 diff:新增或变更(size/mtime 不同)的文件即本轮产物。"""
    produced: list[dict] = []
    for rel, (size, mtime) in _snapshot_outbox(ws_root).items():
        if before.get(rel) == (size, mtime):
            continue
        produced.append({"path": rel, "name": Path(rel).name, "size": size})
    produced.sort(key=lambda f: f["path"])
    return produced


# ── 产物投递收拢(2026-10-13 起)──────
# dashi-ppt/html-ppt 类 skill 的产物是一整棵目录树(deck HTML + assets + 各页
# 文件),outbox diff 逐文件投递会刷几十上百个附件(实测 207 个)。收拢策略:
# ① 站点目录:产物目录含 index.html 时(html_inline 已把 index.html 自包含化),
#    只投 index.html + 整树 zip 备份,其余文件不再单发;
# ② 附件数护栏:收拢后仍超 DELIVERY_MAX_ATTACHMENTS 时,全部打成一个 zip 只投它。
def _zip_files(paths: list[Path], zip_path: Path, ws: Path) -> dict:
    """把 paths 打进 zip_path(保持 workspace 相对结构);返回 produced 同构条目。"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, str(p.relative_to(ws)).replace("\\", "/"))
    return {
        "path": str(zip_path.relative_to(ws)).replace("\\", "/"),
        "name": zip_path.name,
        "size": zip_path.stat().st_size,
    }


def _consolidate_delivery(
    produced: list[dict], ws_root: str, turn_id: str
) -> tuple[list[dict], list[str]]:
    """产物收拢:返回 (投递清单, 收拢说明);zip 写入 outbox 随 workspace 持久。"""
    ws = Path(ws_root)
    notes: list[str] = []
    idx_dirs = sorted(
        {
            str(PurePosixPath(f["path"]).parent)
            for f in produced
            if f["name"].lower() == "index.html"
        }
    )
    # 嵌套去重:index.html 目录互为包含时只取最外层
    tops = [
        d
        for d in idx_dirs
        if not any(d.startswith(o + "/") for o in idx_dirs if o != d)
    ]
    folded: set[str] = set()
    extras: list[dict] = []
    for d in tops:
        members = [f for f in produced if f["path"].startswith(d + "/")]
        if len(members) <= 1:
            continue
        zip_rel = f"{d}.zip"
        extras.append(_zip_files([ws / f["path"] for f in members], ws / zip_rel, ws))
        keep = f"{d}/index.html"
        folded.update(f["path"] for f in members if f["path"] != keep)
        notes.append(
            f"{d}/ 共 {len(members)} 个产物收拢: 仅投 index.html + 整树备份 {zip_rel}"
        )
    deliver = [f for f in produced if f["path"] not in folded] + extras
    if len(deliver) > config.DELIVERY_MAX_ATTACHMENTS:
        zip_rel = f"outbox/deliverables-{turn_id}.zip"
        entry = _zip_files([ws / f["path"] for f in deliver], ws / zip_rel, ws)
        notes.append(
            f"附件 {len(deliver)} 个超护栏 {config.DELIVERY_MAX_ATTACHMENTS},"
            f"全部收拢为 {zip_rel}"
        )
        deliver = [entry]
    return deliver, notes


# ── 正文内联文件兜底(deepseek-flash DSML / tool_use / 大代码块)──────
# 弱工具调用模型间歇性把 write_file 以伪格式写进正文,实测三种形态:
# A. DSML:<｜｜DSML｜｜ invoke name="write_file"> 或 <｜｜DSML｜｜ calls>,
#    parameter 标签可带 DSML 前缀也可不带,甚至只剩闭合标记;
# B. <tool_use><name>write_file</name><arguments>{JSON}</arguments></tool_use>;
# C. 整段 ```html 大代码块。
# 有时 Action 成功了正文仍残留一段伪调用文本。这里在生成结束后兜底:
# 无真实产物时把内联内容提取为 outbox 真实文件(走既有 diff 投递链路);
# 无论是否落盘,正文里的伪调用段一律剥除。启发式擦屁股逻辑,阈值从严,
# 避免误伤正常短代码示例。

_DSML_PATH_RE = re.compile(r'parameter name="path"[^>]*>([^<]+)')
_DSML_CONTENT_RE = re.compile(r'parameter name="content"[^>]*>(.*)', re.DOTALL)
# 开/闭合 DSML 标记都认(实测模型会只输出闭合标记)
_DSML_MARKER_RE = re.compile(r"<[｜|/][^<>\n]{0,12}DSML")
_DSML_BARE_PARAM_RE = re.compile(r'<parameter name="path"')
# 伪调用补偿执行(2026-10-13 起):回复阶段模型把工具调用写成 DSML 文本
# (回复阶段没有 action 通道,规划阶段又提前收尾),清洗+空重试实测无效
# (turn b246536e28e2 两轮都 DSML 收场)。这里把 invoke 解析出来真实执行,
# 结果随验证反馈回灌。invoke 名带 DSML 全角竖线变体也认;参数值截到闭合标记。
_DSML_INVOKE_RE = re.compile(r'invoke name="([\w.\-]+)"')
_DSML_PARAM_RE = re.compile(
    r'parameter name="([\w.\-]+)"[^>]*>(.*?)</[^>]*parameter\s*>',
    re.DOTALL,
)
_TOOL_USE_HEAD_RE = re.compile(
    r"<tool_use>\s*<name>\s*(write_file)\s*</name>\s*<arguments>\s*\{", re.DOTALL
)
_TOOL_USE_BLOCK_RE = re.compile(r"<tool_use>.*?</tool_use>", re.DOTALL)
_FENCED_BLOCK_RE = re.compile(
    r"```(?:html|xml)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
RESCUE_MIN_DSML_CHARS = 200
RESCUE_MIN_FENCED_CHARS = 800


def _strip_dsml_tail(answer: str) -> str:
    """剥掉正文里的 DSML 伪工具调用段;无伪调用特征时原样返回。

    剥除起点取最早命中:DSML 开/闭合标记;裸 <parameter name="path">
    (仅在正文同时出现 DSML 字样时采信,避免误伤正常文本)。
    """
    starts: list[int] = []
    marker = _DSML_MARKER_RE.search(answer)
    if marker:
        starts.append(marker.start())
    if "DSML" in answer:
        bare = _DSML_BARE_PARAM_RE.search(answer)
        if bare:
            starts.append(bare.start())
    if not starts:
        return answer
    return answer[: min(starts)].rstrip()


def _remove_tool_use_blocks(answer: str) -> str:
    """剥掉正文里的 <tool_use> 伪调用块(未闭合的截到结尾);无则原样返回。"""
    if "<tool_use>" not in answer:
        return answer
    cleaned = _TOOL_USE_BLOCK_RE.sub("", answer)
    dangling = cleaned.find("<tool_use>")
    if dangling >= 0:
        cleaned = cleaned[:dangling]
    return cleaned.strip()


_TOOL_RESULT_BLOCK_RE = re.compile(r"<tool_result>.*?</tool_result>", re.DOTALL)
# 畸形变体:缺前导 < 的裸 tool_result> 行(2026-10-07 飞书事故实证)
_TOOL_RESULT_LINE_RE = re.compile(r"(?m)^\s*<?tool_result>.*$")


def _remove_tool_result_blocks(answer: str) -> str:
    """剥掉正文里的 <tool_result> 幻觉工具回包(伪调用形态 D);无则原样返回。"""
    if "tool_result>" not in answer:
        return answer
    cleaned = _TOOL_RESULT_BLOCK_RE.sub("", answer)
    cleaned = _TOOL_RESULT_LINE_RE.sub("", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)


def _remove_tool_call_blocks(answer: str) -> str:
    """剥掉正文里的 <tool_call> 伪调用块(形态 E,2026-10-11 实测:
    模型在 Action 回路结束后把更多工具调用以该格式写进正文);
    未闭合的截到结尾;无则原样返回。"""
    if "<tool_call>" not in answer:
        return answer
    cleaned = _TOOL_CALL_BLOCK_RE.sub("", answer)
    dangling = cleaned.find("<tool_call>")
    if dangling >= 0:
        cleaned = cleaned[:dangling]
    return cleaned.strip()


def _clean_pseudo_calls(answer: str) -> str:
    """正文清洗:tool_use/tool_call 块 + tool_result 幻觉回包 + DSML 段一并剥除。"""
    return _strip_dsml_tail(
        _remove_tool_call_blocks(
            _remove_tool_result_blocks(_remove_tool_use_blocks(answer))
        )
    ).strip()


_PSEUDO_EXEC_MAX_INVOKES = 5
_PSEUDO_EXEC_OUTPUT_CAP = 2000


def _parse_dsml_invokes(text: str) -> list[tuple[str, dict[str, str]]]:
    """从正文解析 DSML 伪调用 invoke 列表 [(action_name, {param: value})]。

    供"补偿执行"使用:回复阶段模型写出的 DSML 调用文本是结构化的真实意图
    (2026-10-13 实证:invoke 名与参数都正确,只是没走 action 机制)。
    参数值内的 DSML 标记残留一并剥除。上限 _PSEUDO_EXEC_MAX_INVOKES 条。
    """
    if "DSML" not in text:
        return []
    invokes: list[tuple[str, dict[str, str]]] = []
    matches = list(_DSML_INVOKE_RE.finditer(text))
    for i, m in enumerate(matches[:_PSEUDO_EXEC_MAX_INVOKES]):
        name = m.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = text[m.end() : end]
        params: dict[str, str] = {}
        for pm in _DSML_PARAM_RE.finditer(segment):
            value = re.sub(r"<[｜|/][^<>\n]{0,12}DSML[^<>\n]{0,20}>", "", pm.group(2))
            params[pm.group(1)] = value.strip()
        invokes.append((name, params))
    return invokes


def _pseudo_exec_safe_path(ws_root: str, rel: str) -> Optional[Path]:
    """workspace 相对路径解析 + 逃逸防护;越界返回 None。"""
    rel = str(rel or "").strip().lstrip("/")
    if not rel:
        return None
    try:
        root = Path(ws_root).resolve()
        target = (root / rel).resolve()
        target.relative_to(root)
        return target
    except (OSError, ValueError):
        return None


def _execute_pseudo_invokes(
    ws_root: str, invokes: list[tuple[str, dict[str, str]]]
) -> list[str]:
    """把回复阶段 DSML 伪调用真实执行(白名单制),返回逐条结果行(截断)。

    白名单:read_file / list_files(宿主文件读)、skill_shell_*(专用镜像
    docker 直跑,与 action 同隔离剖面)。write_file/write_deliverable 已由
    rescue 的提取落盘覆盖,这里不重复;未知 action 记录跳过。全部 fail-open。
    """
    results: list[str] = []
    for name, params in invokes:
        try:
            if name == "read_file":
                target = _pseudo_exec_safe_path(ws_root, params.get("path", ""))
                if target is None or not target.is_file():
                    results.append(
                        f"{name}({params.get('path', '')}): 文件不存在或路径越界"
                    )
                    continue
                offset = int(params.get("offset") or 0)
                max_bytes = min(int(params.get("max_bytes") or 8000), 8000)
                data = target.read_bytes()[offset : offset + max_bytes]
                results.append(
                    f"{name}({params.get('path', '')}, offset={offset}) 真实执行结果:\n"
                    + data.decode("utf-8", errors="replace")[:_PSEUDO_EXEC_OUTPUT_CAP]
                )
            elif name == "list_files":
                target = _pseudo_exec_safe_path(ws_root, params.get("path") or ".")
                if target is None or not target.is_dir():
                    results.append(
                        f"{name}({params.get('path', '')}): 目录不存在或路径越界"
                    )
                    continue
                pattern = params.get("pattern") or "*"
                max_results = min(int(params.get("max_results") or 100), 200)
                entries = sorted(
                    p.name + ("/" if p.is_dir() else "") for p in target.glob(pattern)
                )
                results.append(
                    f"{name}({params.get('path') or '.'}) 真实执行结果:\n"
                    + "\n".join(entries[:max_results])[:_PSEUDO_EXEC_OUTPUT_CAP]
                )
            elif name.startswith("skill_shell_"):
                output = skill_registry.run_skill_shell_compensation(
                    name, str(params.get("command") or ""), ws_root
                )
                results.append(
                    f"{name} 真实执行结果:\n" + output[:_PSEUDO_EXEC_OUTPUT_CAP]
                )
            else:
                results.append(f"{name}: 不在补偿执行白名单,未执行")
        except Exception as e:
            results.append(f"{name}: 补偿执行失败({e})")
    return results


def _json_unescape_lenient(raw: str) -> str:
    """JSON 字符串值反转义;完整串走 json.loads,截断/畸形串手工还原常见转义。"""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
        return re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(1)), raw)


def _extract_tool_use_file(answer: str) -> Optional[tuple[str, str, str]]:
    """形态 B:<tool_use> 块内 arguments 是 JSON,解析出 path/content。

    JSON 完整时走 raw_decode;截断/畸形(实测存在)时按正则抠 "path"/
    "content" 字符串值,content 不要求闭合引号(截断时保部分内容交付)。
    """
    for head in _TOOL_USE_HEAD_RE.finditer(answer):
        try:
            args, _end = json.JSONDecoder().raw_decode(answer, head.end() - 1)
        except json.JSONDecodeError:
            args = None
        if isinstance(args, dict):
            raw_path = str(args.get("path") or "")
            raw_content = str(args.get("content") or "")
        else:
            block_end = answer.find("</tool_use>", head.end())
            block = answer[head.end() : block_end if block_end > 0 else len(answer)]
            path_match = re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', block)
            content_match = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)', block)
            if not (path_match and content_match):
                continue
            raw_path = _json_unescape_lenient(path_match.group(1))
            raw_content = _json_unescape_lenient(content_match.group(1))
        file_content = raw_content.strip()
        if raw_path.strip() and len(file_content) >= RESCUE_MIN_DSML_CHARS:
            name = inbox._sanitize(Path(raw_path.strip()).name)
            return name, file_content, _clean_pseudo_calls(answer)
    return None


def _extract_inline_file(answer: str) -> Optional[tuple[str, str, str]]:
    """从正文兜底提取「内联文件」,返回 (文件名, 内容, 清洗后正文);无可提取返回 None。

    形态 A:DSML 伪工具调用(path/content 参数被写进正文,函数名可能不出现);
    形态 B:<tool_use> JSON 伪调用;形态 C:整段 ```html/```xml 围栏代码块。
    """
    if (
        "DSML" in answer
        and 'parameter name="path"' in answer
        and 'parameter name="content"' in answer
    ):
        path_match = _DSML_PATH_RE.search(answer)
        content_match = _DSML_CONTENT_RE.search(answer)
        if path_match and content_match:
            name = inbox._sanitize(Path(path_match.group(1).strip()).name)
            file_content = re.split(
                r"</parameter\s*>|<[｜|/]*DSML", content_match.group(1)
            )[0].strip()
            if len(file_content) >= RESCUE_MIN_DSML_CHARS:
                return name, file_content, _strip_dsml_tail(answer)
    if "<tool_use>" in answer and "write_file" in answer:
        extracted = _extract_tool_use_file(answer)
        if extracted is not None:
            return extracted
    blocks = list(_FENCED_BLOCK_RE.finditer(answer))
    if blocks:
        longest = max(blocks, key=lambda m: len(m.group(1)))
        file_content = longest.group(1).strip()
        if len(file_content) >= RESCUE_MIN_FENCED_CHARS:
            title_match = _HTML_TITLE_RE.search(file_content)
            name = inbox._sanitize(title_match.group(1).strip()) if title_match else ""
            if not name:
                name = "document"
            if not name.lower().endswith((".html", ".htm", ".xml")):
                name += ".html"
            cleaned = (answer[: longest.start()] + answer[longest.end() :]).strip()
            return name, file_content, cleaned
    return None


# ── 验证重试环 + 事实合成层(生成可靠性 ②③ 层)──────────────────
# 模型无权自述交付事实:声称有产物但 outbox diff 为空 → 回灌反馈有界重试;
# 最终交付状态由系统脚注陈述(ground truth: outbox diff + send_file 回执),
# 脚注不进会话历史,防模型从历史模仿脚注格式。

_VERIFY_FEEDBACK = (
    "系统反馈: 上一轮你声称已生成文件,但系统检查工作区 outbox/ 后确认文件并不存在——"
    "你刚才没有真正调用写文件工具。如用户确实需要文件,必须在本轮的工具调用(Action)"
    "阶段真实调用 write_deliverable(或 write_file)写入 outbox/ 目录;如果做不到,"
    "如实说明无法完成,不要在正文里模拟工具调用或工具返回,也不要声称已完成。"
)

# 伪调用专用反馈(2026-10-13 起):模型把工具调用写成正文文本(DSML/tool_use 等),
# 清洗后只剩"我来尝试下载/生成…"的承诺句,工具实际从未执行(实证:下载轮模型
# 宣称"我来尝试从你给的仓库地址下载"后回合即结束,什么都没发生)
_VERIFY_FEEDBACK_PSEUDO = (
    "系统反馈: 你上一轮的回复正文里包含工具调用文本,但那只是写在正文里的文字,"
    "系统没有收到任何真实的工具调用(Action)——工具从未执行,用户只看到了一句承诺。"
    "请在本轮通过真实的 Action 调用执行(调用由系统在 Action 阶段接管,"
    "不要把调用语法写进正文);若确实无法完成,如实说明原因,不要只承诺不执行。"
)

# 交付声明检测(只用于触发验证/脚注,不做删句——删句是脆弱 NLP,真相由脚注陈述)
# 动词表实证补丁(2026-10-15): qwen 兜底回复用「已为您制作…PPT」绕过旧动词表
# (生成/保存/写入/放在/创建/做好/完成),验证环全程静默、虚构交付直接发给用户
_DELIVERABLE_CLAIM_RE = re.compile(
    r"已(?:为您|替你|为大家|帮忙)?"
    r"(生成|保存|写入|放在|创建|做好|完成|制作|撰写|编写|整理|汇总|搞定|弄好)"
    r".{0,24}(outbox|文件|ppt|html|文档|表格|幻灯片|简报|deck)"
    r"|outbox/[\w\-.一-鿿]+",
    re.IGNORECASE,
)

# 文件交付的结构旁证(2026-10-15 起):needs_file 轮正文出现具体文件名(扩展名)但
# 全程无写盘 action——多半是在虚构交付;澄清提问类回复几乎不含带扩展名的文件名
_FILE_NAME_RE = re.compile(
    r"[\w一-鿿][\w一-鿿\-]{1,60}\.(html|pptx?|docx?|xlsx?|pdf|csv)",
    re.IGNORECASE,
)


@dataclass
class _GenerationOutcome:
    """单次生成的结果;aborted=True 表示失败/撤回已自行收尾,外层直接返回。"""

    full_text: str = ""
    aborted: bool = False
    rescue_hit: bool = False  # rescue 提取了内联文件落盘
    pseudo_stripped: bool = False  # 正文剥除过伪调用文本(可能"承诺了但没真调用")
    pseudo_invokes: list = field(default_factory=list)  # 剥除的 DSML 伪调用(供补偿执行)
    stream_id: Optional[str] = None
    stream_broken: bool = False
    card_deferred: bool = False  # 空回复兜底文案暂缓上卡(占位提示,等重试/终态统一覆写)


def _compose_fact_footer(
    produced: list[dict], delivery_results: list[bool], claim_detected: bool
) -> str:
    """系统撰写的交付事实脚注(交付事实的唯一权威);空串=无需脚注。"""
    if produced:
        lines = []
        for i, f in enumerate(produced):
            ok = delivery_results[i] if i < len(delivery_results) else False
            size_kb = f["size"] / 1024
            if ok:
                lines.append(f"📎 {f['name']}({size_kb:.1f} KB)已作为附件发送")
            else:
                lines.append(f"📎 {f['name']} 已生成但发送失败")
        return "\n".join(lines)
    if claim_detected:
        return "⚠️ 以上提到的文件实际并未生成成功(系统未检测到真实文件写入),如需文件请让我重试。"
    return ""


class Pipeline:
    """agent 核心:会话历史、turn 状态、LLM 流式生成;渠道差异由 ReplySink 屏蔽。"""

    async def handle_message(
        self,
        msg: InboundMessage,
        sink: ReplySink,
        turn: Optional[session_store.Turn] = None,
    ):
        chat_id = msg.chat_id
        content = msg.text.strip()
        if not chat_id:
            logger.warning("消息缺少 chat_id,忽略: %s", msg)
            return

        if msg.msg_type not in ("text", "post"):
            if msg.attachment is None:
                if turn is None:
                    turn = session_store.create_turn(
                        chat_id, msg.ref, msg.source, content or "[非文本消息]"
                    )
                    events.publish(_turn_created_event(chat_id, turn))
                reply_text = "暂只支持文本消息~"
                if await sink.reply(reply_text):
                    turn.answer = reply_text
                self._set_turn_status(chat_id, turn, session_store.STATUS_DONE)
                return
            att = msg.attachment
            if turn is None:
                turn = session_store.create_turn(
                    chat_id, msg.ref, msg.source, f"[{msg.msg_type}] {att['name']}"
                )
                events.publish(_turn_created_event(chat_id, turn))
            self._add_step(
                chat_id,
                turn,
                "attachment",
                f"已保存: inbox/{att['saved_as']} ({att['size'] / 1024:.1f} KB)",
            )
            kind = "图片" if msg.msg_type == "image" else "文件"
            if msg.msg_type == "image" and config.VISION_ENABLED and msg.images:
                # VL:用户配文即问题,无配文用默认问题;图片在 _generate_and_reply 挂载
                content = content or "描述这张图片的主要内容"
            else:
                content = (
                    f"[用户发送了{kind}] {att['name']},"
                    f"已保存到工作区 inbox/{att['saved_as']}。"
                    "可用 read_file 读取内容后回答;图片内容暂不可读,请如实说明。"
                )
        # post/引用关联的图片:VL 关闭时补"图片不可读"说明;VL 开启且无文字时补默认问题
        if msg.images and not config.VISION_ENABLED:
            note = (
                f"(消息含 {len(msg.images)} 张图片,已存工作区 inbox/,"
                "当前版本不可读,请如实说明)"
            )
            content = f"{content}\n{note}" if content else note
        if not content and msg.images and config.VISION_ENABLED:
            content = "描述这张图片的主要内容"
        if not content:
            return

        if turn is None:
            turn = session_store.create_turn(chat_id, msg.ref, msg.source, content)
            events.publish(_turn_created_event(chat_id, turn))

        async with session_store.get_lock(chat_id):
            self._init_usage(chat_id, content, turn)
            self._set_turn_status(chat_id, turn, session_store.STATUS_PROCESSING)
            await self._generate_and_reply(
                sink, chat_id, content, turn, images=msg.images, sender=msg.sender
            )

    def _set_turn_status(
        self,
        chat_id: str,
        turn: session_store.Turn,
        status: str,
        error: Optional[str] = None,
    ):
        turn.status = status
        if status == session_store.STATUS_PROCESSING:
            turn.started_at = time.time()
        if status in (session_store.STATUS_DONE, session_store.STATUS_FAILED):
            turn.finished_at = time.time()
        # 每次状态迁移都落盘(不只终态):进行中的轮次被重启打断后,
        # 恢复时能识别为"被中断"并告知用户,而不是随进程消失
        session_store.save()
        if error:
            turn.error = error
        session_store.touch(chat_id)
        events.publish(_turn_status_event(chat_id, turn))

    def _add_step(
        self, chat_id: str, turn: session_store.Turn, kind: str, detail: str = ""
    ):
        step = turn.add_step(kind, detail)
        events.publish(_step_event(chat_id, turn, step))

    def _init_usage(self, chat_id: str, content: str, turn: session_store.Turn):
        messages = [{"role": "system", "content": config.SYSTEM_PROMPT}]
        messages.extend(session_store.get_history(chat_id))
        messages.append({"role": "user", "content": content})
        turn.usage = {
            "input_tokens": token_estimator.estimate_messages(messages),
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "context_window": config.LLM_CONTEXT_WINDOW,
            "estimated": True,
        }

    def _finalize_usage(self, turn: session_store.Turn):
        if turn.usage is None:
            return
        turn.usage["output_tokens"] = token_estimator.estimate_tokens(turn.answer)
        turn.usage["reasoning_tokens"] = token_estimator.estimate_tokens(turn.reasoning)

    async def _generate_and_reply(
        self,
        sink: ReplySink,
        chat_id: str,
        content: str,
        turn: session_store.Turn,
        images: Optional[list[dict]] = None,
        sender: Optional[str] = None,
    ):
        """外层编排:生成(产物验证有界重试)→ 投递 → 系统事实脚注 → 落历史。"""
        ws_root = workspace_store.root_for_chat(chat_id)
        outbox_before = _snapshot_outbox(ws_root) if ws_root else {}
        turn_started = time.time()
        # 意图门(① 层,默认关):chitchat/ambiguous 轮不挂重能力,机制上防长任务误触发;
        # 分类失败 fail-open 为 task。图片轮跳过(VL 问题本身就是任务)。
        # needs_file 二级信号控制交付流程 instruct 是否注入(意图门关时回退 True,零回归)
        intent = intent_gate.INTENT_TASK
        needs_file = True
        content_effective = content  # 指代改写后的自包含请求(无改写时=原文)
        if config.INTENT_GATE_ENABLED and not images:
            result = await intent_gate.classify(
                content, history=session_store.get_history(chat_id)
            )
            intent, needs_file = result.intent, result.needs_file
            detail = f"意图分类: {intent}"
            if intent == intent_gate.INTENT_TASK and needs_file:
                detail += ", 文件任务"
            if result.rewritten:
                # 指代改写:后续流程(记忆召回/skill 匹配/模型输入)统一消费改写后的
                # 自包含请求;会话历史与记忆抽取仍用用户原文
                detail += f", 指代改写: {result.rewritten[:60]}"
                content_effective = result.rewritten
            self._add_step(chat_id, turn, "intent", detail)
        outcome = await self._run_generation(
            sink,
            chat_id,
            content_effective,
            turn,
            sender=sender,
            images=images,
            ws_root=ws_root,
            outbox_before=outbox_before,
            attempt=0,
            stream=True,
            intent=intent,
            needs_file=needs_file,
            may_retry=bool(
                config.VERIFY_MAX_RETRIES > 0
                and ws_root
                and not config.WORKSPACE_READ_ONLY
            ),
        )
        if outcome.aborted:
            return
        # 产物验证重试环:模型声称交付但 outbox diff 为空 → 回灌系统反馈重试。
        # 仅读写 workspace 有意义(只读没有写能力,虚假声明靠事实脚注诚实化)
        stream_id, stream_broken = outcome.stream_id, outcome.stream_broken
        # 意图升级(① 层自愈,有界一次):chitchat/ambiguous 轮不挂重能力,若模型
        # 仍在正文写出工具伪调用/交付声明,说明分类偏保守(2026-10-13 实测:文件
        # 任务中途的追问被判 ambiguous,重能力卸载后模型只能用正文假装调用)——
        # 升级为 task 全量挂载重生成一轮,不把伪承诺直接发给用户
        if intent != intent_gate.INTENT_TASK and (
            outcome.pseudo_invokes
            or outcome.pseudo_stripped
            or _DELIVERABLE_CLAIM_RE.search(outcome.full_text or "")
        ):
            self._add_step(
                chat_id,
                turn,
                "intent_upgrade",
                f"{intent} 轮检测到工具调用/交付痕迹,升级为 task 重新生成",
            )
            logger.info(
                "意图升级重生成: chat=%s turn=%s intent=%s", chat_id, turn.id, intent
            )
            intent = intent_gate.INTENT_TASK
            if stream_id and not stream_broken:
                await sink.stream_set(
                    stream_id, "⏳ 检测到任务意图,正在按任务流程重新生成…"
                )
            turn.answer = ""
            outcome = await self._run_generation(
                sink,
                chat_id,
                content_effective,
                turn,
                sender=sender,
                images=None,
                ws_root=ws_root,
                outbox_before=outbox_before,
                attempt=0,
                stream=False,
                intent=intent,
                needs_file=True,
                may_retry=bool(
                    config.VERIFY_MAX_RETRIES > 0
                    and ws_root
                    and not config.WORKSPACE_READ_ONLY
                ),
            )
            if outcome.aborted:
                return
            # 重生成的 outcome 不带流卡片:终态仍覆写最初卡片,不进历史前由外层统一发送
            outcome.stream_id, outcome.stream_broken = None, False
            outcome.card_deferred = True
        attempt = 0
        retries = config.VERIFY_MAX_RETRIES
        # 补偿执行有独立预算:一次补偿拿到真实结果后模型多半还要继续
        # (dashi-ppt 官方流程需 scaffold→render→export 多次 shell),重试预算
        # 只兜"无伪调用可补偿"的空转轮
        compensations = 3
        while (
            (retries > 0 or (compensations > 0 and outcome.pseudo_invokes))
            and ws_root
            and not config.WORKSPACE_READ_ONLY
            and not _diff_outbox(outbox_before, ws_root)
            and (
                self._needs_deliverable(outcome, turn, needs_file)
                # 伪调用承诺轮:正文剥除过伪调用文本且无产物——模型"承诺了但工具
                # 没真执行"(2026-10-13 实证:下载轮清洗后只剩"我来尝试从你给的
                # 仓库地址下载。"一句承诺,回合就结束了)
                or outcome.pseudo_stripped
            )
        ):
            attempt += 1
            pseudo_only = outcome.pseudo_stripped and not self._needs_deliverable(
                outcome, turn, needs_file
            )
            feedback = _VERIFY_FEEDBACK_PSEUDO if pseudo_only else _VERIFY_FEEDBACK
            # 伪调用补偿执行(2026-10-13 起):回复阶段的 DSML 伪调用是结构化的真实
            # 意图(invoke 名与参数都对,只是没走 action 机制)——白名单真实执行后
            # 把结果随反馈回灌,而不是让模型盲重试(实证:盲重试两轮都 DSML 收场)
            compensated = False
            if outcome.pseudo_invokes and compensations > 0:
                compensations -= 1
                compensated = True
                exec_results = _execute_pseudo_invokes(ws_root, outcome.pseudo_invokes)
                if exec_results:
                    self._add_step(
                        chat_id,
                        turn,
                        "pseudo_exec",
                        "补偿执行伪调用: "
                        + "; ".join(r.split("\n", 1)[0][:80] for r in exec_results),
                    )
                    feedback += (
                        "\n\n系统已代为真实执行你写在正文里的工具调用,真实结果如下:\n"
                        + "\n\n".join(exec_results)
                        + "\n\n请基于这些真实结果继续完成任务:还需调用工具就在 Action 阶段"
                        "真实发起(不要把调用语法写进正文);全部完成后输出最终答复。"
                    )
            else:
                retries -= 1
            self._add_step(
                chat_id,
                turn,
                "verify_retry",
                (
                    "伪调用补偿执行后回灌重试"
                    if compensated
                    else (
                        "正文含伪调用文本但工具未真实执行,回灌反馈重试"
                        if pseudo_only
                        else "声明的产物未真实生成,回灌反馈重试"
                    )
                ),
            )
            logger.warning("产物验证失败,回灌重试: chat=%s turn=%s", chat_id, turn.id)
            turn.answer = ""
            outcome = await self._run_generation(
                sink,
                chat_id,
                content_effective,
                turn,
                sender=sender,
                images=None,
                ws_root=ws_root,
                outbox_before=outbox_before,
                extra_feedback=feedback,
                attempt=attempt,
                stream=False,
                may_retry=retries > 0 or compensations > 0,
            )
            if outcome.aborted:
                return
        full_text = outcome.full_text
        # 轮后 HTML 自包含化(html_inline.py):skill 沙箱产物(如 html-ppt deck)
        # 的资源引用指向容器内 bundle,容器销毁即失效;投递前把失效本地引用
        # 按宿主 skill 源内联成单文件,保证投出的 HTML 直接可打开。
        # 在 _deliver_produced 的 diff 之前执行,投出的就是内联后的版本。
        if ws_root and not config.WORKSPACE_READ_ONLY:
            try:
                for item in html_inline.inline_workspace_html(ws_root, turn_started):
                    detail = f"{item['file']}: 内联 {len(item['inlined'])} 处资源"
                    if item.get("missing"):
                        detail += (
                            f", {len(item['missing'])} 处未找到源"
                            f"({', '.join(item['missing'][:3])})"
                        )
                    if item.get("skipped"):
                        detail += f", {item['skipped']}"
                    self._add_step(chat_id, turn, "html_inline", detail)
            except Exception as e:
                logger.warning("HTML 自包含化失败(静默跳过): %s", e)
        # 产物投递(ground truth) → 系统事实脚注(交付事实的唯一权威陈述)
        delivery_results: list[bool] = []
        if ws_root:
            delivery_results = await self._deliver_produced(
                sink, chat_id, turn, ws_root, outbox_before
            )
        claim_detected = bool(_DELIVERABLE_CLAIM_RE.search(full_text))
        footer = (
            _compose_fact_footer(turn.produced_files, delivery_results, claim_detected)
            if config.FACT_FOOTER_ENABLED
            else ""
        )
        if footer:
            self._add_step(chat_id, turn, "fact_footer", footer.replace("\n", " "))
        display_text = f"{full_text}\n\n{footer}" if footer else full_text
        # 终态发送:始终以系统口径 stream_set 全量覆写卡片——流式期间内容靠逐次
        # append 上卡,任一次更新在操作台侧丢失,卡片就冻结在中间态(2026-10-13
        # 实证:21:16 轮卡片只剩首个 delta"开工",终态因无脚注/无重试未覆写,
        # 1026 字最终答复整体丢失);一次额外覆写换终态可靠。脚注只进展示,不进会话历史
        if stream_id:
            if not stream_broken:
                # 终态覆写失败等同流卡片中断,落到 reply 兜底,不能静默丢答复
                if not await sink.stream_set(stream_id, display_text):
                    stream_broken = True
            await sink.stream_end(stream_id)
            if stream_broken:
                await sink.reply(display_text)
        else:
            await sink.reply_final(display_text)
        session_store.append(chat_id, content, full_text)
        # 轮后异步自动记忆抽取(辅通道,默认关):不阻塞回复,失败静默
        if (
            config.MEMORY_ENABLED
            and kb_client.kb_enabled()
            and config.MEMORY_AUTO_EXTRACT
        ):
            asyncio.create_task(
                self._auto_extract_memory(chat_id, sender, content, full_text)
            )
        self._finalize_usage(turn)
        self._set_turn_status(chat_id, turn, session_store.STATUS_DONE)

    def _needs_deliverable(
        self,
        outcome: _GenerationOutcome,
        turn: session_store.Turn,
        needs_file: bool = False,
    ) -> bool:
        """本轮是否承诺了文件产物:rescue 提取过 / action 回路出现过 write_file/write_deliverable
        / 正文命中交付声明 / (needs_file 轮)正文出现具体文件名但全程无写盘 action。"""
        wrote = any(
            "write_file" in a or "write_deliverable" in a for a in turn.actions_seen
        )
        return bool(
            outcome.rescue_hit
            or wrote
            or _DELIVERABLE_CLAIM_RE.search(outcome.full_text or "")
            or (
                needs_file
                and not wrote
                and _FILE_NAME_RE.search(outcome.full_text or "")
            )
        )

    async def _run_generation(
        self,
        sink: ReplySink,
        chat_id: str,
        content: str,
        turn: session_store.Turn,
        *,
        sender: Optional[str],
        images: Optional[list[dict]],
        ws_root: Optional[str],
        outbox_before: dict,
        extra_feedback: Optional[str] = None,
        attempt: int = 0,
        stream: bool = True,
        may_retry: bool = False,
        intent: str = intent_gate.INTENT_TASK,
        needs_file: bool = True,
    ) -> _GenerationOutcome:
        """单次生成(可重入):能力挂载 → 流式生成 → 清洗/引用渲染。
        终态发送(stream_end/reply_final)、投递、历史落盘由外层统一负责。
        intent 非 task 时重能力(workspace 文件/web/kb_remember)不挂载;
        needs_file 为 False 时不注入交付流程 instruct(工具仍挂载,降级兜底);
        may_retry 为 True 时空回复兜底文案暂缓上卡(外层还会重试,避免假失败)。"""
        history = session_store.get_history(chat_id)
        agent_name = f"chat-{chat_id}-{turn.id}" + (f"-r{attempt}" if attempt else "")
        # input 不在这里设:走 skills 路径时要设在 execution 上,避免重复
        agent = (
            Agently.create_agent(name=agent_name)
            .set_chat_history(history)
            .instruct(config.SYSTEM_PROMPT)
        )
        # 能力装配(workspace/MCP 联网高德/kb/记忆/VL/skills)由 capability resolver
        # 消化:agent 只感知 workspace 一个对象,provider 每轮新建、无状态;
        # instruct 收集与一次性合并注入(get_result 前)在 resolver 内完成
        ctx = capability_resolver.TurnContext(
            pipeline=self,
            agent=agent,
            chat_id=chat_id,
            content=content,
            turn=turn,
            sender=sender,
            images=images,
            ws_root=ws_root,
            extra_feedback=extra_feedback,
            stream=stream,
            sink=sink,
            intent=intent,
            needs_file=needs_file,
            agent_name=agent_name,
        )
        asm = await capability_resolver.assemble(ctx)
        kb_citations = asm.kb_citations
        live = asm.live
        skill_execution = asm.skill_execution
        skill_refs = asm.skill_refs
        result = asm.result
        # 生成前查一次撤回状态:消息已被撤回则直接中止,不烧生成
        if await sink.recalled():
            await self._abort_recalled(sink, chat_id, turn, None)
            return _GenerationOutcome(aborted=True)
        stream_id: Optional[str] = await sink.stream_begin() if stream else None
        if live is not None and stream_id:
            live["stream_id"] = stream_id
        stream_broken = False
        answer_started = False
        # 准备阶段 step 补推上卡:intent/workspace/mcp/kb/skills 等均在建卡前发生,
        # 飞书侧原本无感知;卡片建立后一次性补推(模型重试清卡时同正文一起被清,可接受)
        if stream_id:
            setup_lines = [
                f"{_SETUP_STEP_ICONS[st['kind']]} {_redact_host_paths(st['detail'])}"
                for st in turn.steps
                if st["kind"] in _SETUP_STEP_ICONS and st.get("detail")
            ]
            if setup_lines and not await sink.stream_append(
                stream_id, "\n".join(setup_lines) + "\n"
            ):
                stream_broken = True

        last_push = {"reasoning": 0.0, "answer": 0.0}

        def push_progress(field: str):
            now = time.monotonic()
            if now - last_push[field] >= PROGRESS_PUSH_INTERVAL:
                last_push[field] = now
                events.publish(
                    {
                        "type": "turn_progress",
                        "chat_id": chat_id,
                        "turn_id": turn.id,
                        "field": field,
                        "text": getattr(turn, field),
                    }
                )

        parts: list[str] = []
        recalled = False
        last_recall_check = 0.0  # 首个事件即检查一次
        agen = result.get_async_generator(
            type="specific", specific=["reasoning_delta", "delta", "status"]
        )
        try:
            async for event, data in agen:
                now = time.monotonic()
                if now - last_recall_check >= RECALL_CHECK_INTERVAL:
                    last_recall_check = now
                    if await sink.recalled():
                        recalled = True
                        break
                if event == "status":
                    # 模型重试会重放全文:清空已收内容和卡片,重新累积
                    if isinstance(data, dict) and data.get("retry"):
                        parts.clear()
                        turn.answer = ""
                        answer_started = False
                        self._add_step(
                            chat_id, turn, "retry", "模型重试,已丢弃本次部分输出"
                        )
                        if stream_id and not stream_broken:
                            if not await sink.stream_set(stream_id, ""):
                                stream_broken = True
                    continue
                if event == "delta":
                    parts.append(data)
                    turn.answer += data
                    push_progress("answer")
                    if stream_id and not stream_broken and data:
                        if not answer_started:
                            # 正文开始:清掉占位显示的推理内容
                            answer_started = True
                            ok = await sink.stream_set(stream_id, data)
                        else:
                            ok = await sink.stream_append(stream_id, data)
                        if not ok:
                            stream_broken = True
                            self._add_step(
                                chat_id,
                                turn,
                                "stream_broken",
                                "流卡片中断,改为收集全文后兜底回复",
                            )
                            logger.warning("流会话中断,改为收集全文后兜底回复")
                elif event == "reasoning_delta":
                    turn.reasoning += data
                    push_progress("reasoning")
                    # 正文开始前:推理内容先上卡片占位;正文一旦开始就丢弃后续推理
                    if not answer_started and stream_id and not stream_broken and data:
                        if not await sink.stream_append(stream_id, data):
                            stream_broken = True
                            self._add_step(
                                chat_id,
                                turn,
                                "stream_broken",
                                "流卡片中断,改为收集全文后兜底回复",
                            )
                            logger.warning("流会话中断,改为收集全文后兜底回复")
        except Exception as e:
            logger.exception("LLM 生成失败")
            err = f"⚠️ 生成出错: {e}"
            self._add_step(chat_id, turn, "error", str(e))
            self._finalize_usage(turn)
            self._set_turn_status(
                chat_id, turn, session_store.STATUS_FAILED, error=str(e)
            )
            if stream_id:
                if not stream_broken:
                    await sink.stream_append(stream_id, err)
                await sink.stream_end(stream_id)
                if stream_broken:
                    await sink.reply(err)
            else:
                await sink.reply(err)
            return _GenerationOutcome(aborted=True)
        finally:
            _active_action_turns.pop(agent_name, None)

        if recalled or await sink.recalled():
            # 生成中/刚结束时发现撤回:关闭生成器取消底层请求,中止本轮
            await agen.aclose()
            await self._abort_recalled(sink, chat_id, turn, stream_id)
            return _GenerationOutcome(aborted=True)

        if skill_execution is not None:
            sel = (getattr(skill_execution, "diagnostics", None) or {}).get(
                "skill_selection"
            ) or {}
            chosen = sel.get("revision_refs") or []
            detail = f"候选 {len(skill_refs)} 个,选中 {len(chosen)} 个"
            if chosen:
                names = [str(r).split("@")[0].removeprefix("skill:") for r in chosen]
                detail += ": " + ", ".join(names)
            self._add_step(chat_id, turn, "skills", detail)

        raw_text = "".join(parts).strip() or "(空回复)"
        # 伪调用清洗对所有轮生效(无 workspace 的轮也可能幻觉 kb_remember 等伪调用,
        # 2026-10-08 评测实测);仅读写 workspace 且本轮无真实产物时才提取内联内容落盘
        full_text, rescue_hit, pseudo_stripped, pseudo_invokes = (
            self._rescue_inline_file(chat_id, turn, ws_root, outbox_before, raw_text)
        )
        # 空回复兜底(seq 14 实证:正文全是伪调用文本,清洗剥光后空卡片直接发给
        # 用户,出 0 token 且无任何说明)。分两种情形:
        # ① 已有真实产物(outbox diff 非空):文件已落盘,正文空只是伪调用残留被剥光,
        #    兜底文案必须如实告知交付事实,不能说"未能生成有效回复"(实测矛盾:
        #    附件已发送 + 正文却让用户重试);
        # ② 无任何产物:才是真正的空轮,给重试引导。
        deferred = False
        if not full_text.strip():
            produced_now = _diff_outbox(outbox_before, ws_root) if ws_root else []
            if produced_now:
                names = "、".join(f["name"] for f in produced_now[:5])
                full_text = f"✅ 已生成文件:{names}(见附件)。"
                self._add_step(
                    chat_id,
                    turn,
                    "empty_reply",
                    "清洗后正文为空但已有真实产物,兜底文案改为交付告知",
                )
            else:
                # ②b 有工具调用且未达成:如实汇总尝试与失败,不给"换个说法重试"
                # 的空话(2026-10-12 实证:8 次抓取全败——github.com/codeload 网络
                # 不可达,用户需要的结论是"请手动 git clone",不是"重试")
                failed = [
                    st["detail"]
                    for st in turn.steps
                    if st["kind"] == "action"
                    and ("→ 失败" in st["detail"] or "→ 未达成" in st["detail"])
                ]
                if failed:
                    full_text = (
                        f"⚠️ 本轮进行了 {len(turn.actions_seen)} 次工具调用,均未能完成目标。"
                        f"最近一次:{failed[-1][:120]}。"
                        "如属网络/权限类限制,重试大概率同样失败——"
                        "建议手动获取后把文件发给我,或告诉我换个方式。"
                    )
                    self._add_step(
                        chat_id,
                        turn,
                        "empty_reply",
                        "清洗后正文为空,兜底文案改为失败汇总",
                    )
                else:
                    full_text = (
                        "⚠️ 本轮未能生成有效回复(正文为空),请让我重试或换个说法。"
                    )
                    self._add_step(
                        chat_id,
                        turn,
                        "empty_reply",
                        "清洗后正文为空,已用系统兜底文案替换",
                    )
            # 还会进产物验证重试环时,兜底文案不立即上卡——否则重试期间卡片长时间
            # 显示"本轮未能生成有效回复"的假失败(2026-10-13 实证:下载轮 attempt0
            # 伪调用剥空→兜底文案上卡,attempt1 正常生成 67s 期间用户看到的就是
            # 失败文案)。先上占位提示,终态由外层统一覆写。
            deferred = may_retry
        # 已流式上卡的内容用 stream_set 全量覆写为清洗后文本
        if full_text != raw_text and stream_id and not stream_broken:
            if deferred:
                await sink.stream_set(stream_id, "⏳ 回复校验未通过,正在重新生成…")
            else:
                await sink.stream_set(stream_id, full_text)
        # 知识库引用渲染:[[ref:rN]] → 〔来源: 标题〕;已上卡内容覆写为解析后文本
        if kb_citations:
            rendered = kb_client.render_citations(full_text, kb_citations)
            if rendered != full_text:
                full_text = rendered
                if stream_id and not stream_broken:
                    await sink.stream_set(stream_id, full_text)
        turn.answer = full_text
        # 终态发送(stream_end/reply_final)、投递、落历史由外层统一负责(可验证重试)
        return _GenerationOutcome(
            full_text=full_text,
            rescue_hit=rescue_hit,
            pseudo_stripped=pseudo_stripped,
            pseudo_invokes=pseudo_invokes,
            stream_id=stream_id,
            stream_broken=stream_broken,
            card_deferred=deferred,
        )

    async def _auto_extract_memory(
        self, chat_id: str, sender: Optional[str], user_text: str, answer: str
    ):
        """轮后异步记忆抽取(辅通道,默认关):结构化抽取 + 置信度门槛,全程 fail-open。"""
        try:

            def _extract():
                return (
                    Agently.create_agent(
                        name=f"memory-extract-{chat_id}-{int(time.time())}"
                    )
                    .input(f"用户: {user_text}\n助手: {answer}")
                    .instruct(MEMORY_EXTRACT_INSTRUCT)
                    .output(MEMORY_OUTPUT_SCHEMA)
                    .get_result()
                )

            result = await asyncio.to_thread(_extract)
            data = result.get_data()
            memories = data.get("memories") if isinstance(data, dict) else None
            if not isinstance(memories, list):
                return
            scope = kb_client.memory_write_scope(chat_id, sender)
            for m in memories:
                summary = str(m.get("summary") or "").strip()
                try:
                    confidence = float(m.get("confidence") or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
                try:
                    ttl_days = int(m.get("ttl_days") or 0)
                except (TypeError, ValueError):
                    ttl_days = 0
                ttl_days = max(0, min(ttl_days, 30))
                # 模型漏标时的文本特征兜底:过程状态类必须有 DDL
                if ttl_days == 0 and _MEMORY_PROCESS_STATE_RE.search(summary):
                    ttl_days = config.MEMORY_PROCESS_TTL_DAYS
                if summary and confidence >= config.MEMORY_MIN_CONFIDENCE:
                    await kb_client.write_memory(
                        summary, scope, source="auto", ttl_days=ttl_days
                    )
        except Exception as e:
            logger.warning("自动记忆抽取失败(静默跳过): %s", e)

    async def _abort_recalled(
        self,
        sink: ReplySink,
        chat_id: str,
        turn: session_store.Turn,
        stream_id: Optional[str],
    ):
        """用户已撤回源消息:尽力收尾卡片,turn 落失败;不写历史、不投递产物。"""
        self._add_step(chat_id, turn, "recalled", "用户已撤回消息,本轮中止")
        logger.info("源消息已撤回,本轮中止: chat=%s turn=%s", chat_id, turn.id)
        if stream_id:
            await sink.stream_end(stream_id)  # 卡片接口大概率同样失败,仅尽力收尾
        self._finalize_usage(turn)
        self._set_turn_status(
            chat_id, turn, session_store.STATUS_FAILED, error="用户已撤回消息,本轮中止"
        )

    def _rescue_inline_file(
        self,
        chat_id: str,
        turn: session_store.Turn,
        ws_root: Optional[str],
        outbox_before: dict,
        answer: str,
    ) -> tuple[str, bool, bool, list]:
        """正文内联文件兜底,返回 (清洗后正文, 是否提取落盘, 是否剥除过伪调用文本,
        解析出的 DSML 伪调用列表)。

        清洗(伪调用文本剥除)对所有轮生效——只读/无 workspace 的轮也可能幻觉
        伪调用文本(2026-10-07 飞书事故实证,只读早退把清洗也跳过是历史 bug);
        提取落盘仅读写 workspace 且本轮尚无真实产物时进行,避免重复交付。
        第三返回值供验证重试环识别"承诺了但工具没真执行"的轮次;
        第四返回值供验证重试环做伪调用补偿执行(2026-10-13 起)。
        """
        can_write = bool(ws_root) and not config.WORKSPACE_READ_ONLY
        extracted = None
        if can_write and not _diff_outbox(outbox_before, ws_root):
            extracted = _extract_inline_file(answer)
        if extracted is None:
            cleaned = _clean_pseudo_calls(answer)
            if cleaned != answer:
                self._add_step(
                    chat_id,
                    turn,
                    "delivery_rescue",
                    f"正文残留伪工具调用文本已清洗(约{len(answer) - len(cleaned)}字符)",
                )
                # 记录剥除位置与片段,便于诊断提取为何未命中(内容过短/格式新变体)
                prefix = 0
                for left, right in zip(answer, cleaned):
                    if left != right:
                        break
                    prefix += 1
                logger.info(
                    "伪调用清洗但未提取落盘,正文自 %d 处剥除: %.200s",
                    prefix,
                    answer[prefix:],
                )
            invokes = _parse_dsml_invokes(answer) if cleaned != answer else []
            return cleaned, False, cleaned != answer, invokes
        name, file_content, cleaned = extracted
        outbox = Path(ws_root) / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        saved_as = inbox._dedupe(outbox, name)
        (outbox / saved_as).write_text(file_content, encoding="utf-8")
        self._add_step(
            chat_id,
            turn,
            "delivery_rescue",
            f"正文内联文件已提取为 outbox/{saved_as}({len(file_content)} 字符)",
        )
        note = f"已将文件生成为 outbox/{saved_as},稍后会作为附件发送。"
        return (f"{cleaned}\n\n{note}" if cleaned else note), True, True, []

    async def _deliver_produced(
        self,
        sink: ReplySink,
        chat_id: str,
        turn: session_store.Turn,
        ws_root: str,
        outbox_before: dict,
    ) -> list[bool]:
        """产物投递:outbox diff → 收拢(站点目录/附件护栏)→ send_file 逐发;
        失败/超限降级为文本路径提示。返回逐文件投递结果(事实脚注的 ground truth)。"""
        produced = _diff_outbox(outbox_before, ws_root)
        if not produced:
            return []
        produced, notes = _consolidate_delivery(produced, ws_root, turn.id)
        for note in notes:
            self._add_step(chat_id, turn, "delivery_consolidate", note)
        turn.produced_files = produced
        results: list[bool] = []
        for f in produced:
            rel = f["path"]
            if f["size"] > config.FILE_DELIVERY_MAX_BYTES:
                self._add_step(
                    chat_id,
                    turn,
                    "delivery",
                    f"{rel} 超过投递上限,未发送",
                )
                results.append(False)
                await sink.reply(
                    f"文件已生成于工作区 {rel}({f['size'] / 1024 / 1024:.1f} MB),"
                    "超过投递上限,未发送。"
                )
                continue
            ok = await sink.send_file(str(Path(ws_root) / rel), f["name"])
            results.append(ok)
            self._add_step(
                chat_id,
                turn,
                "delivery",
                f"{rel} ({f['size'] / 1024:.1f} KB) 投递{'成功' if ok else '失败'}",
            )
            if not ok:
                await sink.reply(f"文件已生成于工作区 {rel},但发送失败。")
        return results
