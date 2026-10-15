"""KnowledgeStore: RecordStore 单例封装 + 文档注册表。

设计要点(Phase 0 探针实证):
- scope 单键多值模型: scope={"ns": "global" | "chat:<chat_id>" | "user:<id>"},
  检索用 filters={"scope.ns": [...]} 表达 OR(框架 _matches_filters 对列表值天然支持);
- RecordStore 无记录级删除, 文档删除 = 注册表逻辑墓碑 + 检索时剔除;
- chunk 记录 kind="chunk", meta 携带 doc_id/chunk_index/doc_name 供回填与渲染;
- 入库按 (ns, name) 内容哈希幂等: 同内容跳过, 内容变更墓碑旧文档重入。
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from collections import deque
from pathlib import Path

# Windows 兼容补丁(Phase 0 探针实证): Agently 4.1.4.7 的 _fsync_directory 用
# os.open(目录, O_RDONLY), Windows 抛 PermissionError; 目录 fsync 仅为持久化加固,
# 降级 no-op(文件级 fsync 仍保留)。必须在 RecordStore 任何写入前生效。
if os.name == "nt":
    from agently.core.storage.Identity import Catalog as _Catalog

    _Catalog._fsync_directory = lambda path: None

from agently.core.storage import RecordStore

from . import config
from .chunking import chunk_text

logger = logging.getLogger(__name__)


def ns_global() -> str:
    return "global"


def ns_for_chat(chat_id: str) -> str:
    return f"chat:{chat_id}"


def ns_for_user(user_id: str) -> str:
    return f"user:{user_id}"


class KnowledgeStore:
    def __init__(self, root: str, embedding_provider=None):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._embedder = embedding_provider
        self._vector_enabled = embedding_provider is not None
        self._store = RecordStore(
            self._root / "records",
            mode="read_write",
            db_store_provider="sqlite",
            embedding_provider=embedding_provider,
            vector_store_provider="sqlite",
        )
        self._docs_file = self._root / "documents.json"
        self._lock = asyncio.Lock()
        self._docs: dict[str, dict] = self._load_docs()
        # 最近查询环形日志(dashboard 观测用): {ts, query, scopes, hits:[{title, score}]}
        self._query_log: deque[dict] = deque(maxlen=200)
        self._vector_db: Path | None = None  # records.db 路径(首次用时定位)

    # ── 文档注册表(逻辑删除/幂等入库的依据) ──────────────────

    def _load_docs(self) -> dict[str, dict]:
        if self._docs_file.is_file():
            try:
                return json.loads(self._docs_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("文档注册表读取失败, 按空表启动: %s", e)
        return {}

    def _save_docs(self):
        tmp = self._docs_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._docs, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self._docs_file)

    def _find_doc(self, ns: str, name: str) -> dict | None:
        for doc in self._docs.values():
            if doc["ns"] == ns and doc["name"] == name and not doc.get("deleted"):
                return doc
        return None

    # ── 入库 ────────────────────────────────────────────

    async def ingest_document(
        self,
        name: str,
        text: str,
        ns: str,
        collection: str = "kb",
        extra_meta: dict | None = None,
    ) -> dict:
        """分块入库; (ns, name) 内容哈希幂等, 内容变更时墓碑旧文档重入。
        extra_meta(如记忆的 expires_at)并入 chunk meta 与文档注册表,
        检索时对过期文档按墓碑同等过滤(不物理删除)。"""
        digest = hashlib.sha256(f"{ns}\n{name}\n{text}".encode("utf-8")).hexdigest()
        async with self._lock:
            existing = self._find_doc(ns, name)
            if existing and existing["hash"] == digest:
                return {
                    "doc_id": existing["doc_id"],
                    "chunks": existing["chunks"],
                    "skipped": True,
                }
            if existing:
                existing["deleted"] = True  # 内容变更: 旧版本墓碑
            doc_id = (existing or {}).get("doc_id") or uuid.uuid4().hex[:12]
            chunks = chunk_text(text, config.KB_CHUNK_SIZE, config.KB_CHUNK_OVERLAP)
            for i, chunk in enumerate(chunks):
                # content 用纯文本(标题+正文): dict 会被 JSON 化后嵌入, 拉低向量质量;
                # summary 存纯文本片段, 检索投影直接可用(否则回退为 content 的 JSON 串)
                await self._store.put(
                    f"《{name}》\n{chunk}",
                    collection=collection,
                    kind="chunk",
                    summary=chunk[:200],
                    meta={
                        "doc_id": doc_id,
                        "doc_name": name,
                        "chunk_index": i,
                        **(extra_meta or {}),
                    },
                    scope={"ns": ns},
                    vector=self._vector_enabled,
                )
            self._docs[doc_id] = {
                "doc_id": doc_id,
                "name": name,
                "ns": ns,
                "collection": collection,
                "hash": digest,
                "chunks": len(chunks),
                "chars": len(text),
                "deleted": False,
                "updated_at": time.time(),
                **(extra_meta or {}),
            }
            self._save_docs()
            if collection == "memory":
                # 记忆写入也进观测日志(kb_remember/自动抽取走 /kb/ingest collection=memory)
                self._query_log.append(
                    {
                        "ts": time.time(),
                        "type": "remember",
                        "query": text[:120],
                        "scopes": [ns],
                        "collection": collection,
                        "hits": [],
                    }
                )
            return {"doc_id": doc_id, "chunks": len(chunks), "skipped": False}

    # ── 检索 ────────────────────────────────────────────

    def _vector_db_path(self) -> Path | None:
        """定位框架向量表所在的 records.db(首次 rglob, 之后缓存)。"""
        if self._vector_db is None:
            found = next(self._root.rglob("records.db"), None)
            self._vector_db = found or self._root  # 找不到时存哨兵, 不反复扫
        return self._vector_db if self._vector_db.is_file() else None

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    async def _score_map(self, query: str, record_ids: list[str]) -> dict[str, float]:
        """框架 retrieve 不回传相似度分数(item.score=None, 探针实证), 这里对命中的
        record 直读向量表补算 cosine; 仅用于观测展示, 不影响排序。"""
        db = self._vector_db_path()
        if not (self._vector_enabled and db and record_ids):
            return {}
        try:
            (qv,) = await self._embedder.embed_texts([query])
            placeholders = ",".join("?" for _ in record_ids)

            def _read_rows():
                conn = sqlite3.connect(db)
                try:
                    return conn.execute(
                        f"SELECT record_id, embedding_json FROM record_store_vectors "
                        f"WHERE record_id IN ({placeholders})",
                        record_ids,
                    ).fetchall()
                finally:
                    conn.close()

            rows = await asyncio.to_thread(_read_rows)
            return {
                rid: round(self._cosine(qv, json.loads(vec)), 4)
                for rid, vec in rows
            }
        except Exception as e:
            logger.warning("相似度分数补算失败(不影响检索): %s", e)
            return {}

    async def retrieve(
        self,
        query: str,
        ns_list: list[str],
        collection: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """语义检索; 墓碑文档剔除(超取 top_k*3 补足), 返回投影不含完整正文。"""
        filters: dict = {"kind": "chunk", "scope.ns": ns_list}
        if collection:
            filters["collection"] = collection
        # method="auto" 在探针中未稳定走向量排序(返回插入序); 有嵌入能力时显式 vector,
        # 嵌入缺失的退化态走 auto(关键词), 不静默声称向量检索
        pkg = await self._store.retrieve(
            query,
            filters=filters,
            top_n=top_k * 3,
            method="vector" if self._vector_enabled else "auto",
        )
        results = []
        now = time.time()
        for item in pkg.get("items") or []:
            ref = item.get("ref") or {}
            meta = ref.get("meta") or {}
            doc = self._docs.get(meta.get("doc_id") or "")
            if doc is None or doc.get("deleted"):
                continue
            expires_at = doc.get("expires_at")
            if expires_at and expires_at < now:
                # 过期(记忆类过程状态 DDL):按墓碑同等过滤,不物理删除
                continue
            results.append(
                {
                    "record_id": ref.get("id"),
                    "doc_id": doc["doc_id"],
                    "title": doc["name"],
                    "snippet": item.get("summary") or item.get("content") or "",
                    "score": item.get("score"),
                    "ns": doc["ns"],
                    "collection": doc["collection"],
                }
            )
            if len(results) >= top_k:
                break
        scores = await self._score_map(query, [r["record_id"] for r in results if r["record_id"]])
        for r in results:
            r["score"] = scores.get(r["record_id"], r["score"])
        self._query_log.append(
            {
                "ts": time.time(),
                "type": "retrieve",
                "query": query,
                "scopes": ns_list,
                "collection": collection,
                "hits": [
                    {"title": r["title"], "score": r["score"], "ns": r["ns"]}
                    for r in results
                ],
            }
        )
        return results

    def recent_queries(self, limit: int = 50) -> list[dict]:
        """最近查询日志(新的在前), dashboard 观测用。"""
        return list(reversed(self._query_log))[:limit]

    # ── 管理面 ──────────────────────────────────────────

    def list_documents(self, ns: str | None = None) -> list[dict]:
        docs = [d for d in self._docs.values() if not d.get("deleted")]
        if ns is not None:
            docs = [d for d in docs if d["ns"] == ns]
        return sorted(docs, key=lambda d: d["updated_at"], reverse=True)

    def delete_document(self, doc_id: str) -> bool:
        """逻辑删除(框架无记录级删除); 检索立即不可见, 物理记录留存冷数据。"""
        doc = self._docs.get(doc_id)
        if doc is None or doc.get("deleted"):
            return False
        doc["deleted"] = True
        self._save_docs()
        return True

    def stats(self) -> dict:
        active = [d for d in self._docs.values() if not d.get("deleted")]
        return {
            "documents": len(active),
            "chunks": sum(d["chunks"] for d in active),
            "vector_enabled": self._vector_enabled,
        }
