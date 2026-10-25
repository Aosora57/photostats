# -*- coding: utf-8 -*-
"""PhotoStats —— 轻量本地照片统计查看器。

用法:
  python photostats.py                 # 首次自动扫描,然后启动本地服务并打开浏览器
  python photostats.py --scan-only     # 只扫描生成缓存,不启动服务
  python photostats.py --rescan        # 启动前强制重新扫描
  python photostats.py --root D:\\pics  # 指定其他照片目录
"""
import argparse
import json
import sys
import threading
import time
import webbrowser
from collections import Counter
from pathlib import Path

import scanner
import server


def fmt_gb(n) -> str:
    if not n:
        return "0"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def pair_stats(files) -> tuple:
    groups = {}
    for f in files:
        if f["t"] == "video":
            continue
        i = f["p"].rfind("/")
        dir_part = f["p"][:i] if i >= 0 else ""
        base = f["p"][i + 1:].rsplit(".", 1)[0].lower()
        g = groups.setdefault((dir_part, base), {"raw": 0, "jpg": 0})
        g["raw" if f["t"] == "raw" else "jpg"] += 1
    paired = sum(1 for g in groups.values() if g["raw"] and g["jpg"])
    raw_only = sum(1 for g in groups.values() if g["raw"] and not g["jpg"])
    jpg_only = sum(1 for g in groups.values() if g["jpg"] and not g["raw"])
    return paired, raw_only, jpg_only


def print_summary(lib: dict, secs) -> None:
    files = lib["files"]
    by_type = Counter(f["t"] for f in files)
    total_size = sum(f["s"] or 0 for f in files)
    dates = sorted(f["dt"] for f in files if f["dt"])
    models = Counter(f["model"] for f in files if f["model"])
    paired, raw_only, jpg_only = pair_stats(files)
    vids = [f for f in files if f["t"] == "video"]
    vid_secs = sum(f["dur"] or 0 for f in vids)
    others = {k: v for k, v in lib["meta"].get("extCounts", {}).items()
              if k not in scanner.TARGET_EXTS}
    print("=" * 52)
    print(f"  照片 {by_type['jpg'] + by_type['raw']:,} 张 "
          f"(RAW {by_type['raw']:,} / JPG {by_type['jpg']:,}) · 视频 {by_type['video']} 个")
    print(f"  总大小 {fmt_gb(total_size)} · 其他文件 {sum(others.values())} 个")
    if dates:
        print(f"  拍摄时间 {dates[0][:10]} ~ {dates[-1][:10]}")
    top = models.most_common(3)
    if top:
        print("  机身 " + " · ".join(f"{m}({c})" for m, c in top))
    print(f"  RAW+JPG 成对 {paired} 组 · 仅RAW {raw_only} · 仅JPG {jpg_only}")
    if vids:
        print(f"  视频总时长 {vid_secs / 3600:.1f} 小时")
    if secs:
        print(f"  扫描耗时 {secs:.1f} 秒")
    print("=" * 52)


def main() -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="PhotoStats 本地照片统计查看器")
    ap.add_argument("--root", default=None, help="照片目录,默认为 PhotoStats 所在文件夹")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scan-only", action="store_true", help="只扫描生成缓存,不启动服务")
    ap.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--rescan", action="store_true", help="启动前强制重新扫描")
    args = ap.parse_args()

    project = Path(__file__).resolve().parent
    exif = project / "tools" / "exiftool.exe"
    if not exif.exists():
        sys.exit("缺少 tools\\exiftool.exe,请重新获取")
    root = Path(args.root).resolve() if args.root else project.parent
    if not root.exists():
        sys.exit(f"目录不存在: {root}")

    cache = project / "cache" / "metadata.json"
    if args.scan_only or args.rescan or not cache.exists():
        print(f"正在全量扫描 {root} ...")
        t0 = time.time()
        lib = scanner.scan(root, str(exif), exclude_dirs=[project],
                           on_progress=lambda d, t: print(f"\r  进度 {d}/{t}", end="", flush=True))
        print()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
        print_summary(lib, time.time() - t0)
        if args.scan_only:
            print(f"缓存已保存: {cache}")
            return
    else:
        prev = json.loads(cache.read_text(encoding="utf-8"))
        print(f"正在增量同步 {root} ...")
        t0 = time.time()
        lib = scanner.scan_incremental(root, str(exif), prev["files"], exclude_dirs=[project],
                                       on_progress=lambda d, t: print(f"\r  进度 {d}/{t}", end="", flush=True))
        stats = lib["meta"].get("stats", {})
        print(f"\n  新增/变化 {stats.get('new', 0)} · 复用 {stats.get('reused', 0)} · "
              f"移除 {max(0, stats.get('removed', 0))} · 耗时 {time.time() - t0:.1f} 秒")
        if stats.get("new") or stats.get("removed"):
            cache.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
        print_summary(lib, None)

    state = server.AppState(root, project, exif, lib)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", args.port), server.make_handler(state))
    url = f"http://127.0.0.1:{args.port}"
    print(f"PhotoStats 运行中: {url}   (Ctrl+C 停止)")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
