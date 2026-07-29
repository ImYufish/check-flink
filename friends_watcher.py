# -*- coding: utf-8 -*-
"""
friends_watcher.py - friendsConfig.ts 变更监听 + 增量检测/截图调度

用法:
  python friends_watcher.py diff  --config <path> [--json] [--exit-code]
  python friends_watcher.py run   --config <path> [--skip-screenshot]
  python friends_watcher.py watch --config <path> [--interval 10] [--debounce 5] [--skip-screenshot]

功能:
  1. 解析 TypeScript 导出的 friendsConfig: FriendLink[]（Layer1=tsx AST, Layer2=正则修复）
  2. 与 friends_snapshot.json 比较，产出 added/modified/removed
  3. 生成 SOURCE_URL 用的 friends.json（check-flink 兼容格式）
  4. diff/run/watch 三种 CLI 模式
  5. run 模式下，调用 main.py + screenshot_runner.py 做增量检测和截图
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import argparse
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from typing import Any

# ===== 常量 =====
SCRIPT_DIR = Path(__file__).resolve().parent
SNAPSHOT_FILE = SCRIPT_DIR / "friends_snapshot.json"
OUTPUT_FRIENDS_JSON = SCRIPT_DIR / "friends.json"
MAIN_PY = SCRIPT_DIR / "main.py"
SCREENSHOT_RUNNER_PY = SCRIPT_DIR / "screenshot_runner.py"
SNAPSHOT_VERSION = 1

# 截图关键字段：这些字段变了要重测
CRITICAL_FIELDS = {"siteurl", "linkpage", "enabled"}

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="🔔 %(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ======================================================================
# 1. TypeScript → Python list[dict] 解析
# ======================================================================

def _parse_with_tsx(config_path: Path) -> list[dict] | None:
    """Layer 1：使用外部 tsx 执行导入，把 friendsConfig JSON 打印到 stdout。
    环境需 `npx tsx` 或 `tsx` 在 PATH；失败返回 None（调用方回退到 Layer2）。
    """
    # 1) 检查 tsx 是否可用
    tsx_bin = shutil.which("tsx")
    npx_bin = shutil.which("npx")
    if not tsx_bin and not npx_bin:
        logger.warning("[parse] 未找到 tsx/npx，跳过 Layer1 AST 解析")
        return None

    config_abs = config_path.resolve()
    # Windows 绝对路径需要 file:/// 协议前缀，否则 Node ESM 报 "Received protocol 'f:'"
    config_file_url = config_abs.as_uri()
    # 临时脚本：导入后打印 JSON（使用动态 import，兼容 .ts 文件）
    eval_code = (
        "process.on('unhandledRejection', e => { console.error(e); process.exit(2); });"
        "import('" + config_file_url + "').then(m => {"
        "  const arr = m.friendsConfig || m.default;"
        "  console.log(JSON.stringify(arr, null, 0));"
        "}).catch(e => { console.error(e); process.exit(2); });"
    )
    cmd = [tsx_bin or npx_bin, "--eval", eval_code]
    if not tsx_bin:  # 通过 npx
        cmd = [npx_bin, "tsx", "--eval", eval_code]
    try:
        logger.info("[parse] Layer1: 调用 tsx 解析 ...")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(config_abs.parent))
        if r.returncode != 0:
            logger.warning(f"[parse] tsx 失败 (code={r.returncode}): {r.stderr[:500]}")
            return None
        arr = json.loads(r.stdout.strip())
        if isinstance(arr, list):
            logger.info(f"[parse] Layer1 成功解析 {len(arr)} 条友链")
            return arr
        logger.warning("[parse] tsx 输出非 list")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("[parse] tsx 解析超时(60s)")
        return None
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[parse] tsx 调用失败: {e}")
        return None


def _repair_ts_to_json(raw: str) -> list[dict]:
    """Layer 2：正则修复 TS 数组字面量 → 合法 JSON。
    仅针对 friendsConfig: FriendLink[] 的字面量数组格式；不支持嵌套太复杂的表达式。
    """
    # 1) 找到数组起点：`export const friendsConfig: FriendLink[] = [ `
    start_re = re.search(
        r"export\s+const\s+friendsConfig\s*:\s*FriendLink\s*\[\s*\]\s*=\s*\[",
        raw,
    )
    if not start_re:
        # 也兼容无类型标注
        start_re = re.search(r"export\s+const\s+friendsConfig\s*=\s*\[", raw)
    if not start_re:
        raise ValueError("找不到 friendsConfig 数组起始位置")
    i = start_re.end() - 1  # 指向 '['
    n = len(raw)
    depth = 0
    in_str: str | None = None
    in_tpl = False
    in_line_comment = False
    in_block_comment = False
    start_idx = i
    while i < n:
        ch = raw[i]
        nxt = raw[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue
        if in_str or in_tpl:
            quote = in_str or ("`" if in_tpl else None)
            if ch == "\\" and quote:
                i += 2
                continue
            if quote and ch == quote:
                in_str = None
                in_tpl = False
            i += 1
            continue
        # 不在字符串里
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ('"', "'"):
            in_str = ch
            i += 1
            continue
        if ch == "`":
            in_tpl = True
            i += 1
            continue
        if ch == "[" or ch == "{":
            depth += 1
        elif ch == "]" or ch == "}":
            depth -= 1
            if depth == 0:
                # 截取到整个数组（含括号）
                arr_src = raw[start_idx : i + 1]
                return _parse_repaired_array(arr_src)
        i += 1
    raise ValueError("匹配 friendsConfig 数组失败（括号不平衡）")


def _parse_repaired_array(arr_src: str) -> list[dict]:
    """对抽取出来的数组源码执行 TS→JSON 修复并 json.loads。"""
    # 1) 先处理注释和字符串：字符串临时占位避免误处理
    #    占位符必须包含 @ 字符，避免被 "补键引号" 正则误识别为合法标识符键
    placeholder_map: dict[str, str] = {}
    _PREFIX = "@@@_STRPLACEHOLDER_"
    _SUFFIX = "_@@@"

    def _protect_strings(s: str) -> str:
        out = []
        i = 0
        n = len(s)
        in_str: str | None = None
        in_tpl = False
        buf = []
        pid = 0
        while i < n:
            ch = s[i]
            nxt = s[i + 1] if i + 1 < n else ""
            if in_str or in_tpl:
                q = in_str or ("`" if in_tpl else None)
                if ch == "\\" and q:
                    buf.append(ch)
                    if i + 1 < n:
                        buf.append(s[i + 1])
                    i += 2
                    continue
                if q and ch == q:
                    buf.append(ch)
                    key = f"{_PREFIX}{pid}{_SUFFIX}"
                    pid += 1
                    placeholder_map[key] = "".join(buf)
                    out.append(key)
                    buf = []
                    in_str = None
                    in_tpl = False
                    i += 1
                    continue
                buf.append(ch)
                i += 1
                continue
            if ch in ('"', "'"):
                in_str = ch
                buf.append(ch)
                i += 1
                continue
            if ch == "`":
                in_tpl = True
                buf.append(ch)
                i += 1
                continue
            # 不在字符串：处理注释
            if ch == "/" and nxt == "/":
                # line comment → 跳过直到换行
                while i < n and s[i] != "\n":
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                i += 2
                while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    protected = _protect_strings(arr_src)

    # 2) 去掉尾逗号：`,\s*]` → `]`, `,\s*}` → `}`
    protected = re.sub(r",\s*([\]\}])", r"\1", protected)

    # 3) 给没有引号的对象键加双引号
    #    匹配形如:    key:  （前面不是引号，后面是冒号）
    protected = re.sub(
        r"(?<=[\{\[,])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:",
        lambda m: f'"{m.group(1)}":',
        protected,
    )
    # 处理数组第一项（前面是 '[' 的也可能漏）
    protected = re.sub(
        r"([\[\{])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:",
        lambda m: f'{m.group(1)}"{m.group(2)}":',
        protected,
    )

    # 4) 去除类型标注：`key: SomeType = ` 这种，一般修复步骤3后已经没了；
    #    但可能有内联 `as const` / `as string[]` ，去掉
    protected = re.sub(r"\s+as\s+(?:const|[\w\[\]]+)", "", protected)

    # 5) 恢复字符串占位
    for key, val in placeholder_map.items():
        protected = protected.replace(key, val)

    # 6) json.loads
    try:
        data = json.loads(protected)
    except json.JSONDecodeError as e:
        logger.error(f"[parse] Layer2 JSON 修复后仍然解析失败: {e}")
        # 写调试文件帮助定位
        (SCRIPT_DIR / "_debug_repaired.json").write_text(protected, encoding="utf-8")
        logger.info(f"[parse] 调试文件已写入: {SCRIPT_DIR / '_debug_repaired.json'}")
        raise
    if not isinstance(data, list):
        raise ValueError(f"修复后非 list: {type(data)}")
    logger.info(f"[parse] Layer2 成功解析 {len(data)} 条友链")
    return data


def parse_friends_config(config_path: Path) -> list[dict]:
    """对外：TS 文件解析，Layer1 优先，失败回退 Layer2。"""
    raw = config_path.read_text(encoding="utf-8")
    arr = _parse_with_tsx(config_path)
    if arr is not None:
        return arr
    logger.info("[parse] 回退到 Layer2 正则修复解析")
    return _repair_ts_to_json(raw)


# ======================================================================
# 2. friends.json 写出（check-flink SOURCE_URL 用）
# ======================================================================
def write_friends_json(arr: list[dict]) -> Path:
    out = {
        "link_list": [
            {
                "name": it.get("title", ""),
                "link": it.get("siteurl", ""),
                "linkpage": it.get("linkpage") or it.get("siteurl", ""),
            }
            for it in arr
            if it.get("enabled", True)
        ]
    }
    OUTPUT_FRIENDS_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[io] 已写入: {OUTPUT_FRIENDS_JSON} ({len(out['link_list'])} 条启用友链)")
    return OUTPUT_FRIENDS_JSON


# ======================================================================
# 3. Snapshot 读写 & Diff
# ======================================================================

def _normalize_friend(it: dict) -> dict:
    """规范化单条友链（用于快照比较，只保留数据字段，不含衍生字段）。"""
    return {
        "title": it.get("title", ""),
        "imgurl": it.get("imgurl", ""),
        "desc": it.get("desc", ""),
        "siteurl": it.get("siteurl", ""),
        "linkpage": it.get("linkpage") or "",
        "tags": list(it.get("tags") or []),
        "weight": it.get("weight", 0),
        "enabled": bool(it.get("enabled", True)),
    }


def load_snapshot() -> dict:
    if not SNAPSHOT_FILE.exists():
        return {"version": SNAPSHOT_VERSION, "last_modified": "", "friends": {}}
    try:
        d = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        d.setdefault("friends", {})
        return d
    except Exception as e:
        logger.warning(f"[snapshot] 读取失败，将重建: {e}")
        return {"version": SNAPSHOT_VERSION, "last_modified": "", "friends": {}}


def save_snapshot(friends_by_key: dict, ts_file_mtime: str) -> None:
    snap = {
        "version": SNAPSHOT_VERSION,
        "last_modified": ts_file_mtime,
        "friends": friends_by_key,
    }
    SNAPSHOT_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[snapshot] 已保存: {SNAPSHOT_FILE}")


def _key_of(it: dict) -> str:
    """唯一键：以 siteurl 为主，缺失时回退 title（不建议）。"""
    k = (it.get("siteurl") or "").strip()
    if not k:
        k = "__title__:" + (it.get("title") or "").strip()
    return k


def compute_diff(arr: list[dict]) -> dict[str, list[dict]]:
    """比较当前 TS 内容 vs 快照。
    返回: {"added":[...],"modified":[...],"removed":[...],"unchanged":[...]}
    """
    snap = load_snapshot()
    prev_friends = snap.get("friends", {})
    # 构造当前：以 siteurl 为 key；检测重复
    cur: dict[str, dict] = {}
    for it in arr:
        norm = _normalize_friend(it)
        k = _key_of(norm)
        if k in cur:
            logger.warning(f"[diff] siteurl 重复: {k} → 取后者覆盖前者（前者title={cur[k].get('title')}）")
        cur[k] = norm
    prev_keys = set(prev_friends.keys())
    cur_keys = set(cur.keys())

    added    = [cur[k] for k in sorted(cur_keys - prev_keys)]
    removed  = [prev_friends[k] for k in sorted(prev_keys - cur_keys)]
    modified = [cur[k] for k in sorted(cur_keys & prev_keys) if prev_friends[k] != cur[k]]
    unchanged = [cur[k] for k in sorted(cur_keys & prev_keys) if prev_friends[k] == cur[k]]

    return {
        "added": added,
        "modified": modified,
        "removed": removed,
        "unchanged": unchanged,
        "_current_map": cur,
    }


def filter_needs_detect(diff: dict) -> list[dict]:
    """从 added/modified 中挑出需要做 检测+截图 的条目。
    modified 中仅 CRITICAL_FIELDS 有变动的才重测；纯 title/imgurl/desc/tags/weight 变动跳过检测。
    """
    snap = load_snapshot()
    prev = snap.get("friends", {})
    out: list[dict] = []
    for it in diff["added"]:
        if it.get("enabled", True):
            out.append(it)
    for it in diff["modified"]:
        if not it.get("enabled", True):
            continue
        k = _key_of(it)
        prev_it = prev.get(k, {})
        # enabled 从 false→true 也要测
        changed_critical = (
            any(prev_it.get(f) != it.get(f) for f in CRITICAL_FIELDS)
            or (prev_it.get("enabled", True) is False and it.get("enabled", True) is True)
        )
        if changed_critical:
            out.append(it)
    return out


# ======================================================================
# 4. 增量调度：main.py + screenshot_runner.py
# ======================================================================

def _run_py(script: Path, env_extra: dict, label: str) -> bool:
    """子进程执行脚本，合并当前 env + env_extra。成功返回 True。"""
    env = os.environ.copy()
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    # 数据源统一指向生成的 friends.json（绝对/相对都可）
    env.setdefault("SOURCE_URL", str(OUTPUT_FRIENDS_JSON))
    logger.info(f"[run] → {label}: {script.name}  TARGET_LINK={env.get('TARGET_LINK')}")
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            env=env,
            cwd=str(SCRIPT_DIR),
            capture_output=False,  # 继承 stdout/stderr 以便查看进度
        )
        ok = (r.returncode == 0)
        logger.info(f"[run] ← {label}: {'OK' if ok else f'FAIL(code={r.returncode})'}")
        return ok
    except Exception as e:
        logger.error(f"[run] ← {label}: 异常 {e}")
        return False


def schedule_incremental(targets: list[dict], skip_screenshot: bool = False) -> None:
    """对 target 列表执行增量检测 + 截图。
    将所有 target 的 (title|siteurl) 用 "|" 拼接，一次 main.py 调用避免多次启动。
    """
    if not targets:
        logger.info("[schedule] 没有需要增量处理的条目，跳过")
        return
    # 构造 TARGET_LINK：优先精确匹配 siteurl，再补 title 辅助
    exact_keys: list[str] = []
    for it in targets:
        u = (it.get("siteurl") or "").strip()
        t = (it.get("title") or "").strip()
        if u:
            exact_keys.append(u)
        elif t:
            exact_keys.append(t)
    target_link = "|".join(dict.fromkeys(exact_keys))  # 去重保序
    if not target_link:
        logger.warning("[schedule] 构造 TARGET_LINK 为空，取消")
        return
    env_extra = {"TARGET_LINK": target_link}
    # 第一步：检测
    ok = _run_py(MAIN_PY, env_extra, label="状态检测")
    if not ok:
        logger.error("[schedule] 状态检测失败；继续尝试截图（可能已有部分数据可用）")
    # 第二步：截图
    if not skip_screenshot:
        _run_py(SCREENSHOT_RUNNER_PY, env_extra, label="截图更新")
    else:
        logger.info("[schedule] --skip-screenshot 已设置，跳过截图")


# ======================================================================
# 5. CLI: diff / run / watch
# ======================================================================

def _do_diff(config_path: Path, print_json: bool, exit_code: bool) -> int:
    """diff 模式：解析、输出差异（不执行检测）。"""
    arr = parse_friends_config(config_path)
    write_friends_json(arr)
    diff = compute_diff(arr)
    report = {
        "added":    [{"title": it.get("title"), "siteurl": it.get("siteurl")} for it in diff["added"]],
        "modified": [{"title": it.get("title"), "siteurl": it.get("siteurl")} for it in diff["modified"]],
        "removed":  [{"title": it.get("title"), "siteurl": it.get("siteurl")} for it in diff["removed"]],
        "needs_detect": [{"title": it.get("title"), "siteurl": it.get("siteurl")} for it in filter_needs_detect(diff)],
        "enabled_count": sum(1 for it in arr if it.get("enabled", True)),
        "total_count": len(arr),
    }
    if print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        def _fmt(xs):
            return ", ".join(f"{x['title'] or '?'}({x['siteurl'] or '?'})" for x in xs) or "∅"
        print(f"📊 总数: {report['total_count']} (启用: {report['enabled_count']})")
        print(f"➕ 新增 {len(report['added'])}:    {_fmt(report['added'])}")
        print(f"✏️  修改 {len(report['modified'])}:  {_fmt(report['modified'])}")
        print(f"➖ 移除 {len(report['removed'])}:   {_fmt(report['removed'])}")
        print(f"🎯 需检测截图 {len(report['needs_detect'])}: {_fmt(report['needs_detect'])}")
    if exit_code:
        has_changes = bool(diff["added"] or diff["modified"] or diff["removed"])
        return (0 if not has_changes else 1)
    return 0


def _do_run(config_path: Path, skip_screenshot: bool, no_update_snapshot: bool) -> int:
    """run 模式：diff → 写快照 → 增量调度。"""
    arr = parse_friends_config(config_path)
    write_friends_json(arr)
    diff = compute_diff(arr)
    targets = filter_needs_detect(diff)
    summary = f"新增={len(diff['added'])}, 修改={len(diff['modified'])}, 移除={len(diff['removed'])}, 需检测={len(targets)}"
    logger.info(f"[run] 差异概览: {summary}")
    try:
        schedule_incremental(targets, skip_screenshot=skip_screenshot)
    finally:
        if not no_update_snapshot:
            # 不管成功失败都更新快照，避免重复执行相同 diff（失败用户可手动重试）
            cur_map = diff["_current_map"]
            mtime = datetime.fromtimestamp(config_path.stat().st_mtime).isoformat(timespec="seconds")
            save_snapshot(cur_map, mtime)
    return 0


def _do_watch(config_path: Path, interval: int, debounce: int, skip_screenshot: bool) -> int:
    """watch 模式：轮询 + debounce，文件 mtime 变化触发 run。"""
    logger.info(f"[watch] 开始监听: {config_path} (轮询间隔={interval}s, debounce={debounce}s)")
    last_mtime = -1.0
    last_snapshot_ok = False
    debounce_timer: threading.Timer | None = None
    pending_lock = threading.Lock()
    running_lock = threading.Lock()  # 防并发执行

    def _trigger():
        nonlocal last_snapshot_ok
        with running_lock:
            try:
                logger.info("[watch] debounce 触发，开始执行增量流程")
                # 复用 run 逻辑，但 run 结束要更新 snapshot 所以 last_mtime 之后也要同步
                arr = parse_friends_config(config_path)
                write_friends_json(arr)
                diff = compute_diff(arr)
                targets = filter_needs_detect(diff)
                logger.info(f"[watch] 差异: 新增={len(diff['added'])}, 修改={len(diff['modified'])}, 移除={len(diff['removed'])}, 需检测={len(targets)}")
                try:
                    schedule_incremental(targets, skip_screenshot=skip_screenshot)
                finally:
                    cur_map = diff["_current_map"]
                    mtime_s = datetime.fromtimestamp(config_path.stat().st_mtime).isoformat(timespec="seconds")
                    save_snapshot(cur_map, mtime_s)
                    last_snapshot_ok = True
            except Exception as e:
                logger.exception(f"[watch] 增量流程异常: {e}")

    while True:
        try:
            if not config_path.exists():
                logger.warning(f"[watch] 配置文件不存在: {config_path}，等待...")
                time.sleep(interval)
                continue
            cur_mtime = config_path.stat().st_mtime
            if cur_mtime != last_mtime:
                logger.info(f"[watch] 检测到文件变更 (mtime={cur_mtime})")
                last_mtime = cur_mtime
                with pending_lock:
                    if debounce_timer and debounce_timer.is_alive():
                        debounce_timer.cancel()
                    debounce_timer = threading.Timer(debounce, _trigger)
                    debounce_timer.daemon = True
                    debounce_timer.start()
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("[watch] Ctrl+C，退出")
            return 0
        except Exception as e:
            logger.exception(f"[watch] 轮询循环异常: {e}")
            time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="friends_watcher", description="friendsConfig.ts 变更监听 + 增量检测/截图调度")
    ap.add_argument("--config", required=True, type=Path, help="friendsConfig.ts 文件路径")
    sub = ap.add_subparsers(dest="mode", required=True)
    # diff
    d = sub.add_parser("diff", help="解析并输出差异（不执行检测/截图）")
    d.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    d.add_argument("--exit-code", action="store_true", help="有变更时退出码=1，无变更=0（CI 用）")
    # run
    r = sub.add_parser("run", help="一次性执行：解析→diff→增量检测+截图→更新快照")
    r.add_argument("--skip-screenshot", action="store_true", help="只执行检测，不执行截图")
    r.add_argument("--no-update-snapshot", action="store_true", help="完成后不更新快照（调试用）")
    # watch
    w = sub.add_parser("watch", help="常驻监听：按 --interval 轮询文件 mtime，变更经 debounce 后触发 run")
    w.add_argument("--interval", type=int, default=10, help="轮询间隔(秒)，默认 10")
    w.add_argument("--debounce", type=int, default=5, help="变更防抖(秒)，默认 5；连续保存只触发一次")
    w.add_argument("--skip-screenshot", action="store_true", help="只执行检测，不执行截图")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path: Path = args.config
    if not config_path.exists():
        logger.error(f"找不到配置文件: {config_path}")
        return 2
    if args.mode == "diff":
        return _do_diff(config_path, print_json=args.json, exit_code=args.exit_code)
    if args.mode == "run":
        return _do_run(config_path, skip_screenshot=args.skip_screenshot, no_update_snapshot=args.no_update_snapshot)
    if args.mode == "watch":
        return _do_watch(config_path, interval=args.interval, debounce=args.debounce, skip_screenshot=args.skip_screenshot)
    logger.error(f"未知 mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
