# skill scripts 沙箱自定义运行时:在 python:3.12-slim 上预装常用包。
# 构建一次、所有会话共享(镜像是只读模板,容器 --rm 一次性):
#   docker build -t agent-skill-python:3.12 sandbox/python
# 官方 docker hub 不通时 base 走 daocloud;pip 走官方源
# (清华镜像 2026-10-12 起对容器出口 403,实测不可用)。
FROM docker.m.daocloud.io/library/python:3.12-slim

RUN pip install --no-cache-dir -i https://pypi.org/simple \
    python-docx \
    docxtpl
