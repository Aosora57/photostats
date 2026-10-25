# -*- coding: utf-8 -*-
"""扫描照片库元数据:调用 exiftool 批量提取,输出统一的短字段 JSON 记录。

记录字段:
  p        相对路径(POSIX 风格,UTF-8)
  t        类型: jpg / raw / video
  s        文件大小(字节)      w,h  像素尺寸
  make,model,lens       机身与镜头
  fl       实际焦距(mm)   fl35 35mm等效焦距(mm)
  ap       光圈F值         exp  快门速度(秒)   iso  感光度
  ec       曝光补偿        flash 闪光(1开/0关)  meter 测光  prog 拍摄模式
  dt       拍摄时间(ISO字符串,取不到为 null)
  dur      视频时长(秒)    fps  帧率           codec 视频编码
  sc       快门数(尼康机身) rating 评分        lat,lon GPS坐标
"""
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

RAW_EXTS = {"nef", "hif", "dng", "rw2"}
JPG_EXTS = {"jpg", "jpeg"}
VIDEO_EXTS = {"mp4", "mov"}
TARGET_EXTS = RAW_EXTS | JPG_EXTS | VIDEO_EXTS

SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information"}

# 通过 argfile 请求的标签(避免命令行传入中文路径的编码问题)
BASE_ARGS = [
    "-charset", "filename=utf8",
    "-j", "-n", "-struct", "-quicktimeUTC",
    "-SourceFile", "-FileSize", "-ImageWidth", "-ImageHeight",
    "-Make", "-Model", "-LensModel", "-LensSpec",
    "-FocalLength", "-FocalLength35efl",
    "-ExposureTime", "-FNumber", "-ISO", "-ExposureCompensation",
    "-Flash", "-MeteringMode", "-ExposureProgram", "-Orientation",
    "-DateTimeOriginal", "-CreateDate",
    "-GPSLatitude", "-GPSLongitude", "-Rating", "-ShutterCount",
    "-Duration", "-VideoFrameRate", "-QuickTime:Compression", "-QuickTime:CompressorID",
]

BATCH_SIZE = 500


def file_type(ext: str) -> str:
    ext = ext.lower()
    if ext in JPG_EXTS:
        return "jpg"
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    return "other"


def collect_files(root: Path, exclude_dirs: set) -> tuple:
    """遍历目录,返回 (目标文件相对路径列表, 各扩展名计数)。"""
    rels, ext_counts = [], {}
    exclude_abs = {Path(d).resolve() for d in exclude_dirs}
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        # 剪掉排除目录和系统目录
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and (dp / d).resolve() not in exclude_abs]
        for fn in filenames:
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            ext_counts[ext or "(无扩展名)"] = ext_counts.get(ext or "(无扩展名)", 0) + 1
            if ext in TARGET_EXTS:
                rels.append((dp / fn).relative_to(root).as_posix())
    return rels, ext_counts


def _run(exiftool: str, cwd: Path, lines: list, timeout=600) -> subprocess.CompletedProcess:
    """把参数和路径写进 UTF-8 argfile 再调用 exiftool,绕开 Windows 中文路径编码问题。"""
    fd, tmp = tempfile.mkstemp(suffix=".txt", dir=str(cwd))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        return subprocess.run([exiftool, "-@", tmp], cwd=str(cwd),
                              capture_output=True, timeout=timeout)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def parse_dt(raw) -> str | None:
    """'2025:02:01 10:30:45' -> '2025-02-01T10:30:45'"""
    if not raw or not isinstance(raw, str):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None


def _num(d: dict, key: str):
    v = d.get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):  # exiftool 有时返回 {Value, Scale} 结构,秒数 = Value × Scale
        try:
            v = float(v["Value"]) * float(v["Scale"])
        except (KeyError, TypeError, ValueError):
            return None
    return v


def normalize(d: dict) -> dict:
    rel = str(d.get("SourceFile", "")).replace("\\", "/")
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    flash = _num(d, "Flash")
    codec = d.get("CompressorID") or d.get("Compression")
    if isinstance(codec, list):
        codec = codec[0] if codec else None
    w, h = _num(d, "ImageWidth"), _num(d, "ImageHeight")
    o = _num(d, "Orientation")
    if o in (5, 6, 7, 8):  # 拍摄时竖握相机:存储尺寸是横向的,按旋转标签换算视觉宽高
        w, h = h, w
    return {
        "p": rel,
        "t": file_type(ext),
        "s": _num(d, "FileSize"),
        "w": w,
        "h": h,
        "make": d.get("Make"),
        "model": d.get("Model"),
        "lens": d.get("LensModel") or d.get("LensSpec"),
        "fl": _num(d, "FocalLength"),
        "fl35": _num(d, "FocalLength35efl"),
        "ap": _num(d, "FNumber"),
        "exp": _num(d, "ExposureTime"),
        "iso": _num(d, "ISO"),
        "ec": _num(d, "ExposureCompensation"),
        "flash": int(flash) if isinstance(flash, (int, float)) else None,
        "meter": _num(d, "MeteringMode"),
        "prog": _num(d, "ExposureProgram"),
        "dt": parse_dt(d.get("DateTimeOriginal") or d.get("CreateDate")),
        "dur": _num(d, "Duration"),
        "fps": _num(d, "VideoFrameRate"),
        "codec": codec,
        "sc": _num(d, "ShutterCount"),
        "rating": _num(d, "Rating"),
        "lat": _num(d, "GPSLatitude"),
        "lon": _num(d, "GPSLongitude"),
    }


