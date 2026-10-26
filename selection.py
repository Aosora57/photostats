# -*- coding: utf-8 -*-
"""统一选片评分引擎(B4):把分散的信号(美学/清晰度/闭眼/星级/同场景分组)
融合为每张照片的综合选片分(0-10)、组内状态(best/ok/soft/cull)与中文理由。

数据源(均已有):
  aesthetic.json        美学分 0-10
  quality.json          [lap_var, lap_max, over, under]
  blink.json            photos: {路径: 是否含疑似闭眼脸}, faces: {key: 眼部std}
  softness.json         clusters: 同场景组(嵌入余弦 ≥0.90,lap_max 降序)
  ratings.json          人工星级 0-5

输出 cache/selection.json:
  {sig, computedAt, items: {路径: {score, state, reasons:[...]}}}
  state ∈ best(组内最佳)/ ok(正常)/ soft(同簇偏软)/ cull(建议删除)
sig = 嵌入包签名 + quality/blink 文件 mtime —— 任一数据更新自动重算。
"""
import hashlib
import json
import threading
import time
from pathlib import Path

import numpy as np

import classifier
import quality

CACHE_FILE = classifier.CACHE_DIR / "selection.json"

# 综合分权重(可调)
W_AES = 0.6          # 美学分权重(0-10 直接计入)
W_SHARP = 4.0        # 清晰度归一分权重(0-1 × 4 = 最高 4 分)
W_RATING = 0.3       # 人工星级权重(0-5 × 0.3)
BLINK_PENALTY = 1.5  # 疑似闭眼罚分
CULL_GAP = 1.5       # 组内低于最佳该分数 → 建议删除
SOFT_RATIO = 0.60    # 清晰度低于组中位该比例 → 同簇偏软

_state_lock = threading.Lock()


def _sig(root: Path) -> str:
    """数据签名:嵌入包 + quality/blink/softness 文件 mtime。"""
    import encoders
    pack = encoders.active_pack()
    parts = [encoders.embed_sig(pack)]
    for f in ("quality.json", "blink.json", "softness.json"):
        p = classifier.CACHE_DIR / f
        parts.append(f"{f}:{int(p.stat().st_mtime) if p.exists() else 0}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _load_json(name: str) -> dict:
    f = classifier.CACHE_DIR / name
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _sharp01(lap_max: float, lap_values: list) -> float:
    """清晰度归一:log 缩放到 0-1(lap_max 跨数量级,log 更平稳)。"""
    if lap_max <= 0:
        return 0.0
    return float(min(1.0, np.log10(lap_max + 1) / np.log10(3000)))


def compute_selection(state=None, on_progress=None) -> dict:
    """计算全库综合选片分与组内状态,存 cache/selection.json。"""
    aes = _load_json("aesthetic.json")
    qdata = _load_json("quality.json")
    blink = _load_json("blink.json")
    soft = _load_json("softness.json")
    ratings = _load_json("ratings.json")

    sig = _sig(Path("."))
    if _valid_cache(sig):
        result = _load_selection()
        if state:
            with state.lock:
                state.total, state.done, state.running = 0, 0, False
                state.finishedAt = time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    lap_of = {p: m[1] for p, m in qdata.get("items", {}).items()}
    blink_photos = blink.get("photos", {})
    blink_facestd = blink.get("faces", {})
    faces_data = _load_faces()
    crop2path = {r["k"]: p for p, recs in faces_data.items() for r in recs}

    # 1) 逐张基础分与理由
    items = {}
    all_paths = set(lap_of) | set(aes)
    paths = sorted(all_paths)
    total = len(paths)
    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = total, 0, True, None
    for p in paths:
        reasons = []
        q = qdata.get("items", {}).get(p)
        lap_max = q[1] if q else None
        aes_v = aes.get(p)
        rating = ratings.get(p, 0)
        sharp01 = _sharp01(lap_max, []) if lap_max is not None else None
        is_blink = bool(blink_photos.get(p))
        score = 0.0
        if aes_v is not None:
            score += W_AES * aes_v
        else:
            reasons.append("缺美学分(未识别)")
        if sharp01 is not None:
            score += W_SHARP * sharp01
            reasons.append(f"清晰度 {int(lap_max)}")
            if lap_max < quality.BLUR_BLOCK_MAX:
                reasons.insert(0, "严重糊片")
        if is_blink:
            score -= BLINK_PENALTY
            key = next((k for k, v in blink_facestd.items()
                        if crop2path.get(k) == p and v < 13.0), None)
            if key:
                reasons.append(f"疑似闭眼(眼部纹理 {blink_facestd[key]})")
            else:
                reasons.append("含疑似闭眼脸")
        if rating:
            score += W_RATING * rating
            reasons.append(f"人工 {rating} 星")
        q = qdata.get("items", {}).get(p)
        if q:
            if q[2] > 0.10:
                reasons.append(f"过曝 {round(q[2] * 100)}%")
            if q[3] > 0.35:
                reasons.append(f"欠曝 {round(q[3] * 100)}%")
        items[p] = {"score": round(score, 2), "state": "ok", "reasons": reasons}

    # 2) 组内相对:同场景组 → 排名/组内最佳/同簇偏软/建议删除
    clusters = soft.get("clusters", [])
    best_paths = []
    for members in clusters:
        scored = [(p, items.get(p, {}).get("score")) for p in members if p in items]
        if len(scored) < 2:
            continue
        scored.sort(key=lambda x: -x[1])
        best_p, best_s = scored[0]
        best_paths.append(best_p)
        laps = [lap_of.get(p) for p, _ in scored if lap_of.get(p) is not None]
        med_lap = float(np.median(laps)) if laps else 0
        items[best_p]["state"] = "best"
        items[best_p]["reasons"].insert(0, f"组内最佳(综合 {best_s})")
        rank = 1
        for p, sc in scored[1:]:
            it = items[p]
            gap = round(best_s - sc, 1)
            lap_p = lap_of.get(p)
            if lap_p and med_lap > 0 and lap_p < SOFT_RATIO * med_lap:
                it["state"] = "soft"
                it["reasons"].insert(0, f"清晰度比同组中位低 {round((1 - lap_p / med_lap) * 100)}%")
            elif gap >= CULL_GAP:
                it["state"] = "cull"
                it["reasons"].insert(0, f"低于组内最佳 {gap} 分")
            else:
                it["state"] = "ok"
            rank += 1

    result = {"sig": sig,
              "computedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
              "items": items}
    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    if state:
        with state.lock:
            state.done, state.running = total, False
            state.finishedAt = time.strftime("%Y-%m-%d %H:%M:%S")
    if on_progress:
        on_progress(total, total)
    return result


def _valid_cache(sig: str) -> bool:
    f = CACHE_FILE
    if not f.exists():
        return False
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d.get("sig") == sig and "items" in d
    except json.JSONDecodeError:
        return False


def _load_selection() -> dict:
    f = CACHE_FILE
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"items": {}}


def load_selection() -> dict:
    """对外读取(自动校验签名,失效返回空)。"""
    f = CACHE_FILE
    if not f.exists():
        return {"items": {}}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("sig") != _sig(Path(".")):
            return {"items": {}}
        return d
    except json.JSONDecodeError:
        return {"items": {}}


def _load_faces() -> dict:
    f = classifier.CACHE_DIR / "faces.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


class SelectionState:
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
