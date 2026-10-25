# -*- coding: utf-8 -*-
"""人脸检测与人物聚类(B2):YuNet 检测 + 5 点对齐 + SFace 嵌入 + 余弦聚类。

模型:OpenCV Zoo 的 face_detection_yunet_2023mar(原始头输出,本模块负责解码)
与 face_recognition_sface(112×112 → 128 维),均在 tools/models/face/。

管线:取图(JPG/同组JPG/cls_previews)→ letterbox 到 640×640 → YuNet 检测
→ 5 点相似变换对齐 112×112 → SFace 嵌入 → 贪心聚类(余弦 ≥0.363)→ 人物。
人脸裁剪图缓存到 cache/face_crops/ 供界面头像使用。
"""
import hashlib
import io
import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import classifier
import phash

FACE_DIR = Path(__file__).resolve().parent / "tools" / "models" / "face"
CACHE_FILE = classifier.CACHE_DIR / "faces.json"
CROP_DIR = classifier.CACHE_DIR / "face_crops"

DET_SIZE = 1024
DET_THRESH = 0.6
SIM_THRESH = 0.363      # SFace 余弦同人阈值(OpenCV Zoo 官方)

_tls = threading.local()   # cv2 DNN 对象非线程安全,每线程独立实例
_lock = threading.Lock()



def _detector():
    if not hasattr(_tls, "det"):
        import cv2
        _tls.det = cv2.FaceDetectorYN.create(
            str(FACE_DIR / "yunet.onnx"), "", (DET_SIZE, DET_SIZE),
            score_threshold=DET_THRESH)
    return _tls.det


def _recognizer():
    if not hasattr(_tls, "rec"):
        import cv2
        _tls.rec = cv2.FaceRecognizerSF.create(str(FACE_DIR / "sface.onnx"), "")
    return _tls.rec


def detect(img_bgr: np.ndarray, score_thresh=DET_THRESH):
    """BGR 图 → [(x1,y1,x2,y2,score,kps5)](原图坐标,cv2.FaceDetectorYN 全托管)。"""
    det = _detector()
    H, W = img_bgr.shape[:2]
    scale = min(1.0, DET_SIZE / max(W, H))
    nw, nh = round(W * scale), round(H * scale)
    work = cv2.resize(img_bgr, (nw, nh)) if scale < 1 else img_bgr
    det.setInputSize((nw, nh))
    _, dets = det.detect(work)
    out = []
    if dets is not None:
        for r in dets:
            x, y, w, h, score = r[0], r[1], r[2], r[3], r[14]
            kps5 = [(r[4 + 2 * k] / scale, r[5 + 2 * k] / scale) for k in range(5)]
            out.append((x / scale, y / scale, (x + w) / scale, (y + h) / scale,
                        float(score), kps5))
    return out


def align_and_embed(img_bgr: np.ndarray, faces) -> np.ndarray:
    """人脸列表 → (N,128) 归一化嵌入(含对齐裁剪图,供缓存)。"""
    rec = _recognizer()
    rows = []
    keep = []
    for f in faces:
        row = np.array([f[0], f[1], f[2] - f[0], f[3] - f[1]]
                       + [c for pt in f[5] for c in pt], dtype=np.float32)
        rows.append(row)
        keep.append(f)
    if not rows:
        return np.zeros((0, 128), dtype=np.float32), []
    embs, crops = [], []
    for row in rows:
        aligned = rec.alignCrop(img_bgr, row)       # 112×112 BGR,官方对齐
        emb = rec.feature(aligned).flatten()        # (128,)
        embs.append(emb)
        crops.append(aligned)
    embs = np.stack(embs).astype(np.float32)
    embs = embs / np.linalg.norm(embs, axis=1, keepdims=True)
    return embs, crops


# ---------- 全库管线 ----------

class FaceState:
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


def _source_image(f: dict, root: Path, jpg_for: dict, preview_dir: Path):
    """复用 phash 的取图规则,返回解码后的 BGR 数组或 None。"""
    from PIL import Image
    src = phash._hash_source(f, root, jpg_for, preview_dir)
    if src is None or not Path(src).exists():
        return None
    try:
        img = Image.open(io.BytesIO(Path(src).read_bytes())).convert("RGB")
        w, h = img.size
        scale = min(1.0, 1024 / max(w, h))
        if scale < 1:
            img = img.resize((round(w * scale), round(h * scale)), 3)
        return np.asarray(img)[:, :, ::-1].copy()  # RGB→BGR
    except Exception:
        return None