def scan(root: Path, exiftool: str, exclude_dirs: set = None, on_progress=None) -> dict:
    """全量扫描整个目录树,返回 {meta, files}。"""
    root = Path(root).resolve()
    rels, ext_counts = collect_files(root, exclude_dirs or set())
    total = len(rels)
    files, done = [], 0
    for i in range(0, total, BATCH_SIZE):
        chunk = rels[i:i + BATCH_SIZE]
        proc = _run(exiftool, root, BASE_ARGS + chunk)
        if proc.returncode != 0 and not proc.stdout:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"exiftool 运行失败: {err[:500]}")
        try:
            data = json.loads(proc.stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            data = []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            rec = normalize(d)
            if not rec["dt"]:  # 无拍摄时间时用文件修改时间兜底(如剪辑导出的视频)
                try:
                    mtime = (root / rec["p"]).stat().st_mtime
                    rec["dt"] = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
                    rec["dt_src"] = "mtime"
                except OSError:
                    pass
            files.append(rec)
        done = min(i + BATCH_SIZE, total)
        if on_progress:
            on_progress(done, total)
    for rec in files:  # 记录 mtime,供增量扫描比对
        try:
            rec["mt"] = int((root / rec["p"]).stat().st_mtime)
        except OSError:
            pass
    meta = {
        "root": str(root),
        "scannedAt": datetime.now().isoformat(timespec="seconds"),
        "totalFiles": total,
        "extCounts": ext_counts,
        "scanMode": "full",
    }
    return {"meta": meta, "files": files}


def scan_incremental(root: Path, exiftool: str, prev_files: list,
                     exclude_dirs: set = None, on_progress=None) -> dict:
    """增量扫描:与上次记录比对 mtime+大小,只对新增/变化的文件跑 exiftool。

    历史记录缺 mt 时,只要大小一致就视为未变并顺带回填 mt(免重跑)。
    已从磁盘消失的文件会被移除。返回 {meta, files} 及 meta.stats 统计。
    """
    root = Path(root).resolve()
    rels, ext_counts = collect_files(root, exclude_dirs or set())
    prev_by = {f["p"]: f for f in prev_files}
    files, todo = [], []
    seen = set()
    reused = 0
    for rel in rels:
        seen.add(rel)
        prev = prev_by.get(rel)
        try:
            mt = int((root / rel).stat().st_mtime)
        except OSError:
            mt = None
        if prev and prev.get("s") is not None and prev.get("s") == _current_size(root, rel):
            if not prev.get("mt"):
                prev["mt"] = mt
            files.append(prev)
            reused += 1
        else:
            todo.append(rel)

    total = len(todo)
    done = 0
    for i in range(0, total, BATCH_SIZE):
        chunk = todo[i:i + BATCH_SIZE]
        proc = _run(exiftool, root, BASE_ARGS + chunk)
        if proc.returncode != 0 and not proc.stdout:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"exiftool 运行失败: {err[:500]}")
        try:
            data = json.loads(proc.stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            data = []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            rec = normalize(d)
            if not rec["dt"]:
                try:
                    mtime = (root / rec["p"]).stat().st_mtime
                    rec["dt"] = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
                    rec["dt_src"] = "mtime"
                except OSError:
                    pass
            try:
                rec["mt"] = int((root / rec["p"]).stat().st_mtime)
            except OSError:
                pass
            files.append(rec)
        done = min(i + BATCH_SIZE, total)
        if on_progress:
            on_progress(done, total)

    meta = {
        "root": str(root),
        "scannedAt": datetime.now().isoformat(timespec="seconds"),
        "totalFiles": len(files),
        "extCounts": ext_counts,
        "scanMode": "incremental",
        "stats": {"new": total, "reused": reused,
                  "removed": len(prev_files) - (len(files) - total)},
    }
    return {"meta": meta, "files": files}


def _current_size(root: Path, rel: str):
    try:
        return (root / rel).stat().st_size
    except OSError:
        return None


def extract_binary(exiftool: str, cwd: Path, rel: str, tag: str) -> bytes | None:
    """提取内嵌二进制数据(如 NEF 的预览图 / JPG 的缩略图)。"""
    proc = _run(exiftool, cwd, ["-charset", "filename=utf8", "-b", tag, rel], timeout=60)
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout
    return None
