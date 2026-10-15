"""kb_service 配置: 独立 RAG 微服务的环境变量。

与主项目共用根目录 .env(都经 python-dotenv 加载), KB_* 前缀避免冲突。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

KB_SERVICE_HOST = os.environ.get("KB_SERVICE_HOST", "127.0.0.1")  # 仅本机回环, 不对外暴露
KB_SERVICE_PORT = int(os.environ.get("KB_SERVICE_PORT", "8002"))

# RecordStore 落盘目录(记录库 + 向量表 + 文档注册表)
KB_DATA_DIR = os.environ.get(
    "KB_DATA_DIR", str(Path(__file__).parent / "data")
)

# 项目全局知识库目录(markdown/txt), 启动扫描入库
KNOWLEDGE_DIR = os.environ.get(
    "KNOWLEDGE_DIR", str(Path(__file__).parent.parent / "knowledge")
)

# 嵌入 provider: fastembed(默认, ONNX 轻量) | sentence_transformers
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "fastembed")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

# 分块: 段落边界优先, 目标 600 字符, 重叠 100
KB_CHUNK_SIZE = int(os.environ.get("KB_CHUNK_SIZE", "600"))
KB_CHUNK_OVERLAP = int(os.environ.get("KB_CHUNK_OVERLAP", "100"))
KB_MAX_FILE_BYTES = int(os.environ.get("KB_MAX_FILE_BYTES", str(2 * 1024 * 1024)))

# 知识库扫描支持的扩展名
KB_SCAN_EXTENSIONS = tuple(
    e.strip()
    for e in os.environ.get("KB_SCAN_EXTENSIONS", ".md,.txt,.markdown").split(",")
    if e.strip()
)
