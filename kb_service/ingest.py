"""知识库目录扫描。

按文件内容哈希幂等(复用 skills 的内容寻址哲学): 未变更只是 hash 开销;
变更文件经 KnowledgeStore.ingest_document 重入(旧版本墓碑)。
分块逻辑在 chunking.py。
"""

import hashlib
import json
import logging
from pathlib import Path

from . import config
from .store import KnowledgeStore, ns_global

logger = logging.getLogger(__name__)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class KnowledgeScanner:
    """KNOWLEDGE_DIR 增量扫描入库: 扫描状态落盘, 变更才重入。"""

    def __init__(self, store: KnowledgeStore):
        self._store = store
        self._state_file = Path(config.KB_DATA_DIR) / "scan_state.json"
        self._state: dict[str, str] = self._load_state()

    def _load_state(self) -> dict[str, str]:
        if self._state_file.is_file():
            try:
                return json.loads(self._state_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("扫描状态读取失败, 按全量重扫: %s", e)
        return {}

    def _save_state(self):
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        tmp.replace(self._state_file)

    async def scan(self) -> dict:
        root = Path(config.KNOWLEDGE_DIR)
        result = {"ingested": 0, "skipped": 0, "errors": 0, "missing_dir": False}
        if not root.is_dir():
            result["missing_dir"] = True
            logger.info("知识库目录不存在, 跳过扫描: %s", root)
            return result
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in config.KB_SCAN_EXTENSIONS:
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            try:
                if path.stat().st_size > config.KB_MAX_FILE_BYTES:
                    logger.warning("知识库文件超限跳过: %s", rel)
                    result["errors"] += 1
                    continue
                digest = _file_digest(path)
                if self._state.get(rel) == digest:
                    result["skipped"] += 1
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                await self._store.ingest_document(rel, text, ns=ns_global())
                self._state[rel] = digest
                result["ingested"] += 1
            except Exception as e:
                logger.error("知识库文件入库失败 %s: %s", rel, e)
                result["errors"] += 1
        self._save_state()
        logger.info("知识库扫描完成: %s", result)
        return result
