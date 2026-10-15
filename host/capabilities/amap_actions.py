"""高德地图能力:amap_weather / amap_route / amap_poi Action(渠道中立,与 web_actions.py 同级)。

- 数据源为高德 Web 服务 API(restapi.amap.com),必须配 AMAP_API_KEY;
  缺 key 按未启用处理(与 WEB_ENABLED 缺 key 同款哲学),validate 记 warning。
- amap_weather:实况(extensions=base)/ 未来 3 天预报(all),city 直接传城市名或 adcode。
- amap_route:驾车/步行/公交路径规划;起终点支持"lng,lat"坐标或地名(内部先 geocode);
  返回距离/时长/分段指引摘要 + uri.amap.com 导航链接(用户点开可直接唤起高德导航)。
- amap_poi:城市内 POI 检索(吃饭/加油/景点等"附近有什么"类问题)。
- 护栏与 web_actions 对齐:单 turn 调用次数限额、超时、输出截断;
  所有可预期失败返回结构化错误字符串由模型如实转述,不抛异常打断 Action 回路。
"""
import logging
import re
from typing import Optional

import httpx

from .. import config

logger = logging.getLogger(__name__)

_BASE = "https://restapi.amap.com"
_COORD_RE = re.compile(r"^\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*$")
_MAX_STEPS = 8  # 路径分段指引摘要上限,防超长回包挤占上下文
_MAX_POIS = 5


def amap_enabled() -> bool:
    """总开关开且已配 key(Web 服务 API 无 key 全不可用)。"""
    return config.AMAP_ENABLED and bool(config.AMAP_API_KEY)


async def _get(path: str, params: dict) -> dict:
    """共享 GET;高德业务错误(status!=1)也抛 RuntimeError 由调用方归一化。"""
    params = {**params, "key": config.AMAP_API_KEY, "output": "json"}
    async with httpx.AsyncClient(timeout=config.AMAP_TIMEOUT) as client:
        resp = await client.get(f"{_BASE}{path}", params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:150]}")
    data = resp.json()
    if str(data.get("status")) != "1":
        raise RuntimeError(
            f"高德返回错误: {data.get('info')}({data.get('infocode')})"
        )
    return data


async def _geocode(address: str, city: str = "") -> tuple[str, str]:
    """地名/地址 -> (lng,lat 坐标, 格式化地址);失败抛 RuntimeError。"""
    params: dict = {"address": address}
    if city:
        params["city"] = city
    data = await _get("/v3/geocode/geo", params)
    geocodes = data.get("geocodes") or []
    if not geocodes:
        raise RuntimeError(f"无法解析地址: {address}")
    return geocodes[0]["location"], geocodes[0].get("formatted_address") or address


async def _resolve_point(text: str, city: str = "") -> tuple[str, str]:
    """入参已是 lng,lat 直接用,否则 geocode;返回 (坐标, 展示名)。"""
    if _COORD_RE.match(text or ""):
        return text.strip(), text.strip()
    return await _geocode(text, city)


def _fmt_duration(seconds) -> str:
    minutes = int(float(seconds or 0)) // 60
    if minutes >= 60:
        return f"{minutes // 60}小时{minutes % 60}分钟"
    return f"{minutes}分钟"


def _nav_link(origin: str, origin_name: str, dest: str, dest_name: str, mode: str) -> str:
    """uri.amap.com 导航直链:用户点开可选择唤起高德 App 开始实时导航。"""
    from urllib.parse import quote

    car = "car" if mode == "driving" else ("walk" if mode == "walking" else "bus")
    return (
        "https://uri.amap.com/navigation?"
        f"from={origin},{quote(origin_name)}&to={dest},{quote(dest_name)}"
        f"&mode={car}&coordinate=gaode&callnative=1"
    )


# ---------------------------------------------------------------- 工具核心(MCP 服务器直接调用)
# 计数护栏在 MCP 服务器中间件(按 chat 滑动窗口);key 缺失在核心内返回错误文案,
# 不再依赖宿主侧挂载门控。


