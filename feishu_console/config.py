import os
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
# 飞书自建应用凭证：优先读环境变量，也可以直接改这里的默认值
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# Web 面板端口
PORT = int(os.environ.get("PORT", "8000"))
