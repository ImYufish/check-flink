# -*- coding: utf-8 -*-
"""
screenshot.py - 友链主页截图 + 上传图床
移植自 thun888/Python-WebSite-Screenshot，适配 fqzlr 友链系统

环境变量：
- IMG_UPLOAD_URL：图床上传端点（默认 https://tu.fqzlr.com/upload）
- IMG_AUTH_CODE：上传认证码（可选）
- IMG_UPLOAD_FOLDER：上传目录（默认 youlian）
"""

import os
import re
import io
import json
import time
import logging
import requests
from urllib.parse import urlparse
from typing import Optional

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())

# 视窗大小（与 thun888 一致，桌面端展示效果佳）
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800
PAGE_LOAD_WAIT = 3  # 页面渲染等待时间（秒）

# 图床配置
IMG_UPLOAD_URL = os.getenv("IMG_UPLOAD_URL", "https://tu.fqzlr.com/upload")
IMG_AUTH_CODE = os.getenv("IMG_AUTH_CODE", "")
IMG_UPLOAD_FOLDER = os.getenv("IMG_UPLOAD_FOLDER", "youlian")


def _safe_filename(host: str) -> str:
    """将域名转为安全文件名：blog.example.com -> blog.example.com.png"""
    # 替换不安全字符为下划线
    safe = re.sub(r"[^a-zA-Z0-9.\-]", "_", host)
    return f"{safe}.png"


def _build_thumio_url(url: str) -> str:
    """thum.io 兜底 URL（始终可用，无需本地资源）"""
    return f"https://image.thum.io/get/width/{WINDOW_WIDTH}/crop/{WINDOW_HEIGHT}/png/{url}"


