import os
import re
import csv
import json
import time
import logging
import requests
import warnings
from queue import Queue
from datetime import datetime
from urllib.parse import urlparse, quote
import concurrent.futures
import threading
from typing import Optional, Tuple, Any

# 地域屏蔽诊断模块
from geo_diagnose import diagnose_access_failure

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="😎 %(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

warnings.filterwarnings("ignore", message="Unverified HTTPS request is being made.*")

# chromedriver 路径：在线程池启动前单线程预解析一次，所有 worker 复用同一路径，
# 避免并发调用 webdriver_manager 抢缓存导致 tuple index out of range（与截图竞态同理）。
CHROMEDRIVER_PATH: Optional[str] = None
_DRIVER_PATH_LOCK = threading.Lock()


def _resolve_chromedriver() -> Optional[str]:
    """单线程预解析 chromedriver 路径，结果缓存复用（线程安全）。

    返回解析到的可执行路径；若环境无 selenium/webdriver_manager 则返回 None，
    由调用方静默退回静态检测（与 _fetch_rendered_html 的 try/except 一致）。
    """
    global CHROMEDRIVER_PATH
    if CHROMEDRIVER_PATH is not None:
        return CHROMEDRIVER_PATH
    with _DRIVER_PATH_LOCK:
        if CHROMEDRIVER_PATH is not None:
            return CHROMEDRIVER_PATH
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            CHROMEDRIVER_PATH = ChromeDriverManager().install()
        except Exception as e:  # noqa: BLE001
            logging.info(f"[render] 无法解析 chromedriver 路径，跳过无头渲染兜底: {e}")
            CHROMEDRIVER_PATH = ""  # 标记为已解析但失败，避免反复重试
        return CHROMEDRIVER_PATH or None

# 请求头统一配置
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(check-flink/2.0; +https://github.com/willow-god/check-flink)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "X-Check-Flink": "1.0"
}

RAW_HEADERS = {  # 仅用于获取原始数据，防止接收到Accept-Language等头部导致乱码
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(check-flink/2.0; +https://github.com/willow-god/check-flink)"
    ),
    "X-Check-Flink": "2.0"
}

PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "https://blog.liushen.fun/flink_count.json")  # 默认本地文件
RESULT_FILE = "./result.json"
AUTHOR_URL = os.getenv("AUTHOR_URL", "blog.liushen.fun")  # 作者URL，用于检测反链

# TARGET_LINK 支持 "," 或 "|" 分隔多目标，支持精确/子串匹配
_RAW_TARGET = os.getenv("TARGET_LINK", "").strip()
TARGET_LINK_LIST: list[str] = [
    p.strip() for p in _RAW_TARGET.replace(",", "|").split("|") if p.strip()
]

# 站点豁免：已知可正常访问但被 WAF/CDN 拦截（如 EdgeOne/Cloudflare）
# 从 GitHub Actions（海外 IP）访问会被拦截，但国内用户可正常访问
# 格式：规范化 host（去掉协议、www、末尾斜杠）
FRIENDLY_GEO_HOSTS = {
    "123456l.com",
}

api_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

if AUTHOR_URL:
    logging.info("作者 URL: %s", AUTHOR_URL)
else:
    logging.warning("未提供作者 URL，将跳过友链页面检测")

if TARGET_LINK_LIST:
    logging.info("目标友链过滤: %s（仅检测匹配项，其余保留历史状态）", TARGET_LINK_LIST)


# ============================================================
# 历史匹配鲁棒性：防止因 URL 末尾斜杠/大小写/协议差异丢失 siteshot
# ============================================================
def _norm_url(url: str) -> str:
    """规范化 URL 用于匹配：去协议、去末尾 /、转小写、统一 https→http 等价。

    对匹配来说，协议差异通常是同一站点，不应导致 siteshot 匹配失败。
    末尾有无斜杠也应视为同一 URL。
    """
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r'^https?://', '', u)   # 去协议：http/https 等价
    # 先去掉 query/hash（一般友链首页不带，但保险起见）
    for sep in ('?', '#'):
        if sep in u:
            u = u.split(sep, 1)[0]
    u = u.rstrip('/')                  # 去末尾斜杠（在剥 query/hash 之后）
    return u