def _no_key():
    return "错误: 未配置 AMAP_API_KEY,高德服务不可用。请如实告知用户。"


async def _weather(city: str, forecast: bool = False):
    if not config.AMAP_API_KEY:
        return _no_key()
    try:
        data = await _get(
            "/v3/weather/weatherInfo",
            {"city": city, "extensions": "all" if forecast else "base"},
        )
    except Exception as e:
        logger.warning("amap_weather 失败 %s: %s", city, e)
        return f"天气查询失败: {e}。可换 adcode 重试,或如实告知用户暂不可用。"
    if not forecast:
        lives = data.get("lives") or []
        if not lives:
            return f"未查询到 {city} 的实时天气,请确认城市名是否正确。"
        live = lives[0]
        return {
            "city": f"{live.get('province')}{live.get('city')}",
            "reporttime": live.get("reporttime"),
            "weather": live.get("weather"),
            "temperature": f"{live.get('temperature')}°C",
            "wind": f"{live.get('winddirection')}风 {live.get('windpower')}级",
            "humidity": f"{live.get('humidity')}%",
        }
    forecasts = data.get("forecasts") or []
    casts = (forecasts[0].get("casts") if forecasts else []) or []
    if not casts:
        return f"未查询到 {city} 的天气预报,请确认城市名是否正确。"
    return {
        "city": f"{forecasts[0].get('province')}{forecasts[0].get('city')}",
        "reporttime": forecasts[0].get("reporttime"),
        "casts": [
            {
                "date": c.get("date"),
                "day": f"{c.get('dayweather')} {c.get('daytemp')}°C",
                "night": f"{c.get('nightweather')} {c.get('nighttemp')}°C",
                "wind": f"{c.get('daywind')}风 {c.get('daypower')}级",
            }
            for c in casts
        ],
    }


async def _route(origin: str, destination: str, mode: str = "driving", city: str = ""):
    if not config.AMAP_API_KEY:
        return _no_key()
    mode = (mode or "driving").strip().lower()
    if mode not in ("driving", "walking", "transit"):
        return f"错误: 不支持的方式 {mode},仅支持 driving / walking / transit。"
    if mode == "transit" and not city:
        return "错误: 公交(transit)路径规划必须提供 city 参数(起点所在城市名)。"
    try:
        o_lnglat, o_name = await _resolve_point(origin, city)
        d_lnglat, d_name = await _resolve_point(destination, city)
    except Exception as e:
        return f"地址解析失败: {e}。可换更具体的地址或坐标重试。"
    try:
        if mode == "transit":
            data = await _get(
                "/v3/direction/transit/integrated",
                {"origin": o_lnglat, "destination": d_lnglat, "city": city},
            )
            transits = (data.get("route") or {}).get("transits") or []
            if not transits:
                return f"未找到 {o_name} 到 {d_name} 的公交方案。"
            plan = transits[0]
            segments = []
            for seg in (plan.get("segments") or [])[:_MAX_STEPS]:
                for line in (seg.get("bus") or {}).get("buslines") or [{}][:1]:
                    if line.get("name"):
                        segments.append(
                            f"乘坐{line['name']}({line.get('departure_stop', {}).get('name', '?')}上"
                            f"→{line.get('arrival_stop', {}).get('name', '?')}下)"
                        )
                if seg.get("walking") and seg["walking"].get("distance"):
                    segments.append(f"步行{seg['walking']['distance']}米")
            summary = {
                "origin": o_name,
                "destination": d_name,
                "mode": mode,
                "duration": _fmt_duration(plan.get("duration")),
                "walking_distance": f"{plan.get('walking_distance', '?')}米",
                "segments": segments or ["(无分段详情)"],
            }
        else:
            path = "/v3/direction/driving" if mode == "driving" else "/v3/direction/walking"
            data = await _get(path, {"origin": o_lnglat, "destination": d_lnglat})
            paths = (data.get("route") or {}).get("paths") or []
            if not paths:
                return f"未找到 {o_name} 到 {d_name} 的路线。"
            best = paths[0]
            steps = [
                s.get("instruction", "")
                for s in (best.get("steps") or [])[:_MAX_STEPS]
                if s.get("instruction")
            ]
            summary = {
                "origin": o_name,
                "destination": d_name,
                "mode": mode,
                "distance": f"{int(best.get('distance') or 0) / 1000:.1f}公里",
                "duration": _fmt_duration(best.get("duration")),
                "steps": steps or ["(无分段详情)"],
            }
        summary["navigation_link"] = _nav_link(o_lnglat, o_name, d_lnglat, d_name, mode)
        summary["note"] = "navigation_link 可直接发给用户,点开可唤起高德 App 实时导航。"
        return summary
    except Exception as e:
        logger.warning("amap_route 失败 %s->%s: %s", origin, destination, e)
        return f"路径规划失败: {e}。可换地址或方式重试,或如实告知用户暂不可用。"


