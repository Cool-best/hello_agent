import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)

TURN_MAX_PER_SESSION = 50

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# 重启打断轮次的标记文案(from_dict 落标;consume_interrupted_turns 据此识别未告知的)
INTERRUPTED_ERROR = "服务重启,本轮被中断"


@dataclass
class Turn:
    id: str
    seq: Optional[int]
    source: str  # "feishu" | "web"
    input: str
    status: str = STATUS_PENDING
    reasoning: str = ""
    answer: str = ""
    steps: list[dict] = field(default_factory=list)
    produced_files: list[dict] = field(default_factory=list)  # 本轮产物:[{path, name, size}]
    actions_seen: list[str] = field(default_factory=list)  # 本轮 action 回路出现过的调用名(瞬态,不落盘)
    usage: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "source": self.source,
            "input": self.input,
            "status": self.status,
            "reasoning": self.reasoning,
            "answer": self.answer,
            "steps": list(self.steps),
            "produced_files": list(self.produced_files),
            "usage": self.usage,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def add_step(self, kind: str, detail: str = "") -> dict:
        step = {"ts": time.time(), "kind": kind, "detail": detail}
        self.steps.append(step)
        return step

    @classmethod
    def from_dict(cls, data: dict) -> "Turn":
        turn = cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            seq=data.get("seq"),
            source=str(data.get("source") or ""),
            input=str(data.get("input") or ""),
        )
        turn.status = str(data.get("status") or STATUS_FAILED)
        turn.reasoning = str(data.get("reasoning") or "")
        turn.answer = str(data.get("answer") or "")
        turn.steps = list(data.get("steps") or [])
        turn.produced_files = list(data.get("produced_files") or [])
        turn.usage = data.get("usage")
        turn.error = data.get("error")
        turn.created_at = float(data.get("created_at") or time.time())
        turn.started_at = data.get("started_at")
        turn.finished_at = data.get("finished_at")
        return turn