def _host_only(url: str) -> str:
    """提取规范化 host（去掉 path），作为最粗粒度兜底键。"""
    return _norm_url(url).split('/', 1)[0] if _norm_url(url) else ""


def _build_history_index(prev_list: list) -> dict:
    """从历史 link_status 构建多重索引，支持多种 fallback 查找。

    返回: dict with keys:
      by_link_exact: {原始 link_str: entry}
      by_link_norm:  {_norm_url(link): entry}
      by_host_norm:  {_host_only(link): [entry, ...]}  # 一个 host 可能多条（子路径）
      by_name_host:  {(name.lower(), host): entry}
    """
    idx = {
        "by_link_exact": {},
        "by_link_norm": {},
        "by_host_norm": {},
        "by_name_host": {},
    }
    for e in prev_list:
        link = e.get("link", "") or ""
        name = (e.get("name", "") or "").strip().lower()
        ne = _norm_url(link)
        ho = _host_only(link)
        if link and link not in idx["by_link_exact"]:
            idx["by_link_exact"][link] = e
        if ne and ne not in idx["by_link_norm"]:
            idx["by_link_norm"][ne] = e
        if ho:
            idx["by_host_norm"].setdefault(ho, []).append(e)
        if name and ho:
            key = (name, ho)
            if key not in idx["by_name_host"]:
                idx["by_name_host"][key] = e
    return idx


def _lookup_history(idx: dict, item_link: str, item_name: str = "") -> dict:
    """按优先级从历史索引中查找匹配条目，找到即返回 entry，找不到返回 {}。"""
    if not item_link:
        return {}
    ne = _norm_url(item_link)
    ho = _host_only(item_link)
    nm = (item_name or "").strip().lower()

    # 1. 精确原始链接（最可靠，不丢失）
    if item_link in idx["by_link_exact"]:
        return idx["by_link_exact"][item_link]
    # 2. 规范化 URL（处理末尾斜杠/协议/大小写）
    if ne and ne in idx["by_link_norm"]:
        return idx["by_link_norm"][ne]
    # 3. name + host（name 改了 link 的小瑕疵时兜底）
    if nm and ho and (nm, ho) in idx["by_name_host"]:
        return idx["by_name_host"][(nm, ho)]
    # 4. 仅 host 兜底：若该 host 下只有 1 条记录，直接用（绝大多数情况是 1:1）
    if ho and len(idx["by_host_norm"].get(ho, [])) == 1:
        return idx["by_host_norm"][ho][0]
    # 5. 仅 host + 规范化 URL 的 host 部分包含：name 子串匹配该 host 下唯一条目
    candidates = idx["by_host_norm"].get(ho, [])
    if nm and len(candidates) == 1:
        only = candidates[0]
        prev_name = (only.get("name", "") or "").strip().lower()
        if prev_name and (nm == prev_name or nm in prev_name or prev_name in nm):
            return only
    return {}


def _cross_fill_siteshot(link_status_out: list, previous_results: dict) -> int:
    """最终兜底：对 link_status_out 中 siteshot 为空的条目，再次用历史索引回填。

    用于处理「本轮前半段 prev_entry 没匹配到」或「非增量模式下新条目 siteshot 为空」
    的情况，防止因为一次 URL 格式小波动永久清空 siteshot。

    返回 回填成功的条目数量（用于日志）。
    """
    hist = previous_results.get("link_status", []) or []
    if not hist:
        return 0
    idx = _build_history_index(hist)
    filled = 0
    for e in link_status_out:
        shot = e.get("siteshot") or ""
        if shot.strip():
            continue  # 已有值，不覆盖
        match = _lookup_history(idx, e.get("link", ""), e.get("name", ""))
        prev_shot = (match.get("siteshot") or "").strip()
        if prev_shot:
            e["siteshot"] = prev_shot
            filled += 1
    return filled


