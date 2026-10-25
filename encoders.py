# -*- coding: utf-8 -*-
"""模型包(Model Pack)注册表:语义检索与题材识别的模型模块化层。

一个模型包 = 图像编码器 + 分类用文本编码器 + 查询用文本编码器 + 预处理参数,
描述在 pack.json 里(当前包在 tools/models/pack.json,未来新模型放
tools/models/packs/<id>/pack.json,文件路径相对 pack.json 所在目录)。

切换模型包(SET active)后:
  - 图像嵌入签名 = 包 id → cache/embeddings.npz 自动失效,点「运行识别」重建
  - 提示词嵌入缓存同样失效(重建仅数秒)
  - 查询编码器随包切换
新增更强模型的步骤见 TECHNICAL.md §「模型包机制」。
"""
import json
import threading
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).resolve().parent / "tools" / "models"
ACTIVE_FILE = MODELS_DIR / "active_pack.json"

_lock = threading.Lock()
_pack_cache = {}          # pack_id -> (mtime, config)
_sessions = {}            # (pack_id, slot) -> onnxruntime 会话
_tokenizers = {}          # (pack_id, slot) -> tokenizer
_dense = {}               # pack_id -> 投影矩阵
_active = None            # 当前包 id(启动后首次读取并缓存)


# ---------- 注册表 ----------

def _pack_files() -> list:
    """所有 pack.json 的路径:根目录一个(当前默认)+ packs/<id>/ 若干。"""
    files = []
    root = MODELS_DIR / "pack.json"
    if root.exists():
        files.append(root)
    packs_dir = MODELS_DIR / "packs"
    if packs_dir.exists():
        files += sorted(packs_dir.glob("*/pack.json"))
    return files


def list_packs() -> list:
    out = []
    for f in _pack_files():
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
            pid = cfg.get("id")
            if pid:
                out.append({"id": pid, "name": cfg.get("name", pid),
                            "dim": cfg.get("dim", 512), "dir": str(f.parent)})
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _active_id() -> str:
    global _active
    with _lock:
        if _active is None:
            if ACTIVE_FILE.exists():
                try:
                    _active = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))["active"]
                except (json.JSONDecodeError, KeyError, OSError):
                    _active = None
            if not _active:
                packs = list_packs()
                _active = packs[0]["id"] if packs else "clip-vitb32-openai"
        return _active


def set_active(pack_id: str) -> bool:
    """切换激活包并持久化。返回是否成功(包必须已注册)。"""
    global _active
    if not any(p["id"] == pack_id for p in list_packs()):
        return False
    with _lock:
        ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_FILE.write_text(json.dumps({"active": pack_id}), encoding="utf-8")
        _active = pack_id
    return True


def active_pack() -> dict:
    """当前包配置(带 mtime 缓存,改 pack.json 后自动重载)。"""
    pid = _active_id()
    f = next((f for f in _pack_files()
              if json.loads(f.read_text(encoding="utf-8")).get("id") == pid), None)
    if f is None:  # 激活包被删:回落第一个注册的包
        return _load_first()
    mt = f.stat().st_mtime
    cached = _pack_cache.get(pid)
    if cached and cached[0] == mt:
        return cached[1]
    cfg = json.loads(f.read_text(encoding="utf-8"))
    cfg["_dir"] = str(f.parent)
    _pack_cache[pid] = (mt, cfg)
    return cfg


def _load_first() -> dict:
    f = _pack_files()[0]
    cfg = json.loads(f.read_text(encoding="utf-8"))
    cfg["_dir"] = str(f.parent)
    return cfg


def _p(cfg: dict, slot: str, name: str) -> Path:
    """包内相对路径 → 绝对路径。"""
    return Path(cfg["_dir"]) / cfg[slot][name]


# ---------- 会话与编码 ----------

def vision_session(cfg: dict):
    """图像编码会话。返回 (session, output_name, needs_dummy_text)。"""
    key = (cfg["id"], "vision")
    with _lock:
        if key not in _sessions:
            import onnxruntime as ort
            providers = ["DmlExecutionProvider", "CPUExecutionProvider"] \
                if "DmlExecutionProvider" in ort.get_available_providers() \
                else ["CPUExecutionProvider"]
            _sessions[key] = ort.InferenceSession(str(_p(cfg, "vision", "onnx")), providers=providers)
    sess = _sessions[key]
    out_names = {o.name for o in sess.get_outputs()}
    out_name = cfg["vision"].get("output") or ("image_embeds" if "image_embeds" in out_names else None)
    return sess, out_name, bool(cfg["vision"].get("needs_dummy_text"))


