# -*- coding: utf-8 -*-
"""
screenshot_runner.py - 截图独立运行入口
由 GitHub Action 每 6 天调用一次

流程：
1. 读取 check_links job 生成的 result.json
2. 提取所有 latency > 0（可达）的友链
3. 对每个友链调用 screenshot.py:take_screenshot()
4. 把 siteshot 字段写回 result.json
5. 复用 result.json 推送到 page 分支
"""

import os
import json
import logging
import concurrent.futures
from urllib.parse import urlparse

from screenshot import take_screenshot, resolve_driver_path

logging.basicConfig(
    level=logging.INFO,
    format="😎 %(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

RESULT_FILE = "./result.json"
# 浏览器并发：Chrome 进程吃内存，不开太高
SCREENSHOT_WORKERS = int(os.getenv("SCREENSHOT_WORKERS", "2"))
# 指定只截图某个/某些友链（名称/URL子串匹配），留空=截图全部可达友链
# 支持逗号 "," 或竖线 "|" 分隔多个目标
_RAW_TARGET = os.getenv("TARGET_LINK", "").strip()
TARGET_LINK_LIST: list[str] = [
    p.strip() for p in _RAW_TARGET.replace(",", "|").split("|") if p.strip()
]


def host_from_url(url: str) -> str:
    """从 URL 提取 host，失败时返回 'unknown'"""
    try:
        h = urlparse(url).hostname or ""
        return h if h else "unknown"
    except Exception:
        return "unknown"


def main():
    if not os.path.exists(RESULT_FILE):
        logger.error(f"找不到 {RESULT_FILE}，请先运行 check_links job")
        return

    with open(RESULT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    link_status = data.get("link_status", [])
    if not link_status:
        logger.warning("link_status 为空，跳过截图")
        return

    # 只对可达友链截图
    targets = [it for it in link_status if it.get("latency", -1) > 0]
    if TARGET_LINK_LIST:
        targets = [
            it for it in targets
            if any(t in it.get("name", "") or t in it.get("link", "") for t in TARGET_LINK_LIST)
        ]
        logger.info(f"目标过滤 '{_RAW_TARGET}'：匹配到 {len(targets)} 个可达友链（目标: {TARGET_LINK_LIST}）")
        if not targets:
            logger.error("未匹配到任何可达友链，请检查 TARGET_LINK（或该友链不可达）")
            return
    logger.info(f"开始截图，共 {len(targets)} 个可达友链，并发数 {SCREENSHOT_WORKERS}")

    # 先把 chromedriver 单独装好，再开线程池。
    # 之前每个线程各装各的，驱动没缓存时会抢缓存目录、
    # 崩在 tuple index out of range，导致个别友链截图失败掉回 thum.io。
    try:
        driver_path = resolve_driver_path()
        logger.info(f"chromedriver 路径已预解析：{driver_path}")
    except Exception as e:
        logger.warning(f"[driver] 预解析失败，worker 将各自回退解析：{e}")
        driver_path = None

    success = 0
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCREENSHOT_WORKERS) as executor:
        future_to_item = {
            executor.submit(
                take_screenshot, item["link"], host_from_url(item["link"]), driver_path
            ): item
            for item in targets
        }
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                siteshot_url = future.result()
                item["siteshot"] = siteshot_url
                if "thum.io" in siteshot_url:
                    failed += 1
                    logger.warning(f"⚠️ {item.get('name', item['link'])} → thum.io 兜底")
                else:
                    success += 1
                    logger.info(f"✅ {item.get('name', item['link'])} → {siteshot_url}")
            except Exception as e:
                failed += 1
                logger.error(f"❌ {item.get('name', item['link'])}: {e}")
                item["siteshot"] = f"https://image.thum.io/get/width/1280/crop/800/png/{item['link']}"

    # 写回 result.json
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    logger.info(f"截图完成：成功 {success} 个，兜底/失败 {failed} 个")
    logger.info(f"结果已写回 {RESULT_FILE}")


if __name__ == "__main__":
    main()
