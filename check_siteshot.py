"""调试脚本：检查 result.json 中 siteshot 字段状态"""
import json
import sys

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "./result.json"
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"❌ 无法读取 {path}: {e}")
        return

    s = d.get("link_status", [])
    empty = sum(1 for x in s if not x.get("siteshot"))
    thumio = sum(1 for x in s if "thum.io" in (x.get("siteshot") or ""))
    real = len(s) - empty - thumio
    print(f"  siteshot 统计: 共 {len(s)} 条, 空={empty}, thum.io={thumio}, 真图床={real}")
    if len(s) > 0:
        print(f'  示例: {s[0].get("name","?")} -> {s[0].get("siteshot","<空>")[:80]}')

if __name__ == "__main__":
    main()