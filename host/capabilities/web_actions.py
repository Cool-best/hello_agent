"""Web 联网能力:web_search / web_fetch Action(渠道中立,与 inbox.py 同级)。

- 搜索后端为可插拔适配器注册表(_PROVIDERS):合规 API 型的博查(主源候选)、
  抓取式的 baidu/cn.bing.com(免 key 兜底;违反引擎 ToS、UI 改版
  会静默失效、量大触发风控,不建议作主源)与 sina 新闻垂直 JSON API
  (免 key、直链、自带可靠 ctime 发布时间透出为 published,但仅限 sina 系内容);
  纯垂直要闻查询(如"今日要闻""国际新闻")直达门户栏目数据面
  (cctv 栏目 JSONP / sina 国内滚动 / 百度热榜,见"垂直栏目直达"注释块),
  不经过搜索引擎;含具体实体词的查询仍走引擎。
  归一化输出 [{title, url, snippet, site, published?}],
  配置的多个源并行查询、按 URL 去重合并(≥1 源成功即返回);单源单页约 10 条,
  宽检索(count>12)靠多源合并凑量,最终返回 snippet 截断至 60 字控制回包体积;
  baidu 走 m.baidu.com 移动版,data-log JSON 的 mu 字段即真实落地页直链,
  触发安全验证时 Playwright 渲染兜底(软档 JS 挑战可自动通过;硬档滑块验证码
  无法绕过,检出后进入 10 分钟冷却,期内快速失败避免延长惩罚)。
- web_fetch 负责抓页面:url 支持字符串或列表(列表并发抓取);text/html 抽正文
  截断返回(主内容提取优先 trafilatura,退回 bs4);条目链接 ≥5 的链接密集型
  页面(门户首页/频道页/热榜聚合,正文多为导航噪声)额外附条目链接列表
  [{title, url}](批量模式截 5 条),供顺藤摸瓜抓具体条目;另有按域名后缀匹配的
  站点策略注册表 _SITE_STRATEGIES(handler 完全定制,如 cctv 频道页直调 JSONP
  数据接口;body/remove 选择器做瘦身抽取,剔除面包屑/推荐位等非关键要素),
  策略未命中/失败回退通用链路;薄内容(JS 页面)自动
  Playwright 渲染兜底(web_render.py,渲染结果仅在优于静态时采用);
  text/* 与 application/json(如 raw.githubusercontent、api.github.com)
  按原始文本截断返回;PDF/Office/图片/zip 类字节物化进会话 workspace 的
  downloads/(宿主行为,与 inbox 附件落盘同理,不受 WORKSPACE_READ_ONLY 约束),
  返回 file ref;无 workspace 时不物化。
- 护栏:SSRF 校验(仅 http/https,DNS 解析后拦截私网/回环/链路本地地址,
  重定向逐跳重新校验,渲染 navigation 同级校验)、流式下载超限即断、
  单 turn 调用次数限额、正文截断、同 URL 短 TTL 缓存(WEB_FETCH_CACHE_TTL)。
- 所有可预期失败(网络/超时/拦截/限额/类型不支持)返回结构化错误字符串,
  由模型如实转述,不抛异常打断 Action 回路。
"""
import asyncio
import base64
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.parse
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .. import config
from ..state import inbox, workspace_store

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = "downloads"
# 浏览器化请求头:搜索/抓取共用,降低被简单 UA 拦截的概率
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_MAX_REDIRECTS = 6
_MIN_TEXT_CHARS = 200  # 抽取正文低于此长度,提示可能需要 JS 渲染

_FRESHNESS_ALIASES = {
    "d": "oneDay",
    "w": "oneWeek",
    "m": "oneMonth",
    "y": "oneYear",
    "oneday": "oneDay",
    "oneweek": "oneWeek",
    "onemonth": "oneMonth",
    "oneyear": "oneYear",
    "nolimit": "noLimit",
}

_BINARY_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