def compute_faces(root, exiftool, files, state: FaceState = None, on_progress=None) -> dict:
    """全库人脸检测+嵌入+聚类。结果存 cache/faces.json。"""
    from PIL import Image
    from concurrent.futures import ThreadPoolExecutor

    root = Path(root)
    faces_data = _load_faces()
    jpg_for = {}
    for f in files:
        if f["t"] == "jpg":
            i = f["p"].rfind("/")
            jpg_for[(f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower())] = f["p"]
    preview_dir = classifier.CACHE_DIR / "cls_previews"
    todo = [f for f in files if f["t"] != "video" and f["p"] not in faces_data]
    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = len(todo), 0, True, None
    if not todo:
        result = _cluster(faces_data)
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    CROP_DIR.mkdir(parents=True, exist_ok=True)
    embs, keys = [], []
    done_n = 0

    def work(f):
        src = phash._hash_source(f, root, jpg_for, preview_dir)
        if src is None or not Path(src).exists():
            return f["p"], None, None
        try:
            img = Image.open(io.BytesIO(Path(src).read_bytes())).convert("RGB")
            w, h = img.size
            sc = min(1.0, 1024 / max(w, h))
            if sc < 1:
                img = img.resize((round(w * sc), round(h * sc)), 3)
            bgr = np.asarray(img)[:, :, ::-1].copy()
            fs = detect(bgr, DET_THRESH)
            if not fs:
                return f["p"], [], None
            embs_n, crops = align_and_embed(bgr, fs)
            recs, crops_out = [], []
            for k, (x1, y1, x2, y2, score, kps5) in enumerate(fs):
                key = hashlib.md5(f"{f['p']}#{k}".encode()).hexdigest()[:16]
                crop_path = CROP_DIR / (key + ".jpg")
                ci = Image.fromarray(crops[k][:, :, ::-1].astype(np.uint8))
                ci.save(crop_path, quality=90)
                recs.append({"k": key, "box": [round(x1), round(y1), round(x2), round(y2)],
                             "s": round(score, 3), "c": str(crop_path)})
                crops_out.append(crops[k])
            return f["p"], recs, embs_n
        except Exception:
            return f["p"], None, None

    with ThreadPoolExecutor(max_workers=6) as pool:
      for p, recs, embs_n in pool.map(work, todo):
        if recs:
            faces_data[p] = recs
            for k, e in enumerate(embs_n):
                embs.append(e)
                keys.append(f"{p}#{k}")
        done_n += 1
        if state:
            with state.lock:
                state.done = done_n
        if on_progress:
            on_progress(done_n, len(todo))

    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(faces_data, ensure_ascii=False), encoding="utf-8")
    np.savez_compressed(classifier.CACHE_DIR / "face_embs.npz",
                        keys=np.array(keys), vecs=np.stack(embs) if embs else np.zeros((0, 128), np.float32))
    result = _cluster(faces_data, keys, np.stack(embs) if embs else None)
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def _load_faces() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _cluster(faces_data: dict, keys=None, embs=None) -> dict:
    """贪心聚类:按检测分排序,余弦 ≥SIM_THRESH 归入既有人物,否则新建。"""
    d = np.load(classifier.CACHE_DIR / "face_embs.npz", allow_pickle=False)
    if keys is None:
        keys = list(d["keys"].tolist())
        embs = d["vecs"]
    if len(keys) == 0:
        return {"persons": [], "faces": faces_data}
    embs = embs / np.linalg.norm(embs, axis=1, keepdims=True)
    order = sorted(range(len(keys)), key=lambda i: -faces_data
                   [keys[i].rsplit("#", 1)[0]][int(keys[i].rsplit("#", 1)[1])]["s"])
    persons = []  # [{sum, count, member:[face_idx]}]
    assign = {}
    for fi in order:
        v = embs[fi]
        best, best_sim = -1, 0
        for pi, p in enumerate(persons):
            sim = float(v @ (p["sum"] / p["count"]))
            if sim > best_sim:
                best, best_sim = pi, sim
        if best_sim >= SIM_THRESH:
            persons[best]["sum"] += v
            persons[best]["count"] += 1
            persons[best]["member"].append(fi)
            assign[fi] = best
        else:
            persons.append({"sum": v.copy(), "count": 1, "member": [fi]})
            assign[fi] = len(persons) - 1

    out_persons = []
    for pi, p in enumerate(persons):
        if p["count"] < 3:  # 少于 3 张脸的散簇不建人物
            for fi in p["member"]:
                assign[fi] = -1
            continue
        rep = max(p["member"], key=lambda fi: faces_data[keys[fi].rsplit("#", 1)[0]]
                  [int(keys[fi].rsplit("#", 1)[1])]["s"])
        out_persons.append({"id": len(out_persons), "count": p["count"],
                            "rep": {"p": keys[rep].rsplit("#", 1)[0],
                                    "k": faces_data[keys[rep].rsplit("#", 1)[0]][int(keys[rep].rsplit("#", 1)[1])]["k"]}})
    result = {"persons": out_persons, "faces": faces_data}
    (classifier.CACHE_DIR / "persons.json").write_text(
        json.dumps({"persons": out_persons,
                    "assign": {keys[i]: assign[i] for i in range(len(keys))}},
                   ensure_ascii=False), encoding="utf-8")
    return result