def _ort_session(cfg: dict, slot: str):
    key = (cfg["id"], slot)
    with _lock:
        if key not in _sessions:
            import onnxruntime as ort
            _sessions[key] = ort.InferenceSession(str(_p(cfg, slot, "onnx")),
                                                  providers=["CPUExecutionProvider"])
    return _sessions[key]


def _tokenizer(cfg: dict, slot: str):
    key = (cfg["id"], slot)
    with _lock:
        if key not in _tokenizers:
            from tokenizers import Tokenizer
            _tokenizers[key] = Tokenizer.from_file(str(_p(cfg, slot, "tokenizer")))
    return _tokenizers[key]


def _l2n(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def encode_texts(cfg: dict, slot: str, texts: list) -> np.ndarray:
    """按包配置编码一批文本 → (N, dim),未归一化。

    支持两种 kind:
      native-clip      OpenAI CLIP 文本编码器(BOS/EOS 77 token)
      st-multilingual  sentence-transformers 多语言编码器(mean pooling + Dense)
    """
    if not texts:
        return np.zeros((0, cfg.get("dim", 512)), dtype=np.float32)
    tcfg = cfg[slot]
    kind = tcfg["kind"]
    sess = _ort_session(cfg, slot)
    tok = _tokenizer(cfg, slot)
    max_len = int(tcfg.get("max_len", 77))

    ids = np.zeros((len(texts), max_len), dtype=np.int64)
    mask = np.zeros((len(texts), max_len), dtype=np.int64)
    for i, t in enumerate(texts):
        enc = tok.encode(t, add_special_tokens=(kind == "native-clip"))
        seq = list(enc.ids)[:max_len]
        if kind == "native-clip" and (not seq or seq[0] != 49406):
            seq = [49406] + seq[:max_len - 2] + [49407]
        ids[i, :len(seq)] = seq
        mask[i, :len(seq)] = 1

    if kind == "native-clip":
        feed = {i.name: ids for i in sess.get_inputs()}
        outs = sess.run(None, feed)
        dim = cfg.get("dim", 512)
        vec = next(o for o in outs if o.ndim == 2 and o.shape[-1] == dim)
    else:  # st-multilingual
        feed = {i.name: (ids if "input" in i.name else mask) for i in sess.get_inputs()}
        hidden = sess.run(None, feed)[0]                      # (N, L, H)
        m = mask.astype(np.float32)
        vec = (hidden * m[:, :, None]).sum(axis=1) / np.maximum(m.sum(axis=1, keepdims=True), 1e-6)
        W = _dense_weight(cfg, slot)                          # (dim, H)
        vec = vec.astype(np.float32) @ W.T
    return vec.astype(np.float32)


def _dense_weight(cfg: dict, slot: str) -> np.ndarray:
    """解析 safetensors 投影矩阵(8字节头长 + JSON头 + 张量)。"""
    key = cfg["id"]
    with _lock:
        if key in _dense:
            return _dense[key]
        data = _p(cfg, slot, "dense").read_bytes()
        n = int.from_bytes(data[:8], "little")
        header = json.loads(data[8:8 + n])
        body = data[8 + n:]
        W = None
        for name, info in header.items():
            if name.startswith("__") or "weight" not in name:
                continue
            dt = np.float32 if info["dtype"] == "F32" else np.float16
            cnt = (info["data_offsets"][1] - info["data_offsets"][0]) // np.dtype(dt).itemsize
            arr = np.frombuffer(body, dtype=dt, count=cnt,
                                offset=info["data_offsets"][0]).astype(np.float32)
            W = arr.reshape(info["shape"])
        _dense[key] = W
        return W


def query_embed(cfg: dict, q: str) -> np.ndarray:
    """单条查询文本 → 归一化向量。"""
    return _l2n(encode_texts(cfg, "query_text", [q])[0].astype(np.float32))


def embed_sig(cfg: dict) -> str:
    """图像嵌入签名 = 包 id。换包即失效重算。"""
    return f"pack:{cfg['id']}"


# 兼容历史缓存:v1 签名就是当前默认包的产物
EMB_SIG_ALIASES = {"v1": "clip-vitb32-openai"}
