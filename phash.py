# -*- coding: utf-8 -*-
"""感知哈希(pHash)视觉去重:64 位 DCT 哈希,能抓改尺寸/重新导出的重复。

哈希来源复用 classifier 的图片管线(JPG 原图 draft 解码 / RAW 用 cls_previews),
结果存 cache/phashes.json {路径: 64位哈希十六进制}。
视觉重复 = 跨文件夹的哈希汉明距离 ≤ 阈值的照片对(同文件夹连拍不算,噪声大)。
"""
import io
import json
import threading
import time
from pathlib import Path

import numpy as np

import classifier

CACHE_FILE = classifier.CACHE_DIR / "phashes.json"
THRESHOLD = 8          # 汉明距离阈值(64bit,≤8 视为视觉重复)
MIN_BYTES_DIFF = 0.15  # 可选过滤:文件大小相差小于 15% 的才比(同图重导出大小接近)——未启用,保留接口


# ---------- pHash ----------

def _dct_basis(n: int) -> np.ndarray:
    """正交 DCT-II 变换矩阵。"""
    k = np.arange(n)[:, None]
    r = np.arange(n)[None, :]
    basis = np.cos(np.pi * (2 * r + 1) * k / (2 * n)) * np.sqrt(2 / n)
    basis[0] /= np.sqrt(2)
    return basis.astype(np.float32)


_DCT32 = _dct_basis(32)
_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def phash_of(img) -> int:
    """PIL Image → 64 位感知哈希(int)。"""
    img = img.convert("L").resize((32, 32), 3)  # bicubic
    d = _DCT32 @ np.asarray(img, dtype=np.float32) @ _DCT32.T
    block = d[:8, :8].flatten()          # 低频块(含 DC)
    med = np.median(block[1:])           # 中位数排除 DC
    bits = block > med
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


# ---------- 全库计算 ----------

def _load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


class HashState:
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


def _hash_source(f: dict, root: Path, jpg_for: dict, preview_dir: Path):
    """返回某照片用于哈希的图片字节来源(JPG 原图 / 同组 JPG / RAW 预览缓存)。"""
    if f["t"] == "jpg":
        return root / f["p"]
    i = f["p"].rfind("/")
    pair = jpg_for.get((f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower()))
    if pair:
        return root / pair
    key = __import__("hashlib").md5(f["p"].encode()).hexdigest()[:16] + ".jpg"
    cached = preview_dir / key
    return cached if cached.exists() else None


def compute_hashes(root, exiftool, files, state: HashState = None, on_progress=None) -> dict:
    """补算缺失的哈希并缓存;返回全量 {路径: 哈希十六进制}。"""
    from PIL import Image
    from concurrent.futures import ThreadPoolExecutor

    root = Path(root)
    hashes = _load()
    jpg_for = {}
    for f in files:
        if f["t"] == "jpg":
            i = f["p"].rfind("/")
            jpg_for[(f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower())] = f["p"]
    preview_dir = classifier.CACHE_DIR / "cls_previews"

    todo = [f for f in files if f["t"] != "video" and f["p"] not in hashes]
    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = len(todo), 0, True, None
    if not todo:
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return {k: int(v, 16) for k, v in hashes.items()}

    def work(f):
        src = _hash_source(f, root, jpg_for, preview_dir)
        if src is None:
            return f["p"], None
        try:
            img = Image.open(io.BytesIO(Path(src).read_bytes()))
            if img.format == "JPEG" and img.size[0] > 128:
                img.draft("L", (128, 128))
            return f["p"], phash_of(img)
        except Exception:
            return f["p"], None

    done_n = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for p, h in pool.map(work, todo):
            if h is not None:
                hashes[p] = format(h, "016x")
            done_n += 1
            if state:
                with state.lock:
                    state.done = done_n
            if on_progress:
                on_progress(done_n, len(todo))

    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(hashes, ensure_ascii=False), encoding="utf-8")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return {k: int(v, 16) for k, v in hashes.items()}


# ---------- 重复组检测 ----------

def find_visual_dups(hashes: dict, threshold: int = THRESHOLD) -> list:
    """跨文件夹视觉重复组:并查集合并距离 ≤ threshold 的对。返回 [[路径,...],...] 按组大小排序。"""
    paths = list(hashes.keys())
    if len(paths) < 2:
        return []
    vals = np.array([hashes[p] for p in paths], dtype=np.uint64)
    dirs = [p.rsplit("/", 1)[0] if "/" in p else "" for p in paths]
    bases = [p.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower() for p in paths]
    n = len(paths)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    arr_bytes = vals.view(np.uint8).reshape(n, 8)
    chunk = 512
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        xor = arr_bytes[s:e, None, :] ^ arr_bytes[None, :, :]       # (c, n, 8)
        dist = _POPCOUNT[xor].sum(axis=2)                            # (c, n)
        for di in range(e - s):
            i = s + di
            js = np.nonzero(dist[di] <= threshold)[0]
            for j in js:
                j = int(j)
                # 排除:同文件夹、自身、同名 RAW/JPG 伴生文件
                if j != i and dirs[i] != dirs[j] and bases[i] != bases[j]:
                    union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(paths[i])
    return sorted((g for g in groups.values() if len(g) > 1),
                  key=lambda g: -len(g))