def _target_match(it: dict) -> bool:
    """单条友链是否命中 TARGET_LINK 过滤（任一子串精确匹配/精确匹配）。
    无 TARGET_LINK 时返回 True。
    匹配优先级：精确 link == 目标，精确 name == 目标，才子串包含。
    """
    if not TARGET_LINK_LIST:
        return True
    name = it.get("name", "") or ""
    link = it.get("link", "") or ""
    for t in TARGET_LINK_LIST:
        if not t:
            continue
        if link == t or name == t:
            return True
        if t in name or t in link:
            return True
    return False


def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True, **kwargs):
    """统一封装的 GET 请求函数。
    返回: (response|None, latency_sec)
    额外：在异常情况下把异常对象附加到 response._last_error（外部通过闭包传）
    """
    try:
        start_time = time.time()
        response = session.get(url, headers=headers, timeout=timeout, verify=verify, **kwargs)
        latency = round(time.time() - start_time, 2)
        return response, latency
    except requests.RequestException as e:
        logging.warning(f"[{desc}] 请求失败: {url}，错误如下: \n================================================================\n{e}\n================================================================")
        return None, -1


def load_previous_results():
    if os.path.exists(RESULT_FILE):
        try:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.warning("JSON 解析错误，使用空数据")
    return {}


def save_results(data):
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def is_url(path):
    return urlparse(path).scheme in ("http", "https")


def _page_has_author_link(content, bare, domain_variants, page_url):
    """在给定 HTML 文本中判定是否包含指向作者域名的真实链接。

    同时支持：
      - 普通 <a href>
      - data-url / data-link / data-href 自定义属性
      - 跳转/短链型 href 内嵌的完整 URL（如 /go?url=https://x1anyu.cn、
        /redirect?target=x1anyu.cn），需把内嵌 URL 也抽取出来一并比对主机名
    """
    candidates = []
    for m in re.finditer(
        r'(?:href|data-url|data-link|data-href)\s*=\s*["\']([^"\']+)["\']',
        content,
        re.IGNORECASE,
    ):
        candidates.append(m.group(1))
    # 从候选里再抽取任何内嵌的完整 URL（跳转型），展开后一并判定
    expanded = list(candidates)
    for c in candidates:
        for inner in re.findall(r'https?://([^/?#\s"\'>]+)', c):
            expanded.append('https://' + inner)
        for inner in re.findall(r'(?<!:)//([^/?#\s"\'>]+)', c):
            expanded.append('//' + inner)

    for url in expanded:
        host = re.sub(r'^https?:', '', url.strip()).lstrip('/').split('/')[0].lower()
        if host in domain_variants:
            logging.info(f"友链页面 {page_url} 中找到作者链接: {url}")
            return True
    return False


