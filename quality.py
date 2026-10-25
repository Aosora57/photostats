# -*- coding: utf-8 -*-
"""画质检测(B1 选片辅助):模糊度 + 曝光裁切,纯经典算法无新模型。

指标(存原始值,阈值后调不重算):
  lap_var   全图拉普拉斯方差(对 512px 灰度图)
  lap_max   8×8 分块拉普拉斯方差的最大值(抑制大面积纯色场景的误判,如星空/雪)
  over_frac 亮度 >250 的像素占比(高光裁切)
  under_frac 亮度 <5 的像素占比(暗部裁切)

图片来源与 phash 相同:JPG 原图 draft 解码 / RAW 复用同组 JPG / 无 JPG 的 RAW 用
cache/cls_previews 预览图。结果存 cache/quality.json。
"""
import io
import json
import threading
import time
from pathlib import Path

import numpy as np

import classifier
import phash

CACHE_FILE = classifier.CACHE_DIR / "quality.json"
SOFT_FILE = classifier.CACHE_DIR / "softness.json"

# 画质阈值(全库校准结论:lap_max 在 60-180 区间被"天然低纹理"场景(云/雾/星空/日落渐变)
# 主导,单图盲测无法区分,精度 <80%;<60 的极保守区间实测一致率 83%。想扩召回请配合
# "同簇相对清晰度"对比,而非放松阈值)
BLUR_BLOCK_MAX = 60.0    # 分块最大拉普拉斯方差低于此 → 严重糊片
OVER_FRAC = 0.10         # 高光裁切占比高于此 → 过曝
UNDER_FRAC = 0.35        # 暗部裁切占比高于此 → 欠曝(夜拍题材大量黑底属正常,阈值放宽)
BLOCK = 8                # 分块数(512px 图 → 每块 64px)


def _load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))["items"]
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}


def metrics_of(img) -> dict:
    """PIL Image → 原始画质指标。"""
    g = img.convert("L")
    w, h = g.size
    scale = 512 / max(w, h)
    if scale < 1:
        g = g.resize((round(w * scale), round(h * scale)), 3)
    a = np.asarray(g, dtype=np.float32)
    # 离散拉普拉斯(切片卷积,免 scipy)
    lap = (-4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1]
           + a[1:-1, :-2] + a[1:-1, 2:])
    lap_var = float(lap.var())
    # 分块方差取最大:有任一清晰纹理区域就算对上焦
    H, W = lap.shape
    bh, bw = H // BLOCK, W // BLOCK
    if bh > 0 and bw > 0:
        blocks = lap[:bh * BLOCK, :bw * BLOCK].reshape(BLOCK, bh, BLOCK, bw)
        lap_max = float(blocks.var(axis=(1, 3)).max())
    else:
        lap_max = lap_var
    return {"lap_var": lap_var, "lap_max": lap_max,
            "over": float((a > 250).mean()), "under": float((a < 5).mean())}


def flags_of(m: dict) -> dict:
    """原始指标 → 布尔画质标记。

    暗帧(欠曝 >50%,如星空/夜景)天然拉普拉斯值极低,不判"模糊"——
    它们由"欠曝"标记负责,避免夜拍题材被误标。
    """
    dark = m["under"] > 0.5
    return {"blur": (not dark) and m["lap_max"] < BLUR_BLOCK_MAX,
            "over": m["over"] > OVER_FRAC,
            "under": m["under"] > UNDER_FRAC or dark}


class QualityState:
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


def compute_quality(root, exiftool, files, state: QualityState = None, on_progress=None) -> dict:
    """补算缺失指标的照片(并行),返回全量 {路径: [lap_var, lap_max, over, under]}。"""
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
        if src is None:
            return f["p"], None
        try:
            img = Image.open(io.BytesIO(Path(src).read_bytes()))
            if img.format == "JPEG" and img.size[0] > 1024:
                img.draft("RGB", (1024, 1024))
            m = metrics_of(img)
            return f["p"], [round(m["lap_var"], 2), round(m["lap_max"], 2),
                            round(m["over"], 4), round(m["under"], 4)]
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
    CACHE_FILE.write_text(json.dumps({"v": 1, "items": items}, ensure_ascii=False),
                          encoding="utf-8")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return items


def summary(items: dict) -> dict:
    """按当前阈值汇总各标记数量。"""
    blur = over = under = 0
    for m in items.values():
        f = flags_of({"lap_max": m[1], "over": m[2], "under": m[3]})
        blur += f["blur"]
        over += f["over"]
        under += f["under"]
    return {"analyzed": len(items), "blur": blur, "over": over, "under": under}


# ---------- B1.5 同簇相对清晰度 ----------

SOFT_SIM = 0.90   # 嵌入余弦 ≥0.90 视为同场景/同连拍
SOFT_RATIO = 0.60  # 清晰度低于组中位数 60% → 同簇偏软


class SoftState:
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


