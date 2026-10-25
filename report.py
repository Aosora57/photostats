# -*- coding: utf-8 -*-
"""年度报告生成器(C5):把一年的拍摄统计生成自包含的可分享 HTML 海报页。

数据源:cache/metadata.json + cache/classifications.json + cache/quality.json,
代表作缩略图内嵌为 base64(单文件可分享)。输出 cache/report_<year>.html。
"""
import base64
import io
import json
from collections import Counter
from pathlib import Path

import classifier
import colors
import phash
import quality

MODELS_DIR = Path(__file__).resolve().parent / "tools" / "models"


def _fmt_gb(n):
    if not n:
        return "0"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def _thumb_datauri(root: Path, rel: str, size=320) -> str:
    src = None
    try:
        f = _FILES_BY.get(rel)
        if f:
            i = rel.rfind("/")
            jpg_for = _JPG_FOR
            src = phash._hash_source(f, root, jpg_for, classifier.CACHE_DIR / "cls_previews")
        if src and Path(src).exists():
            from PIL import Image
            img = Image.open(io.BytesIO(Path(src).read_bytes())).convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return ""


_FILES_BY, _JPG_FOR = {}, {}


def generate(year: int, root, files: list) -> dict:
    """生成指定年份的年报,返回 {html, path}。"""
    global _FILES_BY, _JPG_FOR
    root = Path(root)
    _FILES_BY = {f["p"]: f for f in files}
    _JPG_FOR = {}
    for f in files:
        if f["t"] == "jpg":
            i = f["p"].rfind("/")
            _JPG_FOR[(f["p"][:i], f["p"][i + 1:].rsplit(".", 1)[0].lower())] = f["p"]

    subjects = classifier.load_result()
    year_prefix = f"{year}-"
    year_files = [f for f in files if (f["dt"] or "").startswith(year_prefix)]
    photos = [f for f in year_files if f["t"] != "video"]
    vids = [f for f in year_files if f["t"] == "video"]
    size = sum(f["s"] or 0 for f in year_files)

    # 月度节奏
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    month_counts = [sum(1 for f in year_files if (f["dt"] or "").startswith(m)) for m in months]
    max_month = max(month_counts) if month_counts else 0
    month_bars = "".join(
        f'<div class="mbar"><div class="mfill" style="height:{(c / max_month * 100) if max_month else 0}%"></div>'
        f'<span>{c}</span><small>{m}</small></div>'
        for m, c in zip(months, month_counts))

    # 题材构成
    subj_counter = Counter(subjects[f["p"]][0] for f in photos if f["p"] in subjects)
    subj_rows = "".join(
        f'<div class="srow"><span class="sname">{k}</span>'
        f'<div class="sbar"><div style="width:{v / max(subj_counter.values()) * 100:.0f}%"></div></div>'
        f'<span class="snum">{v}</span></div>'
        for k, v in subj_counter.most_common())

    # 器材 Top3
    cams = Counter(f["model"] for f in photos if f["model"]).most_common(3)
    lenses = Counter(f["lens"] for f in photos if f["lens"]).most_common(3)

    # 年度之最
    def extreme(key, bigger=True, fmt=str):
        cands = [f for f in photos if f.get(key) is not None]
        if not cands:
            return "-"
        pick = max(cands, key=lambda f: f[key]) if bigger else min(cands, key=lambda f: f[key])
        return fmt(pick)

    longest = extreme("exp", True, lambda f: f"{f['exp']:.1f}s · {f['p'].split('/')[-1]}")
    max_iso = extreme("iso", True, lambda f: f"ISO{int(f['iso'])} · {f['p'].split('/')[-1]}")
    max_fl = extreme("fl35", True, lambda f: f"{int(f['fl35'])}mm · {f['p'].split('/')[-1]}")
    min_fl = extreme("fl35", False, lambda f: f"{int(f['fl35'])}mm · {f['p'].split('/')[-1]}")

    # 画质(全年)
    q_items = quality._load()
    q_year = {p: m for p, m in q_items.items() if p in _FILES_BY and p.startswith(year_prefix)}
    q_sum = quality.summary(q_year) if q_year else {"blur": 0, "over": 0, "under": 0}

    # 色彩构成(暖/冷/无彩色 + 主导色族 Top3)
    try:
        c_items = colors.load_colors()
        c_year = {p: m for p, m in c_items.items() if p.startswith(year_prefix) and p in _FILES_BY}
        c_sum = colors.summary(c_year)
        warm_pct = round(c_sum["warm"] * 100)
        cool_pct = round(c_sum["cool"] * 100)
        neutral_pct = round(c_sum["neutral"] * 100)
        top_fams = '、'.join(f"{k}({v}张)" for k, v in c_sum["fams"][:3]) if c_sum["fams"] else "-"
    except Exception:
        warm_pct, cool_pct, neutral_pct, top_fams = 0, 0, 0, "-"
    color_row = (f"<tr><td>色彩构成</td><td>暖色 {warm_pct}% · 冷色 {cool_pct}% · "
                 f"无彩色 {neutral_pct}% · 主导色族 {top_fams}</td></tr>")

    # 代表作:每题材置信度最高的一张
    reps = []
    for label, _ in classifier.SUBJECTS:
        best, conf = None, 0
        for f in photos:
            s = subjects.get(f["p"])
            if s and s[0] == label and s[1] > conf:
                best, conf = f["p"], s[1]
        if best:
            uri = _thumb_datauri(root, best)
            if uri:
                reps.append(f'<figure class="rep"><img src="{uri}">'
                            f'<figcaption>{label} · {best.split("/")[-1]}</figcaption></figure>')

    shutter = max((f["sc"] for f in files if f.get("sc")), default=0)

    css = """
    body{margin:0;background:#0f1219;color:#dfe5f0;font:15px/1.7 "Segoe UI","Microsoft YaHei",sans-serif}
    .wrap{max-width:960px;margin:0 auto;padding:48px 28px 80px}
    h1{font-size:34px;margin:0 0 4px} h1 span{color:#4f8cff}
    .sub{color:#8792a8;margin-bottom:36px}
    h2{font-size:19px;margin:44px 0 16px;color:#fff;border-left:4px solid #4f8cff;padding-left:12px}
    .hero{display:flex;gap:14px;flex-wrap:wrap}
    .card{flex:1;min-width:150px;background:#181d29;border:1px solid #232a3a;border-radius:12px;padding:16px 20px}
    .card b{display:block;font-size:26px;color:#fff;margin-top:4px}
    .card small{color:#8792a8}
    .months{display:flex;gap:6px;align-items:flex-end;height:150px}
    .mbar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%}
    .mbar .mfill{width:100%;background:linear-gradient(180deg,#4f8cff,#2a5fd0);border-radius:4px 4px 0 0;min-height:2px}
    .mbar span{font-size:10px;color:#8792a8;margin:4px 0 2px}
    .mbar small{font-size:10px;color:#5a6478}
    .srow{display:flex;align-items:center;gap:10px;margin:7px 0}
    .sname{width:56px;text-align:right;color:#aab4c8;font-size:13px}
    .sbar{flex:1;background:#181d29;border-radius:4px;height:14px;overflow:hidden}
    .sbar div{height:100%;background:#4f8cff;border-radius:4px}
    .snum{width:60px;font-size:12px;color:#8792a8}
    table{border-collapse:collapse;width:100%}
    td{padding:7px 10px;border-bottom:1px solid #232a3a;font-size:14px}
    td:first-child{color:#8792a8;width:130px}
    .reps{display:flex;flex-wrap:wrap;gap:12px}
    .rep{width:calc(33% - 8px);margin:0;background:#181d29;border-radius:10px;overflow:hidden;border:1px solid #232a3a}
    .rep img{width:100%;height:170px;object-fit:cover;display:block}
    .rep figcaption{font-size:11.5px;color:#aab4c8;padding:8px 10px}
    .foot{color:#5a6478;font-size:12px;margin-top:56px;text-align:center}
    a{color:#4f8cff}
    """

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>PhotoStats · {year} 拍摄年报</title><style>{css}</style></head><body><div class="wrap">
<h1>📅 <span>{year} 拍摄年报</span></h1>
<p class="sub">PhotoStats 生成 · 拍摄地信息均来自本机元数据 · 照片未离开电脑</p>
<div class="hero">
 <div class="card"><small>快门总数</small><b>{len(photos):,}</b><small>张照片</small></div>
 <div class="card"><small>视频</small><b>{len(vids)}</b><small>个片段</small></div>
 <div class="card"><small>总容量</small><b>{_fmt_gb(size)}</b><small>原始文件</small></div>
 <div class="card"><small>快门数</small><b>{shutter:,}</b><small>机身累计</small></div>
</div>
<h2>月度节奏</h2>
<div class="months">{month_bars}</div>
<h2>题材构成</h2>
{subj_rows}
<h2>器材出勤</h2>
<table>
<tr><td>机身 Top3</td><td>{' · '.join(f'{m}({c})' for m, c in cams) or '-'}</td></tr>
<tr><td>镜头 Top3</td><td>{' · '.join(f'{m}({c})' for m, c in lenses) or '-'}</td></tr>
<tr><td>最长曝光</td><td>{longest}</td></tr>
<tr><td>最高感光度</td><td>{max_iso}</td></tr>
<tr><td>最远 / 最近焦段</td><td>{max_fl} / {min_fl}</td></tr>
<tr><td>画质提醒</td><td>严重糊片 {q_sum['blur']} · 过曝 {q_sum['over']} · 欠曝 {q_sum['under']}</td></tr>
{color_row}
</table>
<h2>题材代表作</h2>
<div class="reps">{''.join(reps)}</div>
<p class="foot">由 PhotoStats 本地生成 · {year}</p>
</div></body></html>"""

    out = Path(__file__).resolve().parent / "cache" / f"report_{year}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return {"html": html, "path": str(out)}
