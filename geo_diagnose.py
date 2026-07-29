# -*- coding: utf-8 -*-
"""
geo_diagnose.py - 友链访问失败二次诊断：区分"地域屏蔽"与"真实故障"

当 main.py 中直接/代理/API 三重检测均失败时，调用本模块的
diagnose_access_failure() 进行二次判断：
  - 策略 A：HTTP 响应状态码 + 响应体/标题 关键字特征匹配
  - 策略 B：国内公共 HTTP 探测节点交叉验证
  - 策略 C：DNS 解析 + TCP 443 握手健康度辅助判断

返回: (status, geo_hint)
  status   ∈ {"geo_blocked", "error", "unknown"}
  geo_hint ∈ {"CN-block", "response-403-text", "cdn-waf", "tcp-ok-http-fail", None}
"""

import re
import socket
import logging
from typing import Optional, Tuple, List

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger(__name__)

# ---------- 关键字词典 ----------

# 高置信度地域屏蔽词（中文 + 英文）
GEO_KEYWORDS_STRONG = [
    # 中文
    "地区", "地域", "所在地区", "您所在的国家", "所在国家",
    "限制访问", "限制您所在的", "暂不支持您所在",
    "您的地区", "您的ip地区", "ip地区限制", "地区限制",
    "中国大陆", "仅限中国", "仅支持中国", "大陆访问",
    "仅限内地", "内地访问", "港澳台", "境外访问",
    # 英文
    "not available in your region", "not available in your country",
    "available in china", "china mainland only", "mainland china only",
    "blocked country", "blocked region", "geo block", "geo_block",
    "geo-restricted", "geoblocked", "geographic restriction",
    "forbidden by country", "region blocked", "restricted region",
]

# 中置信度（需结合状态码/其它线索）
GEO_KEYWORDS_WEAK = [
    "访问被拒绝", "拒绝访问", "禁止访问", "无权访问", "访问受限",
    "access denied", "forbidden", "403 forbidden", "permission denied",
    "unavailable in your", "unavailable from your",
]

# 常见 WAF / CDN 标识
CDN_WAF_MARKERS = [
    "cloudflare", "cf-ray", "ray id",
    "aliyundun", "aliyun waf", "aegis",
    "tencent cloud waf", "qcloud waf", "tsec",
    "wangsu", "wangsuwaf",
    "qianxin", "360waf", "anquanbao",
]

GEO_STATUS_CODES = {403, 451, 444, 401}

# ---------- 国内探测节点（按优先级）----------
# 均为公共 HTTP ping/可用性检测 API，timeout=8s，失败即跳过
CN_PROBE_APIS: List[str] = [
    # vvhan: ?url=xxx 返回 {code, data:{status_code}}
    "https://api.vvhan.com/api/ping?url={url}",
    # seovx: ?url=xxx
    "https://cdn.seovx.com/api/ping/?url={url}",
    # 备用：使用 uomg 公共 API
    "https://api.uomg.com/api/ping?url={url}",
]

PROBE_TIMEOUT = 8


# ======================================================================
# 策略 A：状态码 + 响应内容特征匹配
# ======================================================================
def _match_status_and_content(
    response: Optional["requests.Response"],
) -> Tuple[bool, Optional[str]]:
    """根据响应状态码 + 响应文本关键字匹配，判断是否疑似地域屏蔽。
    返回: (疑似 geo_blocked?, 命中的 geo_hint 或 None)
    """
    if response is None:
        return False, None

    status = response.status_code
    text: str = ""
    try:
        text = response.text or ""
    except Exception:
        text = ""

    text_lower = text.lower()
    title_lower = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        title_lower = m.group(1).strip().lower()

    # 1) 强特征词命中（不区分大小写，命中 1 个即算）
    strong_hits = [kw for kw in GEO_KEYWORDS_STRONG if kw.lower() in text_lower or kw.lower() in title_lower]
    if strong_hits:
        logger.debug(f"[geo] 强特征词命中: {strong_hits[:3]} (status={status})")
        return True, "response-403-text"

    # 2) 状态码 + 弱特征词组合（需要状态码 ∈ {403,451,444,401} 且命中 ≥1 个弱词）
    if status in GEO_STATUS_CODES:
        weak_hits = [kw for kw in GEO_KEYWORDS_WEAK if kw.lower() in text_lower or kw.lower() in title_lower]
        if weak_hits:
            logger.debug(f"[geo] 状态码{status}+弱特征词命中: {weak_hits[:3]}")
            return True, "response-403-text"
        # 只有状态码 + WAF 标识 → 可能是 WAF 拦截
        waf_hits = [w for w in CDN_WAF_MARKERS if w in text_lower or w in title_lower]
        if waf_hits and status in (403, 401, 444):
            logger.debug(f"[geo] 状态码{status}+WAF标识: {waf_hits[:2]} → 可能是地域WAF")
            return True, "cdn-waf"

    return False, None


