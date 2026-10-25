# -*- coding: utf-8 -*-
"""CLIP 零样本题材分类:图片经 ONNX 视觉编码,与候选题材的文本嵌入算相似度。

优先用 DirectML(任意 DX12 显卡,如 NVIDIA/AMD/Intel)加速,无显卡时回落 CPU。
分类结果缓存到 cache/classifications.json:{路径: [题材, 置信度]}。
"""
import hashlib
import io
import json
import threading
import time
from pathlib import Path

import numpy as np

import encoders

# 题材:中文标签 + 英文提示词(多做提示词集成提升稳定性)
SUBJECTS = [
    ("人像", ["a portrait photo of a person", "a close-up photo of a person's face",
              "a photo of a woman or a man posing for the camera",
              "a photo of a group of people posing together",
              "a wedding photo of the bride or the couple",
              "a photo of students at a graduation ceremony"]),
    ("风景", ["a landscape photo of natural scenery", "a photo of mountains, lakes or sunsets in nature",
              "a scenic photo of the outdoors"]),
    ("动物", ["a photo of an animal", "a photo of a pet like a cat or a bird",
              "a wildlife photo of an animal"]),
    ("美食", ["a photo of food or drinks", "a photo of a meal or a dish on a table",
              "a close-up photo of delicious food"]),
    ("建筑", ["a photo of a building or architecture", "a photo of a city skyline or a modern building",
              "a photo of a temple, church, tower or bridge"]),
    ("夜景", ["a night scene photo with city lights", "a long exposure photo taken at night",
              "a photo of illuminated streets or neon lights at night"]),
    ("街拍", ["a street photography photo of people in the city", "a candid photo of people walking on a street",
              "a photo of a busy street scene in town"]),
    ("微距", ["a macro close-up photo of a small flower or insect", "an extreme close-up photo of a tiny subject",
              "a close-up photo showing fine details of a small object"]),
    ("花卉", ["a photo of flowers as the main subject", "a close-up photo of cherry blossoms or plum blossoms",
              "a photo of a flower field or a garden in bloom"]),
    ("星空", ["an astrophotography photo of the starry night sky", "a photo of the milky way or a sky full of stars",
              "a photo of the moon or a comet in the night sky"]),
    ("其他", ["a photo", "an everyday snapshot photo", "a random photo of miscellaneous things"]),
]

BATCH = 32
CONF_SCALE = 100.0

MODELS_DIR = Path(__file__).resolve().parent / "tools" / "models"
CACHE_DIR = Path(__file__).resolve().parent / "cache"


def _l2n(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def _text_embeds(pack: dict) -> np.ndarray:
    """所有提示词的文本嵌入,按题材取平均并归一化;结果缓存到磁盘(签名含模型包)。"""
    cache = CACHE_DIR / "text_embeds.json"
    sig = hashlib.md5((encoders.embed_sig(pack) + "|" +
                       json.dumps(SUBJECTS, ensure_ascii=False)).encode()).hexdigest()
    if cache.exists():
        d = json.loads(cache.read_text(encoding="utf-8"))
        if d.get("sig") == sig:
            return np.array(d["embeds"], dtype=np.float32)
    prompts, group_idx = [], []
    for gi, (_, ps) in enumerate(SUBJECTS):
        for p in ps:
            prompts.append(p)
            group_idx.append(gi)
    out = encoders.encode_texts(pack, "classify_text", prompts)
    per_prompt = _l2n(out)                       # (提示词数, dim)
    dim = out.shape[-1]
    embeds = np.zeros((len(SUBJECTS), dim), dtype=np.float32)
    for gi in range(len(SUBJECTS)):
        rows = per_prompt[[k for k, g in enumerate(group_idx) if g == gi]]
        embeds[gi] = _l2n(rows.mean(axis=0))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"sig": sig, "embeds": embeds.tolist()}), encoding="utf-8")
    return embeds


def _preprocess(img, pack: dict) -> np.ndarray:
    v = pack["vision"]
    size, mean = v["image_size"], np.array(v["mean"], dtype=np.float32)
    std = np.array(v["std"], dtype=np.float32)
    img = img.convert("RGB")
    w, h = img.size
    scale = size / min(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), 3)  # bicubic
    w, h = img.size
    left, top = (w - size) // 2, (h - size) // 2
    a = np.asarray(img.crop((left, top, left + size, top + size)), dtype=np.float32) / 255.0
    a = (a - mean) / std
    return a.transpose(2, 0, 1)


