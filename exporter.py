# -*- coding: utf-8 -*-
"""批量导出(E5):把筛选结果导出为 ZIP。RAW 取内嵌预览/对齐缓存,原图直接缩放。

输出:cache/export_<时间戳>.zip;JPEG 质量 88;尺寸上限由 maxpx 控制(0=不缩放)。
"""
import io
import json
import time
import zipfile
from pathlib import Path

import classifier
import phash


def build_zip(root, files_by: dict, ids: list, maxpx: int = 1920, on_progress=None) -> dict:
    """files_by: 路径→记录。返回 {zip_path, count, failed}。"""
    import zipfile

    root = Path(root)
    preview_dir = classifier.CACHE_DIR / "cls_previews"
    jpg_for = {}
    for f in files_by.values():
        if f["t"] == "jpg":
            i = f["p"].rfind("/")
            jpg_for[(f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower())] = f["p"]

    from PIL import Image

    out = classifier.CACHE_DIR / f"export_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    count = failed = 0
    used = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:  # JPEG 已压缩,STORED 快
        for rel in ids:
            f = files_by.get(rel)
            if not f:
                continue
            try:
                src = phash._hash_source(f, root, jpg_for, preview_dir)
                if src is None or not Path(src).exists():
                    failed += 1
                    continue
                img = Image.open(io.BytesIO(Path(src).read_bytes())).convert("RGB")
                if maxpx and max(img.size) > maxpx:
                    img.thumbnail((maxpx, maxpx), 3)
                base = rel.split("/")[-1].rsplit(".", 1)[0]
                name = base + ".jpg"
                k = 1
                while name in used:
                    name = f"{base}_{k}.jpg"
                    k += 1
                used.add(name)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88)
                zf.writestr(name, buf.getvalue())
                count += 1
            except Exception:
                failed += 1
            if on_progress:
                on_progress(count + failed, len(ids))
    return {"zip_path": str(out), "count": count, "failed": failed}