# ======================================================================
# 策略 B：国内探测节点交叉验证
# ======================================================================
def _probe_cn_nodes(session: "requests.Session", url: str) -> Tuple[bool, Optional[str]]:
    """调用国内公共 HTTP Ping API 验证。
    只要任一节点返回"目标可达（200 OK）"→ 判定为国内能通、国外不通 = geo_blocked。
    返回: (国内可达?, geo_hint)
    """
    if session is None or requests is None:
        return False, None

    for api_tpl in CN_PROBE_APIS:
        api = api_tpl.format(url=url)
        try:
            resp = session.get(api, timeout=PROBE_TIMEOUT, verify=False)
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            # 兼容多种返回格式：
            # vvhan: { code:200, data:{ status_code:200, ... } }
            # 其它: { code:200, data:200 } / { code:200, status:"ok", http_code:200 }
            code = data.get("code")
            inner = data.get("data")
            reachable = False
            if isinstance(inner, dict):
                sc = inner.get("status_code") or inner.get("http_code") or inner.get("status")
                reachable = isinstance(sc, int) and 200 <= sc < 400
            elif isinstance(inner, int):
                reachable = 200 <= inner < 400
            else:
                # 兜底：整体 code == 200 且存在任何非失败成功字段
                reachable = (code == 200) and str(data).lower().find("fail") == -1 and str(data).lower().find("down") == -1
            if reachable:
                logger.info(f"[geo] 国内探测可达: {api[:60]}... → 判定为地域屏蔽")
                return True, "CN-block"
        except Exception as e:
            logger.debug(f"[geo] 国内探测失败 {api[:40]}: {e}")
            continue
    return False, None


# ======================================================================
# 策略 C：DNS + TCP 443 握手健康度
# ======================================================================
def _tcp_handshake_ok(url: str) -> bool:
    """检查能否解析域名并成功建立 TCP 连接到 443 端口。
    若 TCP 可建连但 HTTP 层异常 → 服务器本身在线，更可能是 WAF/地域屏蔽。
    """
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname
        if not host:
            return False
        port = urlparse(url).port or 443
        # 先 DNS
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        if not infos:
            return False
        # 尝试第一个地址的 TCP 建连（超时 3s，不阻塞主流程）
        af, socktype, proto, _, sa = infos[0]
        with socket.socket(af, socktype, proto) as s:
            s.settimeout(3.0)
            s.connect(sa)
        return True
    except Exception:
        return False


# ======================================================================
# 对外统一入口
# ======================================================================
def diagnose_access_failure(
    url: str,
    last_response: Optional["requests.Response"],
    last_error: Optional[Exception],
    session: Optional["requests.Session"] = None,
    enable_cn_probe: bool = True,
) -> Tuple[str, Optional[str]]:
    """访问失败后的统一诊断入口。

    Args:
        url:            目标站点 URL
        last_response:  三重检测中"最后一次有响应对象"的请求结果（若有）
        last_error:     最后一次抛出的异常（若有）
        session:        共享 requests.Session（用于策略 B）
        enable_cn_probe: 是否启用国内节点探测（可能有 8s 延迟）

    Returns:
        (status, geo_hint)
    """
    # ---- A. 响应内容特征 ----
    a_hit, a_hint = _match_status_and_content(last_response)
    if a_hit:
        return "geo_blocked", a_hint

    # ---- B. 国内节点探测（成本较高，仅在 session 存在时做）----
    if enable_cn_probe and session is not None:
        try:
            b_hit, b_hint = _probe_cn_nodes(session, url)
            if b_hit:
                return "geo_blocked", b_hint
        except Exception as e:
            logger.debug(f"[geo] 国内探测节点异常: {e}")

    # ---- C. TCP 握手 + HTTP 状态组合判断 ----
    tcp_ok = _tcp_handshake_ok(url)
    status_is_4xx = False
    if last_response is not None and 400 <= last_response.status_code < 500:
        status_is_4xx = True
    if tcp_ok and (status_is_4xx or last_error is not None):
        # TCP 能连上但 HTTP 异常 → 服务器活着但拦请求 → 高可能地域/WAF
        logger.debug(f"[geo] TCP可建连但HTTP异常(status={getattr(last_response,'status_code',None)}) → 视为疑似geo")
        return "geo_blocked", "tcp-ok-http-fail"

    # 没有任何特征 → 当作常规错误
    return "error", None


if __name__ == "__main__":  # pragma: no cover
    # 手动测试: python geo_diagnose.py <url>
    import sys, time
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else "https://123456l.com/"
    if requests:
        s = requests.Session()
        try:
            r = s.get(target, timeout=10, verify=False)
            err = None
        except Exception as e:
            r = None
            err = e
        t0 = time.time()
        st, hint = diagnose_access_failure(target, r, err, s, enable_cn_probe=True)
        print(f"[result] url={target} status={st} hint={hint} cost={time.time()-t0:.2f}s")
    else:
        print("requests 未安装")
