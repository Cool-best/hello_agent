# -*- coding: utf-8 -*-
"""web_fetch 的浏览器渲染兜底(MCP 服务器进程内执行):headless Chromium 单例。

仅在静态抽取薄内容时由 web_actions._do_fetch 触发;playwright 包或 chromium
二进制缺失时 render_html 一律返回 None,调用方退回静态结果,行为与旧版一致。

安全:主框架每次 navigation(含重定向)复用 web_actions._validate_url 过 SSRF;
子资源请求按 hostname 解析(带 TTL 缓存)拦截私网/保留地址,防渲染器变 SSRF 跳板。
"""
import asyncio
import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

from .. import config

logger = logging.getLogger(__name__)

_pw = None  # async_playwright 上下文
_browser = None  # Chromium 单例(懒启动,崩溃后重建)
_launch_lock = asyncio.Lock()
_sem: asyncio.Semaphore | None = None  # 首次使用时按配置建(进程内唯一 loop)
_unavailable_reason: str | None = None  # 一次性降级判定:包缺失/二进制缺失

_DNS_CACHE: dict[str, tuple[float, bool]] = {}  # host -> (到期 ts, 是否公网可达)
_DNS_TTL = 300.0

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_ABORT_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


def _domain_hit(url: str, csv_domains: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == d or host.endswith("." + d)
        for d in (x.strip().lower() for x in csv_domains.split(","))
        if d
    )


def render_excluded(url: str) -> bool:
    return _domain_hit(url, config.WEB_RENDER_EXCLUDE_DOMAINS)


def render_forced(url: str) -> bool:
    return _domain_hit(url, config.WEB_RENDER_FORCE_DOMAINS)


def render_available() -> bool:
    return config.WEB_RENDER_FALLBACK and _unavailable_reason is None


def _host_public(host: str) -> bool:
    """子资源 SSRF 检查:hostname 解析(带 TTL 缓存)命中私网/保留地址则不可达。"""
    now = time.monotonic()
    cached = _DNS_CACHE.get(host)
    if cached and cached[0] > now:
        return cached[1]
    ok = False
    try:
        infos = socket.getaddrinfo(host, None)
        addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
        from . import web_actions  # 晚绑定避免循环依赖

        ok = bool(addrs) and all(not web_actions._is_forbidden_ip(a) for a in addrs)
    except Exception:
        ok = False
    _DNS_CACHE[host] = (now + _DNS_TTL, ok)
    return ok


async def _ensure_browser():
    """懒启动 Chromium 单例;不可用原因一次性判定并记 warning。"""
    global _pw, _browser, _unavailable_reason, _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.WEB_RENDER_MAX_CONCURRENCY)
    if _unavailable_reason is not None:
        return None
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _launch_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            _unavailable_reason = "未安装 playwright 包"
            logger.warning("渲染兜底不可用: %s,退回纯静态抓取", _unavailable_reason)
            return None
        try:
            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(headless=True)
            logger.info("渲染兜底 Chromium 已启动(headless)")
            return _browser
        except Exception as e:
            _unavailable_reason = f"Chromium 启动失败: {e}"
            logger.warning(
                "渲染兜底不可用: %s(可执行 playwright install chromium 安装浏览器)",
                _unavailable_reason,
            )
            return None


async def render_html(url: str) -> str | None:
    """渲染抓取,返回渲染后 HTML;任何失败返回 None(调用方退回静态结果)。"""
    if not render_available() or render_excluded(url):
        return None
    from . import web_actions  # 晚绑定避免循环依赖

    try:
        await web_actions._validate_url(url)
    except ValueError as e:
        logger.warning("渲染入口 SSRF 拦截 %s: %s", url, e)
        return None
    browser = await _ensure_browser()
    if browser is None:
        return None

    async def _route_handler(route):
        request = route.request
        if request.resource_type in _ABORT_RESOURCE_TYPES:
            return await route.abort()
        target = request.url
        # 主框架 navigation 走完整 SSRF(含重定向跳内网);子资源按 hostname 缓存判定
        if request.is_navigation_request() and request.frame == request.frame.page.main_frame:
            try:
                await web_actions._validate_url(target)
            except ValueError:
                return await route.abort()
        elif not _host_public(urlparse(target).hostname or ""):
            return await route.abort()
        return await route.continue_()

    timeout_ms = config.WEB_RENDER_TIMEOUT * 1000
    async with _sem:
        context = None
        try:
            context = await browser.new_context(
                user_agent=_CHROME_UA, locale="zh-CN", viewport={"width": 1280, "height": 900}
            )
            await context.route("**/*", _route_handler)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(8000, timeout_ms))
            except Exception:
                pass  # 页面长连接不止时容忍,用当前已渲染内容
            try:
                await web_actions._validate_url(page.url)  # 渲染期再跳转仍过 SSRF
            except ValueError as e:
                logger.warning("渲染落地地址 SSRF 拦截 %s -> %s: %s", url, page.url, e)
                return None
            return await page.content()
        except Exception as e:
            logger.warning("渲染抓取失败 %s: %s", url, e)
            if _browser is not None and not _browser.is_connected():
                _browser = None  # 崩溃重建留给下次调用
            return None
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
