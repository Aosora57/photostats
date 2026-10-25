# -*- coding: utf-8 -*-
"""色彩分析(E10):全库主色调/冷暖/无彩色统计,纯 numpy + PIL HSV,无新依赖。

对每张照片(图片来源与 phash 相同:JPG 原图/同组 JPG/cls_previews)取 64px 缩略图,
转 HSV 后按像素统计:
  w 暖色占比(红/橙/黄 + 品红端)
  c 冷色占比(黄绿/绿/青/蓝/紫)
  n 无彩色占比(饱和度 < 0.12 的黑/白/灰)
  fam 主导色族(有彩色像素中最大的色相 bin;无彩色占主导时为 "无彩色")
结果存 cache/colors.json {路径: {w,c,n,fam,v}}。
"""
import io
import json
import threading
import time
from pathlib import Path

import numpy as np

import classifier
import phash

CACHE_FILE = classifier.CACHE_DIR / "colors.json"
NEUTRAL_SAT = 0.12   # 饱和度低于此视为无彩色

FAMILIES = ["红", "橙", "黄", "黄绿", "绿", "青绿", "青", "蓝", "蓝紫", "紫", "品红", "红紫"]
WARM_BINS = {0, 1, 2, 10, 11}          # 红/橙/黄/品红/红紫
FAMILY_COLORS = ["#e53935", "#fb8c00", "#fdd835", "#9ccc65", "#43a047",
                 "#26a69a", "#00acc1", "#1e88e5", "#3949ab", "#8e24aa",
                 "#d81b60", "#c2185b"]


def family_color(name: str) -> str:
    return FAMILY_COLORS[FAMILIES.index(name)] if name in FAMILIES else "#8792a8"


def _load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def metrics_of(img) -> dict:
    """PIL Image(RGB)→ 色彩指标。"""
    small = img.convert("RGB").resize((64, 64), 3)
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32)
    h = hsv[:, :, 0] / 255.0 * 360.0
    s = hsv[:, :, 1] / 255.0
    v = hsv[:, :, 2] / 255.0
    neutral = s < NEUTRAL_SAT
    n_share = float(neutral.mean())
    colored = ~neutral
    colored_share = float(colored.mean())
    # 色相 12 bin(仅统计有彩色像素)
    bins = np.zeros(12, dtype=np.float32)
    if colored_share > 0:
        hb = np.clip((h / 30.0).astype(np.int32), 0, 11)
        for b in range(12):
            m = colored & (hb == b)
            bins[b] = float(m.sum())
        bins /= max(colored.sum(), 1)
    warm = sum(bins[b] for b in WARM_BINS) * colored_share
    cool = colored_share - warm
    fam = "无彩色"
    if colored_share > 0:
        hi = int(np.argmax(bins))
        if bins[hi] > 0:
            fam = FAMILIES[hi]
    if n_share >= 0.5:
        fam = "无彩色"
    return {"w": round(float(warm), 3), "c": round(float(cool), 3),
            "n": round(float(n_share), 3), "fam": fam,
            "v": round(float(v.mean()), 3)}


class ColorState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.error = None
        self.finishedAt = None

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "done": self.done, "total": self.total,
                    "error": self.error, "finishedAt": self.finishedAt}


def compute_colors(root, exiftool, files, state: ColorState = None, on_progress=None) -> dict:
    """补算缺失照片的色彩指标(并行),存 cache/colors.json。"""
    from PIL import Image
    from concurrent.futures import ThreadPoolExecutor

    root = Path(root)
    items = _load()
    jpg_for = {}
    for f in files:
        if f["t"] == "jpg":
            i = f["p"].rfind("/")
            jpg_for[(f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower())] = f["p"]
    preview_dir = classifier.CACHE_DIR / "cls_previews"

    todo = [f for f in files if f["t"] != "video" and f["p"] not in items]
    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = len(todo), 0, True, None
    if not todo:
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return items

    def work(f):
        src = phash._hash_source(f, root, jpg_for, preview_dir)
        if src is None or not Path(src).exists():
            return f["p"], None
        try:
            img = Image.open(io.BytesIO(Path(src).read_bytes()))
            if img.format == "JPEG" and img.size[0] > 128:
                img.draft("RGB", (128, 128))
            m = metrics_of(img)
            return f["p"], {"w": m["w"], "c": m["c"], "n": m["n"], "fam": m["fam"], "v": m["v"]}
        except Exception:
            return f["p"], None

    done_n = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for p, m in pool.map(work, todo):
            if m is not None:
                items[p] = m
            done_n += 1
            if state:
                with state.lock:
                    state.done = done_n
            if on_progress:
                on_progress(done_n, len(todo))

    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return items


def load_colors() -> dict:
    return _load()


def summary(items: dict) -> dict:
    """全库汇总:暖/冷/无彩色平均占比 + 色族分布(按主导色族计)。"""
    w = c = n = 0.0
    fam_counter = {}
    for m in items.values():
        w += m["w"]
        c += m["c"]
        n += m["n"]
        fam_counter[m["fam"]] = fam_counter.get(m["fam"], 0) + 1
    k = len(items)
    fams = sorted(fam_counter.items(), key=lambda x: -x[1])
    return {"analyzed": k,
            "warm": round(w / k, 3) if k else 0, "cool": round(c / k, 3) if k else 0,
            "neutral": round(n / k, 3) if k else 0,
            "fams": fams}


def monthly_warmth(items: dict, files: list) -> list:
    """按月汇总暖色像素占比(仅彩色照片)。by = {路径: 记录}"""
    by = {f["p"]: f for f in files}
    months = {}
    for p, m in items.items():
        f = by.get(p)
        if not f or not f.get("dt"):
            continue
        mo = f["dt"][:7]
        colored = m["w"] + m["c"]
        if colored <= 0:
            continue
        months.setdefault(mo, []).append(m["w"] / colored)
    out = [{"month": mo, "warm": round(float(np.mean(v)), 3), "n": len(v)}
           for mo, v in sorted(months.items())]
    return out


