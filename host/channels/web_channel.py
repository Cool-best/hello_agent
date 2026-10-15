import logging
from typing import Optional

from .channel_base import ReplySink

logger = logging.getLogger(__name__)


class WebSink(ReplySink):
    """web 渠道:输出经 SSE turn_progress 事件实时展示,回复仅记日志。"""

    async def send_file(self, file_path: str, display_name: str) -> bool:
        # web 的交付 = turn.produced_files + dashboard 下载链接,无需主动推,视为成功
        return True

    async def reply(self, text: str) -> bool:
        logger.info("[web] 回复: %s", text)
        return True

    async def reply_final(self, text: str) -> None:
        logger.info("[web] 最终回复: %s", text)

    async def stream_begin(self) -> Optional[str]:
        return None

    async def stream_append(self, stream_id: str, delta: str) -> bool:
        return False

    async def stream_set(self, stream_id: str, content: str) -> bool:
        return False

    async def stream_end(self, stream_id: str) -> bool:
        return False
