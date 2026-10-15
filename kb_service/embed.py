"""嵌入 provider: 实现 Agently RecordStore 要求的 embed_texts(texts) 接口。

模型推理是同步阻塞调用, 一律经 asyncio.to_thread  offload, 不阻塞事件循环。
"""

import asyncio
import logging
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)


class FastembedProvider:
    """fastembed(ONNX 运行时): 轻量, 无需 torch。

    模型缓存固定在 KB_DATA_DIR/models(默认缓存位于系统临时目录, 重启可能被清);
    首次下载需 HF 可达, 不通时在 .env 设 HF_ENDPOINT=https://hf-mirror.com(Phase 0 实证)。
    """

    name = "fastembed"

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding

        cache_dir = Path(config.KB_DATA_DIR) / "models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
        logger.info("fastembed 模型已加载: %s", model_name)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(
            lambda: [e.tolist() for e in self._model.embed(texts)]
        )


class SentenceTransformersProvider:
    """sentence-transformers: 生态成熟但依赖 torch(约 2GB), 备选。"""

    name = "sentence_transformers"

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        logger.info("sentence-transformers 模型已加载: %s", model_name)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(
            lambda: self._model.encode(texts, normalize_embeddings=True).tolist()
        )


def make_embedding_provider():
    """按配置创建嵌入 provider; 在 lifespan 里调用(加载耗时数秒)。"""
    if config.EMBEDDING_PROVIDER == "sentence_transformers":
        return SentenceTransformersProvider(config.EMBEDDING_MODEL)
    return FastembedProvider(config.EMBEDDING_MODEL)
