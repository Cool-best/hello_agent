"""kb_service FastAPI 入口: 独立 RAG 微服务。

启动: myenv310/Scripts/python.exe -m kb_service.main
首次运行需下载嵌入模型(HF 不通时在 .env 设 HF_ENDPOINT=https://hf-mirror.com)。
嵌入模型加载失败时服务仍可启动: 入库退化为不向量化, 检索退化为关键词,
/health 经 embedding_ready 上报(不静默声称向量检索)。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config
from .embed import make_embedding_provider
from .ingest import KnowledgeScanner
from .store import KnowledgeStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

_store: KnowledgeStore | None = None
_scanner: KnowledgeScanner | None = None
_embedding_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store, _scanner, _embedding_ready
    provider = None
    try:
        provider = await asyncio.to_thread(make_embedding_provider)
        _embedding_ready = True
    except Exception as e:
        logger.error("嵌入模型加载失败, 退化为关键词检索: %s", e)
    _store = KnowledgeStore(config.KB_DATA_DIR, provider)
    _scanner = KnowledgeScanner(_store)
    await _scanner.scan()
    yield


app = FastAPI(title="kb_service", lifespan=lifespan)

_DASHBOARD_FILE = Path(__file__).parent / "static" / "dashboard.html"


@app.get("/")
async def dashboard():
    """RAG 独立观测界面(与主应用 dashboard 分离, RAG 是独立模块)。"""
    return FileResponse(_DASHBOARD_FILE)


class RetrieveRequest(BaseModel):
    query: str
    scopes: list[str] = Field(default_factory=lambda: ["global"])
    collection: str | None = None
    top_k: int = 5


class IngestRequest(BaseModel):
    name: str
    text: str
    scope: str = "global"
    collection: str = "kb"
    meta: dict | None = None  # 调用方附加元数据(如记忆的 expires_at)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "embedding_ready": _embedding_ready,
        "embedding_provider": config.EMBEDDING_PROVIDER if _embedding_ready else None,
        **_store.stats(),
    }


@app.post("/kb/retrieve")
async def retrieve(req: RetrieveRequest):
    results = await _store.retrieve(
        req.query,
        ns_list=req.scopes,
        collection=req.collection,
        top_k=req.top_k,
    )
    return {"results": results, "count": len(results)}


@app.post("/kb/ingest")
async def ingest(req: IngestRequest):
    return await _store.ingest_document(
        req.name, req.text, ns=req.scope, collection=req.collection,
        extra_meta=req.meta,
    )


@app.get("/kb/documents")
async def documents(scope: str | None = None):
    return {"documents": _store.list_documents(ns=scope)}


@app.get("/kb/queries")
async def queries(limit: int = 50):
    """最近检索日志(内存环形缓冲, 服务重启即清空)。"""
    return {"queries": _store.recent_queries(limit)}


@app.delete("/kb/documents/{doc_id}")
async def delete_document(doc_id: str):
    deleted = _store.delete_document(doc_id)
    return {"deleted": deleted}


@app.post("/kb/scan")
async def scan():
    """触发知识库目录增量扫描(内容哈希幂等); 嵌入模型更换后的全量重建
    需停服清空 KB_DATA_DIR 再启动(v1 取舍)。"""
    return await _scanner.scan()


def main():
    uvicorn.run(
        "kb_service.main:app",
        host=config.KB_SERVICE_HOST,
        port=config.KB_SERVICE_PORT,
    )


if __name__ == "__main__":
    main()