@dataclass
class Session:
    chat_id: str
    created_at: float
    updated_at: float
    history: deque
    turns: deque

    @property
    def status(self) -> str:
        turns = list(self.turns)
        if any(t.status == STATUS_PROCESSING for t in turns):
            return STATUS_PROCESSING
        if any(t.status == STATUS_PENDING for t in turns):
            return STATUS_PENDING
        if turns:
            return turns[-1].status
        return "idle"

    def summary(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": len(self.turns),
            "message_count": len(self.history),
        }

    def to_dict(self) -> dict:
        data = self.summary()
        data["history"] = list(self.history)
        data["turns"] = [t.to_dict() for t in self.turns]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        session = cls(
            chat_id=str(data.get("chat_id") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            history=deque(maxlen=config.HISTORY_MAX_MESSAGES),
            turns=deque(maxlen=TURN_MAX_PER_SESSION),
        )
        session.history.extend(data.get("history") or [])
        for turn_data in data.get("turns") or []:
            turn = Turn.from_dict(turn_data)
            # 重启时仍处于进行中状态的轮次是被打断的,落为失败并注明
            if turn.status in (STATUS_PENDING, STATUS_PROCESSING):
                turn.status = STATUS_FAILED
                turn.error = turn.error or INTERRUPTED_ERROR
                turn.finished_at = turn.finished_at or time.time()
            session.turns.append(turn)
        return session


_sessions: dict[str, Session] = {}
_locks: dict[str, asyncio.Lock] = {}


def _get_or_create(chat_id: str) -> Session:
    session = _sessions.get(chat_id)
    if session is None:
        now = time.time()
        session = Session(
            chat_id=chat_id,
            created_at=now,
            updated_at=now,
            history=deque(maxlen=config.HISTORY_MAX_MESSAGES),
            turns=deque(maxlen=TURN_MAX_PER_SESSION),
        )
        _sessions[chat_id] = session
    return session


def get_history(chat_id: str) -> list[dict]:
    session = _sessions.get(chat_id)
    return list(session.history) if session else []


def append(chat_id: str, user_text: str, assistant_text: str):
    session = _get_or_create(chat_id)
    session.history.append({"role": "user", "content": user_text})
    session.history.append({"role": "assistant", "content": assistant_text})
    session.updated_at = time.time()


def clear(chat_id: str) -> bool:
    removed = _sessions.pop(chat_id, None) is not None
    if removed:
        save()
    return removed


def list_sessions() -> dict[str, int]:
    return {chat_id: len(s.history) for chat_id, s in _sessions.items()}


def get_lock(chat_id: str) -> asyncio.Lock:
    lock = _locks.get(chat_id)
    if lock is None:
        lock = _locks[chat_id] = asyncio.Lock()
    return lock


def create_turn(chat_id: str, seq: Optional[int], source: str, user_input: str) -> Turn:
    session = _get_or_create(chat_id)
    turn = Turn(id=uuid.uuid4().hex[:12], seq=seq, source=source, input=user_input)
    session.turns.append(turn)
    session.updated_at = time.time()
    # 创建即落盘:进程在生成中途被杀时,重启后仍能恢复出这条被打断的轮次
    # (此前只在终态落盘,进行中的轮次随进程消失,会话里完全无迹)
    save()
    return turn


def get_turn(chat_id: str, turn_id: str) -> Optional[Turn]:
    session = _sessions.get(chat_id)
    if not session:
        return None
    for turn in session.turns:
        if turn.id == turn_id:
            return turn
    return None


def touch(chat_id: str):
    session = _sessions.get(chat_id)
    if session:
        session.updated_at = time.time()


def get_summary(chat_id: str) -> Optional[dict]:
    session = _sessions.get(chat_id)
    return session.summary() if session else None


def snapshot() -> dict:
    sessions = sorted(
        (s.to_dict() for s in _sessions.values()),
        key=lambda s: s["updated_at"],
        reverse=True,
    )
    return {"sessions": sessions, "ts": time.time()}


# ── 落盘持久化 ──────────────────────────────────────────
# 全量快照写 SESSION_STORE_FILE(原子替换);每个轮次终态时由
# chat_pipeline 触发 save(),启动时 main.py 调 load() 恢复,
# 重启后仪表盘仍能看到之前的会话记录。


def save() -> None:
    """把全部会话原子写入 SESSION_STORE_FILE;失败只记日志,不影响对话。"""
    path = Path(config.SESSION_STORE_FILE)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        payload = {"sessions": [s.to_dict() for s in _sessions.values()]}
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 — 持久化失败不阻断主流程
        logger.warning("会话落盘失败: %s", e)


def consume_interrupted_turns() -> list[tuple[str, "Turn"]]:
    """启动恢复后取出被重启打断、尚未告知的轮次;标记已告知并落盘(防重复通知)。"""
    marked: list[tuple[str, "Turn"]] = []
    for session in _sessions.values():
        for turn in session.turns:
            if turn.status == STATUS_FAILED and turn.error == INTERRUPTED_ERROR:
                turn.error = INTERRUPTED_ERROR + "(已告知)"
                marked.append((session.chat_id, turn))
    if marked:
        save()
    return marked


def load() -> int:
    """启动时从 SESSION_STORE_FILE 恢复会话;文件缺失/损坏按空启动。返回恢复数。"""
    path = Path(config.SESSION_STORE_FILE)
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sessions = payload.get("sessions") or []
    except Exception as e:  # noqa: BLE001 — 损坏文件不阻断启动
        logger.warning("会话存档读取失败,按空启动: %s", e)
        return 0
    restored = 0
    for data in sessions:
        try:
            session = Session.from_dict(data)
        except Exception as e:  # noqa: BLE001 — 单条损坏跳过
            logger.warning("跳过损坏的会话记录 %s: %s", data.get("chat_id"), e)
            continue
        if session.chat_id:
            _sessions[session.chat_id] = session
            restored += 1
    logger.info("已从 %s 恢复 %d 个会话", path, restored)
    return restored