_DEFAULT_EXTENSION_BY_MIME = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def _provider_names() -> list[str]:
    """主源 + 降级链(去重、保序)。"""
    names = [config.WEB_SEARCH_PROVIDER] + [
        name.strip() for name in config.WEB_SEARCH_FALLBACK.split(",") if name.strip()
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


# 需要 API key 的适配器;baidu/bing 抓取源免 key
_KEY_REQUIRED_PROVIDERS = {"bocha"}


def web_enabled() -> bool:
    """总开关开且链路里至少有一个可用源(key 型源需已配 key)。"""
    if not config.WEB_ENABLED:
        return False
    return any(
        name in _PROVIDERS
        and (name not in _KEY_REQUIRED_PROVIDERS or config.WEB_SEARCH_API_KEY)
        for name in _provider_names()
    )


# ---------------------------------------------------------------- 搜索适配器


async def _search_bocha(query: str, freshness: str, count: int) -> list[dict]:
    """博查 Web Search API;响应 Bing 兼容:data.webPages.value[]。"""
    async with httpx.AsyncClient(timeout=config.WEB_SEARCH_TIMEOUT) as client:
        response = await client.post(
            "https://api.bochaai.com/v1/web-search",
            headers={
                "Authorization": f"Bearer {config.WEB_SEARCH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "freshness": freshness,
                "summary": True,
                "count": count,
            },
        )
    if response.status_code != 200:
        raise RuntimeError(f"博查搜索 HTTP {response.status_code}: {response.text[:200]}")
    payload = response.json()
    web_pages = (payload.get("data") or {}).get("webPages") or {}
    results = []
    for item in web_pages.get("value") or []:
        results.append(
            {
                "title": item.get("name") or "",
                "url": item.get("url") or "",
                "snippet": item.get("summary") or item.get("snippet") or "",
                "site": item.get("siteName") or "",
            }
        )
    return results


_PROVIDERS = {
    "bocha": _search_bocha,
    # baidu/bing 抓取适配器在下方定义后注册(见 _PROVIDERS.update)
}


def _normalize_freshness(time_range: str) -> str:
    return _FRESHNESS_ALIASES.get(str(time_range or "").strip().lower(), "noLimit")


# ---------------------------------------------------------------- SSRF 防护


def _is_forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _validate_url(url: str) -> str:
    """仅允许 http/https 且解析结果不含内网地址;非法时抛 ValueError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 链接: {url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"链接缺少主机名: {url}")
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80)
            ),
            timeout=10,
        )
    except asyncio.TimeoutError:
        # getaddrinfo 无内建超时,DNS 挂起会让 action 无限阻塞(2026-10-12 探针实证)
        raise ValueError(f"域名解析超时(10s): {host}——DNS 无响应,该站点从当前网络不可达")
    except socket.gaierror as e:
        raise ValueError(f"域名解析失败: {host} ({e})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_forbidden_ip(ip):
            raise ValueError(f"目标地址 {ip} 属于内网/保留地址,已拦截")
    return url


# ------------------------------------------- 容忍重定向传输层(搜索抓取/落地页共用)
# 标准 httpx 重定向对脏 Location(baidu 跳转服务偶发带非法字节)直接抛
# RemoteProtocolError;这里下沉到 transport 层手动跟随:Location 还原到字节层
# 清洗、Set-Cookie 逐跳合并,且每一跳都过 SSRF 校验(防重定向进内网)。


def _normalize_whitespace(raw_text: str) -> str:
    return " ".join((raw_text or "").split())


def _sanitize_location_header(raw_location: str, base_url: str) -> str:
    """清洗 Location 头:还原原始字节、剔除控制字节、百分号编码非 ASCII、解析相对地址。

    httpx 对头按 ascii -> utf-8 -> latin-1 依次解码,脏字节可能表现为 mojibake
    或原生非 ASCII 字符;统一还原到字节层按浏览器方式处理。清洗后为空返回空串。
    """
    try:
        location_bytes = raw_location.encode("latin-1")
    except UnicodeEncodeError:
        location_bytes = raw_location.encode("utf-8")
    printable_bytes = bytes(b for b in location_bytes if 0x21 <= b <= 0x7E or b >= 0x80)
    if not printable_bytes:
        return ""
    cleaned = urllib.parse.quote_from_bytes(printable_bytes, safe=":/?#[]@!$&'()*+,;=%~")
    return urljoin(base_url, cleaned)


def _merge_set_cookies(response_headers: httpx.Headers, request_headers: dict) -> None:
    """把上一跳的 Set-Cookie 并进下一跳请求头(对齐浏览器跳转携带行为)。"""
    new_cookies = [
        value.split(";", 1)[0].strip()
        for value in response_headers.get_list("set-cookie")
    ]
    if not new_cookies:
        return
    existing = request_headers.get("Cookie", "").strip()
    pairs = ([existing] if existing else []) + new_cookies
    request_headers["Cookie"] = "; ".join(pairs)


# 搜索引擎的跳转服务(baidu 跳转链 fallback 等):可能返回 200 + 极小 HTML,
# 用 JS/meta refresh 跳落地页而非 302
_JS_REDIRECT_PATTERN = re.compile(
    r"""window\.location\.replace\(["']([^"']+)["']\)"""
    r"""|<meta[^>]+http-equiv=["']?refresh["']?[^>]*url=['"]?([^'" >]+)""",
    re.IGNORECASE,
)


def _extract_js_redirect(html: str) -> str:
    """从跳转页 HTML 提取 JS/meta refresh 目标地址;不是跳转页返回空串。"""
    m = _JS_REDIRECT_PATTERN.search(html)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


async def _fetch_html(
    url: str,
    params: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> httpx.Response:
    """共享 GET 抓取(供抓取式搜索适配器使用):SSRF 校验 + 容忍重定向 + 状态码校验。"""
    headers = {**_FETCH_HEADERS, **(dict(extra_headers) if extra_headers else {})}
    timeout_value = timeout or config.WEB_SEARCH_TIMEOUT
    timeout_config = {
        "connect": timeout_value,
        "read": timeout_value,
        "write": timeout_value,
        "pool": timeout_value,
    }
    current_url = await _validate_url(url)
    request_params: Optional[dict] = dict(params) if params else None
    async with httpx.AsyncHTTPTransport() as transport:
        for _ in range(_MAX_REDIRECTS):
            request = httpx.Request(
                "GET", current_url, params=request_params,
                headers=headers, extensions={"timeout": timeout_config},
            )
            response = await transport.handle_async_request(request)
            response.request = request  # transport 层不回填,手动补上以支持 .url
            is_redirect = response.status_code in (301, 302, 303, 307, 308)
            next_url = (
                _sanitize_location_header(response.headers.get("location", ""), str(request.url))
                if is_redirect
                else ""
            )
            if not next_url:
                await response.aread()
                response.raise_for_status()
                return response
            _merge_set_cookies(response.headers, headers)
            await response.aclose()
            current_url = await _validate_url(next_url)  # 每跳重新过 SSRF
            request_params = None  # 后续跳直接用完整 Location 地址
    raise ValueError(f"超过 {_MAX_REDIRECTS} 跳重定向: {url}")


# ------------------------------------------------- 抓取式搜索适配器(baidu/bing)
# 抓搜索结果页 HTML 解析,不需要 API key、无调用成本;代价是违反引擎 ToS、
# UI 改版会导致解析静默失效、量大会触发风控——定位为兜底源/无 key 时的可用源,
# 主源建议仍用合规 API(博查)。bing 支持 freshness(经 filters 参数映射,2026-10-14 起);
# baidu 的 freshness 仍不生效(接口对齐保留)。


def unwrap_bing_url(redirect_href: str) -> str:
    """把 bing 的 /ck/a 跳转链接还原为真实落地页 url;解不出返回空串。

    跳转参数 u 形如 "a1<base64(落地页url)>",字母表可能是标准或 urlsafe 变体、
    可能缺 padding;parse_qs 会把 '+' 解释成空格,需还原。
    """
    query_string = urllib.parse.urlsplit(redirect_href).query
    encoded_parts = urllib.parse.parse_qs(query_string).get("u") or [""]
    encoded_target = encoded_parts[0].replace(" ", "+")
    if not encoded_target.startswith("a1"):
        return ""
    payload = encoded_target[2:].translate(str.maketrans("-_", "+/"))
    payload += "=" * (-len(payload) % 4)
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


# baidu 结果中的站内导航/图片/聊天链接,不是落地页,跳过
_BAIDU_SKIP_HREFS = (
    "/s?wd=",
    "image.baidu.com",
    "chat.baidu.com",
    "top.baidu.com",
    "tieba.baidu.com",
    "baidu.com/s?word=",
    "map.baidu.com",
)


def _is_baidu_noise_entry(title: str, href: str) -> bool:
    """baidu 噪声:标题过短 /「查看更多」类按钮 / 站内导航链接。
    移动版 mu 直链下,baidu.com 域内只放行 /link?url= 跳转链,
    baijiahao/登录/广告等站内链接一律过滤。"""
    if len(title) < 8:
        return True
    if any(marker in title for marker in ("查看更多", "查看全部", "换一换")):
        return True
    if any(hint in href for hint in _BAIDU_SKIP_HREFS):
        return True
    netloc = urlparse(href).netloc.lower()
    if netloc.endswith("baidu.com") and "baidu.com/link?url=" not in href:
        return True
    return False


# bing freshness 映射:ex1:"ezN" 是 bing 结果页时间筛选的查询参数
# (ez1=过去24小时 ez2=过去一周 ez3=过去一月);oneYear 无可靠代码,不映射
_BING_FRESHNESS_FILTERS = {
    "oneDay": 'ex1:"ez1"',
    "oneWeek": 'ex1:"ez2"',
    "oneMonth": 'ex1:"ez3"',
}


def _parse_bing_results(tree, max_results: int) -> list[dict]:
    """cn.bing.com 结果解析:只认 li.b_algo 块内 h2/a;/ck/a 跳转链接还原落地页。"""
    results: list[dict] = []
    for block in tree.xpath("//li[contains(@class, 'b_algo')]"):
        anchors = block.xpath(".//h2/a")
        if not anchors:
            continue
        anchor = anchors[0]
        title = _normalize_whitespace("".join(anchor.xpath(".//text()")))[:60]
        href = anchor.get("href", "")
        if href.startswith(("https://cn.bing.com/ck/a", "https://www.bing.com/ck/a")):
            href = unwrap_bing_url(href) or href
        summary = " ".join(
            _normalize_whitespace("".join(p.xpath(".//text()")))
            for p in block.xpath(".//p")
        )[:80]
        if title:
            results.append(
                {
                    "title": title,
                    "href": href,
                    "body": summary,
                }
            )
        if len(results) >= max_results:
            break
    return results


_BAIDU_BLOCK_UNTIL = 0.0  # monotonic 时间戳:安全验证冷却期内直接跳过 baidu
_BAIDU_BLOCK_COOLDOWN = 600.0


async def _search_baidu(query: str, freshness: str, count: int) -> list[dict]:
    """抓 m.baidu.com 移动版结果页(freshness 不生效)。

    2026-10-15 从桌面版迁到移动版 + 渲染兜底:桌面 www.baidu.com/s 无 Cookie
    必被 302 到 wappass 安全验证页(200 但无结果块,表现为静默 0 条);
    "首页取 Cookie + 完整浏览器头"在 IP 被风控标记后同样失效(实测同一探针
    10 分钟内从通过到被拦);移动版静态在低频时可用,连续/并发同样触发验证墙。
    验证墙分两档:软档为 JS 挑战,Playwright 渲染可自动通过(实测 4.2s);
    硬档(IP 惩罚期)为 wappass 滑块拼图验证码,渲染同样落地验证页,无法绕过,
    只能等风控解除——故检出"安全验证"后进入 10 分钟冷却,期内快速失败,
    避免持续撞墙延长惩罚,其余源(sina/bing)正常兜底。
    结果块 data-log JSON 的 mu 字段即真实落地页,天然直链。"""
    global _BAIDU_BLOCK_UNTIL

    from lxml import html as lxml_html

    if time.monotonic() < _BAIDU_BLOCK_UNTIL:
        logger.info("baidu 处于安全验证冷却期,跳过本次查询")
        return []
    url = "https://m.baidu.com/s"
    params = {"word": query}
    response = await _fetch_html(
        url,
        params=params,
        extra_headers={"User-Agent": _BAIDU_MOBILE_UA},
    )
    page = response.text
    if "安全验证" in page or "data-log" not in page:
        from . import web_render

        rendered = await web_render.render_html(f"{url}?{urllib.parse.urlencode(params)}")
        if rendered and "data-log" in rendered and "安全验证" not in rendered:
            page = rendered
        elif "安全验证" in page or (rendered and "安全验证" in rendered):
            _BAIDU_BLOCK_UNTIL = time.monotonic() + _BAIDU_BLOCK_COOLDOWN
            logger.warning(
                "baidu 触发安全验证(静态+渲染均未通过),进入 %ds 冷却",
                int(_BAIDU_BLOCK_COOLDOWN),
            )
    tree = lxml_html.fromstring(page)
    rows = _parse_baidu_mobile_results(tree, count)
    return [
        {"title": r["title"], "url": r["href"], "snippet": r["body"], "site": r["site"]}
        for r in rows
    ]


_BAIDU_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


_BAIDU_DATE_LINE = re.compile(r"^(\d+\s*(天|小时|分钟)前|\d{4}[-/年]\d{1,2})")


def _parse_baidu_mobile_results(tree, max_results: int) -> list[dict]:
    """m.baidu.com 结果解析:只认带 data-log 的结果块,mu 字段为真实落地页。
    移动端标题锚常只含图片/图标,取不到文本;渲染后块内文本行结构为
    [标题 1~2 短行][时间行("3天前"/"2026/10/14"开头)][长行正文摘要][站点名],
    故标题=首行起逐行拼接到时间行或满 40 字,正文=时间行后第一条 15+ 字长行。"""
    from lxml import etree

    results: list[dict] = []
    seen: set[str] = set()
    for block in tree.xpath("//div[@data-log]"):
        try:
            data = json.loads(block.get("data-log") or "")
        except ValueError:
            continue
        mu = str(data.get("mu") or "")
        if not mu.startswith(("http://", "https://")) or "recommend_list" in mu:
            continue
        etree.strip_elements(block, "script", "style", with_tail=False)
        lines = [s for s in (x.strip() for x in block.itertext()) if s]
        if not lines:
            continue
        title = ""
        body_start = 0
        for i, line in enumerate(lines):
            if _BAIDU_DATE_LINE.match(line):
                body_start = i + 1
                break
            if i >= 3 or len(title) + len(line) >= 40:
                body_start = i
                break
            title += line
        else:
            body_start = len(lines)
        title = title[:60]
        if len(title) < 4 or _is_baidu_noise_entry(title, mu):
            continue
        body = ""
        for line in lines[body_start:]:
            if len(line) >= 15:
                body = line[:80]
                break
        key = _dedup_key(mu)
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": title, "href": mu, "body": body, "site": ""})
        if len(results) >= max_results:
            break
    return results


async def _search_bing(query: str, freshness: str, count: int) -> list[dict]:
    """抓 cn.bing.com 结果页;/ck/a 跳转已还原为落地页 url。
    freshness 映射为 bing 时间筛选参数(oneDay/oneWeek/oneMonth 生效)。
    first/count 分页参数实测均被忽略(2026-10-14,翻页返回与首页相同结果),单页约 10 条。"""
    from lxml import html as lxml_html

    params = {"q": query, "setlang": "zh-cn"}
    filters = _BING_FRESHNESS_FILTERS.get(freshness)
    if filters:
        params["filters"] = filters
    response = await _fetch_html("https://cn.bing.com/search", params=params)
    tree = lxml_html.fromstring(response.text)
    rows = _parse_bing_results(tree, count)
    return [
        {
            "title": r["title"],
            "url": r["href"],
            "snippet": r["body"],
            "site": "",
        }
        for r in rows
    ]


_PROVIDERS.update(
    {
        "baidu": _search_baidu,
        "bing": _search_bing,
    }
)


# sina 结果标题的关键词高亮标签(<font color='red'>)
_SINA_HIGHLIGHT_PATTERN = re.compile(r"</?font[^>]*>")


async def _search_sina(query: str, freshness: str, count: int) -> list[dict]:
    """新浪搜索 JSON API(search.sina.com.cn/api/search,免 key)。

    定位:新闻内容垂直源(结果均为 sina 系域名,非通用网页搜索)。
    优点是直链无跳转、自带可靠发布时间 ctime(随结果透出 published 字段,
    是各源里唯一可保真的日期);freshness 不做映射(不引入 range 特异化逻辑)。
    非官方公开接口,随时可能改版/限流,按可静默失效的兜底源对待。"""
    async with httpx.AsyncClient(
        timeout=config.WEB_SEARCH_TIMEOUT, headers=_FETCH_HEADERS
    ) as client:
        response = await client.get(
            "https://search.sina.com.cn/api/search",
            params={"q": query, "tp": "news", "page": 1, "size": count},
        )
    if response.status_code != 200:
        raise RuntimeError(f"sina 搜索 HTTP {response.status_code}: {response.text[:200]}")
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"sina 搜索返回错误: {payload.get('message')}")
    results: list[dict] = []
    for item in ((payload.get("data") or {}).get("list") or []):
        title = _normalize_whitespace(
            _SINA_HIGHLIGHT_PATTERN.sub("", str(item.get("title") or ""))
        )
        url = str(item.get("url") or "")
        if not title or not url:
            continue
        entry = {
            "title": title,
            "url": url,
            "snippet": _normalize_whitespace(
                str(item.get("intro") or item.get("searchSummary") or "")
            ),
            "site": str(item.get("media_show") or item.get("author") or ""),
        }
        ctime = item.get("ctime")
        if isinstance(ctime, (int, float)) and ctime > 0:
            entry["published"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(ctime)
            )
        results.append(entry)
    return results


_PROVIDERS["sina"] = _search_sina


# ---------------------------------------------------------------- 垂直栏目直达
# 纯垂直要闻查询(如"今日要闻""国际新闻""社会热点")不经过搜索引擎,
# 直接抓门户栏目数据面,把栏目条目作为搜索结果返回——栏目源天然是权威
# 直链、自带发布时间,且绕开引擎风控(baidu 惩罚期此路仍通)。
# 判定保守:query 去掉时间词/泛词后只剩类目词(或为空)才路由;含任何
# 具体实体词(如"某公司裁员新闻")一律返回 None 走常规引擎。
# 直达失败(接口改版/全空)回退常规引擎,不静默空转。
# 2026-10-15 探针实证:微博热搜需登录不可行;sina roll 仅 lid=1356(国内
# 要闻滚动)新鲜,其余 lid 全是陈年旧闻;百度热榜无落地页(url 为搜索链,
# 定位为线索+desc 摘要源);cctv 无财经/体育栏目,财经查询仍走引擎。

_VERTICAL_FILLER = re.compile(
    r"今天|今日|昨天|昨日|最近|近期|最新|现在|当前|眼下|早上|上午|下午|晚上|"
    r"一下|有什么|有哪些|有啥|帮我|给我|看看|看|瞧|播报|汇总|整理|盘点|回顾|速览|"
    r"请|告诉我|我想|我要|的|了|吧|呢|啊"
)
_VERTICAL_KIND_WORDS = (
    "大新闻", "新闻", "要闻", "热点", "头条", "资讯", "消息", "要点",
    "大事", "动态", "快讯", "时报", "日报",
)

# 类目关键词严格以门户首页实测栏目名为准(2026-10-15 抓 news.cctv.com
# 导航:国内/国际/社会/法治/文娱/科技/生活/教育;无财经/体育/民生栏目),
# 不自拟同义词("民生""互联网"等查询照常走引擎);"娱乐"为新浪真实频道名。
# (类目关键词, 频道 id 列表);类目词按序匹配,命中即从剩余文本中移除
_VERTICAL_CATEGORY_CHANNELS: list[tuple[tuple[str, ...], list[str]]] = [
    (("国内",), ["cctv_china", "sina_roll"]),
    (("国际",), ["cctv_world"]),
    (("社会",), ["cctv_society"]),
    (("法治",), ["cctv_law"]),
    (("文娱", "娱乐"), ["cctv_ent"]),
    (("科技",), ["cctv_tech"]),
    (("生活",), ["cctv_life"]),
    (("教育",), ["cctv_edu"]),
    (("热搜", "热榜", "热搜榜", "热点榜"), ["baidu_hot"]),
]
_VERTICAL_DEFAULT_CHANNELS = ["cctv_china", "sina_roll", "cctv_world"]


def _match_vertical_channels(query: str) -> Optional[list[str]]:
    """纯垂直要闻查询判定:返回频道 id 列表;非纯垂直返回 None。"""
    text = re.sub(r"[\s,，。、!！?？~～…:：;；]+", "", str(query or ""))
    text = _VERTICAL_FILLER.sub("", text)
    for word in _VERTICAL_KIND_WORDS:
        text = text.replace(word, "")
    if not text:
        return list(_VERTICAL_DEFAULT_CHANNELS)
    channels: list[str] = []
    remaining = text
    for keywords, chans in _VERTICAL_CATEGORY_CHANNELS:
        matched = False
        for kw in keywords:
            if kw in remaining:
                remaining = remaining.replace(kw, "")
                matched = True
        if matched:
            channels.extend(chans)
    if remaining:  # 有未识别的具体词 → 非纯垂直
        return None
    return list(dict.fromkeys(channels)) or None


async def _channel_sina_roll(count: int) -> list[dict]:
    """新浪国内要闻滚动 API(feed.sina.com.cn,免 key,lid=1356);
    仅此 lid 实测新鲜(2026-10-15),其余 lid 均为陈年旧闻,不要用。"""
    r = await _download(
        f"https://feed.sina.com.cn/api/roll/get?pageid=121&lid=1356&num={min(count * 2, 50)}&page=1"
    )
    if "error" in r:
        return []
    try:
        payload = json.loads(r["content"].decode(r.get("encoding") or "utf-8", errors="replace"))
    except ValueError:
        return []
    entries: list[dict] = []
    for item in ((payload.get("result") or {}).get("data") or [])[:count]:
        title = _normalize_whitespace(str(item.get("title") or ""))
        url = str(item.get("url") or "")
        if not title or not url.startswith(("http://", "https://")):
            continue
        entry = {
            "title": title,
            "url": url,
            "snippet": _normalize_whitespace(
                str(item.get("intro") or item.get("summary") or "")
            )[:120],
            "site": str(item.get("media_name") or "新浪新闻"),
        }
        ctime = item.get("ctime")
        if isinstance(ctime, (int, float)) and ctime > 0:
            entry["published"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ctime))
        entries.append(entry)
    return entries


async def _channel_baidu_hot(count: int) -> list[dict]:
    """百度热搜榜(top.baidu.com/board,免 key,独立于搜索风控——搜索被
    惩罚时此面仍通,2026-10-15 实测)。条目无落地页:url/rawUrl 均指向
    百度搜索结果页,定位为线索源;desc 自带约 80 字事件摘要可直接用。"""
    r = await _download("https://top.baidu.com/board?tab=realtime")
    if "error" in r:
        return []
    html = r["content"].decode(r.get("encoding") or "utf-8", errors="replace")
    m = re.search(r"<!--s-data:(.*?)-->", html, re.S)
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except ValueError:
        return []
    cards = (payload.get("data") or {}).get("cards") or []
    items = (cards[0].get("content") if cards else []) or []
    entries: list[dict] = []
    for item in items[:count]:
        word = _normalize_whitespace(str(item.get("word") or ""))
        url = str(item.get("rawUrl") or item.get("url") or "")
        if not word or not url.startswith(("http://", "https://")):
            continue
        entries.append(
            {
                "title": word,
                "url": url,
                "snippet": _normalize_whitespace(str(item.get("desc") or ""))[:120],
                "site": "百度热搜",
            }
        )
    return entries


_CHANNEL_FETCHERS = {
    "cctv_china": lambda count: _cctv_channel_entries("china", count),
    "cctv_world": lambda count: _cctv_channel_entries("world", count),
    "cctv_society": lambda count: _cctv_channel_entries("society", count),
    "cctv_law": lambda count: _cctv_channel_entries("law", count),
    "cctv_tech": lambda count: _cctv_channel_entries("tech", count),
    "cctv_ent": lambda count: _cctv_channel_entries("ent", count),
    "cctv_life": lambda count: _cctv_channel_entries("life", count),
    "cctv_edu": lambda count: _cctv_channel_entries("edu", count),
    "sina_roll": _channel_sina_roll,
    "baidu_hot": _channel_baidu_hot,
}


async def _fetch_vertical_channels(channel_ids: list[str], count: int) -> list[dict]:
    """并发抓垂直栏目,按 URL 去重合并,标注 source;栏目源天然相关,
    不过 _refine_results(省一次小 LLM 调用)。"""
    per_channel = max(5, -(-count // max(len(channel_ids), 1)))
    tasks = [
        (cid, _CHANNEL_FETCHERS[cid](per_channel))
        for cid in channel_ids
        if cid in _CHANNEL_FETCHERS
    ]
    settled = await asyncio.gather(*(coro for _, coro in tasks), return_exceptions=True)
    merged: list[dict] = []
    seen: set[str] = set()
    for (cid, _), outcome in zip(tasks, settled):
        if isinstance(outcome, Exception):
            logger.warning("垂直栏目 %s 失败: %s", cid, outcome)
            continue
        if not outcome:
            logger.warning("垂直栏目 %s 返回 0 条", cid)
            continue
        for entry in outcome:
            key = _dedup_key(entry["url"])
            if key in seen:
                continue
            seen.add(key)
            merged.append({**entry, "source": cid})
    return merged[:count]


# ---------------------------------------------------------------- HTML 抽取


def _extract_html_text(html: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup

    # 用 html.parser 而非 lxml:gov.cn 等站点的畸形 HTML 会被 lxml 树构建器
    # 丢掉正文子树(实测 24K 页面只剩 95 字符),html.parser 宽容保留
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    # 主内容提取优先 trafilatura(去导航/页脚/推荐位噪声,回包更瘦);
    # 抽不出内容(薄页/非常规结构)退回 bs4 全文本,保持旧行为下限
    try:
        import trafilatura

        main_text = trafilatura.extract(
            html, include_comments=False, include_tables=True, favor_recall=True
        )
        if main_text and len(main_text.strip()) >= 50:
            return title, main_text.strip()
    except ImportError:
        pass
    except Exception:
        pass
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return title, "\n".join(lines)


def _extract_content_links(html: str, base_url: str, limit: int = 20) -> list[dict]:
    """提取页面的条目链接 [{title, url}]:长锚文本(≥10 字符)且指向具体路径的
    http(s) URL。导航链接锚文本短("国内"/"财经"),条目/头条锚文本长,以此区分。
    门户首页/频道页/热榜聚合页静态抽取的正文常是导航噪声(实测 cctv/澎湃首页
    大半是导航栏与节目单),条目链接才是这类页面真正的价值。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    links: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        text = _normalize_whitespace(anchor.get_text(" ", strip=True))
        if len(text) < 10:
            continue
        href = urljoin(base_url, str(anchor["href"]))
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or parsed.path in ("", "/"):
            continue
        key = _dedup_key(href)
        if key in seen:
            continue
        seen.add(key)
        links.append({"title": text[:60], "url": href})
        if len(links) >= limit:
            break
    return links


