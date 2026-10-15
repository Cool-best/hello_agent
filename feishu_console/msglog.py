import json
import os
import threading
from datetime import datetime


class MessageLogger:
    """收发消息落盘：每次启动在 logs/ 下按时间戳新建一个日志文件，一行一条。"""

    def __init__(self, log_dir: str | None = None):
        if log_dir is None:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.abspath(os.path.join(log_dir, f"feishu_{ts}.log"))
        self._lock = threading.Lock()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"# 会话开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    @staticmethod
    def _one_line(text) -> str:
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        return str(text).replace("\r", " ").replace("\n", " ⏎ ")

    def log(self, direction: str, action: str, block: str | None = None, **fields):
        parts = " | ".join(
            f"{k}={self._one_line(v)}" for k, v in fields.items() if v is not None
        )
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{direction}] {action}"
        if parts:
            line += f" | {parts}"
        if block:
            line += "\n" + block
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
