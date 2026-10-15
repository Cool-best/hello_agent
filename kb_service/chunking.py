"""分块: 段落/标题边界优先, 贪心合并到目标长度, 尾部重叠。

策略 v1 从简, 不引入语义分块(方案文档「风险与取舍」)。
独立成模块避免 store ↔ ingest 循环导入。
"""

import re


def chunk_text(text: str, target: int, overlap: int) -> list[str]:
    """段落边界贪心分块: 每块 ≤ target(软限制), 块间重叠 overlap 字符。

    单段超过 target 时按字符硬切(同样带重叠), 保证任意输入都有产出。
    """
    text = text.strip()
    if not text:
        return []
    paragraphs = [
        p.strip()
        for p in re.split(r"\n\s*\n|(?=^#{1,6}\s)", text, flags=re.MULTILINE)
        if p.strip()
    ]
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= target:
            pieces.append(para)
        else:
            step = max(target - overlap, 1)
            for start in range(0, len(para), step):
                pieces.append(para[start : start + target])
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if current and len(candidate) > target:
            chunks.append(current)
            tail = current[-overlap:] if overlap > 0 else ""
            current = f"{tail}\n\n{piece}" if tail else piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