# ---------------------------------------------------------------- 站点策略
# 按域名后缀匹配的定制解析注册表(与搜索侧 _PROVIDERS 对称)。每个条目:
#   handler: 完全定制的异步函数 (url, html) -> dict|None(如 cctv 频道页 JSONP)
#   body:    文章页正文容器 CSS 选择器列表,命中则只取该子树文本(瘦身:
#            剔除面包屑/侧栏/推荐位等非关键要素)
#   remove:  抽取前先 decompose 的元素选择器列表
#   skip_links: 瘦身命中的文章页不再附条目链接(该站关联链接是推荐位噪声时用)
# 策略未命中/返回 None/抛异常均回退通用链路。
# 只收录实测通用链路产出不合格的站点(2026-10-15 实测:新华/人民/中新网首页
# 静态 HTML 有 185-241 条长锚文本链接,通用链接提取已够用,无需定制)。

_CCTV_CHANNEL_JSONP = "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/{channel}_1.jsonp"


async def _cctv_channel_entries(channel: str, limit: int = 20) -> list[dict]:
    """央视网栏目 JSONP 数据接口取条目 [{title,url,snippet,published}],
    单页约 80 条;失败/无数据返回空列表(调用方回退)。"""
    r = await _download(_CCTV_CHANNEL_JSONP.format(channel=channel))
    if "error" in r:
        return []
    raw = r["content"].decode(r.get("encoding") or "utf-8", errors="replace")
    m = re.match(r"^\s*\w+\((.*)\)\s*;?\s*$", raw, re.S)
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except ValueError:
        return []
    entries: list[dict] = []
    for item in ((payload.get("data") or {}).get("list") or [])[:limit]:
        title = _normalize_whitespace(str(item.get("title") or ""))
        item_url = str(item.get("url") or "")
        if not title or not item_url.startswith(("http://", "https://")):
            continue
        entry = {
            "title": title,
            "url": item_url,
            "snippet": _normalize_whitespace(str(item.get("brief") or ""))[:120],
            "site": "央视网",
        }
        if item.get("focus_date"):
            entry["published"] = str(item["focus_date"])
        entries.append(entry)
    return entries


