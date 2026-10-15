# -*- coding: utf-8 -*-
"""轮后 HTML 自包含化:把 workspace 里本轮产物 HTML 的失效本地资源引用内联为单文件。

背景:html-ppt 的 new-deck.sh 脚手架只把资源引用重写成指向容器内 bundle
(`../../source/assets/...`),资源随容器销毁,产物在容器外打开缺样式
(2026-10-12 实证)。宿主侧在轮后(产物投递前)扫本轮改动的 .html:
- 相对 HTML 位置能解析的本地引用不动(多文件本来就能工作);
  例外:指向 skills_ref/ 镜像的引用虽可解析,但随附件单发即失效,
  按重定位处理直接内联镜像内容(字节与 skill 源一致);
- 解析不了的本地引用按"去掉最后一个 source/ 前缀(无则剥前导 ../)→
  skills/<name>/<tail>"重定位到宿主 skill 源目录,找到则内联:
  <link stylesheet> → <style>、<script src> → 内联 <script>(保留其他属性)、
  <img src> → data URI;
- 远程引用(http/https/协议相对/data:)一律不动——fonts.css 的 Google Fonts
  @import 原样保留(联网可用,离线仅字体回退,不断版式);
- 宿主 skill 源里也找不到的引用如实记入结果(missing),不臆造。

防误伤:只处理 mtime ≥ 轮起始时间的文件;单文件内联总量封顶
HTML_INLINE_MAX_BYTES,超出整文件跳过(宁缺毋滥,留原文件+missing 记录)。
CSS 内部不再递归解析 url():html-ppt 实测 base/theme/animations CSS 无本地
url() 引用(仅 runtime.js 动态拼接字符串与 fonts.css 远程 @import),
若未来 skill 引入本地 url() 资源需在此补递归。
"""
import base64
import logging
import os
import re
from pathlib import Path, PurePosixPath

from .. import config

logger = logging.getLogger(__name__)

HTML_INLINE_MAX_BYTES = 4 * 1024 * 1024

_TAG_ATTR_RE = {
    # tag 名 -> (整 tag 正则, 引用属性名)
    "link": (
        re.compile(r"<link\b[^>]*\bhref=(\"|\')([^\"\']+)\1[^>]*>", re.IGNORECASE),
        "href",
    ),
    "script": (
        re.compile(
            r"<script\b[^>]*\bsrc=(\"|\')([^\"\']+)\1[^>]*>\s*</script>",
            re.IGNORECASE,
        ),
        "src",
    ),
    "img": (
        re.compile(r"<img\b[^>]*\bsrc=(\"|\')([^\"\']+)\1[^>]*>", re.IGNORECASE),
        "src",
    ),
}
_STYLESHEET_RE = re.compile(r"rel\s*=\s*(\"|\')[^\"\']*stylesheet", re.IGNORECASE)
_REMOTE_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//|#)")
_IMG_MIME = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}


def _strip_ref(ref: str) -> str:
    """去掉 query/fragment;空引用返回空串。"""
    return ref.split("?", 1)[0].split("#", 1)[0].strip()


def _resolve_ref(
    ref: str,
    html_path: Path,
    skill_dirs: list[Path],
    skills_ref_root: Path | None = None,
):
    """返回 (status, path):ok=相对 HTML 可解析;remapped=经 skill 源重定位;
    missing=两处都找不到。skills_ref/ 镜像下的引用虽可解析,但随附件单发会失效,
    按 remapped 处理(直接内联镜像内容,字节与 skill 源一致)。"""
    local = Path(os.path.normpath(str(html_path.parent / ref)))
    if local.is_file():
        if skills_ref_root is not None:
            try:
                local.relative_to(skills_ref_root)
                return "remapped", local
            except ValueError:
                pass
        return "ok", local
    parts = PurePosixPath(ref.replace("\\", "/")).parts
    if "source" in parts:
        idx = len(parts) - 1 - parts[::-1].index("source")  # 最后一个 source 段
        tail = parts[idx + 1:]
    else:
        tail = tuple(p for p in parts if p not in ("..", "."))
    if not tail:
        return "missing", None
    for skill_dir in skill_dirs:
        candidate = skill_dir.joinpath(*tail)
        if candidate.is_file():
            return "remapped", candidate
    return "missing", None


