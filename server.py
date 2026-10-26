# -*- coding: utf-8 -*-
"""本地 HTTP 服务:提供统计界面、元数据 JSON、缩略图、原图预览与视频播放(支持 Range)。"""
import hashlib
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aesthetic
import classifier
import colors
import encoders
import exporter
import faces
import phash
import quality
import report
import scanner
import selection
import searcher
import xmp

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".nef": "application/octet-stream", ".hif": "application/octet-stream",
}
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
STATIC_DIR = Path(__file__).resolve().parent / "static"


class AppState:
    def __init__(self, root, project_dir, exiftool, library):
        self.root = Path(root).resolve()
        self.project_dir = Path(project_dir).resolve()
        self.thumb_dir = self.project_dir / "cache" / "thumbs"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.project_dir / "cache" / "metadata.json"
        self.exiftool = str(exiftool)
        self.library = library
        self.lock = threading.Lock()
        self.classify_state = classifier.ClassifyState()
        self.hash_state = phash.HashState()
        self.quality_state = quality.QualityState()
        self.xmp_state = xmp.XmpState()
        self.soft_state = quality.SoftState()
        self.face_state = faces.FaceState()
        self.aes_state = aesthetic.AestheticState()
        self.blink_state = quality.SoftState()
        self.color_state = colors.ColorState()
        self.sel_state = selection.SelectionState()

    def start_selection(self) -> dict:
        st = self.sel_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                selection.compute_selection(st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_colors(self) -> dict:
        st = self.color_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                colors.compute_colors(self.root, self.exiftool, self.library["files"], st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_aes(self) -> dict:
        st = self.aes_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                aesthetic.compute_all(st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_blink(self) -> dict:
        st = self.blink_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                quality.compute_blink(st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    @staticmethod
    def _albums() -> list:
        f = Path(__file__).resolve().parent / "cache" / "albums.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []

    @staticmethod
    def _save_albums(albums: list) -> None:
        f = Path(__file__).resolve().parent / "cache" / "albums.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(albums, ensure_ascii=False), encoding="utf-8")

    def start_faces(self) -> dict:
        """后台人脸检测与聚类;进行中则幂等返回。"""
        st = self.face_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                faces.compute_faces(self.root, self.exiftool, self.library["files"], st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_soft(self) -> dict:
        """后台计算同簇相对清晰度;进行中则幂等返回。"""
        st = self.soft_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                quality.compute_softness(st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_xmp(self) -> dict:
        """后台回写 XMP sidecar;进行中则幂等返回。"""
        st = self.xmp_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                subjects = classifier.load_result()
                xmp.write_sidecars(self.root, self.library["files"], subjects, st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_quality(self) -> dict:
        """后台计算画质指标;进行中则幂等返回。"""
        st = self.quality_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                quality.compute_quality(self.root, self.exiftool, self.library["files"], st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_hash(self) -> dict:
        """后台计算感知哈希;进行中则幂等返回。"""
        st = self.hash_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                phash.compute_hashes(self.root, self.exiftool, self.library["files"], st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def start_classify(self) -> dict:
        """后台线程跑题材识别;进行中则直接返回当前进度。"""
        st = self.classify_state
        snap = st.snapshot()
        if snap["running"]:
            return snap
        st.error = None

        def run():
            try:
                classifier.classify_all(self.root, self.exiftool, self.library["files"], st)
            except Exception as e:
                with st.lock:
                    st.error = repr(e)
                    st.running = False

        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def rescan(self) -> dict:
        with self.lock:
            # 默认增量:与现有记录比对,只提取新增/变化的文件
            lib = scanner.scan_incremental(self.root, self.exiftool,
                                           self.library["files"], exclude_dirs=[self.project_dir])
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
            self.library = lib
        return lib


def _handle_export(state: AppState, handler) -> None:
    """处理 POST /api/export。"""
    length = int(handler.headers.get("Content-Length", 0))
    body = json.loads(handler.rfile.read(length) or b"{}")
    ids = [x for x in body.get("ids", []) if x][:2000]
    maxpx = int(body.get("maxpx", 1920))
    by = {f["p"]: f for f in state.library["files"]}
    r = exporter.build_zip(state.root, by, ids, maxpx)
    data = Path(r["zip_path"]).read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Content-Disposition", 'attachment; filename="photostats_export.zip"')
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默访问日志,保持控制台清爽
            pass

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/export":
                try:
                    _handle_export(state, self)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    try:
                        self._send_json({"error": repr(e)}, 500)
                    except Exception:
                        pass
            else:
                self._send_json({"error": "not found"}, 404)

        # ---------- 基础发送 ----------
        def _send(self, code, body: bytes, ctype: str, cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), MIME[".json"])

        # ---------- 路由 ----------

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            path = url.path
            try:
                if path in ("/", "/index.html"):
                    self._static("index.html")
                elif path.startswith("/static/"):
                    self._static(path[len("/static/"):])
                elif path == "/api/library":
                    self._send_json(state.library)
                elif path == "/api/subjects":
                    self._send_json({"categories": [s[0] for s in classifier.SUBJECTS],
                                     "results": classifier.load_result()})
                elif path == "/api/classify/start":
                    self._send_json(state.start_classify())
                elif path == "/api/classify/status":
                    self._send_json(state.classify_state.snapshot())
                elif path == "/api/search/info":
                    self._send_json(searcher.info())
                elif path == "/api/search":
                    self._send_json({"results": searcher.search(
                        q.get("q", [""])[0], int(q.get("top", [200])[0]))})
                elif path == "/api/similar":
                    self._send_json({"results": searcher.similar(
                        q.get("p", [""])[0], int(q.get("top", [20])[0]))})
                elif path == "/api/dups/rebuild":
                    self._send_json(state.start_hash())
                elif path == "/api/dups":
                    st = state.hash_state.snapshot()
                    hashes = phash._load()
                    groups = phash.find_visual_dups(
                        {k: int(v, 16) for k, v in hashes.items()}) if hashes else []
                    self._send_json({"status": st, "threshold": phash.THRESHOLD,
                                     "hashed": len(hashes), "groups": groups})
                elif path == "/api/packs":
                    packs = encoders.list_packs()
                    active = encoders._active_id()
                    for p in packs:
                        p["active"] = p["id"] == active
                    self._send_json({"packs": packs, "active": active})
                elif path == "/api/packs/select":
                    ok = encoders.set_active(q.get("id", [""])[0])
                    self._send_json({"ok": ok, "active": encoders._active_id(),
                                     "embeddingsStale": ok})
                elif path == "/api/quality/rebuild":
                    self._send_json(state.start_quality())
                elif path == "/api/quality":
                    items = quality._load()
                    soft = quality.load_soft()
                    self._send_json({"status": state.quality_state.snapshot(),
                                     "thresholds": {"blur": quality.BLUR_BLOCK_MAX,
                                                    "over": quality.OVER_FRAC,
                                                    "under": quality.UNDER_FRAC},
                                     "summary": quality.summary(items), "items": items,
                                     "soft": {"count": len(soft.get("flagged", [])),
                                              "flagged": soft.get("flagged", []),
                                              "groups": soft.get("groups", 0),
                                              "status": state.soft_state.snapshot(),
                                              "computedAt": soft.get("computedAt")}})
                elif path == "/api/softness/rebuild":
                    self._send_json(state.start_soft())
                elif path == "/api/xmp/preview":
                    subjects = classifier.load_result()
                    self._send_json({"status": state.xmp_state.snapshot(),
                                     "preview": xmp.preview(state.root, state.library["files"],
                                                            subjects)})
                elif path == "/api/xmp/write":
                    self._send_json(state.start_xmp())
                elif path == "/api/xmp/status":
                    self._send_json(state.xmp_state.snapshot())
                elif path == "/api/faces":
                    d = faces.resolve_persons()
                    self._send_json({"status": state.face_state.snapshot(),
                                     "persons": d["persons"], "assign": d["assign"]})
                elif path == "/api/persons/rename":
                    r = faces.rename_person(int(q.get("id", [0])[0]),
                                            q.get("name", [""])[0])
                    self._send_json({"ok": True, "persons": r["persons"],
                                     "assign": r["assign"]})
                elif path == "/api/persons/merge":
                    ids = [int(x) for x in (q.get("ids", [""])[0] or "").split(",") if x]
                    to = int(q.get("to", [ids[0] if ids else 0])[0])
                    r = faces.merge_persons(ids, to)
                    self._send_json({"ok": True, "persons": r["persons"],
                                     "assign": r["assign"]})
                elif path == "/api/persons/reset":
                    r = faces.reset_person_edits()
                    self._send_json({"ok": True, "persons": r["persons"],
                                     "assign": r["assign"]})
                elif path == "/api/faces/rebuild":
                    self._send_json(state.start_faces())
                elif path == "/api/faceimg":
                    k = q.get("k", [""])[0]
                    full = (state.project_dir / "cache" / "face_crops" / (k + ".jpg")).resolve()
                    ok = str(full).startswith(str((state.project_dir / "cache" / "face_crops").resolve())) and full.is_file()
                    if not ok:
                        return self._send_json({"error": "not found"}, 404)
                    self._send(200, full.read_bytes(), "image/jpeg", cache=True)
                elif path == "/api/report":
                    year = int(q.get("year", [0])[0])
                    if not year:
                        dates = [f["dt"][:4] for f in state.library["files"]
                                 if f.get("dt") and f["t"] != "video"]
                        year = max(dates) if dates else 2026
                    html = report.generate(year, state.root, state.library["files"])["html"]
                    self._send(200, html.encode("utf-8"), MIME[".html"])
                elif path == "/api/ratings":
                    self._send_json({"ratings": xmp.load_ratings()})
                elif path == "/api/rating":
                    r = xmp.set_rating(state.root, state.exiftool,
                                       q.get("path", [""])[0], int(q.get("stars", [0])[0]))
                    self._send_json(r)
                elif path == "/api/albums":
                    self._send_json({"albums": state._albums()})
                elif path == "/api/albums/save":
                    name = q.get("name", [""])[0]
                    filters = json.loads(q.get("filters", ["[]"])[0])
                    albums = [a for a in state._albums() if a["name"] != name]
                    albums.append({"name": name, "filters": filters})
                    state._save_albums(albums)
                    self._send_json({"ok": True, "albums": albums})
                elif path == "/api/albums/delete":
                    name = q.get("name", [""])[0]
                    albums = [a for a in state._albums() if a["name"] != name]
                    state._save_albums(albums)
                    self._send_json({"ok": True, "albums": albums})
                elif path == "/api/aesthetic":
                    self._send_json({"status": state.aes_state.snapshot(),
                                     "scores": aesthetic.load_scores()})
                elif path == "/api/aesthetic/rebuild":
                    self._send_json(state.start_aes())
                elif path == "/api/blink/rebuild":
                    self._send_json(state.start_blink())
                elif path == "/api/blink":
                    self._send_json(quality.load_blink())
                elif path == "/api/selection":
                    d = selection.load_selection()
                    soft = quality.load_soft()
                    d["clusters"] = soft.get("clusters", [])
                    self._send_json(d)
                elif path == "/api/culling":
                    f = Path(__file__).resolve().parent / "cache" / "culling_decisions.json"
                    try:
                        self._send_json(json.loads(f.read_text(encoding="utf-8")))
                    except (FileNotFoundError, json.JSONDecodeError):
                        self._send_json({"decisions": {}})
                elif path == "/api/culling/decide":
                    rel = q.get("path", [""])[0]
                    decision = q.get("decision", [""])[0]
                    f = Path(__file__).resolve().parent / "cache" / "culling_decisions.json"
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                    except (FileNotFoundError, json.JSONDecodeError):
                        data = {"decisions": {}}
                    if decision in ("keep", "cull", "skip"):
                        data["decisions"][rel] = decision
                    else:
                        data["decisions"].pop(rel, None)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    self._send_json(data)
                elif path == "/api/selection/rebuild":
                    self._send_json(state.start_selection())
                elif path == "/api/bestpick":
                    self._send_json(quality.compute_bestpick())
                elif path == "/api/colors/rebuild":
                    self._send_json(state.start_colors())
                elif path == "/api/colors":
                    items = colors.load_colors()
                    self._send_json({"status": state.color_state.snapshot(),
                                     "summary": colors.summary(items),
                                     "monthly": colors.monthly_warmth(items, state.library["files"]),
                                     "items": items})
                elif path == "/api/rescan":
                    self._send_json(state.rescan())
                elif path == "/api/thumb":
                    self._thumb(q.get("p", [""])[0])
                elif path == "/api/preview":
                    self._preview(q.get("p", [""])[0])
                elif path == "/api/file":
                    self._serve_file(q.get("p", [""])[0])
                else:
                    self._send_json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self._send_json({"error": repr(e)}, 500)
                except Exception:
                    pass

        # ---------- 静态文件 ----------
        def _static(self, rel):
            full = (STATIC_DIR / rel).resolve()
            if not str(full).startswith(str(STATIC_DIR)) or not full.is_file():
                return self._send_json({"error": "not found"}, 404)
            # 静态页面资源不设强缓存,便于更新;缩略图才用长缓存
            self._send(200, full.read_bytes(),
                       MIME.get(full.suffix.lower(), "application/octet-stream"))

        # ---------- 路径安全 ----------
        def _safe_file(self, p):
            p = (p or "").replace("\\", "/").lstrip("/")
            if not p or ".." in p.split("/"):
                return None
            full = (state.root / p).resolve()
            if full != state.root and state.root not in full.parents:
                return None
            return full if full.is_file() else None

        # ---------- 缩略图 ----------
        def _thumb(self, p):
            full = self._safe_file(p)
            if not full:
                return self._send_json({"error": "not found"}, 404)
            t = scanner.file_type(full.suffix.lower().lstrip("."))
            key = hashlib.md5(f"{p}|{full.stat().st_mtime_ns}".encode()).hexdigest() + ".jpg"
            cf = state.thumb_dir / key
            if cf.exists() and cf.stat().st_size:
                return self._send(200, cf.read_bytes(), MIME[".jpg"], cache=True)
            if t == "jpg":
                # JPG 优先用内嵌缩略图,没有就直接给原图(浏览器自行缩放)
                data = scanner.extract_binary(state.exiftool, state.root, p, "-ThumbnailImage")
                if not data:
                    return self._send(200, full.read_bytes(), MIME[".jpg"], cache=True)
            elif t == "raw":
                data = (scanner.extract_binary(state.exiftool, state.root, p, "-ThumbnailImage")
                        or scanner.extract_binary(state.exiftool, state.root, p, "-PreviewImage"))
            else:
                data = None
            if not data:
                return self._send_json({"error": "no thumbnail"}, 404)
            if len(data) <= 4 * 1024 * 1024:  # 超大预览图不入缓存
                cf.write_bytes(data)
            self._send(200, data, MIME[".jpg"], cache=True)

        # ---------- 大图预览 ----------
        def _preview(self, p):
            full = self._safe_file(p)
            if not full:
                return self._send_json({"error": "not found"}, 404)
            t = scanner.file_type(full.suffix.lower().lstrip("."))
            if t == "jpg":
                return self._send(200, full.read_bytes(), MIME[".jpg"])
            if t == "raw":
                data = (scanner.extract_binary(state.exiftool, state.root, p, "-PreviewImage")
                        or scanner.extract_binary(state.exiftool, state.root, p, "-ThumbnailImage"))
                if data:
                    return self._send(200, data, MIME[".jpg"])
            self._send_json({"error": "no preview"}, 404)

        # ---------- 原文件(视频播放,支持 Range) ----------
        def _serve_file(self, p):
            full = self._safe_file(p)
            if not full:
                return self._send_json({"error": "not found"}, 404)
            size = full.stat().st_size
            ctype = MIME.get(full.suffix.lower(), "application/octet-stream")
            m = RANGE_RE.match(self.headers.get("Range", "") or "")
            if m and (m.group(1) or m.group(2)):
                start = int(m.group(1) or 0)
                end = int(m.group(2)) if m.group(2) else size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                start, length = start, end - start + 1
            else:
                self.send_response(200)
                start, length = 0, size
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    buf = f.read(min(1 << 20, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)

    return Handler
