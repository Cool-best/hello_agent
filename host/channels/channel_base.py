from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class InboundMessage:
    chat_id: str
    text: str
    source: str  # "feishu" | "web" | ...
    msg_type: str = "text"
    ref: Optional[int] = None  # 渠道侧序号(飞书 seq),仅展示/日志用
    parent_id: Optional[str] = None  # 被回复消息的渠道侧 id(飞书 message_id)
    message_id: Optional[str] = None  # 本消息的渠道侧 id(飞书 message_id),用于撤回轮询
    sender: Optional[str] = None  # 发送者渠道侧 id(飞书 sender_open_id),长期记忆 user scope 用
    attachment: Optional[dict] = None  # 已落盘附件:{name, saved_as, path, size, mime}
    images: Optional[list[dict]] = None  # 本轮待 VL 处理的已落盘图片:[{path, name, mime}]


class ReplySink(ABC):
    """渠道回复出口:agent 核心只面向此接口,不感知渠道细节。"""

    @abstractmethod
    async def reply(self, text: str) -> bool:
        """普通/兜底回复。"""

    @abstractmethod
    async def reply_final(self, text: str) -> None:
        """一轮结束时以最佳方式回复全文。"""

    @abstractmethod
    async def stream_begin(self) -> Optional[str]:
        """开启流式输出,返回流 id;不支持流式的渠道返回 None。"""

    @abstractmethod
    async def stream_append(self, stream_id: str, delta: str) -> bool:
        """向流追加增量内容,返回是否成功。"""

    @abstractmethod
    async def stream_set(self, stream_id: str, content: str) -> bool:
        """整体覆盖流内容,返回是否成功。"""

    @abstractmethod
    async def stream_end(self, stream_id: str) -> bool:
        """结束流,返回是否成功。"""

    async def send_file(self, file_path: str, display_name: str) -> bool:
        """投递产物文件;渠道不支持时默认记日志返回 False(零影响)。"""
        return False

    async def recalled(self) -> bool:
        """本轮的源消息是否已被用户撤回;不支持的渠道恒 False(零开销)。"""
        return False