def _load_soft() -> dict:
    if SOFT_FILE.exists():
        try:
            return json.loads(SOFT_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {"flagged": [], "groups": 0}


def compute_softness(state: SoftState = None, on_progress=None) -> dict:
    """同簇相对清晰度:嵌入余弦 ≥SOFT_SIM 聚为同场景组(并查集),
    组内 lap_max < 组中位数 ×SOFT_RATIO 的照片标记为"同簇偏软"——
    同一场景里有更清晰的替代帧,它就是首要删除候选(与场景纹理无关)。"""
    import encoders
    pack = encoders.active_pack()
    paths, vecs = classifier._load_embeddings(pack)
    q = _load()
    idx = [i for i, p in enumerate(paths) if p in q]
    P = [paths[i] for i in idx]
    M = vecs[idx]
    n = len(P)
    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = n, 0, True, None
    if n < 2:
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return {"flagged": [], "groups": 0}

    lap = np.array([q[p][1] for p in P], dtype=np.float32)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # 分块余弦 + 并查集
    chunk = 512
    done_rows = 0
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        sim = M[s:e] @ M.T
        for a, b in np.argwhere(sim >= SOFT_SIM):
            i = s + int(a)
            j = int(b)
            if j != i:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra
        done_rows = e
        if state:
            with state.lock:
                state.done = done_rows
        if on_progress:
            on_progress(done_rows, n)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    flagged = []
    g_count = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        laps = lap[members]
        med = float(np.median(laps))
        if med <= 0:
            continue
        soft = [members[k] for k, v in enumerate(laps) if v < SOFT_RATIO * med]
        if soft:
            g_count += 1
            flagged.extend(P[i] for i in soft)

    clusters = []
    for members in groups.values():
        if len(members) >= 2:
            laps_members = lap[members]
            if float(np.median(laps_members)) > 0:
                clusters.append(sorted((P[i] for i in members),
                                       key=lambda pi: -q[pi][1]))
    result = {"flagged": sorted(set(flagged)), "groups": g_count,
              "clusters": clusters,
              "sim": SOFT_SIM, "ratio": SOFT_RATIO,
              "computedAt": time.strftime("%Y-%m-%d %H:%M:%S")}
    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SOFT_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def load_soft() -> dict:
    return _load_soft()


# ---------- E7 闭眼检测(启发式:对齐脸的眼部区域纹理) ----------

BLINK_FILE = classifier.CACHE_DIR / "blink.json"
# ArcFace 模板中两眼中心 (38.3, 51.7) 与 (73.5, 51.5),取 28×16 邻域
EYE_PATCHES = [(38.3, 51.7), (73.5, 51.5)]
EYE_STD_OPEN = 13.0  # 眼部灰度标准差低于此 → 疑似闭眼(实验性启发式)


def compute_blink(state=None, on_progress=None) -> dict:
    """对全部人脸对齐裁剪计算眼部纹理分数;两眼均低于阈值 → 疑似闭眼。

    返回 {faces: {key: std}, photos: {路径: 是否含闭眼脸}, closed: [key,...]}。
    实验性启发式,仅作选片参考。
    """
    from PIL import Image
    import faces as faces_mod
    faces_data = faces_mod._load_faces()
    crop_dir = classifier.CACHE_DIR / "face_crops"
    done_n = 0
    face_std = {}
    total = sum(len(v) for v in faces_data.values())
    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = total, 0, True, None
    for p, recs in faces_data.items():
        for r in recs:
            cf = crop_dir / (r["k"] + ".jpg")
            if not cf.exists():
                continue
            try:
                g = np.asarray(Image.open(cf).convert("L"), dtype=np.float32)
                stds = []
                for ex, ey in EYE_PATCHES:
                    x0, y0 = int(ex - 14), int(ey - 8)
                    patch = g[max(0, y0):y0 + 16, max(0, x0):x0 + 28]
                    stds.append(float(patch.std()))
                face_std[r["k"]] = round(min(stds), 2)  # 两眼中较低者
            except Exception:
                continue
        done_n += len(recs)
        if state:
            with state.lock:
                state.done = done_n
        if on_progress:
            on_progress(done_n, total)
    closed = [k for k, v in face_std.items() if v < EYE_STD_OPEN]
    photos = {p: any(r["k"] in set(closed) for r in recs)
              for p, recs in faces_data.items()}
    result = {"faces": face_std, "closed": closed, "photos": photos,
              "threshold": EYE_STD_OPEN,
              "computedAt": time.strftime("%Y-%m-%d %H:%M:%S")}
    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BLINK_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def load_blink() -> dict:
    if BLINK_FILE.exists():
        try:
            return json.loads(BLINK_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


# ---------- E8 组内最佳(美学分 + 同簇分组) ----------

BESTPICK_FILE = classifier.CACHE_DIR / "bestpick.json"


def compute_bestpick() -> dict:
    """对每个同场景组:美学分最高者为"组内最佳"(并列时取更清晰者);
    其余成员中美学分低于最佳 1.0 分以上 → "建议删除"。"""
    import aesthetic
    soft = _load_soft()
    clusters = soft.get("clusters", [])
    aes = aesthetic.load_scores()
    out = []
    for members in clusters:
        scored = [(p, aes.get(p)) for p in members]
        have = [(p, a) for p, a in scored if a is not None]
        if len(have) < 2:
            continue
        have.sort(key=lambda x: (-x[1], -q_lap(x[0])))
        best = have[0][0]
        cull = [p for p, a in have[1:] if best_a(have) - a >= 1.0]
        out.append({"best": best, "cull": cull, "members": [p for p, _ in have]})
    result = {"groups": out, "computedAt": time.strftime("%Y-%m-%d %H:%M:%S")}
    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BESTPICK_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def q_lap(p):
    q = _load()
    return q.get(p, [0, 0])[1] if p in q else 0


def best_a(have):
    return have[0][1]


def load_bestpick() -> dict:
    if BESTPICK_FILE.exists():
        try:
            return json.loads(BESTPICK_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {"groups": []}