def _inline_file(
    path: Path,
    skill_dirs: list[Path],
    skills_ref_root: Path | None = None,
) -> dict | None:
    """内联单个 HTML 文件;有实际改动返回报告 dict,无改动返回 None。

    报告: {file, inlined: [ref...], missing: [ref...], skipped: 原因(可选)}。
    原子写(临时文件 + os.replace),失败不污染原文件。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ops: list[tuple[int, int, str, str]] = []  # (start, end, replacement, ref)
    missing: list[str] = []

    def plan(tag_name, match):
        tag = match.group(0)
        raw = match.group(2)  # 标签里的原始引用文本(含 query/fragment)
        ref = _strip_ref(raw)
        if not ref or _REMOTE_RE.match(ref):
            return
        if tag_name == "link" and not _STYLESHEET_RE.search(tag):
            return
        status, src = _resolve_ref(ref, path, skill_dirs, skills_ref_root)
        if status == "missing":
            missing.append(ref)
            return
        if status != "remapped":
            return
        try:
            data = src.read_bytes()
        except OSError:
            missing.append(ref)
            return
        if tag_name == "img":
            mime = _IMG_MIME.get(src.suffix.casefold())
            if mime is None:
                missing.append(ref)
                return
            uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            # 只替换引号内的属性值,保留标签其余部分
            value_at = match.start(2) - match.start(0)
            replacement = tag[:value_at] + uri + tag[value_at + len(raw):]
        else:
            body = data.decode("utf-8", errors="replace")
            if tag_name == "link":
                replacement = f"<style>\n{body}\n</style>"
            else:  # script:剥掉 src 属性,保留 type="module" 等其他属性
                open_end = tag.index(">") + 1
                open_tag = re.sub(
                    r"\s*\bsrc=(\"[^\"]*\"|'[^']*')", "", tag[:open_end], count=1
                )
                replacement = f"{open_tag}\n{body}\n</script>"
        ops.append((match.start(), match.end(), replacement, ref))

    for tag_name, (pattern, _attr) in _TAG_ATTR_RE.items():
        for match in pattern.finditer(text):
            plan(tag_name, match)
    if not ops:
        if missing:
            return {"file": "", "inlined": [], "missing": missing}
        return None
    total = sum(len(rep) for _, _, rep, _ in ops)
    if total > HTML_INLINE_MAX_BYTES:
        logger.warning(
            "HTML 内联总量 %d 超过上限 %d,整文件跳过: %s",
            total, HTML_INLINE_MAX_BYTES, path,
        )
        return {
            "file": "",
            "inlined": [],
            "missing": missing,
            "skipped": f"内联总量 {total / 1024 / 1024:.1f}MB 超限未处理",
        }
    for start, end, replacement, _ref in sorted(ops, reverse=True):
        text = text[:start] + replacement + text[end:]
    tmp = path.with_name(path.name + ".inl.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return {"file": "", "inlined": [ref for _, _, _, ref in ops], "missing": missing}


def _skill_dirs(skills_root: Path | None) -> list[Path]:
    root = Path(skills_root) if skills_root else Path(config.SKILLS_DIR)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir())


def inline_workspace_html(
    ws_root: str, since: float, skills_root: Path | None = None
) -> list[dict]:
    """扫 ws_root 下 mtime >= since 的 .html,内联失效本地引用为单文件。

    返回逐文件报告(file 为 workspace 相对路径);全程 fail-open,单文件
    异常不影响其他文件。
    """
    root = Path(ws_root)
    if not root.is_dir():
        return []
    skill_dirs = _skill_dirs(skills_root)
    sref_root = root / "skills_ref"
    results: list[dict] = []
    for path in sorted(root.rglob("*.html")):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        try:
            entry = _inline_file(
                path, skill_dirs, sref_root if sref_root.is_dir() else None
            )
        except Exception as e:
            logger.warning("HTML 内联处理失败(跳过该文件): %s: %s", path, e)
            continue
        if entry and (entry["inlined"] or entry["missing"] or entry.get("skipped")):
            entry["file"] = str(path.relative_to(root)).replace("\\", "/")
            results.append(entry)
    return results
