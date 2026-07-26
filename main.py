import os
import csv
import json
import time
import logging
import requests
import warnings
from queue import Queue
from datetime import datetime
from urllib.parse import urlparse
import concurrent.futures

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="😎 %(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

warnings.filterwarnings("ignore", message="Unverified HTTPS request is being made.*")

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
api_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

if AUTHOR_URL:
    logging.info("作者 URL: %s", AUTHOR_URL)
else:
    logging.warning("未提供作者 URL，将跳过友链页面检测")

def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True, **kwargs):
    """统一封装的 GET 请求函数"""
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

def check_author_link_in_page(session, linkpage_url):
    """检测友链页面是否包含作者链接"""
    if not AUTHOR_URL:
        return False
    
    response, _ = request_url(session, linkpage_url, headers=RAW_HEADERS, desc="友链页面检测")
    if not response:
        return False
    
    # 处理作者URL，确保有协议号
    author_url = AUTHOR_URL
    if not author_url.startswith(('http://', 'https://')):
        author_url = 'https://' + author_url
    
    # 生成各种可能的URL变体
    author_variants = [
        author_url,
        author_url.replace('https://', 'http://'),
        author_url.replace('https://', '//'),
        author_url.replace('https://', ''),
        AUTHOR_URL,  # 原始值（可能没有协议号）
        '//' + AUTHOR_URL,
        'https://' + AUTHOR_URL,
        'http://' + AUTHOR_URL
    ]
    
    # 去重
    author_variants = list(set(author_variants))
    
    content = response.text
    found_in_href = False
    found_as_text = False
    
    # 检查每种变体
    for variant in author_variants:
        # 检查是否在href属性中
        if f'href="{variant}"' in content or \
           f"href='{variant}'" in content or \
           f'href="{variant}/"' in content or \
           f"href='{variant}/'" in content:
            found_in_href = True
            break
        
        # 检查是否作为文本出现
        if variant in content:
            found_as_text = True
    
    if found_in_href:
        logging.info(f"友链页面 {linkpage_url} 中找到作者链接: {author_url}")
        return True
    elif found_as_text:
        logging.info(f"友链页面 {linkpage_url} 中包含作者URL文本但非链接")
        return True
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

def check_link(item, session):
    link = item['link']
    has_author_link = False
    
    for method, url in [("直接访问", link), ("代理访问", PROXY_URL_TEMPLATE.format(link) if PROXY_URL_TEMPLATE else None)]:
        if not url or not is_url(url):
            logging.warning(f"[{method}] 无效链接: {link}")
            continue
        response, latency = request_url(session, url, desc=method)
        if response and response.status_code == 200:
            logging.info(f"[{method}] 成功访问: {link} ，延迟 {latency} 秒")
            
            # 检测反链：优先检查友链页面，无 linkpage 时回退到首页
            if AUTHOR_URL:
                page_to_check = item.get('linkpage') or link
                has_author_link = check_author_link_in_page(session, page_to_check)
            
            return item, latency, has_author_link
        elif response and response.status_code != 200:
            logging.warning(f"[{method}] 状态码异常: {link} -> {response.status_code}")
        else:
            logging.warning(f"[{method}] 请求失败，Response 无效: {link}")

    api_request_queue.put(item)
    return item, -1, False

def handle_api_requests(session):
    results = []
    while not api_request_queue.empty():
        time.sleep(0.2)
        item = api_request_queue.get()
        link = item['link']
        api_url = f"https://v2.xxapi.cn/api/status?url={link}"
        response, latency = request_url(session, api_url, headers=RAW_HEADERS, desc="API 检查", timeout=30)
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
        
        results.append((item, item.get('latency', -1), has_author_link))
    return results

def main():
    try:
        link_list = fetch_origin_data(SOURCE_URL)
        if not link_list:
            logging.error("数据源为空或解析失败")
            return

        previous_results = load_previous_results()

        with requests.Session() as session:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(lambda item: check_link(item, session), link_list))

            updated_api_results = handle_api_requests(session)
            for updated_item in updated_api_results:
                for idx, (item, latency, has_author) in enumerate(results):
                    if item['link'] == updated_item[0]['link']:
                        results[idx] = updated_item
                        break

        current_links = {item['link'] for item in link_list}
        link_status = []

        for item, latency, has_author_link in results:
            try:
                name = item.get('name', '未知')
                link = item.get('link')
                if not link:
                    logging.warning(f"跳过无效项: {item}")
                    continue

                prev_entry = next((x for x in previous_results.get("link_status", []) if x.get("link") == link), {})
                prev_fail_count = prev_entry.get("fail_count", 0)
                fail_count = prev_fail_count + 1 if latency == -1 else 0

                link_status.append({
                    'name': name,
                    'link': link,
                    'latency': latency,
                    'fail_count': fail_count,
                    'has_author_link': has_author_link,  # 新增字段
                    'linkpage': item.get('linkpage', ''),  # 保留linkpage信息
                    'siteshot': prev_entry.get('siteshot', ''),  # 保留历史截图，由 screenshot_runner 更新
                })
            except Exception as e:
                logging.error(f"处理链接时发生错误: {item}, 错误: {e}")

        link_status = [entry for entry in link_status if entry["link"] in current_links]

        accessible = sum(1 for x in link_status if x["latency"] != -1)
        has_author_count = sum(1 for x in link_status if x["has_author_link"])
        total = len(link_status)
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accessible_count": accessible,
            "inaccessible_count": total - accessible,
            "total_count": total,
            "has_author_link_count": has_author_count,  # 新增统计
            "author_url": AUTHOR_URL,  # 记录使用的作者URL
            "link_status": link_status
        }

        save_results(output)
        logging.info(f"共检查 {total} 个链接，成功 {accessible} 个，失败 {total - accessible} 个")
        logging.info(f"其中 {has_author_count} 个友链页面包含作者链接")
        logging.info(f"结果已保存至: {RESULT_FILE}")
    except Exception as e:
        logging.exception(f"运行主程序失败: {e}")

if __name__ == "__main__":
    main()