def load_persons() -> dict:
    f = classifier.CACHE_DIR / "persons.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"persons": [], "assign": {}}


# ---------- 人物编辑(重命名/合并) ----------

EDITS_FILE = classifier.CACHE_DIR / "person_edits.json"


def load_edits() -> dict:
    if EDITS_FILE.exists():
        try:
            d = json.loads(EDITS_FILE.read_text(encoding="utf-8"))
            mi = {int(k): int(v) for k, v in d.get("mergeInto", {}).items()}
            return {"names": d.get("names", {}), "mergeInto": mi}
        except (json.JSONDecodeError, ValueError):
            pass
    return {"names": {}, "mergeInto": {}}


def save_edits(edits: dict) -> None:
    EDITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EDITS_FILE.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")


def resolve_persons() -> dict:
    """聚类结果 + 用户编辑(改名/合并)→ 界面用的人物视图。

    返回 {persons: [{id, name, count(去重照片数), faces(人脸数), rep}],
          assign: {路径: [人物id,...]}}。id 为合并后的规范 id。
    """
    d = load_persons()
    edits = load_edits()
    names, merge_into = edits["names"], edits["mergeInto"]

    def canon(pid):
        seen = set()
        while pid in merge_into and pid not in seen:
            seen.add(pid)
            pid = merge_into[pid]
        return pid

    groups = {}  # canon -> {faces, photos:set, rep, rep_faces}
    for p in d.get("persons", []):
        c = canon(p["id"])
        g = groups.setdefault(c, {"faces": 0, "photos": set(), "rep": p["rep"],
                                  "rep_faces": p["count"]})
        g["faces"] += p["count"]
        if p["count"] > g["rep_faces"]:  # 合并后用出勤最多那个簇的代表脸
            g["rep"], g["rep_faces"] = p["rep"], p["count"]

    photos_per = {}
    for key, cid in d.get("assign", {}).items():
        c = canon(cid)
        if c not in groups:
            continue
        path = key.rsplit("#", 1)[0]
        photos_per.setdefault(c, set()).add(path)
    for c, g in groups.items():
        g["count"] = len(photos_per.get(c, set()))

    persons = [{"id": c, "name": names.get(str(c), f"人物 {c + 1}"),
                "count": g["count"], "faces": g["faces"], "rep": g["rep"]}
               for c, g in sorted(groups.items())]
    persons.sort(key=lambda p: -p["count"])
    # assign:路径 → 规范人物 id 列表
    assign = {}
    for key, cid in d.get("assign", {}).items():
        c = canon(cid)
        if c in groups:
            path = key.rsplit("#", 1)[0]
            assign.setdefault(path, set()).add(c)
    assign = {k: sorted(v) for k, v in assign.items()}
    return {"persons": persons, "assign": assign}


def rename_person(pid: int, name: str) -> dict:
    edits = load_edits()
    name = (name or "").strip()
    if name:
        edits["names"][str(pid)] = name
    else:
        edits["names"].pop(str(pid), None)
    save_edits(edits)
    return resolve_persons()


def merge_persons(from_ids, to_id: int) -> dict:
    """把 from_ids(聚类簇 id)合并进 to_id(链式合并)。"""
    edits = load_edits()
    mi = edits["mergeInto"]
    to_id = int(to_id)
    for fid in from_ids:
        fid = int(fid)
        if fid == to_id:
            continue
        seen = set()
        cur = fid
        while cur in mi and cur not in seen:
            seen.add(cur)
            cur = mi[cur]
        if cur != to_id:
            mi[fid] = to_id
    save_edits(edits)
    return resolve_persons()


def reset_person_edits() -> dict:
    save_edits({"names": {}, "mergeInto": {}})
    return resolve_persons()


