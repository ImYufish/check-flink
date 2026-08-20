#!/usr/bin/env python3
"""将友链远程清单转换为 check-flink 内部 link_list 格式。

输入支持两种真源格式：
  1. {friends:[{title,imgurl,desc,siteurl,linkpage,enabled,...}]}
     —— friends.yufish.cn/friends.json（check-flink static 清单，Issue 申请自动写入）
  2. {link_list:[{name,link,linkpage,...}]} 或纯 list
     —— SOURCE_URL 兜底源（my-blog 端点，已是 link_list 格式）

统一输出 {link_list:[{name,link,linkpage}]}，供 main.py 与变更检测使用。

用法：
  python convert_friends.py <input.json> <output.json>
"""
import json
import sys


def convert(raw):
    if isinstance(raw, dict) and "friends" in raw:
        # 真源格式：过滤 enabled，映射字段
        friends = raw.get("friends", []) or []
        return [
            {
                "name": (f.get("title") or "").strip(),
                "link": (f.get("siteurl") or "").strip(),
                "linkpage": (f.get("linkpage") or "").strip()
                or (f.get("siteurl") or "").strip(),
                # 透传人工核验标记：main.py 据此对反爬站点强制记为有反链，避免误标
                "verified": bool(f.get("verified", False)),
            }
            for f in friends
            if f.get("enabled", True)
        ]
    if isinstance(raw, dict) and "link_list" in raw:
        # 已是 link_list 格式，原样透传
        return raw["link_list"]
    if isinstance(raw, list):
        return raw
    return []


def main():
    if len(sys.argv) < 3:
        print("用法: python convert_friends.py <input> <output>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        raw = json.load(f)
    link_list = convert(raw)
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump({"link_list": link_list}, f, ensure_ascii=False, indent=2)
    print(f"✅ 生成 {sys.argv[2]}，共 {len(link_list)} 条友链")


if __name__ == "__main__":
    main()