def delete_from_imagebed(filename: str) -> bool:
    """
    上传前删除图床上的同名旧图，避免重复堆积
    GET {base_url}/api/manage/delete/{folder}/{filename}
    Authorization: Bearer {IMG_AUTH_CODE}
    """
    if not IMG_AUTH_CODE:
        logger.info("[delete] 未配置 IMG_AUTH_CODE，跳过删除旧图")
        return False

    # 从 IMG_UPLOAD_URL 推导图床根地址（去掉末尾的 /upload）
    base_url = IMG_UPLOAD_URL.rstrip("/")
    if base_url.endswith("/upload"):
        base_url = base_url[: -len("/upload")]

    file_path = f"{IMG_UPLOAD_FOLDER}/{filename}" if IMG_UPLOAD_FOLDER else filename
    delete_url = f"{base_url}/api/manage/delete/{file_path}"

    try:
        resp = requests.get(
            delete_url,
            headers={"Authorization": f"Bearer {IMG_AUTH_CODE}"},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(
                f"[delete] HTTP {resp.status_code}：{resp.text[:200]}"
            )
            return False
        data = resp.json()
        if data.get("success"):
            logger.info(f"[delete] 已删除旧图：{file_path}")
        else:
            # 文件不存在等情况，不算错误，继续上传即可
            logger.info(f"[delete] 删除跳过（可能不存在）：{data}")
        return True
    except json.JSONDecodeError:
        logger.warning(
            f"[delete] 响应非 JSON（状态码 {resp.status_code}）：{resp.text[:200]}"
        )
        return False
    except Exception as e:
        logger.warning(f"[delete] 删除旧图失败（不影响上传）：{e}")
        return False


def upload_to_imagebed(image_bytes: bytes, filename: str) -> Optional[str]:
    """
    调用 cfbed.sanyue.de 兼容 API 上传图片到图床
    POST {IMG_UPLOAD_URL}?uploadFolder=youlian&uploadNameType=origin
    Authorization: Bearer {IMG_AUTH_CODE}    （cfbed 新版只认 Bearer，不再支持 authCode query）
    返回 publicUrl
    """
    try:
        params = {
            "uploadFolder": IMG_UPLOAD_FOLDER,
            "uploadNameType": "origin",
        }
        headers = {}
        if IMG_AUTH_CODE:
            headers["Authorization"] = f"Bearer {IMG_AUTH_CODE}"

        files = {"file": (filename, io.BytesIO(image_bytes), "image/png")}
        logger.info(f"[upload] 正在上传 {filename} 到 {IMG_UPLOAD_URL} ...")
        resp = requests.post(
            IMG_UPLOAD_URL,
            params=params,
            files=files,
            headers=headers,
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning(f"[upload] HTTP {resp.status_code}：{resp.text[:200]}")
            return None

        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            entry = data[0]
            url = entry.get("publicUrl") or entry.get("src")
            if url and url.startswith("/"):
                # 拼接域名
                parsed = urlparse(IMG_UPLOAD_URL)
                url = f"{parsed.scheme}://{parsed.netloc}{url}"
            logger.info(f"[upload] 上传成功：{url}")
            return url
        elif isinstance(data, dict):
            url = data.get("publicUrl") or data.get("src")
            if url:
                logger.info(f"[upload] 上传成功：{url}")
                return url
        logger.warning(f"[upload] 响应格式异常：{data}")
        return None
    except Exception as e:
        logger.warning(f"[upload] 上传失败：{e}")
        return None


def resolve_driver_path() -> str:
    """
    截图会开多个线程一起跑，每个线程原本都要自己装一遍 chromedriver。
    驱动还没缓存的时候，几个线程同时去下载、抢 webdriver_manager 的缓存目录，
    就会偶发崩在 tuple index out of range，导致那一轮截图掉回 thum.io 兜底。

    所以这里先单独（单线程）把 chromedriver 装好、把路径拿到手，
    后面所有线程直接用这个现成路径，不再各自去装。
    """
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    return ChromeDriverManager().install()


def _take_screenshot_with_selenium(
    url: str, host: str, driver_path: Optional[str] = None
) -> Optional[bytes]:
    """
    用 Selenium + Chrome 截取页面截图
    返回 PNG 字节流（失败返回 None）

    driver_path: 预先解析好的 chromedriver 路径；为 None 时回退到
    ChromeDriverManager().install()（仅单线程场景，如 __main__ 测试）。
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as e:
        logger.error(f"[selenium] 依赖未安装：{e}")
        return None

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # WebGL 软件渲染（Spline/Live2D 等 WebGL 内容在 headless 下默认不渲染，产生黑块）
    # 新版 Chrome 需要 --enable-unsafe-swiftshader 才允许软件 WebGL
    options.add_argument("--use-gl=angle")
    options.add_argument("--use-angle=swiftshader")
    options.add_argument("--enable-unsafe-swiftshader")
    options.add_argument("--ignore-gpu-blocklist")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument(f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}")
    options.add_argument("--hide-scrollbars")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    options.binary_location = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")

    driver = None
    try:
        if driver_path:
            service = Service(executable_path=driver_path)
        else:
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        logger.info(f"[selenium] 访问 {url} ...")
        driver.get(url)
        time.sleep(PAGE_LOAD_WAIT)
        png = driver.get_screenshot_as_png()
        return png
    except Exception as e:
        logger.warning(f"[selenium] 截图失败 {url}：{e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def take_screenshot(url: str, host: str, driver_path: Optional[str] = None) -> str:
    """
    截取指定 URL 的主页截图 + 上传图床 + 失败兜底
    返回最终可用的图片 URL（永远返回字符串，绝不抛异常）

    流程：
    1. 启动 Chrome headless，窗口 1280x800
    2. 访问 URL，等待 3 秒
    3. 截图 → PNG 字节流
    4. 上传至 tu.fqzlr.com/youlian/{host}.png
    5. 失败兜底到 thum.io 在线截图 URL

    driver_path: 预先解析好的 chromedriver 路径（并发场景由调用方传入）
    """
    filename = _safe_filename(host)

    # 1. 尝试本地截图 + 上传
    try:
        png_bytes = _take_screenshot_with_selenium(url, host, driver_path)
        if png_bytes:
            delete_from_imagebed(filename)  # 先删旧图（容错：失败不影响上传）
            uploaded = upload_to_imagebed(png_bytes, filename)
            if uploaded:
                return uploaded
            logger.warning(f"[fallback] 上传失败，降级到 thum.io：{url}")
        else:
            logger.warning(f"[fallback] 截图失败，降级到 thum.io：{url}")
    except Exception as e:
        logger.warning(f"[fallback] 异常，降级到 thum.io：{e}")

    # 3. thum.io 最终兜底（永远可用）
    return _build_thumio_url(url)


if __name__ == "__main__":
    # 简单测试
    import sys
    if len(sys.argv) < 3:
        print("用法: python screenshot.py <url> <host>")
        sys.exit(1)
    result = take_screenshot(sys.argv[1], sys.argv[2])
    print(json.dumps({"url": result}))