async def _poi(keywords: str, city: str = ""):
    if not config.AMAP_API_KEY:
        return _no_key()
    params: dict = {"keywords": keywords, "citylimit": "true" if city else "false"}
    if city:
        params["city"] = city
    try:
        data = await _get("/v3/place/text", params)
    except Exception as e:
        logger.warning("amap_poi 失败 %s: %s", keywords, e)
        return f"POI 检索失败: {e}。可换关键词重试,或如实告知用户暂不可用。"
    pois = data.get("pois") or []
    if not pois:
        return f"未找到与「{keywords}」相关的地点,可换关键词或放宽城市限制重试。"
    return [
        {
            "name": p.get("name"),
            "type": p.get("type"),
            "address": p.get("address") if isinstance(p.get("address"), str) else "",
            "location": p.get("location"),
        }
        for p in pois[:_MAX_POIS]
    ]


# ---------------------------------------------------------------- Action 工厂


def make_amap_actions() -> dict:
    """每 turn 调用,返回 {action_id: callable};闭包绑定调用计数(与 web_actions 同款)。

    主链路已迁移 MCP(chat_pipeline 不再使用);保留作离线/兜底直调入口。
    """
    calls = {"count": 0}

    def _over_limit() -> Optional[str]:
        if calls["count"] >= config.AMAP_MAX_CALLS_PER_TURN:
            return (
                f"本轮高德调用次数已用完(上限 {config.AMAP_MAX_CALLS_PER_TURN} 次),"
                "请基于已获得的信息回答,或告知用户稍后继续。"
            )
        calls["count"] += 1
        return None

    async def amap_weather(city: str, forecast: bool = False):
        """Query real-time weather or a 3-day forecast for a Chinese city {city} (city name like "北京"/"杭州" or adcode). Set {forecast} true to get the 3-day forecast instead of current conditions."""
        if err := _over_limit():
            return err
        return await _weather(city, forecast)

    async def amap_route(origin: str, destination: str, mode: str = "driving", city: str = ""):
        """Plan a route between {origin} and {destination}; each accepts a place name/address (geocoded internally) or "lng,lat" coordinates. {mode}: driving | walking | transit (transit needs {city}, e.g. "杭州"). Returns distance, duration, step-by-step guidance summary, and a navigation link the user can open to start live navigation in the Amap app."""
        if err := _over_limit():
            return err
        return await _route(origin, destination, mode, city)

    async def amap_poi(keywords: str, city: str = ""):
        """Search POIs (restaurants, gas stations, attractions, etc.) by {keywords}, optionally restricted to {city}. Returns up to 5 results with name, type, address and coordinates."""
        if err := _over_limit():
            return err
        return await _poi(keywords, city)

    return {"amap_weather": amap_weather, "amap_route": amap_route, "amap_poi": amap_poi}