class ClassifyState:
    """供服务端轮询的识别进度;phase = extract(提取NEF预览)/classify(模型识别)。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.phase = "classify"
        self.error = None
        self.finishedAt = None

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "done": self.done, "total": self.total,
                    "phase": self.phase, "error": self.error, "finishedAt": self.finishedAt}


def classify_all(root: Path, exiftool: str, files: list, state: ClassifyState = None,
                 on_progress=None) -> dict:
    """对图库中所有照片分类(视频跳过),返回 {路径: [题材, 置信度]}。

    JPG 直接分类;RAW 优先复用同组 JPG 的结果,无 JPG 的 NEF 提取预览图(缓存后离线复用)。
    进度:提取与识别共用一个计数器,done/total 覆盖全流程。
    """
    import scanner  # noqa: F401 (保持与扫描器一致的依赖提示)
    from PIL import Image

    pack = encoders.active_pack()
    root = Path(root)
    out_path = CACHE_DIR / "classifications.json"
    result = _load_result()  # 题材定义或模型包变更时自动作废旧结果
    emb_paths, emb_matrix = _load_embeddings(pack)
    emb_set = set(emb_paths)

    # 配对组:同目录同名 JPG 可代表 RAW
    jpg_for = {}
    for f in files:
        if f["t"] == "jpg":
            i = f["p"].rfind("/")
            jpg_for[(f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower())] = f["p"]

    # 计划任务:('img', 要读的文件) 或 ('raw', 待提取预览的 RAW)
    # 缺分类或缺嵌入的照片都要进队列(推理同时产出两者)
    jobs = []
    for f in files:
        if f["t"] == "video" or (f["p"] in result and f["p"] in emb_set):
            continue
        if f["t"] == "jpg":
            jobs.append((f["p"], "img", f["p"]))
        else:
            i = f["p"].rfind("/")
            pair = jpg_for.get((f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower()))
            jobs.append((f["p"], "img", pair) if pair else (f["p"], "raw", f["p"]))

    raw_jobs = [(rel, arg) for rel, kind, arg in jobs if kind == "raw"]
    if state:  # 总工作量 = 预览图提取数 + 模型推理数
        with state.lock:
            state.total = len(jobs) + len(raw_jobs)
            state.done, state.running, state.error = 0, True, None
    if not jobs:
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    # 阶段一:为无 JPG 配对的 RAW 提取预览图(并行,结果落盘缓存)
    preview_dir = CACHE_DIR / "cls_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    if raw_jobs:
        if state:
            with state.lock:
                state.phase = "extract"
        extracted = {}
        done_n = 0
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            for rel, path in pool.map(lambda ra: (ra[0], _extract_one(root, exiftool, ra[1], preview_dir)),
                                      raw_jobs):
                extracted[rel] = path
                done_n += 1
                bump_done(state, on_progress, done_n)
        jobs = [(rel, "img", extracted[rel]) if kind == "raw" else (rel, kind, arg)
                for rel, kind, arg in jobs if kind != "raw" or extracted.get(rel)]
    if state:
        with state.lock:
            state.phase = "classify"

    if not jobs:
        if state:
            with state.lock:
                state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    tok = encoders._tokenizer(pack, "classify_text")
    text_embeds = _text_embeds(pack)             # (题材数, dim)
    sess, out_name, needs_dummy = encoders.vision_session(pack)
    vsize = pack["vision"]["image_size"]
    # 部分导出(如 OpenAI CLIP)是图文联合模型:喂一条哑文本占位,只取图像嵌入输出
    if needs_dummy:
        dummy = tok.encode("a photo", add_special_tokens=True).ids[:vsize]
        if not dummy or dummy[0] != 49406:
            dummy = [49406] + dummy[:vsize - 2] + [49407]
        dummy_ids = np.zeros((1, vsize), dtype=np.int64)
        dummy_ids[0, :len(dummy)] = dummy
        dummy_mask = np.zeros((1, vsize), dtype=np.int64)
        dummy_mask[0, :len(dummy)] = 1
    else:
        dummy_ids = np.zeros((1, 1), dtype=np.int64)
        dummy_mask = np.zeros((1, 1), dtype=np.int64)
    vinput = pack["vision"].get("input", "pixel_values")
    labels = [s[0] for s in SUBJECTS]

    t0 = time.time()
    errors = {}
    processed = 0
    new_vecs = {}

    def _prep_one(job):
        rel, _kind, src = job
        try:
            data = (root / src).read_bytes() if not Path(src).is_absolute() else Path(src).read_bytes()
            img = Image.open(io.BytesIO(data))
            if img.format == "JPEG" and img.size[0] > vsize * 2:
                img.draft("RGB", (vsize * 2, vsize * 2))  # DCT 缩放解码,大图快数倍
            return rel, _preprocess(img, pack), None
        except Exception as e:
            return rel, None, f"{type(e).__name__}: {str(e)[:120]}"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ppool:  # 解码/缩放是 C 实现,线程并行有效
        for i in range(0, len(jobs), BATCH):
            chunk = jobs[i:i + BATCH]
            prepared = list(ppool.map(_prep_one, chunk))
            processed += len(chunk)
            for _, _, err in prepared:
                if err:
                    errors[err] = errors.get(err, 0) + 1
            imgs = [im for _, im, _ in prepared if im is not None]
            keep = [rel for rel, im, _ in prepared if im is not None]
            if not imgs:
                if state:
                    with state.lock:
                        state.done = len(raw_jobs) + processed
                continue
            feed = {vinput: np.stack(imgs)}
            if needs_dummy:
                feed["input_ids"] = dummy_ids
                feed["attention_mask"] = dummy_mask
            embeds = sess.run([out_name], feed)[0].astype(np.float32)
            if embeds.ndim != 2 or embeds.shape[1] != pack.get("dim", 512):
                raise RuntimeError(f"视觉模型输出形状异常: {embeds.shape}")
            sims = _l2n(embeds) @ text_embeds.T * CONF_SCALE
            sims = sims - sims.max(axis=1, keepdims=True)
            prob = np.exp(sims)
            prob /= prob.sum(axis=1, keepdims=True)
            for rel, p, vec in zip(keep, prob, _l2n(embeds)):
                j = int(p.argmax())
                result[rel] = [labels[j], round(float(p[j]), 4)]
                new_vecs[rel] = vec
            if state:
                with state.lock:
                    state.done = len(raw_jobs) + processed
            if on_progress:
                on_progress(state.done if state else 0, state.total if state else 0)

    out_path.write_text(json.dumps({"sig": _sig(), "results": result}, ensure_ascii=False),
                        encoding="utf-8")
    if new_vecs:  # 合并并保存图像嵌入(旧条目保留)
        idx = {p: i for i, p in enumerate(emb_paths)}
        added = [(p, v) for p, v in new_vecs.items() if p not in idx]
        for p, v in new_vecs.items():
            if p in idx:
                emb_matrix[idx[p]] = v
        if added:
            emb_matrix = np.vstack([emb_matrix] + [v[None, :] for _, v in added])
            emb_paths.extend(p for p, _ in added)
        _save_embeddings(pack, emb_paths, emb_matrix)
    secs = time.time() - t0
    print(f"分类完成: 累计 {len(result)} 条, 新嵌入 {len(new_vecs)} 条, "
          f"本轮 {secs:.1f}s, 失败 {sum(errors.values())} 条")
    for k, v in sorted(errors.items(), key=lambda x: -x[1])[:5]:
        print(f"  [{v}x] {k}")
    if state:
        with state.lock:
            state.running, state.finishedAt = False, time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def bump_done(state, on_progress, done):
    if state:
        with state.lock:
            state.done = done
    if on_progress:
        on_progress(done, state.total if state else 0)


def _extract_one(root: Path, exiftool: str, rel: str, preview_dir: Path):
    """提取单个 RAW 的预览图,返回缓存路径或 None。"""
    import scanner
    key = hashlib.md5(rel.encode()).hexdigest()[:16] + ".jpg"
    cached = preview_dir / key
    if cached.exists() and cached.stat().st_size:
        return str(cached)
    data = (scanner.extract_binary(exiftool, root, rel, "-PreviewImage")
            or scanner.extract_binary(exiftool, root, rel, "-ThumbnailImage"))
    if not data:
        return None
    cached.write_bytes(data)
    return str(cached)


def _sig() -> str:
    return hashlib.md5(json.dumps(SUBJECTS, ensure_ascii=False).encode()).hexdigest()


def _load_embeddings(pack: dict) -> tuple:
    """返回 (paths 列表, 向量矩阵)。签名 = 模型包 id(v1 是默认包的旧别名)。"""
    f = CACHE_DIR / "embeddings.npz"
    valid = {encoders.embed_sig(pack)} | {k for k, v in encoders.EMB_SIG_ALIASES.items()
                                          if v == pack["id"]}
    if f.exists():
        try:
            d = np.load(f, allow_pickle=False)
            if str(d["sig"]) in valid and d["vecs"].ndim == 2:
                return list(d["paths"].tolist()), d["vecs"].astype(np.float32)
        except Exception:
            pass
    return [], np.zeros((0, pack.get("dim", 512)), dtype=np.float32)


def _save_embeddings(pack: dict, paths: list, matrix: np.ndarray) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE_DIR / "embeddings.npz",
                        sig=np.array(encoders.embed_sig(pack)), paths=np.array(paths),
                        vecs=matrix)


def _load_result() -> dict:
    out_path = CACHE_DIR / "classifications.json"
    if not out_path.exists():
        return {}
    try:
        d = json.loads(out_path.read_text(encoding="utf-8"))
        return d["results"] if d.get("sig") == _sig() else {}
    except (json.JSONDecodeError, KeyError):
        return {}


def load_result() -> dict:
    return _load_result()
