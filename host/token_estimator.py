def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数:中文等 CJK 字符约 1 token/字,其余约 4 字符/token。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯":
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def estimate_messages(messages: list[dict]) -> int:
    """估算一组 chat messages 的 token 数,含每条消息的格式开销(~4 token)。"""
    total = 0
    for msg in messages:
        total += 4 + estimate_tokens(str(msg.get("role") or "")) + estimate_tokens(
            str(msg.get("content") or "")
        )
    return total