async def _strategy_cctv(url: str, html: str) -> Optional[dict]:
    """央视网频道页策略:news.cctv.com/<channel>/ 的列表由 JSONP 数据接口加载,
    静态 HTML 只有首屏 1-2 条;直接调接口取条目(标题/发布时间/直链,单页 80 条)。
    文章页(/YYYY/MM/DD/ARTIxxx.shtml)返回 None,交给通用抽取。"""
    m = re.match(r"^/([a-z_]+)/?$", urlparse(url).path)
    if not m:
        return None
    entries = await _cctv_channel_entries(m.group(1), 20)
    if not entries:
        return None
    links = []
    lines = []
    for entry in entries:
        link = {"title": entry["title"], "url": entry["url"]}
        if entry.get("published"):
            link["published"] = entry["published"]
        links.append(link)
        lines.append(f"{entry['title']}({entry.get('published') or ''})")
    return {"title": "", "text": "\n".join(lines), "links": links}


_SITE_STRATEGIES = {
    "cctv.com": {
        "handler": _strategy_cctv,
        "body": ["#page_body"],  # 文章页正文容器(含来源/编辑署名,剔除面包屑)
    },
    "thepaper.cn": {
        # CSSModule 哈希类名,取语义前缀;cententWrap 是正文(官网拼写如此),
        # content__ 前缀是"查看更多/扫码下载"推荐位
        "body": ["div[class^='cententWrap']"],
        "remove": ["div[class^='content__']"],
    },
    "sina.com.cn": {
        # 文章页通用抽取头尾全是导航/推荐栏;#artibody 覆盖 finance/news 等主站,
        # .article 覆盖 k.sina(看点)自媒体页
        "body": ["#artibody", ".article"],
        # 瘦身命中的文章页不再附条目链接——该站的关联链接全是"相关新闻"推荐位
        "skip_links": True,
    },
}


