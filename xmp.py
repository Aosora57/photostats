# -*- coding: utf-8 -*-
"""XMP 标签回写(C4):把 AI 题材写成 sidecar 文件,让 Lightroom/digiKam 可读。

安全策略:
  - 只创建"缺失"的 .xmp sidecar,绝不覆盖已存在的 sidecar(可能含 Lightroom
    的修图参数,覆盖会丢失)也不修改原片
  - sidecar 与照片同目录同名,扩展名 .xmp
  - 写入字段:dc:subject 关键词(题材名 + "PhotoStats")
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import classifier

CACHE_FILE = classifier.CACHE_DIR / "xmp_state.json"

XMP_TEMPLATE = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmp:Rating="0">
   <dc:subject>
    <rdf:Bag>
{li}
    </rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def sidecar_path(root: Path, rel: str) -> Path:
    return Path(root) / (rel.rsplit(".", 1)[0] + ".xmp")


RATINGS_FILE = classifier.CACHE_DIR / "ratings.json"


def load_ratings() -> dict:
    if RATINGS_FILE.exists():
        try:
            return json.loads(RATINGS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def set_rating(root, exiftool: str, rel: str, stars: int) -> dict:
    """设置星级(0-5):更新 ratings.json 并写入 XMP sidecar(exiftool 原位更新,
    不存在则 -o 创建,连带照片标签;已有 Lightroom sidecar 的其他字段不受影响)。"""
    root = Path(root)
    stars = max(0, min(5, int(stars)))
    ratings = load_ratings()
    if stars == 0:
        ratings.pop(rel, None)
    else:
        ratings[rel] = stars
    RATINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RATINGS_FILE.write_text(json.dumps(ratings, ensure_ascii=False), encoding="utf-8")

    sc = sidecar_path(root, rel)
    exif = str(Path(exiftool).resolve()) if not Path(exiftool).is_absolute() else exiftool
    try:
        if sc.exists():
            subprocess.run([exiftool, f"-XMP:Rating={stars}", "-overwrite_original",
                            str(sc)], capture_output=True, timeout=60)
        else:
            subprocess.run([exiftool, f"-XMP:Rating={stars}", "-o", str(sc),
                            str(root / rel)], capture_output=True, timeout=60)
    except Exception:
        pass  # 评分主存储在 ratings.json,XMP 写失败不阻塞
    return {"path": rel, "stars": stars, "sidecar": str(sc)}


def xmp_xml(subject: str) -> str:
    li = "\n".join(f'     <rdf:li>{s}</rdf:li>' for s in (subject, "PhotoStats"))
    return XMP_TEMPLATE.format(li=li)


def _load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {"written": {}}


def preview(root, files, subjects: dict) -> dict:
    """统计可写/已写数量。"""
    root = Path(root)
    written = set(_load().get("written", []))
    to_write = []
    for f in files:
        if f["t"] == "video" or f["p"] not in subjects:
            continue
        sc = sidecar_path(root, f["p"])
        if f["p"] in written or sc.exists():
            continue
        to_write.append((f["p"], subjects[f["p"]][0]))
    return {"withSubject": len([f for f in files if f["p"] in subjects and f["t"] != "video"]),
            "alreadyWritten": len(written),
            "toWrite": len(to_write),
            "list": to_write}


class XmpState:
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


def write_sidecars(root, files, subjects: dict, state: XmpState = None, on_progress=None) -> dict:
    """为没有 sidecar 的照片创建 XMP(并行)。已存在的 sidecar 一律跳过。"""
    from concurrent.futures import ThreadPoolExecutor

    root = Path(root)
    st_data = _load()
    written = set(st_data.get("written", []))
    todo = []
    for f in files:
        if f["t"] == "video" or f["p"] not in subjects:
            continue
        sc = sidecar_path(root, f["p"])
        if f["p"] in written or sc.exists():
            continue
        todo.append((f["p"], str(sc), subjects[f["p"]][0]))

    if state:
        with state.lock:
            state.total, state.done, state.running, state.error = len(todo), 0, True, None
    if not todo:
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return {"written": len(written), "new": 0}

    done_n = 0
    new_paths = []

    def work(t):
        p, sc, subj = t
        try:
            Path(sc).write_text(xmp_xml(subj), encoding="utf-8")
            return p
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for p in pool.map(work, todo):
            if p:
                new_paths.append(p)
            done_n += 1
            if state:
                with state.lock:
                    state.done = done_n
            if on_progress:
                on_progress(done_n, len(todo))

    written.update(new_paths)
    st_data["written"] = sorted(written)
    classifier.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(st_data, ensure_ascii=False), encoding="utf-8")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return {"written": len(written), "new": len(new_paths)}