def _fetch_rendered_html(url):
    """用无头 Chrome 渲染页面并返回渲染后的 HTML。

    用于静态 HTML 抓不到链接的 JS 渲染站点（VitePress / Hexo 部分主题 / SPA）。
    环境无 selenium 或 Chrome 时返回 None（调用方静默退回静态结果）。
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except Exception as e:
        logging.info(f"[render] 未安装 selenium，跳过无头渲染兜底: {e}")
        return None

    # 复用单线程预解析的 chromedriver 路径，绝不在并发 worker 内再调 webdriver_manager
    driver_path = _resolve_chromedriver()
    if not driver_path:
        return None

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.binary_location = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")
    driver = None
    try:
        service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(20)
        driver.set_script_timeout(20)
        driver.get(url)
        # 等待前端框架完成友链列表渲染
        time.sleep(3)
        return driver.page_source
    except Exception as e:
        logging.warning(f"[render] 渲染 {url} 失败，跳过: {e}")
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def check_author_link_in_page(session, linkpage_url):
    """检测友链页面是否包含指向作者的真实链接（<a href>）。

    反链（反向链接）必须是可点击的超链接；仅在页面中以纯文本出现作者域名
    （如脚本、JSON、评论区等）不计为反链，避免误报。

    先以静态 HTML 检测；若未命中，再尝试无头浏览器渲染（应对 JS 渲染站点）。
    """
    if not AUTHOR_URL:
        return False

    response, _ = request_url(session, linkpage_url, headers=RAW_HEADERS, desc="友链页面检测")
    if not response:
        return False

    # 归一化作者域名：去协议、去首尾斜杠、转小写，并兼容 www 前缀
    bare = re.sub(r'^https?://', '', AUTHOR_URL.strip()).strip('/').lower()
    domain_variants = {bare, 'www.' + bare}
    if bare.startswith('www.'):
        domain_variants.add(bare[4:])

    content = response.text

    if _page_has_author_link(content, bare, domain_variants, linkpage_url):
        return True

    # 静态 HTML 未命中：可能是 JS 渲染的 SPA（如 VitePress），用无头浏览器渲染后再查
    rendered = _fetch_rendered_html(linkpage_url)
    if rendered:
        logging.info(f"友链页面 {linkpage_url} 静态检测未命中，尝试无头渲染后复查")
        if _page_has_author_link(rendered, bare, domain_variants, linkpage_url):
            return True

    # 未找到真实链接；若域名仅作为文本出现，单独记录但不计为反链
    if bare in content.lower():
        logging.info(f"友链页面 {linkpage_url} 中仅出现作者URL文本，非真实链接，不计为反链")
    else:
        logging.info(f"友链页面 {linkpage_url} 中未找到作者链接")
    return False


def fetch_origin_data(origin_path):
    logging.info(f"正在读取数据源: {origin_path}")
    try:
        if is_url(origin_path):
            with requests.Session() as session:
                response, _ = request_url(session, origin_path, headers=RAW_HEADERS, desc="数据源")
                content = response.text if response else ""
        else:
            with open(origin_path, "r", encoding="utf-8") as f:
                content = f.read()
    except Exception as e:
        logging.error(f"读取数据失败: {e}")
        return []

    try:
        data = json.loads(content)
        if isinstance(data, dict) and 'link_list' in data:
            logging.info("成功解析 JSON 格式数据")
            return data['link_list']
        elif isinstance(data, list):
            logging.info("成功解析 JSON 数组格式数据")
            return data
    except json.JSONDecodeError:
        pass

    try:
        rows = list(csv.reader(content.splitlines()))
        logging.info("成功解析 CSV 格式数据")
        # 支持新的CSV格式：name, link, linkpage
        result = []
        for row in rows:
            if len(row) >= 2:
                item = {'name': row[0], 'link': row[1]}
                if len(row) >= 3 and row[2].strip():
                    item['linkpage'] = row[2].strip()
                result.append(item)
        return result
    except Exception as e:
        logging.error(f"CSV 解析失败: {e}")
        return []


def check_link(item, session) -> Tuple[dict, float, bool, Optional[Any], Optional[Exception]]:
    """
    检测单个友链。
    返回: (item, latency_sec, has_author_link, last_success_response_or_None, last_exception_or_None)
    latency == -1 表示检测失败；需进一步地域屏蔽判定
    """
    link = item['link']
    has_author_link = False
    last_response: Optional[Any] = None
    last_error: Optional[Exception] = None

    for method, url in [("直接访问", link), ("代理访问", PROXY_URL_TEMPLATE.format(link) if PROXY_URL_TEMPLATE else None)]:
        if not url or not is_url(url):
            logging.warning(f"[{method}] 无效链接: {link}")
            continue
        try:
            response, latency = request_url(session, url, desc=method)
        except Exception as e:
            last_error = e
            response = None
            latency = -1
        if response is not None:
            last_response = response
        if response and response.status_code == 200:
            logging.info(f"[{method}] 成功访问: {link} ，延迟 {latency} 秒")

            # 检测反链：优先检查友链页面，无 linkpage 时回退到首页
            if AUTHOR_URL:
                page_to_check = item.get('linkpage') or link
                has_author_link = check_author_link_in_page(session, page_to_check)

            return item, latency, has_author_link, last_response, last_error
        elif response and response.status_code != 200:
            logging.warning(f"[{method}] 状态码异常: {link} -> {response.status_code}")
        else:
            logging.warning(f"[{method}] 请求失败，Response 无效: {link}")

    api_request_queue.put(item)
    return item, -1, False, last_response, last_error


def handle_api_requests(session) -> list:
    results = []
    while not api_request_queue.empty():
        time.sleep(0.2)
        item = api_request_queue.get()
        link = item['link']
        api_url = f"https://v2.xxapi.cn/api/status?url={quote(link, safe='')}"
        try:
            response, latency = request_url(session, api_url, headers=RAW_HEADERS, desc="API 检查", timeout=30)
        except Exception as e:
            last_error = e
            response = None
            latency = -1
        has_author_link = False

        if response:
            try:
                res_json = response.json()
                if int(res_json.get("code")) == 200 and int(res_json.get("data")) == 200:
                    logging.info(f"[API] 成功访问: {link} ，状态码 200")
                    item['latency'] = latency

                    # 检测反链：优先检查友链页面，无 linkpage 时回退到首页
                    if AUTHOR_URL:
                        page_to_check = item.get('linkpage') or link
                        has_author_link = check_author_link_in_page(session, page_to_check)
                else:
                    logging.warning(f"[API] 状态异常: {link} -> [{res_json.get('code')}, {res_json.get('data')}]")
                    item['latency'] = -1
            except Exception as e:
                logging.error(f"[API] 解析响应失败: {link}，错误: {e}")
                item['latency'] = -1
        else:
            item['latency'] = -1

        results.append((item, item.get('latency', -1), has_author_link, response, None))
    return results


def main():
    session = None
    try:
        session = requests.Session()
        link_list = fetch_origin_data(SOURCE_URL)
        if not link_list:
            logging.error("数据源为空或解析失败")
            return

        previous_results = load_previous_results()

        # 目标过滤：仅检测 TARGET_LINK 匹配的友链，留空则检测全部
        if TARGET_LINK_LIST:
            check_list = [it for it in link_list if _target_match(it)]
            logging.info(f"目标过滤 {TARGET_LINK_LIST}：匹配到 {len(check_list)} 个友链")
            if not check_list:
                logging.error("未匹配到任何友链，请检查 TARGET_LINK 是否正确")
                return
        else:
            check_list = link_list

        # 线程池启动前单线程预解析 chromedriver，避免并发 worker 抢 webdriver_manager 缓存（tuple index out of range）
        _resolve_chromedriver()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # 每个 worker 各自用独立 Session，避免多线程共享同一个 Session（非线程安全）
            results = list(executor.map(lambda item: check_link(item, requests.Session()), check_list))

        # 诊断与补充 API 检查用主 session（单线程调用，安全）
        updated_api_results = handle_api_requests(session)
        # updated_api_results 元素: (item, latency, has_author, last_resp, last_err)
        for updated_item in updated_api_results:
            for idx, (item, latency, has_author, *_rest) in enumerate(results):
                if item['link'] == updated_item[0]['link']:
                    results[idx] = updated_item
                    break

        link_status = []

        # 【修复 siteshot 丢失 #1】预构建历史多键索引，代替原先的单键精确匹配
        _hist_idx = _build_history_index(previous_results.get("link_status", []) or [])

        for entry in results:
            # 兼容5-tuple: (item, latency, has_author_link, last_resp, last_err)
            if len(entry) >= 5:
                item, latency, has_author_link, last_resp, last_err = entry
            elif len(entry) == 4:
                item, latency, has_author_link, last_resp = entry
                last_err = None
            else:
                item, latency, has_author_link = entry
                last_resp, last_err = None, None
            try:
                name = item.get('name', '未知')
                link = item.get('link')
                if not link:
                    logging.warning(f"跳过无效项: {item}")
                    continue

                # 多重 fallback 查找历史，避免末尾斜杠/协议/大小写差异导致 siteshot 匹配失败
                prev_entry = _lookup_history(_hist_idx, link, name)
                prev_fail_count = prev_entry.get("fail_count", 0)
                prev_status = prev_entry.get("status", "unknown")

                # ---- 地域屏蔽二次诊断 & status/fail_count 新规则 ----
                if latency != -1:
                    status = "ok"
                    geo_hint = None
                    fail_count = 0
                else:
                    # 诊断：仅在失败时调用（因为 main 自己是检测的结果
                    try:
                        s, h = diagnose_access_failure(link, last_resp, last_err, session, enable_cn_probe=True)
                    except Exception as de:
                        logging.warning(f"地域诊断异常：{link} -> {de}，降级为 error")
                        s, h = "error", None
                    status = s
                    geo_hint = h

                    if status == "geo_blocked":
                        # 保护性加温：连续 3 次 geo_blocked 第 3 次开始计一次 fail
                        prev_geo_streak = prev_entry.get("geo_streak", 0) + 1
                        if prev_geo_streak >= 3:
                            # 连续3次都 geo → 第4次起才累计一次
                            fail_count = prev_fail_count + 1
                            status_for_count = "error"
                        else:
                            fail_count = prev_fail_count  # 不递增
                    else:
                        prev_geo_streak = 0  # 非 geo → 清0 streak
                        fail_count = prev_fail_count + 1

                # 站点豁免：已知可访问但被 WAF 拦截的站点，强制标记为 ok
                if status != "ok" and _host_only(link) in FRIENDLY_GEO_HOSTS:
                    logging.info(f"[豁免] {link} 在友好站点列表中，强制标记为 ok")
                    status = "ok"
                    geo_hint = None
                    latency = 0
                    fail_count = 0
                    prev_geo_streak = 0

                link_status.append({
                    'name': name,
                    'link': link,
                    'latency': latency,
                    'fail_count': fail_count,
                    'status': status,                 # 新增: ok / geo_blocked / error / unknown
                    'geo_hint': geo_hint,         # 新增: CN-block / response-403-text / cdn-waf / tcp-ok-http-fail / null
                    'geo_streak': prev_geo_streak if status == "geo_blocked" else 0,  # 连续 geo 次数
                    'has_author_link': has_author_link,  # 反链
                    'linkpage': item.get('linkpage', ''),  # 保留linkpage信息
                    'siteshot': prev_entry.get('siteshot', ''),  # 保留历史截图，由 screenshot_runner 更新
                })
            except Exception as e:
                logging.error(f"处理链接时发生错误: {item}, 错误: {e}")

        if TARGET_LINK_LIST:
            # 增量更新：本次只检测了部分友链，其余保留历史状态，按数据源顺序合并
            new_by_link = {e['link']: e for e in link_status}
            # 历史侧也用多键兜底匹配：对不在 new_by_link 中的条目，查找历史 siteshot 时兼容格式
            prev_list = previous_results.get("link_status", []) or []
            prev_hist_idx = _build_history_index(prev_list)
            merged = []
            for it in link_list:
                lk = it['link']
                nm = it.get('name', '')
                if lk in new_by_link:
                    merged.append(new_by_link[lk])
                else:
                    # 用 robust 历史匹配（非精确链接也行），比 prev_by_link 单键匹配更可靠
                    hist_match = _lookup_history(prev_hist_idx, lk, nm) if prev_list else {}
                    if hist_match:
                        hist = dict(hist_match)
                        hist.setdefault("status", hist.get("status", "unknown"))
                        hist.setdefault("geo_hint", None)
                        hist.setdefault("geo_streak", 0)
                        # 来源的 link/name 要以当前数据源的为准（可能改了名/尾斜杠），防止下次再匹配不上
                        hist["link"] = lk
                        if nm:
                            hist["name"] = nm
                        if it.get("linkpage"):
                            hist["linkpage"] = it["linkpage"]
                        merged.append(hist)
            link_status = merged
        else:
            # 【全量模式修复】不再只是简单过滤：同样走 robust 合并，确保
            #  a）即使本轮某个条目没出结果（防御性），也不会丢失历史 siteshot；
            #  b）按数据源顺序输出，前后一致。
            new_by_link = {e['link']: e for e in link_status}
            prev_list = previous_results.get("link_status", []) or []
            prev_hist_idx = _build_history_index(prev_list) if prev_list else None
            merged = []
            for it in link_list:
                lk = it['link']
                if lk in new_by_link:
                    merged.append(new_by_link[lk])
                elif prev_hist_idx is not None:
                    hist_match = _lookup_history(prev_hist_idx, lk, it.get('name', ''))
                    if hist_match:
                        hist = dict(hist_match)
                        hist["link"] = lk
                        if it.get('name'):
                            hist["name"] = it['name']
                        if it.get("linkpage"):
                            hist["linkpage"] = it["linkpage"]
                        # 历史条目补默认值
                        hist.setdefault("latency", -1)
                        hist.setdefault("fail_count", hist.get("fail_count", 0))
                        hist.setdefault("status", hist.get("status", "unknown"))
                        hist.setdefault("has_author_link", False)
                        hist.setdefault("siteshot", hist.get("siteshot", ""))
                        hist.setdefault("geo_hint", None)
                        hist.setdefault("geo_streak", 0)
                        merged.append(hist)
                # 兜底：没 new 没 history 就造一个 unknown 条目，保持数据源完整性
                if len(merged) == 0 or merged[-1].get("link") != lk:
                    merged.append({
                        "name": it.get("name", "未知"),
                        "link": lk,
                        "latency": -1,
                        "fail_count": 0,
                        "status": "unknown",
                        "geo_hint": None,
                        "geo_streak": 0,
                        "has_author_link": False,
                        "linkpage": it.get("linkpage", ""),
                        "siteshot": "",
                    })
            link_status = merged

        # 【修复 siteshot 丢失 #2】最终兜底：所有仍为空 siteshot 的条目，
        # 再用历史索引尝试回填一次（防止前序查找偶发失败导致永久丢失）
        _filled_count = _cross_fill_siteshot(link_status, previous_results)
        if _filled_count:
            logging.info(f"[siteshot 兜底回填] 找回 {_filled_count} 个丢失的截图 URL")

        accessible = sum(1 for x in link_status if x["latency"] != -1)
        has_author_count = sum(1 for x in link_status if x["has_author_link"])
        geo_count = sum(1 for x in link_status if x.get("status") == "geo_blocked")
        total = len(link_status)
        total_count = len(link_status)
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accessible_count": accessible,
            "inaccessible_count": total - accessible,
            "total_count": total,
            "geo_blocked_count": geo_count,   # 新增统计：地域屏蔽数量
            "has_author_link_count": has_author_count,  # 新增统计
            "author_url": AUTHOR_URL,  # 记录使用的作者URL
            "link_status": link_status
        }

        save_results(output)
        logging.info(f"共检查 {total} 个链接，成功 {accessible} 个，失败 {total - accessible} 个，其中地域屏蔽 {geo_count} 个")
        logging.info(f"其中 {has_author_count} 个友链页面包含作者链接")
        logging.info(f"结果已保存至: {RESULT_FILE}")
    except Exception as e:
        logging.exception(f"运行主程序失败: {e}")
    finally:
        if session is not None:
            session.close()

if __name__ == "__main__":
    main()
