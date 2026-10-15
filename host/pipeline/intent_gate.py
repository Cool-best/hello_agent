"""意图分类门:每轮生成前的能力门禁(生成可靠性三层防护的 ① 层)。

弱模型劝不住就不给能力:chitchat/ambiguous 轮不挂载 workspace 文件 /
web / kb_remember 等重能力,机制上杜绝调侃、模糊短消息触发多步骤长任务。
分类失败一律 fail-open 为 task(维持现状行为,诚实性由验证重试环 +
事实合成层兜底,不因分类器故障丢能力)。

needs_file 二级信号:用户是否要求生成/交付可下载产物。outbox 交付流程
instruct 按此条件化注入——闲聊/纯问答轮不再携带长段 write_file 流程模板,
消除模板本身诱导模型伪调用写正文的风险(2026-10-09 事故归因);
工具(write_file/write_deliverable)不做条件化,分类器误判时仍可用。

请求理解与指代改写(2026-10-15 起):分类器每轮先结合短期记忆(最近一轮
对话,助手答复给足预算)理解用户消息——含指代/省略时补全为自包含请求
(rewritten),再基于理解后的请求判断意图;消息本身自包含时 rewritten 为空。
不做关键词硬触发:是否含指代由分类器理解判断,改写与分类是同一个 LLM 请求。
改写结果由 IntentResult.rewritten 带出,后续流程(记忆召回/skill 匹配/
模型输入)统一消费,不再依赖弱模型从原始历史里自行推断"刚刚内容"是什么
(实证事故:新闻答复后说"把刚刚内容做成ppt",模型无视历史里的新闻答复,
被过期记忆+inbox 诱饵文件锚定到旧雅思文件)。
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from .. import config
from agently import Agently

logger = logging.getLogger(__name__)

INTENT_TASK = "task"
INTENT_CHITCHAT = "chitchat"
INTENT_AMBIGUOUS = "ambiguous"

_VALID = {INTENT_TASK, INTENT_CHITCHAT, INTENT_AMBIGUOUS}

_INTENT_INSTRUCT = (
    "你是用户请求理解与意图分类器。按两步工作:\n"
    "第一步,结合对话上下文理解用户新消息:若消息含指代(如「刚刚」「上面」"
    "「这些」)或明显省略,把指代/省略补全为不依赖上下文也能独立执行的完整请求,"
    "填入 rewritten(把指代替换为上下文中对应内容的要点概括,一两句话,含关键实体);"
    "若消息本身已自包含、无需补全,rewritten 输出空字符串。"
    "改写只补全指代,不添加用户没要求的新内容。\n"
    "第二步,基于理解后的请求(rewritten 非空时以它为准)判断意图,只分三类:\n"
    "- task: 明确的任务请求(提问、生成文件、查资料、翻译、总结、要求记住等)\n"
    "- chitchat: 闲聊、调侃、问候、语气词、情绪表达(如「笨蛋」「哈哈」「在吗」)\n"
    "- ambiguous: 可能指向某个任务但表述不明确(如「再弄一下」「然后呢」),"
    "或对上一轮结果表达不满、可能需要重做\n"
    "指代/追问类消息(如「没有找到这个环境么?」「那个文件呢」「不对」)在上文有任务"
    "正在进行时,是对该任务的延续,判 task,不要因为单条消息短或带问号就判 ambiguous。\n"
    "同时判断 needs_file:用户是否要求生成或交付可下载的产物"
    "(文档、PPT、网页、表格、图片、代码文件等)。仅闲聊、纯知识问答、"
    "只在对话里要一段文字回答的,needs_file 为否。\n"
    "只输出结果。"
)

_OUTPUT_SCHEMA = {
    "rewritten": "str, 补全指代后的完整请求;消息本身自包含时输出空字符串",
    "intent": "str, task | chitchat | ambiguous",
    "needs_file": "bool, 用户是否要求生成/交付可下载产物(文档、PPT、表格、网页等)",
}

# 上下文预算(字符):助手答复是指代对象的主要载体,给足;用户消息只需唤起场景
_ASSISTANT_CONTEXT_MAX = 1200
_USER_CONTEXT_MAX = 200

# fail-open 值 IntentResult():(task, needs_file=True, rewritten=""),维持现状行为,
# 分类器故障不丢能力、不丢交付流程引导


@dataclass
class IntentResult:
    intent: str = INTENT_TASK
    needs_file: bool = True
    rewritten: str = ""  # 补全指代后的自包含请求;空串=消息本身自包含


async def classify(content: str, history: list | None = None) -> IntentResult:
    """对本轮用户输入理解+分类;超时/异常/输出非法一律 fail-open。

    history 为会话历史(OpenAI 风格 role/content),作短期记忆:取最近一条
    助手答复(截 _ASSISTANT_CONTEXT_MAX 字)与最近一条用户消息(截
    _USER_CONTEXT_MAX 字)供理解与指代补全;单看单条消息时指代/追问类消息
    极易误判 ambiguous(2026-10-13 实测:文件任务中途的追问被判 ambiguous,
    重能力卸载后模型只能写伪调用)。
    rewritten 仅在真正改写了原文时被采信,否则视为空,调用方回退原始输入。"""
    text = content.strip()
    if not text:
        return IntentResult()
    prompt = text
    has_context = False
    if history:
        lines = []
        # 最近一条助手答复给足预算(指代对象候选),其余消息截断唤起场景
        assistant_seen = False
        for m in reversed(history):
            role = m.get("role")
            body = str(m.get("content") or "").strip()
            if not body:
                continue
            if role == "assistant" and not assistant_seen:
                lines.append(f"助手: {body[:_ASSISTANT_CONTEXT_MAX]}")
                assistant_seen = True
            else:
                label = "用户" if role == "user" else "助手"
                lines.append(f"{label}: {body[:_USER_CONTEXT_MAX]}")
            if len(lines) >= 3:
                break
        lines.reverse()
        if lines:
            has_context = True
            prompt = (
                "对话上下文(供理解,处理对象是最后的用户新消息):\n"
                + "\n".join(lines)
                + f"\n\n用户新消息: {text}"
            )

    def _classify():
        return (
            Agently.create_agent(name=f"intent-{int(time.time() * 1000)}")
            .input(prompt)
            .instruct(_INTENT_INSTRUCT)
            .output(_OUTPUT_SCHEMA)
            .get_result()
        )

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_classify), timeout=config.INTENT_GATE_TIMEOUT
        )
        data = result.get_data()
        if isinstance(data, dict):
            intent = str(data.get("intent") or "").strip().lower()
            needs_file = data.get("needs_file")
            needs_file = needs_file if isinstance(needs_file, bool) else True
            candidate = str(data.get("rewritten") or "").strip()
            # 改写必须真正改变了文本才采信;原样照抄/空串视为无需改写。
            # 无上下文时强制不采信:改写只能来自指代补全,没有上下文可补时模型
            # 仍在"改写"属于臆造(实测:无历史时对「把这些整理一下」凭空改写)
            rewritten = (
                candidate if has_context and candidate and candidate != text else ""
            )
            if intent in _VALID:
                return IntentResult(intent, needs_file, rewritten)
            logger.info("意图分类输出非法(%r),按 task 处理", intent)
            return IntentResult(rewritten=rewritten)
    except Exception as e:
        logger.warning("意图分类失败,按 task 处理: %s", e)
    return IntentResult()
