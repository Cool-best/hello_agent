# -*- coding: utf-8 -*-
"""一次性迁移：清洗 sessions.json 存量历史里的伪工具调用文本。

背景：弱工具调用模型曾把 DSML / <tool_use> / <tool_result> 伪调用写进正文，
当时的清洗代码未覆盖全部形态，污染持久化进了 history，持续诱导后续轮次
模仿"文本假装调用工具"。chat_pipeline._clean_pseudo_calls 只清洗新轮次，
本脚本对存量历史做同样的清洗（正则与 chat_pipeline.py:269-332 保持一致，
仅作用于 role == "assistant" 的条目；turns 是审计记录，不喂模型，不动）。

用法：
    python clean_history_pseudo_calls.py            # 实际执行（先备份再原子写回）
    python clean_history_pseudo_calls.py --dry-run  # 只报告会改什么，不写盘

注意：执行前必须停掉 agent 的 main.py 进程，否则其内存中的会话会在
下一次 save() 时把清洗结果覆盖掉。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STORE_FILE = Path(__file__).parent / "sessions.json"

# ── 以下正则与函数复制自 chat_pipeline.py:269-332，保持同一清洗口径 ──

_DSML_MARKER_RE = re.compile(r"<[｜|/][^<>\n]{0,12}DSML")
_DSML_BARE_PARAM_RE = re.compile(r'<parameter name="path"')
_TOOL_USE_BLOCK_RE = re.compile(r"<tool_use>.*?</tool_use>", re.DOTALL)
_TOOL_RESULT_BLOCK_RE = re.compile(r"<tool_result>.*?</tool_result>", re.DOTALL)
_TOOL_RESULT_LINE_RE = re.compile(r"(?m)^\s*<?tool_result>.*$")


def _strip_dsml_tail(answer: str) -> str:
    starts = []
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
    if "<tool_use>" not in answer:
        return answer
    cleaned = _TOOL_USE_BLOCK_RE.sub("", answer)
    dangling = cleaned.find("<tool_use>")
    if dangling >= 0:
        cleaned = cleaned[:dangling]
    return cleaned.strip()


def _remove_tool_result_blocks(answer: str) -> str:
    if "tool_result>" not in answer:
        return answer
    cleaned = _TOOL_RESULT_BLOCK_RE.sub("", answer)
    cleaned = _TOOL_RESULT_LINE_RE.sub("", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def clean_pseudo_calls(answer: str) -> str:
    return _strip_dsml_tail(
        _remove_tool_result_blocks(_remove_tool_use_blocks(answer))
    ).strip()


def looks_poisoned(text: str) -> bool:
    return (
        "tool_result>" in text
        or "<tool_use>" in text
        or "DSML" in text
    )


def main():
    dry_run = "--dry-run" in sys.argv
    payload = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    sessions = payload.get("sessions") or []

    changed = []  # (chat_id, index, before_len, after_len)
    emptied = []  # (chat_id, index) 清洗后正文为空的条目
    for s in sessions:
        chat_id = s.get("chat_id") or ""
        history = s.get("history") or []
        for i, entry in enumerate(history):
            if entry.get("role") != "assistant":
                continue
            content = entry.get("content") or ""
            if not looks_poisoned(content):
                continue
            cleaned = clean_pseudo_calls(content)
            if cleaned == content:
                continue
            changed.append((chat_id, i, len(content), len(cleaned)))
            if not cleaned:
                emptied.append((chat_id, i))
            entry["content"] = cleaned

    print(f"{'[DRY-RUN] ' if dry_run else ''}扫描 {len(sessions)} 个会话")
    if not changed:
        print("没有发现需要清洗的历史条目。")
        return
    for chat_id, i, before, after in changed:
        print(f"  {chat_id}  history[{i}]: {before} -> {after} 字符")
    for chat_id, i in emptied:
        print(f"  !! {chat_id}  history[{i}] 清洗后为空（保留占位，未删除）")

    if dry_run:
        print("[DRY-RUN] 未写盘。去掉 --dry-run 执行实际清洗。")
        return

    backup = STORE_FILE.with_name(
        f"{STORE_FILE.stem}.bak_{time.strftime('%Y%m%d_%H%M%S')}{STORE_FILE.suffix}"
    )
    backup.write_text(STORE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"已备份原文件: {backup.name}")

    tmp = STORE_FILE.with_suffix(STORE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STORE_FILE)
    print(f"已写回 {STORE_FILE.name}，清洗 {len(changed)} 条。")


if __name__ == "__main__":
    main()
