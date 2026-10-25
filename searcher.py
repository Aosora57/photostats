# -*- coding: utf-8 -*-
"""语义搜索与相似照片:基于 classifier 存储的图像嵌入(npz)+ 模型包的查询编码器。

查询编码器由 encoders.active_pack() 的 query_text 配置决定(当前默认包用
多语言蒸馏编码器,中/英文均可;换包后自动跟随,详见 encoders.py)。
"""
import threading

import numpy as np

import classifier
import encoders

_lock = threading.Lock()
_emb_cache = None       # (mtime, paths, 归一化矩阵)


def _l2n(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def _matrix() -> tuple:
    """嵌入矩阵缓存,按 npz 修改时间失效。返回 (paths, 归一化矩阵)。"""
    global _emb_cache
    pack = encoders.active_pack()
    f = classifier.CACHE_DIR / "embeddings.npz"
    mt = f.stat().st_mtime if f.exists() else 0
    if _emb_cache is not None and _emb_cache[0] == mt:
        return _emb_cache[1], _emb_cache[2]
    paths, vecs = classifier._load_embeddings(pack)
    m = _l2n(vecs) if len(vecs) else vecs
    _emb_cache = (mt, paths, m)
    return paths, m


def info() -> dict:
    pack = encoders.active_pack()
    paths, _ = _matrix()
    mode = "multilingual" if pack["query_text"]["kind"] == "st-multilingual" else "english"
    return {"mode": mode, "count": len(paths)}


def search(q: str, top: int = 200) -> list:
    """语义搜索:返回 [[路径, 相似度], ...] 按相似度降序。"""
    paths, m = _matrix()
    if not len(paths):
        return []
    sims = m @ encoders.query_embed(encoders.active_pack(), q)
    order = np.argsort(-sims)[:top]
    return [[paths[i], round(float(sims[i]), 4)] for i in order]


def similar(p: str, top: int = 20) -> list:
    """相似照片:返回 [[路径, 相似度], ...],不含自身。"""
    paths, m = _matrix()
    if not len(paths) or p not in set(paths):
        return []
    i = paths.index(p)
    sims = m @ m[i]
    order = np.argsort(-sims)
    out = []
    for j in order:
        if paths[j] != p:
            out.append([paths[j], round(float(sims[j]), 4)])
        if len(out) >= top:
            break
    return out