def _match_site_strategy(url: str) -> Optional[dict]:
    """按域名后缀最长匹配站点策略条目;无命中返回 None。"""
    host = (urlparse(url).hostname or "").lower()
    best = None
    for suffix, entry in _SITE_STRATEGIES.items():
        if host == suffix or host.endswith("." + suffix):
            if best is None or len(suffix) > len(best[0]):
                best = (suffix, entry)
    return best[1] if best else None


def _extract_slim(
    html: str, body_selectors: list[str], remove_selectors: list[str]
) -> Optional[tuple[str, str]]:
    """按站点策略瘦身抽取:先剔除 remove 元素,再只取 body 容器子树文本。
    正文不足 50 字符或选择器未命中返回 None,回退通用抽取。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    for sel in remove_selectors:
        for node in soup.select(sel):
            node.decompose()
    for sel in body_selectors:
        node = soup.select_one(sel)
        if not node:
            continue
        lines = [l.strip() for l in node.get_text("\n").splitlines() if l.strip()]
        text = "\n".join(lines)
        if len(text) >= 50:
            return title, text
    return None


# ---------------------------------------------------------------- 落盘


def _filename_from(url: str, content_disposition: str, mime: str) -> str:
    if content_disposition:
        m = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')?([^;]+)", content_disposition)
        if m:
            return unquote(m.group(1).strip().strip('"'))
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition)
        if m:
            return m.group(1).strip()
    name = Path(unquote(urlparse(url).path)).name
    if name and "." in name:
        return name
    ext = _DEFAULT_EXTENSION_BY_MIME.get(mime, ".bin")
    return (name or "download") + ext


def _is_binary_content(mime: str, url: str) -> bool:
    if mime.startswith("image/"):
        return True
    if mime in _DEFAULT_EXTENSION_BY_MIME:
        return True
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix in _BINARY_EXTENSIONS


# ---------------------------------------------------------------- 工具核心(宿主与 MCP 服务器共用)
# web_search/amap 迁移 MCP 后,模型可见的 web_search 由 MCP 公开 app 提供;
# web_fetch 拆成两半:MCP 内部 app 的 _do_fetch 负责抓取+HTML 抽取(二进制返回标记),
# 宿主侧 wrapper 负责把二进制物化进会话 workspace(需要 chat_id,MCP 拿不到)。


def _dedup_key(url: str) -> str:
    """URL 归一化去重键:去 fragment、末尾斜杠,host 小写。"""
    p = urlparse(url.split("#", 1)[0])
    return f"{p.netloc.lower()}{p.path.rstrip('/')}?{p.query}"


_SEARCH_CACHE: dict[tuple, tuple[float, object]] = {}  # (query, freshness, count) -> (到期 monotonic ts, 结果)
_SEARCH_CACHE_MAX = 32


def _search_cache_get(key: tuple):
    hit = _SEARCH_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    _SEARCH_CACHE.pop(key, None)
    return None


def _search_cache_put(key: tuple, result) -> None:
    # 只缓存 list 结果(错误文案不缓存,故障恢复后应立即可重试)
    if not isinstance(result, list):
        return
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        _SEARCH_CACHE.pop(oldest, None)
    _SEARCH_CACHE[key] = (time.monotonic() + config.WEB_FETCH_CACHE_TTL, result)


def _quality_note(merged: list[dict]) -> Optional[dict]:
    """结果质量信号:过半为域名首页时,给模型一条改策略提示。"""
    if not merged:
        return None
    homepages = sum(
        1 for r in merged if urlparse(str(r.get("url", ""))).path in ("", "/")
    )
    if homepages * 2 <= len(merged):
        return None
    return {
        "note": (
            "本次结果多为门户首页,不是具体内容条目。"
            "不要再用近义词重搜(同一查询 300s 内会命中缓存);建议改用 web_fetch "
            "直接抓取新闻频道/热榜页(如 news.baidu.com、news.cctv.com、"
            "www.thepaper.cn、tophub.today),或用更具体的事件关键词搜索。"
        )
    }


# ---------------------------------------------------------------- LLM 剔除过滤
# 合并后的候选列表过一道小 LLM(仅剔除、不改写):输入只有 query + 候选标题/简介
# (~4KB),不带 URL(筛选不需要)、不带会话历史;输出体积随之降到内联阈值内,
# artifact 回读轮自然消失。

_REFINE_INSTRUCT = (
    "你是搜索结果过滤器。给定搜索目标和候选结果列表(序号. 标题 — 简介),"
    "剔除以下条目:与搜索目标无关的;营销号/标题党;无来源内容;过期旧闻;"
    "非正文页(百科词条/日历/工具/导航页)。"
    "保真优先:拿不准的一律保留,只剔除明确不合格的。只输出要剔除的序号。"
)
_REFINE_OUTPUT_SCHEMA = {"drop": ["int, 要剔除的候选序号(从 0 开始)"]}


async def _refine_results(query: str, merged: list[dict]) -> list[dict]:
    """小 LLM 剔除过滤;任何失败/超时/剔除后不足 MIN_KEEP 条均 fail-open 返回原列表。"""
    min_keep = config.WEB_SEARCH_REFINE_MIN_KEEP
    if not config.WEB_SEARCH_REFINE or len(merged) <= min_keep:
        return merged
    lines = [
        f"{i}. {r.get('title', '')} — {str(r.get('snippet') or '')[:60]}"
        for i, r in enumerate(merged)
    ]
    prompt = f"搜索目标: {query}\n候选结果:\n" + "\n".join(lines)

    from agently import Agently

    agent = (
        Agently.create_agent(name=f"webrefine-{int(time.time() * 1000)}")
        # 剔除是浅判断任务,关闭思维链:隐藏 reasoning 在过滤任务上会生成 800+
        # token(实测挤占输出预算致空回复→校验重试,单发 53s),关闭后 0.6s 返回
        # 且不影响剔除质量(2026-10-15 实测)。参数是端点方言(deepseek 与百炼
        # qwen 互不兼容),经 config.thinking_off_options 按活动端点选择
        .set_settings(
            "plugins.ModelRequester.OpenAICompatible.request_options",
            config.thinking_off_options(),
        )
        .input(prompt)
        .instruct(_REFINE_INSTRUCT)
        .output(_REFINE_OUTPUT_SCHEMA)
    )

    try:
        # 原生异步调用:wait_for 超时可真正取消请求。此前用 to_thread 包同步
        # get_result,线程 cancel 不掉、wait_for 的 cancel-and-wait 反而阻塞到
        # 模型重试退避跑完(2026-10-15 实测四路并发被限流后 search 耗 123-192s)
        data = await asyncio.wait_for(
            agent.async_get_data_object(), timeout=config.WEB_SEARCH_REFINE_TIMEOUT
        )
        drop = {
            int(x)
            for x in (getattr(data, "drop", None) or [])
            if isinstance(x, (int, float)) or str(x).isdigit()
        }
        drop = {i for i in drop if 0 <= i < len(merged)}
        kept = [r for i, r in enumerate(merged) if i not in drop]
        if not drop or len(kept) < min_keep:
            if drop:
                logger.info(
                    "web_search 过滤剔除 %d 条后不足 %d 条,回退原始列表",
                    len(drop), min_keep,
                )
            return merged
        logger.info("web_search 过滤: %d -> %d 条", len(merged), len(kept))
        return kept
    except Exception as e:
        logger.warning("web_search 过滤失败(fail-open 返回原始列表): %s", e)
        return merged


async def _do_search(query: str, time_range: str, max_results: int):
    """web_search 核心:配置的多个源并行查询、按 URL 去重合并;返回结果 list 或错误/空结果文案。

    ≥1 源成功即返回(失败源记日志、空结果源记入尾注);全败才返回错误文案。
    相同 (query, time_range, count) 短 TTL 缓存(WEB_FETCH_CACHE_TTL),命中不重复抓取。
    WEB_SEARCH_REFINE 开时合并结果过小 LLM 剔除过滤(仅剔除,fail-open)。
    """
    if not config.WEB_ENABLED:
        return "错误: 联网搜索未启用(WEB_ENABLED=false),请如实告知用户。"
    count = max(1, min(int(max_results or 10), config.WEB_SEARCH_MAX_RESULTS))
    freshness = _normalize_freshness(time_range)
    cache_key = (str(query or "").strip(), freshness, count)
    cached = _search_cache_get(cache_key)
    if cached is not None:
        return list(cached) + [{"note": "(缓存命中:300s 内相同查询,结果未重新抓取)"}]
    # 纯垂直要闻查询直达门户栏目,不经过搜索引擎;直达全空时回退常规引擎
    vertical_channels = _match_vertical_channels(query)
    if vertical_channels is not None:
        entries = await _fetch_vertical_channels(vertical_channels, count)
        if entries:
            _search_cache_put(cache_key, entries)
            return entries + [
                {"note": f"(垂直栏目直达: {'/'.join(vertical_channels)},未经过搜索引擎)"}
            ]
        logger.warning("垂直栏目直达全部为空,回退常规引擎: %s", vertical_channels)
    provider_names = _provider_names()
    skipped: list[str] = []
    tasks: list[tuple[str, object]] = []
    for name in provider_names:
        adapter = _PROVIDERS.get(name)
        if adapter is None:
            skipped.append(f"{name}: 未知的搜索提供方")
            continue
        if name in _KEY_REQUIRED_PROVIDERS and not config.WEB_SEARCH_API_KEY:
            skipped.append(f"{name}: 未配置 WEB_SEARCH_API_KEY,已跳过")
            continue
        tasks.append((name, adapter(query, freshness, count)))
    if not tasks:
        return f"错误: 搜索失败({'；'.join(skipped)})。请如实告知用户搜索暂不可用,不要编造结果。"

    settled = await asyncio.gather(
        *(coro for _, coro in tasks), return_exceptions=True
    )
    merged: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = list(skipped)
    for (name, _), outcome in zip(tasks, settled):
        if isinstance(outcome, Exception):
            logger.warning("web_search 源 %s 失败: %s", name, outcome)
            errors.append(f"{name}: {outcome}")
            continue
        if not outcome:
            # 空结果同样上报(抓取源被风控/UI 改版时表现为静默空转)
            logger.warning("web_search 源 %s 返回 0 条结果", name)
            errors.append(f"{name}: 0 条结果(可能被风控或页面改版)")
            continue
        for r in outcome:
            key = _dedup_key(str(r.get("url", "")))
            if key in seen:
                continue
            seen.add(key)
            item = {**r, "source": name}
            if isinstance(item.get("snippet"), str) and len(item["snippet"]) > 120:
                # snippet 瘦身:合并结果整体须控制在框架可见阈值内
                item["snippet"] = item["snippet"][:120] + "…"
            merged.append(item)
    if not merged:
        if errors:
            return (
                f"错误: 搜索失败({'；'.join(errors)})。"
                "请如实告知用户搜索暂不可用,不要编造结果。"
            )
        return "未搜索到相关结果,可换关键词重试或如实说明。"
    # 合并上限跟随请求量(≤WEB_SEARCH_MAX_RESULTS)
    merged = merged[: min(max(count, 5), config.WEB_SEARCH_MAX_RESULTS)]
    if errors:
        logger.info("web_search 部分源失败/为空: %s", "；".join(errors))

    merged = await _refine_results(query, merged)

    # 宽检索模式(count>12)在过滤之后把 snippet 截到 60 字:剔除LLM 需要完整简介做判断,
    # 但最终返回只需发现线索(title/snippet 摘要/url/site/source)控制回包体积;
    # 精选页面的正文由 web_fetch 精读获得
    if count > 12:
        for item in merged:
            if isinstance(item.get("snippet"), str) and len(item["snippet"]) > 60:
                item["snippet"] = item["snippet"][:60] + "…"
    _search_cache_put(cache_key, merged)
    note = _quality_note(merged)
    return merged + ([note] if note else [])


async def _do_search_batch(queries: list[str], time_range: str, max_results: int):
    """多角度批量检索:一次调用顺序执行多个 query(不并发——对抓取源更友好,
    baidu 实测并发同域抓取易触发风控),按角度分节返回 [{"query", "results"}]。

    每路复用 _do_search 全链路(缓存/多源合并/LLM 剔除/截断);条数按角度数
    均摊(下限 5),控制合并回包不触发框架外部化阈值;不够由模型后续补充搜索。"""
    queries = [str(q).strip() for q in queries if str(q or "").strip()][:5]
    if not queries:
        return "错误: 查询列表为空。"
    # 条数上限取"角度均摊"与"总量预算"的较小值(下限 4):实测每条约 280B,
    # 总量超 ~24 条回包会突破 8KB 触发框架外部化(artifact 回读白付开销)
    per_query = max(4, min(-(-int(max_results or 30) // len(queries)), 24 // len(queries) or 4))
    sections: list[dict] = []
    for q in queries:
        result = await _do_search(q, time_range, max_results)
        if isinstance(result, str):
            sections.append({"query": q, "error": result})
            continue
        items = [r for r in result if isinstance(r, dict) and "title" in r]
        notes = [r for r in result if isinstance(r, dict) and "note" in r]
        sections.append(
            {"query": q, "results": items[:per_query] + notes}
        )
    return sections


async def _download_once(url: str) -> dict:
    """单次尝试:SSRF 校验 + 容忍重定向 GET + 超限保护。
    成功返回 {url, mime, content_disposition, content};失败返回 {error}(文案即模型所见);
    网络类失败(超时/连接失败)额外带 _network=True 供 _download 做镜像回退判定。"""
    try:
        current = await _validate_url(url)
    except ValueError as e:
        return {"error": f"错误: {e}"}
    try:
        async with httpx.AsyncClient(
            timeout=config.WEB_FETCH_TIMEOUT,
            follow_redirects=False,
            headers=_FETCH_HEADERS,
        ) as client:
            response = None
            pre_read: Optional[bytes] = None
            for _ in range(_MAX_REDIRECTS + 1):
                response = await client.get(current)
                if response.status_code in (301, 302, 303, 307, 308):
                    # 脏 Location(baidu 跳转链常见)先字节层清洗再跳;
                    # Set-Cookie 由 client cookie jar 自动携带
                    location = _sanitize_location_header(
                        response.headers.get("location", ""), current
                    )
                    if not location:
                        break
                    try:
                        current = await _validate_url(location)
                    except ValueError as e:
                        return {"error": f"错误: 重定向目标被拦截: {e}"}
                    continue
                # JS/meta 跳转页(搜索引擎 /link 跳转服务可能返回 200 +
                # window.location.replace,非 302):小 HTML 页面读出目标继续跳,同样过 SSRF
                if response.status_code == 200:
                    ctype = response.headers.get("content-type", "")
                    body = response.content  # 非流式 get,body 已完整在内存
                    if "text/html" in ctype and len(body) <= 4096:
                        target = _extract_js_redirect(body.decode("utf-8", errors="replace"))
                        if target:
                            try:
                                current = await _validate_url(urljoin(current, target))
                            except ValueError as e:
                                return {"error": f"错误: 重定向目标被拦截: {e}"}
                            continue
                        pre_read = body  # 已定稿的小页面,复用避免二次下载
                break
            if response is None:
                return {"error": "错误: 请求未获得响应"}
            if response.status_code != 200:
                if 400 <= response.status_code < 500:
                    # 4xx = 目标不存在/无权限,换端点重试同一目标没有意义
                    return {
                        "error": (
                            f"错误: HTTP {response.status_code}({url})——目标不存在或无权限。"
                            "不要换端点/参数重试同一目标;如实告知用户,必要时给出手动获取方式。"
                        )
                    }
                return {
                    "error": (
                        f"错误: HTTP {response.status_code}({url})——服务端故障,"
                        "可稍后重试或换一个来源。"
                    )
                }
            mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            declared = int(response.headers.get("content-length") or 0)
            if declared > config.WEB_FETCH_MAX_BYTES:
                return {
                    "error": (
                        f"错误: 文件过大({declared / 1024 / 1024:.1f} MB),"
                        f"超过下载上限 {config.WEB_FETCH_MAX_BYTES / 1024 / 1024:.0f} MB,未下载。"
                        "不要重试;建议用户手动下载(浏览器或 git clone 等)。"
                    )
                }
            if pre_read is not None:
                content = pre_read
            else:
                chunks: list[bytes] = []
                total = 0
                truncated_bytes = False
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > config.WEB_FETCH_MAX_BYTES:
                        truncated_bytes = True
                        break
                    chunks.append(chunk)
                if truncated_bytes:
                    return {
                        "error": (
                            f"错误: 文件超过下载上限 {config.WEB_FETCH_MAX_BYTES / 1024 / 1024:.0f} MB,"
                            "已中断下载。不要重试;建议用户手动下载(浏览器或 git clone 等)。"
                        )
                    }
                content = b"".join(chunks)
    except ValueError as e:
        return {"error": f"错误: {e}"}
    except Exception as e:
        logger.warning("web_fetch 失败 %s: %s", url, e)
        # httpx.TimeoutException 的 str() 为空,不能直接把空文案抛给模型
        if isinstance(e, httpx.TimeoutException):
            return {
                "error": (
                    f"错误: 请求超时({config.WEB_FETCH_TIMEOUT}s)({url})。"
                    "若同一域名已连续超时,说明该站点从当前网络不可达——"
                    "不要再换该域名的其他端点重试,如实告知用户并给出手动方案"
                    "(如 git clone / 浏览器下载)。"
                ),
                "_network": True,
            }
        detail = str(e) or type(e).__name__
        if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
            return {
                "error": f"错误: 连接失败: {detail}({url})。该站点从当前网络不可达,不要换端点重试。",
                "_network": True,
            }
        return {"error": f"错误: 抓取失败: {detail}。可换一个来源重试。"}
    return {
        "url": current,
        "mime": mime,
        "content_disposition": response.headers.get("content-disposition") or "",
        "encoding": response.encoding,
        "content": content,
    }


# GitHub 镜像回退(2026-10-13 起):本网络 github.com/codeload.github.com 直连超时
# 而 api.github.com 通(实测,curl/git 同样连不上)——GitHub 系下载直连遇网络类失败时,
# 按 WEB_FETCH_GITHUB_MIRRORS 配置的镜像前缀(如 https://ghproxy.net/)顺序重试。
# 仅网络类失败回退:4xx/SSRF/超限是确定性结果,经镜像也一样。镜像为第三方代理,
# 供应链风险由配置者承担,默认空=不回退。
_GITHUB_HOSTS = {
    "github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
}


def _github_mirror_urls(url: str) -> list[str]:
    host = (urlparse(url).hostname or "").lower()
    if host not in _GITHUB_HOSTS:
        return []
    return [
        (m if m.endswith("/") else m + "/") + url
        for m in config.WEB_FETCH_GITHUB_MIRRORS
    ]


async def _download(url: str) -> dict:
    """抓取入口:直连优先;GitHub 系域名网络类失败时按配置镜像回退。"""
    result = await _download_once(url)
    if "error" not in result or not result.pop("_network", False):
        return result
    mirrors = _github_mirror_urls(url)
    direct_error = str(result["error"])
    for mirror_url in mirrors:
        logger.info("GitHub 直连失败,经镜像重试: %s", mirror_url)
        retried = await _download_once(mirror_url)
        if "error" not in retried:
            return retried
        retried.pop("_network", None)
    if mirrors:
        return {
            "error": direct_error + f"(另已尝试 {len(mirrors)} 个配置的 GitHub 镜像,均失败)"
        }
    return {"error": direct_error}


_FETCH_CACHE: dict[str, tuple[float, object]] = {}  # url -> (到期 monotonic ts, 结果)
_FETCH_CACHE_MAX = 64


def _cache_get(url: str):
    hit = _FETCH_CACHE.get(url)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    _FETCH_CACHE.pop(url, None)
    return None


def _cache_put(url: str, result) -> None:
    # 只缓存成功的 HTML dict 结果;错误文案与二进制标记不缓存(后者由宿主物化)
    if not (isinstance(result, dict) and not result.get("binary")):
        return
    if len(_FETCH_CACHE) >= _FETCH_CACHE_MAX:
        oldest = min(_FETCH_CACHE, key=lambda k: _FETCH_CACHE[k][0])
        _FETCH_CACHE.pop(oldest, None)
    _FETCH_CACHE[url] = (time.monotonic() + config.WEB_FETCH_CACHE_TTL, result)


async def _do_fetch(url: str):
    """web_fetch 核心(MCP 内部 app 侧):抓取 + HTML 正文抽取。

    返回:HTML → {url, title, text, truncated, rendered, note};二进制 → binary 标记
    {binary, url, mime, size}(MCP 无会话 workspace,物化由宿主 wrapper 负责);
    失败 → 错误文案字符串。
    """
    cached = _cache_get(url)
    if cached is not None:
        return {**cached, "note": (cached.get("note") or "") + "(缓存命中)"}
    r = await _download(url)
    if "error" in r:
        return r["error"]
    content, mime, current = r["content"], r["mime"], r["url"]
    if mime.startswith("text/html") or (
        not _is_binary_content(mime, current) and b"<html" in content[:2048].lower()
    ):
        from . import web_render

        html = content.decode(r.get("encoding") or "utf-8", errors="replace")
        # 站点策略(按域名后缀匹配):handler 完全定制(如 cctv 频道页走 JSONP
        # 数据接口);body/remove 选择器做瘦身抽取(剔除面包屑/推荐位等非关键要素)。
        # 策略返回 None/抛异常均回退通用抽取链路
        strategy_result: Optional[dict] = None
        site = _match_site_strategy(current)
        if site and site.get("handler"):
            try:
                strategy_result = await site["handler"](current, html)
            except Exception as e:
                logger.warning("web_fetch 站点策略执行失败(%s),回退通用抽取: %s", current, e)
        strategy_links: list[dict] = []
        slim_hit = False
        if strategy_result:
            title = strategy_result.get("title") or ""
            text = strategy_result.get("text") or ""
            strategy_links = strategy_result.get("links") or []
            rendered_hit = False
        else:
            slim = None
            if site and (site.get("body") or site.get("remove")):
                try:
                    slim = _extract_slim(
                        html, site.get("body") or [], site.get("remove") or []
                    )
                except Exception as e:
                    logger.warning("web_fetch 站点瘦身抽取失败(%s),回退通用抽取: %s", current, e)
            if slim:
                title, text = slim
                slim_hit = True
            else:
                title, text = _extract_html_text(html)
            rendered_hit = False
            thin = (not text) or (len(text) < _MIN_TEXT_CHARS)
            # 渲染兜底:薄内容(JS 页面)或命中强制渲染域名时触发;
            # 渲染结果仅在确实优于静态结果时采用,防渲染出更差的页面
            if thin or web_render.render_forced(current):
                rendered = await web_render.render_html(current)
                if rendered:
                    title2, text2 = _extract_html_text(rendered)
                    if len(text2) > len(text):
                        title, text, rendered_hit = title2, text2, True
                        slim_hit = False
        if not text:
            return f"错误: 页面未抽取到正文内容({current}),可能需要 JS 渲染,请换其他来源。"
        truncated = len(text) > config.WEB_FETCH_MAX_CHARS
        note = ""
        if len(text) < _MIN_TEXT_CHARS:
            note = f"(正文仅 {len(text)} 字符,页面可能需要 JS 渲染,内容可能不完整)"
        if rendered_hit:
            note = (note + " " if note else "") + "(经浏览器渲染提取)"
        if slim_hit:
            note = (note + " " if note else "") + "(站点瘦身抽取)"
        result = {
            "url": current,
            "title": title,
            "text": text[: config.WEB_FETCH_MAX_CHARS],
            "truncated": truncated,
            "rendered": rendered_hit,
            "note": note,
        }
        # 条目链接多的页面(门户首页/频道页/热榜聚合页):正文文本常是导航噪声,
        # 提取条目链接供模型顺藤摸瓜抓具体条目;普通文章页够不到 5 条门槛,不受影响
        if strategy_links:
            links = strategy_links
        elif slim_hit and site and site.get("skip_links"):
            # 瘦身命中的文章页:关联链接是"相关新闻"推荐位噪声(如 sina),不附
            links = []
        else:
            links = _extract_content_links(rendered if rendered_hit else html, current)
        if len(links) >= 5:
            result["links"] = links
            result["note"] = (note + " " if note else "") + (
                "(页面含条目链接列表,具体条目可用 web_fetch 打开对应 URL)"
            )
        _cache_put(url, result)
        return result
    # 纯文本/JSON(如 raw.githubusercontent、api.github.com):按文本返回,不做正文抽取
    if mime.startswith("text/") or mime == "application/json" or mime.endswith("+json"):
        text = content.decode(r.get("encoding") or "utf-8", errors="replace")
        if not text.strip():
            return f"错误: 响应内容为空({current})。"
        truncated = len(text) > config.WEB_FETCH_MAX_CHARS
        result = {
            "url": current,
            "title": "",
            "text": text[: config.WEB_FETCH_MAX_CHARS],
            "truncated": truncated,
            "rendered": False,
            "note": f"(原始 {mime} 内容,未做网页正文抽取)",
        }
        _cache_put(url, result)
        return result
    if not _is_binary_content(mime, current):
        return (
            f"错误: 不支持的内容类型 {mime or '未知'}({current})——"
            "仅支持网页、纯文本/JSON、PDF、Office 文档、图片和 zip 压缩包。"
            "不要重试;如需该文件,建议用户手动下载。"
        )
    return {
        "binary": True,
        "url": current,
        "mime": mime,
        "size": len(content),
    }


async def _do_fetch_many(urls: list[str]):
    """web_fetch 列表形态:并发抓取(信号量限 4),单页失败不影响其他页。"""
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    urls = urls[: config.WEB_FETCH_BATCH_MAX]
    if not urls:
        return "错误: url 列表为空或全部无效。"
    sem = asyncio.Semaphore(4)

    async def _one(u: str):
        async with sem:
            result = await _do_fetch(u)
            if isinstance(result, dict):
                # 批量模式每页只回摘要:总 JSON 须 < 框架 8KB 外部化阈值
                if isinstance(result.get("text"), str) and len(result["text"]) > config.WEB_FETCH_BATCH_CHARS:
                    result = {
                        **result,
                        "text": result["text"][: config.WEB_FETCH_BATCH_CHARS],
                        "truncated": True,
                        "note": (result.get("note") or "") + "(批量摘要,精读请单独抓取该页)",
                    }
                # 索引页条目链接在批量模式下同样瘦身(单页 5 条,精读页可拿全量)
                if isinstance(result.get("links"), list) and len(result["links"]) > 5:
                    result = {**result, "links": result["links"][:5]}
                return {"ok": True, **result}
            return {"url": u, "ok": False, "error": result}

    return await asyncio.gather(*(_one(u) for u in urls))


async def _materialize_binary(chat_id: str, url: str, root: Optional[Path] = None):
    """宿主侧:二进制内容二次 GET 并物化进会话 workspace downloads/(MCP 拿不到 chat_id)。

    与 inbox 附件落盘同理,不受 WORKSPACE_READ_ONLY 约束;无 workspace 时不物化。
    root 显式给定(如调试面板选中的 workspace)时优先于 chat_id 映射。
    """
    r = await _download(url)
    if "error" in r:
        return r["error"]
    content, mime, current = r["content"], r["mime"], r["url"]
    if not _is_binary_content(mime, current):
        return (
            f"错误: 不支持的内容类型 {mime or '未知'}({current})——"
            "仅支持网页、纯文本/JSON、PDF、Office 文档、图片和 zip 压缩包。"
            "不要重试;如需该文件,建议用户手动下载。"
        )
    root = root or workspace_store.root_for_chat(chat_id)
    if not root:
        return (
            f"错误: 该链接是 {mime or '二进制'} 类型文件,需要本会话绑定 workspace 才能保存处理;"
            "当前会话未绑定 workspace,请换网页类链接或先绑定 workspace。"
        )
    downloads = Path(root) / DOWNLOADS_DIR
    downloads.mkdir(parents=True, exist_ok=True)
    name = _filename_from(current, r["content_disposition"], mime)
    saved_as = inbox._dedupe(downloads, inbox._sanitize(name))
    target = downloads / saved_as
    try:
        target.write_bytes(content)
    except OSError as e:
        logger.error("web 下载落盘失败 %s: %s", target, e)
        return f"错误: 文件保存失败: {e}"
    return {
        "saved_as": f"{DOWNLOADS_DIR}/{saved_as}",
        "name": saved_as,
        "size": len(content),
        "mime": mime,
        "note": (
            f"文件已保存到工作区 {DOWNLOADS_DIR}/{saved_as}。"
            "若用户的目的就是下载/获取这个文件,到此即完成:直接在回复中告知"
            "保存路径与大小即可,不要再调用 write_deliverable(它只承载你新生成的"
            "文本内容,无法搬运二进制文件)。若用户要对文件内容做进一步处理,"
            "可用 read_file 等文件工具(仅文本);图片内容当前轮不可直接查看,"
            "PDF/Office 解析需相应 skill。"
        ),
    }


# ---------------------------------------------------------------- Action 工厂


def make_web_actions(chat_id: str, materialize_root: Optional[Path] = None) -> dict:
    """每 turn 调用,返回 {action_id: callable};闭包绑定 chat_id 与调用计数。

    web_search 已迁移 MCP(模型侧由 MCP 公开 app 提供);这里只剩 web_fetch 宿主
    wrapper:HTML 抓取委托 MCP 内部 app(_do_fetch),二进制物化留在宿主。
    materialize_root 显式给定(如调试面板选中的 workspace)时,二进制落盘到该目录,
    否则按 chat_id 映射会话 workspace。
    """
    calls = {"count": 0}

    async def web_fetch(url):
        """Fetch web page(s) by {url}: a single URL string, or a list of URLs for batch concurrent fetching (up to 5). HTML pages return extracted text (truncated); plain text/JSON responses (e.g. raw.githubusercontent.com, api.github.com) are returned as raw text (truncated); PDF/Office/image/zip files are saved into the session workspace downloads/ directory and a file reference is returned. Only http/https public URLs are allowed."""
        batch = [u for u in url if isinstance(u, str)] if isinstance(url, list) else None
        cost = len(batch) if batch else 1
        if calls["count"] + cost > config.WEB_MAX_CALLS_PER_TURN:
            return (
                f"错误: 本轮联网调用次数已用完(上限 {config.WEB_MAX_CALLS_PER_TURN} 次),"
                "请基于已获得的信息回答,或告知用户稍后继续。不要再发起新的抓取。"
            )
        calls["count"] += cost
        from . import mcp_client

        try:
            result = await mcp_client.call_tool(
                "web_fetch", {"url": url}, chat_id=chat_id, internal=True
            )
        except Exception as e:
            logger.warning("web_fetch MCP 调用失败 %s: %s", url, e)
            return f"错误: 抓取服务暂不可用({e})。可稍后重试。"

        async def _materialize_item(item: dict):
            saved = await _materialize_binary(chat_id, item["url"], root=materialize_root)
            if isinstance(saved, dict):
                return {"ok": True, "url": item["url"], **saved}
            return {"url": item["url"], "ok": False, "error": saved}

        if isinstance(result, dict) and result.get("binary"):
            return await _materialize_item(result)
        if isinstance(result, list):
            return [
                await _materialize_item(item)
                if isinstance(item, dict) and item.get("binary")
                else item
                for item in result
            ]
        return result

    return {"web_fetch": web_fetch}
