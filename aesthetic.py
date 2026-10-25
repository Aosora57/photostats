# -*- coding: utf-8 -*-
"""美学评分(E4):LAION aesthetic predictor v1 线性头(OpenAI CLIP ViT-B/32 空间)
直接作用于已存储的图像嵌入(embeddings.npz,天然 L2 归一化),无需再处理图像。

头权重:tools/models/aesthetic_head.safetensors 的 predictor.weight/bias(512→1)。
输出为 AVA 风格评分(约 0-10,越高越美),存 cache/aesthetic.json。
"""
import json
import threading
import time
from pathlib import Path

import numpy as np

import classifier

HEAD_FILE = Path(__file__).resolve().parent / "tools" / "models" / "aesthetic_head.safetensors"
CACHE_FILE = classifier.CACHE_DIR / "aesthetic.json"

_lock = threading.Lock()
_HEAD_CACHE = None


def _head() -> np.ndarray:
    """返回 (weight (1,512), bias (1,))。"""
    global _HEAD_CACHE
    with _lock:
        if _HEAD_CACHE is not None:
            return _HEAD_CACHE
        data = HEAD_FILE.read_bytes()
        n = int.from_bytes(data[:8], "little")
        header = json.loads(data[8:8 + n])
        body = data[8 + n:]
        W = b = None
        for name, info in header.items():
            if name.startswith("__") or not name.startswith("predictor."):
                continue
            dt = np.float32 if info["dtype"] == "F32" else np.float16
            cnt = (info["data_offsets"][1] - info["data_offsets"][0]) // np.dtype(dt).itemsize
            arr = np.frombuffer(body, dtype=dt, count=cnt,
                                offset=info["data_offsets"][0]).astype(np.float32)
            if name.endswith("weight"):
                W = arr.reshape(info["shape"])
            else:
                b = arr.reshape(-1)
        _HEAD_CACHE = (W, b)
        return _HEAD_CACHE


def score_embeddings(paths: list, vecs: np.ndarray) -> dict:
    """对(已归一化)嵌入打美学分,返回 {路径: 分数(0-10, 保留两位)}。"""
    W, b = _head()
    M = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    raw = (M @ W.T)[:, 0] + (b[0] if b is not None else 0.0)
    return {p: round(float(np.clip(v, 0.0, 10.0)), 2) for p, v in zip(paths, raw)}


def compute_all(state=None, on_progress=None) -> dict:
    """给所有已有嵌入的照片打分(秒级),存 cache/aesthetic.json。"""
    import encoders
    pack = encoders.active_pack()
    paths, vecs = classifier._load_embeddings(pack)
    items = _load()
    todo = [(p, i) for i, p in enumerate(paths) if p not in items]
    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = len(todo), 0, True, None
    if not todo:
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return items
    W, b = _head()
    sub = vecs[[i for _, i in todo]]
    sub = sub / np.linalg.norm(sub, axis=1, keepdims=True)
    raw = (sub @ W.T)[:, 0] + (b[0] if b is not None else 0.0)
    for (p, _), v in zip(todo, raw):
        items[p] = round(float(np.clip(v, 0.0, 10.0)), 2)
        if state:
            with state.lock:
                state.done = len(items)
        if on_progress:
            on_progress(state.done if state else 0, len(todo))
    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return items


def _load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def load_scores() -> dict:
    return _load()


class AestheticState:
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
