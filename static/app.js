/* PhotoStats 前端:加载数据 → 计算聚合 → ECharts 图表 + 可筛选缩略图墙 */
"use strict";

const $ = s => document.querySelector(s);
const PAGE = 120;

let LIB = null;            // {meta, files}
let filteredList = [];     // 排序后的当前筛选结果
let charts = {};           // 图表实例
let currentTab = "overview";
let gridPage = 1, renderedCount = 0;
let pairMap = new Map(), dupSet = new Set(), dupGroups = [], pairGroups = { paired: 0, raw_only: 0, jpg_only: 0 };
let subjMap = new Map();   // 路径 -> [题材, 置信度]
let SUBJECTS = [];         // 题材类别(来自后端定义)
let searchState = null;    // {mode, title, list} 语义搜索/相似照片的浏览态
let pathToFile = new Map();// 路径 -> 元数据记录
let searchModeInfo = "";   // 搜索编码器模式(multilingual/english)
let qualityMap = new Map();// 路径 -> [lap_var, lap_max, over, under]
let qualityThresholds = null;
let softSet = new Set();   // 同簇偏软的照片路径(B1.5)
let ratingsMap = new Map();// 路径 -> 星级 0-5(E1)
let aestheticMap = new Map(); // 路径 -> 美学分 0-10(E4)
let blinkSet = new Set();  // 含疑似闭眼脸的照片路径(E7)
let bestpickBest = new Set(); // 组内最佳路径(E8)
let bestpickCull = new Set(); // 建议删除路径(E8)
let albums = [];           // 智能相册(E2)
let compareArr = [];       // 对比视图中的路径(E6)
let colorsMap = new Map(); // 路径 -> {w,c,n,fam,v}(E10)
let colorSummary = null;   // 色彩汇总(E10)
let colorMonthly = [];     // 按月暖色占比(E10)
function family_color(name) {
  const F = ["红", "橙", "黄", "黄绿", "绿", "青绿", "青", "蓝", "蓝紫", "紫", "品红", "红紫"];
  const C = ["#e53935", "#fb8c00", "#fdd835", "#9ccc65", "#43a047", "#26a69a",
             "#00acc1", "#1e88e5", "#3949ab", "#8e24aa", "#d81b60", "#c2185b"];
  const i = F.indexOf(name);
  return i >= 0 ? C[i] : "#8792a8";
}
let personMap = new Map(); // 路径 -> [人物 id,...]
let personList = [];
let burstMap = new Map();  // 路径 -> 所在连拍簇的张数

/* ---------- 常量映射 ---------- */
const PROG = {1:"M 手动",2:"P 程序",3:"A 光圈优先",4:"S 快门优先",5:"创意",6:"运动",7:"人像",8:"风景",9:"B 门"};
const METER = {0:"未知",1:"平均",2:"中央重点",3:"点测",4:"多点",5:"矩阵",6:"局部",255:"其他"};
const CODEC = {avc1:"H.264", hvc1:"HEVC", hev1:"HEVC", mp4v:"MPEG-4"};
const FOCAL_ORDER = ["≤16mm","17-24mm","25-35mm","36-50mm","51-85mm","86-135mm","136-200mm","201-300mm","300mm+"];
const AP_ORDER = ["f/1.2","f/1.4","f/1.8","f/2","f/2.5","f/2.8","f/3.5","f/4","f/4.5","f/5","f/5.6","f/6.3","f/8","f/11","f/16","f/22","f/22+"];
const SHUTTER_ORDER = ["≥1s","1-1/2s","1/2-1/4s","1/4-1/8s","1/8-1/15s","1/15-1/30s","1/30-1/60s","1/60-1/125s","1/125-1/250s","1/250-1/500s","1/500-1/1000s","1/1000-1/2000s","<1/2000s"];
const ISO_ORDER = ["ISO100","ISO200","ISO400","ISO800","ISO1600","ISO3200","ISO6400","ISO12800","ISO25600","ISO51200","ISO102400","ISO102400+"];
const WEEK = ["周一","周二","周三","周四","周五","周六","周日"];

/* ---------- 工具 ---------- */
function fmtSize(b) {
  if (!b) return "-";
  if (b < 1024) return b + "B";
  if (b < 1048576) return (b / 1024).toFixed(0) + "KB";
  if (b < 1073741824) return (b / 1048576).toFixed(1) + "MB";
  return (b / 1073741824).toFixed(2) + "GB";
}
function fmtDur(s) {
  if (s == null) return "-";
  s = Math.round(s);
  if (s < 60) return s + "秒";
  if (s < 3600) return Math.floor(s / 60) + "分" + (s % 60 ? s % 60 + "秒" : "");
  return (s / 3600).toFixed(1) + "小时";
}
function shutterLabel(exp) {
  if (exp == null) return null;
  if (exp >= 1) return exp + "s";
  return "1/" + Math.round(1 / exp) + "s";
}

/* ---------- 分桶 ---------- */
function focalBucket(f) {
  const v = f.fl35 || f.fl;
  if (!v || v <= 0) return null;
  const lim = [16, 24, 35, 50, 85, 135, 200, 300, 1e9];
  for (let i = 0; i < lim.length; i++) if (v <= lim[i]) return FOCAL_ORDER[i];
}
const AP_STOPS = [1.2, 1.4, 1.8, 2, 2.5, 2.8, 3.5, 4, 4.5, 5, 5.6, 6.3, 8, 11, 16, 22];
function apBucket(f) {
  if (!f.ap) return null;
  if (f.ap > 24) return "f/22+";
  let best = 0, bd = 1e9;
  AP_STOPS.forEach((s, i) => { const d = Math.abs(f.ap - s); if (d < bd) { bd = d; best = i; } });
  return AP_ORDER[best];
}
function shutterBucket(f) {
  const e = f.exp;
  if (e == null) return null;
  if (e >= 1) return "≥1s";
  const speed = 1 / e;
  const bins = [2, 4, 8, 15, 30, 60, 125, 250, 500, 1000, 2000, Infinity];
  for (let i = 0; i < bins.length; i++) if (speed < bins[i]) return SHUTTER_ORDER[i + 1];
}
const ISO_STOPS = [100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600, 51200, 102400];
function isoBucket(f) {
  if (!f.iso) return null;
  if (f.iso > 150000) return "ISO102400+";
  let best = 0, bd = 1e9;
  ISO_STOPS.forEach((s, i) => { const d = Math.abs(f.iso - s); if (d < bd) { bd = d; best = i; } });
  return ISO_ORDER[best];
}
function progLabel(f) { return f.prog == null ? null : (PROG[f.prog] || "其他"); }
function meterLabel(f) { return f.meter == null ? null : (METER[f.meter] || "其他"); }
function flashLabel(f) { return f.flash == null ? null : (f.flash & 1 ? "开" : "关"); }
function monthOf(f) { return f.dt ? f.dt.slice(0, 7) : null; }
function yearOf(f) { return f.dt ? f.dt.slice(0, 4) : null; }
function hourOf(f) { return f.dt ? +f.dt.slice(11, 13) : null; }
function weekdayOf(f) { const d = f.dt ? new Date(f.dt).getDay() : -1; return d < 0 ? null : WEEK[(d + 6) % 7]; }
function resLabel(f) {
  if (!f.w) return null;
  const k = f.w + "x" + f.h;
  return { "3840x2160": "4K UHD", "1920x1080": "1080P", "1280x720": "720P" }[k] || f.w + "×" + f.h;
}
function fpsLabel(f) { return f.fps ? Math.round(f.fps) + "fps" : null; }
function codecLabel(f) { return f.codec ? (CODEC[f.codec] || f.codec) : null; }
function durBucket(f) {
  const d = f.dur;
  if (d == null) return null;
  if (d < 10) return "<10秒";
  if (d < 30) return "10-30秒";
  if (d < 60) return "30-60秒";
  if (d < 300) return "1-5分钟";
  if (d < 900) return "5-15分钟";
  return ">15分钟";
}
function folderOf(f) { return f.p.split("/")[0]; }
function subjOf(f) { const s = subjMap.get(f.p); return f.t === "video" || !s ? null : s[0]; }
function subjConf(f) { const s = subjMap.get(f.p); return s ? s[1] : null; }
function qualityFlags(f) {
  const q = qualityMap.get(f.p);
  if (!q || !qualityThresholds) return null;
  return { blur: q[1] < qualityThresholds.blur, over: q[2] > qualityThresholds.over,
           under: q[3] > qualityThresholds.under, soft: softSet.has(f.p), lapMax: q[1] };
}
function isBlur(f) { const q = qualityFlags(f); return !!(q && q.blur); }
function qualityLabel(f) {
  const q = qualityFlags(f);
  if (!q) return null;
  const fl = [q.soft && "同簇偏软", q.blur && "严重糊片", q.over && "过曝", q.under && "欠曝"].filter(Boolean);
  return (fl.length ? fl.join("/") : "正常") + `(清晰度 ${Math.round(q.lapMax)})`;
}

/* ---------- 构图与画幅(仅照片,视频不参与) ---------- */
function orientOf(f) {
  if (f.t === "video" || !f.w || !f.h) return null;
  if (f.w > f.h) return "横构图";
  if (f.h > f.w) return "竖构图";
  return "方形";
}
const ORIENT_ORDER = ["横构图", "竖构图", "方形"];
// 宽高比按数值匹配(±2% 容差,覆盖相机实际像素数与理论比例的微小偏差)
const RATIOS = [
  [1, "1:1"], [4 / 5, "4:5"], [5 / 4, "5:4"], [2 / 3, "2:3"], [3 / 2, "3:2"],
  [3 / 4, "3:4"], [4 / 3, "4:3"], [9 / 16, "9:16"], [16 / 9, "16:9"],
  [24 / 65, "24:65"], [65 / 24, "65:24"], [1 / 2, "1:2"], [2 / 1, "2:1"],
];
const RATIO_ORDER = ["3:2 横", "2:3 竖", "16:9 横", "4:3 横", "3:4 竖", "1:1 方",
  "9:16 竖", "4:5 竖", "5:4 横", "65:24 横", "24:65 竖", "2:1 横", "1:2 竖", "其他"];
function ratioOf(f) {
  if (f.t === "video" || !f.w || !f.h) return null;
  const r = f.w / f.h;
  for (const [val, name] of RATIOS) {
    if (Math.abs(r - val) / val < 0.02) {
      const suffix = name === "1:1" ? " 方" : (r > 1 ? " 横" : " 竖");
      return name + suffix;
    }
  }
  return "其他";
}

/* ---------- 配对与重复 ---------- */
function buildMaps() {
  pairMap = new Map(); dupSet = new Set(); dupGroups = []; pairGroups = { paired: 0, raw_only: 0, jpg_only: 0 };
  const groups = {};
  for (const f of LIB.files) {
    if (f.t === "video") continue;
    const i = f.p.lastIndexOf("/");
    const dir = i < 0 ? "" : f.p.slice(0, i);
    const base = f.p.slice(i + 1).replace(/\.[^.]+$/, "").toLowerCase();
    const key = dir + "\u0000" + base;
    (groups[key] = groups[key] || { raw: [], jpg: [] });
    groups[key][f.t].push(f.p);
  }
  for (const g of Object.values(groups)) {
    const status = g.raw.length && g.jpg.length ? "paired" : (g.raw.length ? "raw_only" : "jpg_only");
    pairGroups[status]++;
    for (const p of g.raw) pairMap.set(p, status);
    for (const p of g.jpg) pairMap.set(p, status);
  }
  const dups = {};
  for (const f of LIB.files) {
    if (!f.dt || !f.s) continue;
    const key = f.s + "|" + f.w + "x" + f.h + "|" + f.dt;
    (dups[key] = dups[key] || []).push(f.p);
  }
  for (const [key, list] of Object.entries(dups)) {
    if (list.length < 2) continue;
    list.forEach(p => dupSet.add(p));
    dupGroups.push({ key, files: list });
  }
  dupGroups.sort((a, b) => b.files.length - a.files.length);

  // 连拍簇:同文件夹内拍摄间隔 ≤1.5s 聚为一簇
  burstMap = new Map();
  const timed = LIB.files.filter(f => f.dt && f.t !== "video")
    .map(f => ({ p: f.p, folder: folderOf(f), t: Date.parse(f.dt) }))
    .filter(x => !isNaN(x.t))
    .sort((a, b) => a.folder === b.folder ? a.t - b.t : (a.folder < b.folder ? -1 : 1));
  let cluster = [];
  const flush = () => { for (const x of cluster) burstMap.set(x.p, cluster.length); cluster = []; };
  for (const x of timed) {
    const last = cluster[cluster.length - 1];
    if (last && (x.folder !== last.folder || x.t - last.t > 1500)) flush();
    cluster.push(x);
  }
  flush();
}

/* ---------- 筛选(多条件 AND 叠加,图表点击与浏览面板共用) ---------- */
let filters = new Map();   // field -> {value, title}

function match(f) {
  for (const [field, cond] of filters) {
    const v = cond.value;
    let ok;
    switch (field) {
      case "type": ok = f.t === v; break;
      case "model": ok = f.model === v; break;
      case "lens": ok = f.lens === v; break;
      case "focal": ok = focalBucket(f) === v; break;
      case "ap": ok = apBucket(f) === v; break;
      case "shutter": ok = shutterBucket(f) === v; break;
      case "iso": ok = isoBucket(f) === v; break;
      case "prog": ok = progLabel(f) === v; break;
      case "meter": ok = meterLabel(f) === v; break;
      case "flash": ok = flashLabel(f) === v; break;
      case "month": ok = monthOf(f) === v; break;
      case "year": ok = yearOf(f) === v; break;
      case "hour": { const h = hourOf(f); ok = h != null && h + "时" === v; break; }
      case "weekday": ok = weekdayOf(f) === v; break;
      case "res": ok = resLabel(f) === v; break;
      case "fps": ok = fpsLabel(f) === v; break;
      case "codec": ok = codecLabel(f) === v; break;
      case "durb": ok = durBucket(f) === v; break;
      case "folder": ok = folderOf(f) === v; break;
      case "orient": ok = f.t !== "video" && orientOf(f) === v; break;
      case "ratio": ok = f.t !== "video" && ratioOf(f) === v; break;
      case "subj": ok = subjOf(f) === v; break;
      case "date": ok = (f.dt || "").slice(0, 10) === v; break;
      case "pair": ok = pairMap.get(f.p) === v; break;
      case "dup": ok = dupSet.has(f.p); break;
      case "mp": {
        const px = ((f.w || 0) * (f.h || 0)) / 1e6;
        ok = v === "45+" ? px >= 45 : v === "24-45" ? px >= 24 && px < 45
          : v === "12-24" ? px >= 12 && px < 24 : px > 0 && px < 12;
        break;
      }
      case "gps": ok = v === "has" ? (f.lat != null && f.lon != null)
        : (f.lat == null || f.lon == null); break;
      case "from": ok = (f.dt || "") >= v; break;
      case "to": ok = (f.dt || "").slice(0, 10) <= v; break;
      case "burst": {
        const n = burstMap.get(f.p) ?? 0;
        ok = v === "burst" ? n >= 3 : v === "two" ? n === 2 : n === 1;
        break;
      }
      case "person": ok = (personMap.get(f.p) || []).includes(parseInt(v)); break;
      case "rating": {
        const r = ratingsMap.get(f.p) || 0;
        ok = v === "0" ? r === 0 : r >= parseInt(v);
        break;
      }
      case "aes": {
        const a = aestheticMap.get(f.p);
        ok = a != null && (v === "7" ? a >= 7 : v === "6" ? a >= 6 : a < 5);
        break;
      }
      case "blink": ok = blinkSet.has(f.p); break;
      case "best": ok = bestpickBest.has(f.p); break;
      case "cull": ok = bestpickCull.has(f.p); break;
      case "onday": ok = (f.dt || "").slice(5, 10) === v; break;
      case "colorw": {
        const m = colorsMap.get(f.p);
        if (!m) { ok = false; break; }
        ok = v === "warm" ? m.w > m.c && m.n < 0.5
          : v === "cool" ? m.c > m.w && m.n < 0.5
          : m.n >= 0.5;
        break;
      }
      case "fam": {
        const m = colorsMap.get(f.p);
        ok = !!m && m.fam === v;
        break;
      }
      case "quality": {
        const q = qualityMap.get(f.p);
        if (!q || !qualityThresholds) { ok = false; break; }
        ok = v === "blur" ? q[1] < qualityThresholds.blur
          : v === "over" ? q[2] > qualityThresholds.over
          : v === "soft" ? softSet.has(f.p)
          : q[3] > qualityThresholds.under;
        break;
      }
      default: ok = true;
    }
    if (!ok) return false;
  }
  return true;
}
function currentList() {
  if (searchState) return searchState.list; // 搜索态优先于筛选
  let list = LIB.files;
  if (filters.size) list = list.filter(match);
  return list.slice().sort((a, b) => (b.dt || "").localeCompare(a.dt || "") || a.p.localeCompare(b.p));
}
function setFilter(field, value, title) {
  const cur = filters.get(field);
  if (value == null || value === "" || (cur && cur.value === value)) filters.delete(field);
  else filters.set(field, { value, title });
  searchState = null;
  refreshAll();
}
function clearFilter() { filters.clear(); searchState = null; refreshAll(); }
function filterSummary() {
  return [...filters.values()].map(c => `${c.title}:${c.value}`).join(" · ");
}

/* ---------- 照片浏览筛选面板 ---------- */
const PANEL_TITLES = { model: "机身", lens: "镜头", subj: "题材", orient: "构图",
  mp: "像素", quality: "画质", gps: "GPS", from: "开始", to: "结束", burst: "连拍",
  rating: "星级", aes: "美学", colorw: "色调", fam: "色族" };

function buildPanel() {
  const bar = $("#panelBar");
  const models = [...new Set(LIB.files.map(f => f.model).filter(Boolean))].sort();
  const lenses = [...new Set(LIB.files.map(f => f.lens).filter(Boolean))].sort();
  const opts = (arr, label) => [`<option value="">${label}:全部</option>`]
    .concat(arr.map(v => `<option value="${v}">${v}</option>`)).join("");
  bar.innerHTML = `
    <select data-pf="model">${opts(models, "机身")}</select>
    <select data-pf="lens">${opts(lenses, "镜头")}</select>
    <select data-pf="subj">${opts(SUBJECTS, "题材")}</select>
    <select data-pf="orient">${opts(["横构图", "竖构图", "方形"], "构图")}</select>
    <select data-pf="mp"><option value="">像素:全部</option>
      <option value="45+">≥45MP</option><option value="24-45">24-45MP</option>
      <option value="12-24">12-24MP</option><option value="0-12">&lt;12MP</option></select>
    <select data-pf="burst"><option value="">连拍:全部</option>
      <option value="burst">连拍(≥3张)</option><option value="two">两连拍</option>
      <option value="single">单张</option></select>
    <select data-pf="quality"><option value="">画质:全部</option>
      <option value="blur">严重糊片</option><option value="over">过曝</option>
      <option value="under">欠曝</option><option value="blink">闭眼</option>
      <option value="best">组内最佳</option><option value="cull">建议删除</option></select>
    <select data-pf="rating"><option value="">星级:全部</option>
      <option value="5">★★★★★</option><option value="4">★★★★ 及以上</option>
      <option value="3">★★★ 及以上</option><option value="0">未评分</option></select>
    <select data-pf="aes"><option value="">美学:全部</option>
      <option value="7">≥7 分</option><option value="6">≥6 分</option>
      <option value="low">&lt;5 分</option></select>
    <select data-pf="colorw"><option value="">色调:全部</option>
      <option value="warm">暖色</option><option value="cool">冷色</option>
      <option value="mono">无彩色</option></select>
    <select data-pf="fam"><option value="">色族:全部</option>
      ${["红", "橙", "黄", "黄绿", "绿", "青绿", "青", "蓝", "蓝紫", "紫", "品红", "红紫"]
        .map(f => `<option value="${f}">${f}</option>`).join("")}</select>
    <select data-pf="gps"><option value="">GPS:全部</option>
      <option value="has">有坐标</option><option value="none">无坐标</option></select>
    <input type="date" data-pf="from" title="拍摄开始日期">
    <span class="psep">–</span>
    <input type="date" data-pf="to" title="拍摄结束日期">
    <span class="psep">|</span>
    <select id="expSize" title="导出尺寸">
      <option value="1920">长边 1920</option><option value="0">原图尺寸</option></select>
    <button id="expBtn" title="把当前筛选结果导出为 ZIP(RAW 用预览图)">⬇ 导出</button>
    <button id="panelClear" title="清除全部筛选">✕ 清除</button>
    <span id="panelCount"></span>`;
  bar.querySelectorAll("[data-pf]").forEach(el => {
    el.addEventListener("change", () => setFilter(el.dataset.pf, el.value, PANEL_TITLES[el.dataset.pf]));
  });
  $("#panelClear").addEventListener("click", clearFilter);
  $("#expBtn").addEventListener("click", doExport);
  syncPanel();
}

async function doExport() {
  const btn = $("#expBtn");
  if (!filteredList.length) { alert("当前筛选没有可导出的照片"); return; }
  btn.disabled = true;
  btn.textContent = "打包中…";
  try {
    const maxpx = $("#expSize").value;
    const resp = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: filteredList.map(f => f.p), maxpx: parseInt(maxpx) }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `photostats_export_${new Date().toISOString().slice(0, 10)}.zip`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) { alert("导出失败:" + e); }
  btn.disabled = false;
  btn.textContent = "⬇ 导出";
}

function buildAlbumBar() {
  const sec = $("#tab-browse");
  if ($("#albumBar")) return;
  const bar = document.createElement("div");
  bar.id = "albumBar";
  sec.insertBefore(bar, $("#panelBar"));
  bar.addEventListener("click", e => {
    const chip = e.target.closest(".album-chip");
    if (chip && !e.target.dataset.del) {
      const a = albums.find(x => x.name === chip.dataset.name);
      if (!a) return;
      filters.clear(); searchState = null;
      for (const [f, v, t] of a.filters) filters.set(f, { value: v, title: t });
      showTab("browse");
      refreshAll();
      return;
    }
    const del = e.target.closest("[data-del]");
    if (del) {
      fetch(`/api/albums/delete?name=${encodeURIComponent(del.dataset.del)}`)
        .then(() => loadAlbums()).then(renderAlbums);
      return;
    }
  });
  bar.addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.id === "albumName") saveAlbum();
  });
  bar.addEventListener("click", e => { if (e.target.id === "albumSave") saveAlbum(); });
  renderAlbums();
}
function renderAlbums() {
  const bar = $("#albumBar");
  if (!bar) return;
  bar.innerHTML = `<span class="sub">📁 相册:</span>` +
    (albums.length ? albums.map(a =>
      `<span class="album-chip" data-name="${a.name}">${a.name}
        <b data-del="${a.name}" title="删除相册">✕</b></span>`).join("")
     : `<span class="sub">(暂无,设置筛选后可保存)</span>`) +
    `<input id="albumName" placeholder="相册名,保存当前筛选" spellcheck="false">
     <button id="albumSave" class="album-save">💾 保存</button>`;
}
function saveAlbum() {
  const input = $("#albumName");
  const name = (input.value || "").trim();
  if (!name) { input.focus(); return; }
  if (!filters.size) { input.placeholder = "请先设置筛选条件"; return; }
  const data = [...filters.entries()].map(([f, c]) => [f, c.value, c.title]);
  fetch(`/api/albums/save?name=${encodeURIComponent(name)}&filters=${encodeURIComponent(JSON.stringify(data))}`)
    .then(r => r.json())
    .then(d => { albums = d.albums; renderAlbums(); input.value = ""; })
    .catch(e => alert("保存失败:" + e));
}
async function loadAlbums() {
  try {
    const d = await fetch("/api/albums").then(r => r.json());
    albums = d.albums || [];
  } catch (e) { albums = []; }
}
function syncPanel() {
  const bar = $("#panelBar");
  if (!bar) return;
  bar.querySelectorAll("[data-pf]").forEach(el => {
    el.value = filters.get(el.dataset.pf)?.value ?? "";
  });
  const count = $("#panelCount");
  if (count) count.textContent = `${filteredList.length.toLocaleString()} 张`;
}

/* ---------- 语义搜索与相似照片 ---------- */
function parseSmartQuery(q) {
  // E9:把中文组合查询解析为多条件筛选 + 剩余语义文本
  const filters = [];
  const desc = [];
  let rest = q;
  let year = null;
  const take = (re, fn) => {
    const m = rest.match(re);
    if (m) { fn(m); rest = rest.replace(m[0], " "); return true; }
    return false;
  };
  take(/(20\d{2})\s*年/, m => {
    year = m[1];
    filters.push(["year", year, "年份"]);
    desc.push("年份 " + year);
  });
  take(/(1[0-2]|[1-9])\s*月/, m => {
    if (year) {
      const mm = m[1].padStart(2, "0");
      filters.push(["month", `${year}-${mm}`, "月份"]);
      desc.push(`${mm} 月`);
    }
  });
  for (const label of SUBJECTS) {
    if (label !== "其他" && rest.includes(label)) {
      filters.push(["subj", label, "题材"]);
      desc.push("题材 " + label);
      rest = rest.split(label).join(" ");
    }
  }
  take(/竖(拍|构图)/, () => {
    filters.push(["orient", "竖构图", "构图"]);
    desc.push("竖构图");
  });
  take(/横(拍|构图)/, () => {
    filters.push(["orient", "横构图", "构图"]);
    desc.push("横构图");
  });
  take(/(严重糊片|糊片|模糊)/, () => {
    filters.push(["quality", "blur", "画质"]);
    desc.push("糊片");
  });
  take(/过曝/, () => { filters.push(["quality", "over", "画质"]); desc.push("过曝"); });
  take(/欠曝/, () => { filters.push(["quality", "under", "画质"]); desc.push("欠曝"); });
  take(/闭眼/, () => { filters.push(["quality", "blink", "画质"]); desc.push("闭眼"); });
  take(/([1-5一二三四五])\s*星/, m => {
    const n = { "一": 1, "二": 2, "三": 3, "四": 4, "五": 5 }[m[1]] || +m[1];
    filters.push(["rating", String(n), "星级"]);
    desc.push(`${n} 星以上`);
  });
  take(/连拍/, () => { filters.push(["burst", "burst", "连拍"]); desc.push("连拍"); });
  take(/有\s*GPS|有坐标/, () => {
    filters.push(["gps", "has", "GPS"]);
    desc.push("有 GPS");
  });
  const semantic = rest.replace(/\s+/g, " ").trim();
  return { filters, semantic, desc: desc.join(" · ") };
}

async function doSearch(q) {
  q = (q || "").trim();
  if (!q) return;
  try {
    const parsed = parseSmartQuery(q);
    filters.clear();
    for (const [f2, v2, t2] of parsed.filters) filters.set(f2, { value: v2, title: t2 });
    let list;
    if (parsed.semantic) {
      const d = await fetch(`/api/search?q=${encodeURIComponent(parsed.semantic)}&top=400`).then(r => r.json());
      list = (d.results || []).map(([p, sc]) => {
        const f = pathToFile.get(p);
        return f ? Object.assign({}, f, { _score: sc }) : null;
      }).filter(Boolean).filter(f => match(f));
    } else {
      list = LIB.files.filter(match).sort((a, b) =>
        (b.dt || "").localeCompare(a.dt || "") || a.p.localeCompare(b.p));
    }
    const parseNote = parsed.desc ? ` · 解析:${parsed.desc}` : "";
    searchState = { mode: "search", title: `智能搜索「${q}」${parseNote}`, list };
    refreshAll();
    showTab("browse");
  } catch (e) { alert("搜索失败:" + e); }
}
async function doSimilar(p) {
  try {
    const d = await fetch(`/api/similar?p=${encodeURIComponent(p)}&top=24`).then(r => r.json());
    const list = (d.results || []).map(([p2, s]) => {
      const f = pathToFile.get(p2);
      return f ? Object.assign({}, f, { _score: s }) : null;
    }).filter(Boolean);
    filters.clear();
    searchState = { mode: "similar", title: `与 ${p.split("/").pop()} 相似`, list };
    $("#lightbox").hidden = true;
    $("#lbMedia").innerHTML = "";
    refreshAll();
    showTab("browse");
  } catch (e) { alert("相似查找失败:" + e); }
}
async function loadPersons() {
  try {
    const d = await fetch("/api/faces").then(r => r.json());
    personList = d.persons || [];
    personMap = new Map(Object.entries(d.assign || {}));
  } catch (e) { personList = []; personMap = new Map(); }
}
let mergeMode = false;      // 人物合并模式
let mergeSel = new Set();   // 合并模式中已勾选的人物 id

async function refreshColorsData() {
  try {
    const d = await fetch("/api/colors").then(r => r.json());
    colorsMap = new Map(Object.entries(d.items || {}));
    colorSummary = d.summary;
    colorMonthly = d.monthly || [];
    return true;
  } catch (e) { console.warn("色彩数据加载失败", e); return false; }
}

function renderColorsTab() {
  const sec = $("#tab-colors");
  if (!sec.querySelector(".cards")) {
    sec.innerHTML = `<div class="cards" id="colorCards"></div>
      <div class="chart-grid">
        <div class="chart-card span-all"><h3>色族分布</h3>
          <p class="sub">按照片主导色族统计 · 点击色条筛选</p>
          <div class="chart" id="c-fam" style="height:340px"></div></div>
        <div class="chart-card"><h3>冷暖比例</h3>
          <p class="sub">按像素占比 · 点击筛选</p>
          <div class="chart" id="c-warm"></div></div>
        <div class="chart-card span-all"><h3>每月暖色趋势</h3>
          <p class="sub">各月平均暖色像素占比(彩色照片)· 点击月份筛选</p>
          <div class="chart" id="c-month" style="height:260px"></div></div>
      </div>`;
    $("#c-warm").style.height = "260px";
  }
  if (!colorSummary) {
    $("#colorCards").innerHTML = `<p class="empty" style="grid-column:1/-1">色彩数据加载中…</p>`;
    refreshColorsData().then(ok => { if (ok) { renderColorsTab(); refreshAll(); } });
    return;
  }
  const s = colorSummary;
  $("#colorCards").innerHTML = [
    ["已分析", s.analyzed.toLocaleString(), "张照片"],
    ["暖色占比", Math.round(s.warm * 100) + "%", "红/橙/黄像素"],
    ["冷色占比", Math.round(s.cool * 100) + "%", "绿/青/蓝/紫像素"],
    ["无彩色", Math.round(s.neutral * 100) + "%", "黑/白/灰像素"],
  ].map(c => `<div class="stat-card"><small>${c[0]}</small><b>${c[1]}</b><small>${c[2]}</small></div>`).join("");
  renderColorsCharts();
}

function renderColorsCharts() {
  if (!colorSummary) return;
  // 色族分布(横向条,按族颜色着色,可点击筛选)
  {
    const el = document.getElementById("c-fam");
    const old = charts["c-fam"];
    if (old) { try { old.dispose(); } catch (e) {} delete charts["c-fam"]; }
    const c = echarts.init(el);
    charts["c-fam"] = c;
    const fams = colorSummary.fams || [];
    const names = fams.map(f => f[0]).reverse();
    const vals = fams.map(f => f[1]).reverse();
    c.on("click", p => { if (p.name) setFilter("fam", p.name, "色族"); });
    c.setOption({
      grid: { left: 8, right: 40, top: 10, bottom: 8, containLabel: true },
      tooltip: {},
      xAxis: { type: "value", splitLine: { lineStyle: { color: "#f0f2f7" } } },
      yAxis: { type: "category", data: names, axisLabel: { fontSize: 12 } },
      series: [{ type: "bar", barMaxWidth: 22, data: vals.map((v, i) => ({
        value: v, itemStyle: { color: family_color(names[i]) } })) }],
    }, true);
  }
  // 冷暖比例(环形)
  {
    const el = document.getElementById("c-warm");
    const old = charts["c-warm"];
    if (old) { try { old.dispose(); } catch (e) {} delete charts["c-warm"]; }
    const c = echarts.init(el);
    charts["c-warm"] = c;
    c.on("click", p => {
      const map2 = { 暖色: "warm", 冷色: "cool", 无彩色: "mono" };
      if (map2[p.name]) setFilter("colorw", map2[p.name], "色调");
    });
    c.setOption({
      tooltip: {},
      series: [{ type: "pie", radius: ["45%", "70%"], center: ["50%", "50%"],
        label: { formatter: p => `${p.name} ${Math.round(p.percent)}%`, fontSize: 11 },
        data: [
          { name: "暖色", value: Math.round(colorSummary.warm * 1000), itemStyle: { color: "#fb8c00" } },
          { name: "冷色", value: Math.round(colorSummary.cool * 1000), itemStyle: { color: "#1e88e5" } },
          { name: "无彩色", value: Math.round(colorSummary.neutral * 1000), itemStyle: { color: "#8792a8" } },
        ] }],
    }, true);
  }
  // 每月暖色趋势
  {
    const el = document.getElementById("c-month");
    const old = charts["c-month"];
    if (old) { try { old.dispose(); } catch (e) {} delete charts["c-month"]; }
    const c = echarts.init(el);
    charts["c-month"] = c;
    c.on("click", p => { if (p.name) setFilter("month", p.name, "月份"); });
    c.setOption({
      grid: { left: 8, right: 8, top: 20, bottom: 8, containLabel: true },
      tooltip: { formatter: p => `${p.name}:暖色占比 ${Math.round(p.value * 100)}%(${colorMonthly.find(m => m.month === p.name)?.n || 0} 张)` },
      xAxis: { type: "category", data: colorMonthly.map(m => m.month), axisLabel: { fontSize: 10, rotate: 45 } },
      yAxis: { type: "value", max: 1, axisLabel: { formatter: v => Math.round(v * 100) + "%" },
               splitLine: { lineStyle: { color: "#f0f2f7" } } },
      series: [{ type: "bar", barMaxWidth: 18,
        data: colorMonthly.map(m => ({ value: m.warm,
          itemStyle: { color: m.warm > 0.5 ? "#fb8c00" : m.warm > 0.35 ? "#fdd835" : "#1e88e5" } })) }],
    }, true);
  }
}

function renderPersonsTab() {
  const sec = $("#tab-persons");
  if (!sec.querySelector(".persons-head")) {
    const head = document.createElement("div");
    head.className = "persons-head";
    head.innerHTML = `<h3 style="margin-bottom:4px">人物</h3>
      <p class="sub" id="personsSub">YuNet 检测 + SFace 聚类 · 全部本机计算 · 点击人物查看 TA 出现的照片 · ✎ 重命名</p>
      <p style="margin:8px 0"><button id="facesBtn" style="border:1px solid var(--accent);color:var(--accent);background:none;border-radius:6px;padding:5px 16px;cursor:pointer">开始人脸检测 / 增量更新</button>
      <button id="pMergeBtn" style="border:1px solid var(--line);color:var(--text);background:none;border-radius:6px;padding:5px 16px;cursor:pointer">合并人物</button>
      <button id="pResetBtn" style="border:1px solid var(--line);color:var(--muted);background:none;border-radius:6px;padding:5px 16px;cursor:pointer" title="清除全部重命名与合并">重置编辑</button>
      <span id="facesStatus" class="sub"></span></p>
      <div id="mergeBar" hidden style="margin:8px 0;padding:8px 12px;background:var(--accent-soft);border-radius:8px">
        勾选要合并的人物(2 个以上),将合并到最先勾选的那个:
        <button id="pDoMerge" style="border:1px solid var(--accent);color:var(--accent);background:#fff;border-radius:6px;padding:4px 14px;cursor:pointer">执行合并</button>
        <button id="pCancelMerge" style="border:1px solid var(--line);color:var(--muted);background:#fff;border-radius:6px;padding:4px 14px;cursor:pointer">取消</button>
        <span id="mergeHint" class="sub"></span>
      </div>`;
    const grid = document.createElement("div"); grid.id = "personsGrid"; grid.className = "grid";
    sec.appendChild(head); sec.appendChild(grid);
    $("#facesBtn").addEventListener("click", async () => {
      const btn = $("#facesBtn");
      btn.disabled = true;
      try {
        await fetch("/api/faces/rebuild");
        const poll = setInterval(async () => {
          const d = await fetch("/api/faces").then(r => r.json());
          $("#facesStatus").textContent = d.status.running
            ? `检测中 ${d.status.done}/${d.status.total}(${d.status.total ? Math.round(d.status.done / d.status.total * 100) : 0}%)…` : "";
          if (!d.status.running) {
            clearInterval(poll);
            btn.disabled = false;
            $("#facesStatus").textContent = d.status.error ? "失败:" + d.status.error : "完成 ✓";
            await loadPersons();
            renderPersonsTab();
          }
        }, 1500);
      } catch (e) { btn.disabled = false; $("#facesStatus").textContent = "启动失败:" + e; }
    });
    $("#pMergeBtn").addEventListener("click", () => {
      mergeMode = !mergeMode; mergeSel.clear();
      $("#mergeBar").hidden = !mergeMode;
      $("#pMergeBtn").textContent = mergeMode ? "退出合并" : "合并人物";
      renderPersonsGrid();
    });
    $("#pCancelMerge").addEventListener("click", () => {
      mergeMode = false; mergeSel.clear();
      $("#mergeBar").hidden = true;
      $("#pMergeBtn").textContent = "合并人物";
      renderPersonsGrid();
    });
    $("#pDoMerge").addEventListener("click", async () => {
      if (mergeSel.size < 2) { $("#mergeHint").textContent = "请至少勾选 2 人"; return; }
      const ids = [...mergeSel].sort((a, b) => a - b);
      const to = ids[0];
      await fetch(`/api/persons/merge?ids=${ids.join(",")}&to=${to}`);
      mergeMode = false; mergeSel.clear();
      $("#mergeBar").hidden = true;
      $("#pMergeBtn").textContent = "合并人物";
      await loadPersons();
      renderPersonsTab();
    });
    $("#pResetBtn").addEventListener("click", async () => {
      await fetch("/api/persons/reset");
      await loadPersons();
      renderPersonsTab();
    });
    grid.addEventListener("click", e => {
      const rn = e.target.closest(".prename");
      if (rn) { e.stopPropagation(); startRename(+rn.dataset.pid, rn); return; }
      const card = e.target.closest("figure[data-pid]");
      if (!card) return;
      const pid = +card.dataset.pid;
      const p = personList.find(x => x.id === pid);
      const name = p ? (p.name || `人物 ${pid + 1}`) : `人物 ${pid + 1}`;
      if (mergeMode) {
        if (mergeSel.has(pid)) mergeSel.delete(pid); else mergeSel.add(pid);
        $("#mergeHint").textContent = mergeSel.size ? `已勾选 ${mergeSel.size} 人` : "";
        renderPersonsGrid();
      } else {
        setFilter("person", pid, name);
      }
    });
  }
  renderPersonsGrid();
}

function renderPersonsGrid() {
  const grid = $("#personsGrid");
  if (!grid) return;
  grid.innerHTML = personList.length
    ? personList.map(p => {
        const name = p.name || `人物 ${p.id + 1}`;
        const checked = mergeSel.has(p.id) ? "checked" : "";
        return `<figure class="card person-card" data-pid="${p.id}"
            style="cursor:pointer;${mergeMode && mergeSel.has(p.id) ? "outline:3px solid var(--accent)" : ""}">
          <div style="position:relative">
            <img src="/api/faceimg?k=${p.rep.k}" style="aspect-ratio:1/1;object-fit:cover">
            ${mergeMode ? `<div class="pcheck"><input type="checkbox" data-pid="${p.id}" ${checked}></div>` : ""}
          </div>
          <figcaption><span>${name} · 出现 ${p.count} 张</span>
            <button class="prename" data-pid="${p.id}" title="重命名">✎</button></figcaption>
        </figure>`;
      }).join("")
    : `<p class="empty" style="grid-column:1/-1">尚无人物数据 —— 点击上方按钮开始人脸检测</p>`;
}

function openCompare() {
  if (compareArr.length < 2) return;
  const layer = $("#compareLayer");
  $("#cmpGrid").innerHTML = compareArr.map(p => {
    const isVideo = p.toLowerCase().match(/\.(mp4|mov)$/);
    const inner = isVideo
      ? `<div class="ph">🎬</div>`
      : `<img src="/api/preview?p=${encodeURIComponent(p)}" data-path="${p}" onerror="this.outerHTML='<div class=ph>🖼️</div>'">`;
    return `<div class="cmp-cell" data-path="${p}">${inner}
      <figcaption title="${p}">${p.split("/").pop()}</figcaption></div>`;
  }).join("");
  layer.hidden = false;
  $("#lightbox").hidden = true;
  $("#lbMedia").innerHTML = "";
}
function closeCompare() {
  $("#compareLayer").hidden = true;
}

function startRename(pid, btn) {
  const p = personList.find(x => x.id === pid);
  const cur = p && p.name && !p.name.startsWith("人物 ") ? p.name : "";
  const span = btn.parentElement.querySelector("span");
  const input = document.createElement("input");
  input.value = cur;
  input.className = "prename-input";
  input.placeholder = "输入名字,留空恢复默认";
  span.replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const commit = async () => {
    if (done) return; done = true;
    await fetch(`/api/persons/rename?id=${pid}&name=${encodeURIComponent(input.value.trim())}`);
    await loadPersons();
    renderPersonsGrid();
  };
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") commit();
    if (e.key === "Escape") { done = true; renderPersonsGrid(); }
  });
  input.addEventListener("blur", commit);
}

async function loadSearchInfo() {
  try {
    const d = await fetch("/api/search/info").then(r => r.json());
    searchModeInfo = d.mode === "multilingual" ? "中文/英文" : "仅英文";
  } catch (e) { searchModeInfo = ""; }
}

/* ---------- 聚合 ---------- */
function tally(list, keyFn, topN) {
  const m = new Map();
  for (const f of list) { const k = keyFn(f); if (k == null) continue; m.set(k, (m.get(k) || 0) + 1); }
  let arr = [...m.entries()].sort((a, b) => b[1] - a[1]);
  if (topN && arr.length > topN) {
    const rest = arr.slice(topN).reduce((s, x) => s + x[1], 0);
    arr = arr.slice(0, topN); arr.push(["其他", rest]);
  }
  return arr;
}
function tallyOrdered(list, keyFn, order) {
  const m = new Map();
  for (const f of list) { const k = keyFn(f); if (k == null) continue; m.set(k, (m.get(k) || 0) + 1); }
  return order.filter(k => m.has(k)).map(k => [k, m.get(k)]);
}
function monthSeries(list) {
  const photos = new Map(), videos = new Map();
  for (const f of list) {
    const m = monthOf(f); if (!m) continue;
    if (f.t === "video") videos.set(m, (videos.get(m) || 0) + 1);
    else photos.set(m, (photos.get(m) || 0) + 1);
  }
  const keys = [...new Set([...photos.keys(), ...videos.keys()])].sort();
  return { keys, photos: keys.map(k => photos.get(k) || 0), videos: keys.map(k => videos.get(k) || 0) };
}

/* ---------- 图表定义 ---------- */
const CHART_DEFS = [
  { tab: "overview", id: "ov-type", title: "文件类型占比", type: "donut", field: "type",
    data: l => [["RAW 照片", l.filter(f => f.t === "raw").length, "raw"],
                ["JPG 照片", l.filter(f => f.t === "jpg").length, "jpg"],
                ["视频", l.filter(f => f.t === "video").length, "video"]] },
  { tab: "shooting", id: "c-model", title: "机身", type: "barH", field: "model", data: l => tally(l, f => f.model, 15) },
  { tab: "shooting", id: "c-lens", title: "镜头", type: "barH", field: "lens", data: l => tally(l, f => f.lens, 15) },
  { tab: "shooting", id: "c-focal", title: "焦段分布", sub: "按 35mm 等效焦距", type: "barV", field: "focal",
    data: l => tallyOrdered(l, focalBucket, FOCAL_ORDER) },
  { tab: "shooting", id: "c-ap", title: "光圈分布", type: "barV", field: "ap", data: l => tallyOrdered(l, apBucket, AP_ORDER) },
  { tab: "shooting", id: "c-shutter", title: "快门速度分布", type: "barV", field: "shutter",
    data: l => tallyOrdered(l, shutterBucket, SHUTTER_ORDER) },
  { tab: "shooting", id: "c-iso", title: "ISO 分布", type: "barV", field: "iso", data: l => tallyOrdered(l, isoBucket, ISO_ORDER) },
  { tab: "shooting", id: "c-prog", title: "拍摄模式", type: "barV", field: "prog", data: l => tally(l, progLabel) },
  { tab: "shooting", id: "c-meter", title: "测光模式", type: "barV", field: "meter", data: l => tally(l, meterLabel) },
  { tab: "shooting", id: "c-flash", title: "闪光灯", type: "barV", field: "flash", data: l => tally(l, flashLabel) },
  { tab: "shooting", id: "c-orient", title: "横竖构图比例", sub: "仅照片,点击柱条筛选", type: "barV", field: "orient",
    data: l => tallyOrdered(l, orientOf, ORIENT_ORDER),
    subFn: l => {
      const d = tallyOrdered(l, orientOf, ORIENT_ORDER);
      const total = d.reduce((s, x) => s + x[1], 0);
      return total ? "仅照片 · " + d.map(([k, v]) => `${k} ${Math.round(v / total * 100)}%`).join(" · ") : "仅照片 · 无数据";
    } },
  { tab: "shooting", id: "c-ratio", title: "画幅比例分布", sub: "仅照片,含机内裁切模式", type: "barV", field: "ratio",
    data: l => tallyOrdered(l, ratioOf, RATIO_ORDER) },
  { tab: "time", id: "t-month", title: "按月拍摄量", sub: "滚轮缩放查看细节", type: "stack", field: "month" },
  { tab: "time", id: "t-year", title: "按年拍摄量", type: "barV", field: "year",
    data: l => { const s = tally(l, yearOf); return s.sort((a, b) => a[0].localeCompare(b[0])); } },
  { tab: "time", id: "t-hour", title: "一天中的时段", sub: "0-23 点", type: "barV", field: "hour",
    data: l => tallyOrdered(l, f => { const h = hourOf(f); return h == null ? null : h + "时"; },
      Array.from({ length: 24 }, (_, h) => h + "时")) },
  { tab: "time", id: "t-week", title: "按星期分布", type: "barV", field: "weekday", data: l => tallyOrdered(l, weekdayOf, WEEK) },
  { tab: "time", id: "t-cal", title: "拍摄日历", sub: "颜色深浅 = 当日拍摄量,点击某天筛选当天照片", type: "calendar" },
  { tab: "files", id: "f-folder", title: "文件夹分布", sub: "按顶层文件夹的照片数", type: "barH", field: "folder",
    data: l => tally(l.filter(f => f.t !== "video"), folderOf, 18) },
  { tab: "video", id: "v-res", title: "分辨率", type: "barV", field: "res", video: true, data: l => tally(l, resLabel) },
  { tab: "video", id: "v-fps", title: "帧率", type: "barV", field: "fps", video: true,
    data: l => tally(l, fpsLabel).sort((a, b) => parseFloat(a[0]) - parseFloat(b[0])) },
  { tab: "video", id: "v-codec", title: "编码格式", type: "barV", field: "codec", video: true, data: l => tally(l, codecLabel) },
  { tab: "video", id: "v-dur", title: "视频时长分布", type: "barV", field: "durb", video: true,
    data: l => tallyOrdered(l, durBucket, ["<10秒", "10-30秒", "30-60秒", "1-5分钟", "5-15分钟", ">15分钟"]) },
  { tab: "subjects", id: "s-donut", title: "题材占比", sub: "AI 识别(本机 CLIP 模型),点击扇区筛选", type: "donut", field: "subj",
    data: l => tally(l, subjOf).map(([k, v]) => [k, v, k]) },
  { tab: "subjects", id: "s-orient", title: "各题材的横竖构图", type: "subjOrient", field: "subj" },
];

/* ---------- 图表 DOM 构建 ---------- */
function ensureChartCard(def) {
  const sec = document.getElementById("tab-" + def.tab);
  if (document.getElementById(def.id)) return;
  if (!sec.querySelector(".chart-grid")) {
    const g = document.createElement("div"); g.className = "chart-grid"; sec.appendChild(g);
  }
  const card = document.createElement("div");
  card.className = "chart-card" + (def.type === "calendar" ? " span-all" : "");
  card.innerHTML = `<h3>${def.title}</h3>` +
    ((def.sub || def.subFn) ? `<p class="sub">${def.sub || ""}</p>` : "") +
    `<div class="chart" id="${def.id}"></div>`;
  sec.querySelector(".chart-grid").appendChild(card);
}

/* ---------- 渲染图表 ---------- */
const ACCENT = "#4f8cff", HOT = "#ff7a45";
// 每次渲染都销毁重建实例,避免隐藏容器中产生的损坏实例
function freshChart(def, el, onClick) {
  const old = charts[def.id];
  if (old) { try { old.dispose(); } catch (e) { /* 实例可能已损坏 */ } }
  el.innerHTML = "";
  const c = echarts.init(el);
  charts[def.id] = c;
  if (onClick) c.on("click", p => { if (p.componentType === "series") onClick(p); });
  return c;
}
function drawBar(def, data) {
  const el = document.getElementById(def.id);
  if (!el) return;
  if (!el.offsetWidth) return; // 容器不可见时跳过
  const c = freshChart(def, el, p => setFilter(def.field, p.name, def.title));
  const active = filters.get(def.field)?.value ?? null;
  const vertical = def.type === "barV" || def.type === "stack";
  const names = vertical ? data.map(d => d[0]) : data.map(d => d[0]).reverse();
  const vals = vertical ? data.map(d => d[1]) : data.map(d => d[1]).reverse();
  const series = [{
    type: "bar", barMaxWidth: 26,
    data: vals.map((v, i) => ({ value: v, itemStyle: { color: names[i] === active ? HOT : ACCENT } })),
    label: { show: true, position: vertical ? "top" : "right", fontSize: 10, color: "#8792a8" },
  }];
  const opt = {
    grid: { left: 8, right: vertical ? 16 : 40, top: 28, bottom: 8, containLabel: true },
    tooltip: { trigger: "axis" },
    xAxis: vertical
      ? { type: "category", data: names, axisLabel: { rotate: names.length > 7 ? 38 : 0, fontSize: 10.5, interval: 0 } }
      : { type: "value", splitLine: { lineStyle: { color: "#f0f2f7" } } },
    yAxis: vertical
      ? { type: "value", splitLine: { lineStyle: { color: "#f0f2f7" } } }
      : { type: "category", data: names, axisLabel: { fontSize: 10.5, width: 170, overflow: "truncate" } },
    series,
  };
  c.setOption(opt, true);
}

function drawStack(def) {
  const el = document.getElementById(def.id);
  if (!el || !el.offsetWidth) return;
  const c = freshChart(def, el, p => setFilter("month", p.name, "按月拍摄量"));
  const s = monthSeries(currentList());
  const active = filters.get("month")?.value ?? null;
  c.setOption({
    grid: { left: 8, right: 16, top: 34, bottom: 8, containLabel: true },
    tooltip: { trigger: "axis" },
    legend: { top: 0, right: 0, itemWidth: 12, textStyle: { fontSize: 11 } },
    dataZoom: [{ type: "inside" }],
    xAxis: { type: "category", data: s.keys, axisLabel: { fontSize: 10.5 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#f0f2f7" } } },
    series: [
      { name: "照片", type: "bar", stack: "a", barMaxWidth: 30,
        data: s.photos.map((v, i) => ({ value: v, itemStyle: { color: s.keys[i] === active ? HOT : ACCENT } })) },
      { name: "视频", type: "bar", stack: "a", barMaxWidth: 30,
        data: s.videos.map((v, i) => ({ value: v, itemStyle: { color: s.keys[i] === active ? HOT : "#d63f3f" } })) },
    ],
  }, true);
}

function drawDonut(def, data) {
  const el = document.getElementById(def.id);
  if (!el || !el.offsetWidth) return;
  const c = freshChart(def, el, p => {
    const item = data.find(d => d[0] === p.name);
    if (item && item[2]) setFilter(def.field, item[2], def.title);
  });
  c.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0, textStyle: { fontSize: 11 } },
    series: [{
      type: "pie", radius: ["42%", "68%"], center: ["50%", "44%"],
      label: { formatter: "{b}\n{c} ({d}%)" },
      data: data.map(d => ({ name: d[0], value: d[1] })),
    }],
  }, true);
}

function renderTab(tab) {
  const list = currentList();
  for (const def of CHART_DEFS) {
    if (def.tab !== tab) continue;
    ensureChartCard(def);
    if (def.type === "stack") { drawStack(def); continue; } // 堆叠图自带取数逻辑
    if (def.type === "subjOrient") { drawSubjOrient(def, list); continue; }
    if (def.type === "calendar") { drawCalendar(def, list); continue; }
    const data = def.video ? def.data(list.filter(f => f.t === "video")) : def.data(list);
    if (def.type === "donut") drawDonut(def, data);
    else drawBar(def, data);
    if (def.subFn) { // 动态副标题(如横竖构图百分比)
      const sub = document.getElementById(def.id)?.closest(".chart-card")?.querySelector(".sub");
      if (sub) sub.textContent = def.subFn(list);
    }
  }
  if (tab === "overview") renderOverview(list);
  if (tab === "files") renderFilesCards(list);
  if (tab === "subjects") renderSubjectsTab(list);
  if (tab === "persons") renderPersonsTab();
  if (tab === "colors") renderColorsTab();
  if (tab === "video") renderVideoCards(list);
}

/* ---------- 总览 ---------- */
function renderOverview(list) {
  const photos = list.filter(f => f.t !== "video");
  const raw = list.filter(f => f.t === "raw");
  const jpg = list.filter(f => f.t === "jpg");
  const vids = list.filter(f => f.t === "video");
  const size = list.reduce((s, f) => s + (f.s || 0), 0);
  const dates = list.map(f => f.dt).filter(Boolean).sort();
  const models = new Set(list.map(f => f.model).filter(Boolean));
  const lenses = new Set(list.map(f => f.lens).filter(Boolean));
  const pairedFiles = [...new Set(photos.filter(f => pairMap.get(f.p) === "paired").map(f => f.p))];
  const sc = Math.max(0, ...LIB.files.map(f => f.sc || 0));
  const cards = [
    ["照片总数", photos.length.toLocaleString(), `RAW ${raw.length.toLocaleString()} · JPG ${jpg.length.toLocaleString()}`],
    ["视频", vids.length.toLocaleString(), "个视频文件"],
    ["总大小", fmtSize(size), filters.size ? "当前筛选" : "全部文件"],
    ["拍摄跨度", dates.length ? dates[0].slice(0, 4) + " - " + dates[dates.length - 1].slice(0, 4) : "-",
      dates.length ? dates[0].slice(0, 10) + " ~ " + dates[dates.length - 1].slice(0, 10) : "无时间数据"],
    ["机身 / 镜头", models.size + " / " + lenses.size, "不同机身与镜头数"],
    ["RAW+JPG 成对", pairGroups.paired.toLocaleString() + " 组",
      `涉及 ${pairedFiles.length.toLocaleString()} 张照片(${photos.length ? Math.round(pairedFiles.length / photos.length * 100) : 0}%)`],
    ["快门数", sc ? sc.toLocaleString() : "-", sc ? "快门累计次数(机身内嵌)" : "元数据中未提供"],
    ["连拍占比", (() => {
      let b3 = 0, mx = 0, tot = 0;
      for (const n of burstMap.values()) { tot++; if (n >= 3) b3++; mx = Math.max(mx, n); }
      return tot ? Math.round(b3 / tot * 100) + "%" : "-";
    })(), (() => {
      let mx = 0, c3 = 0;
      for (const n of burstMap.values()) { mx = Math.max(mx, n); if (n >= 3) c3++; }
      return c3 ? `共 ${c3} 组 · 最大簇 ${mx} 张` : "无连拍";
    })()],
  ];
  const sec = $("#tab-overview");
  // E3 当年今日:往年同月同日
  const now = new Date();
  const md = String(now.getMonth() + 1).padStart(2, "0") + "-" + String(now.getDate()).padStart(2, "0");
  const mem = list.filter(f => (f.dt || "").slice(5, 10) === md && f.t !== "video"
    && parseInt((f.dt || "9999").slice(0, 4)) < now.getFullYear());
  let memStrip = sec.querySelector("#memStrip");
  if (mem.length && !memStrip) {
    memStrip = document.createElement("div");
    memStrip.id = "memStrip"; memStrip.className = "mem-strip";
    sec.insertBefore(memStrip, sec.firstChild);
  }
  if (memStrip) {
    if (mem.length) {
      memStrip.innerHTML = `<div class="mem-title">📅 往年今日(${md})· ${mem.length} 张
        <button onclick='setFilter("onday","${md}","往年今日")'>查看全部</button></div>
        <div class="mem-thumbs">${mem.slice(0, 8).map(f =>
          `<img src="/api/thumb?p=${encodeURIComponent(f.p)}" loading="lazy">`).join("")}</div>`;
      memStrip.style.display = "";
    } else memStrip.style.display = "none";
  }
  let cardsEl = sec.querySelector(".cards");
  if (!cardsEl) {
    cardsEl = document.createElement("div"); cardsEl.className = "cards";
    sec.insertBefore(cardsEl, sec.querySelector(".chart-grid"));
  }
    cardsEl.innerHTML = cards.map(c =>
    `<div class="stat-card"><small>${c[0]}</small><b>${c[1]}</b><small>${c[2]}</small></div>`).join("");
}

/* ---------- 文件与存储 ---------- */
function renderFilesCards(list) {
  let pairEl = $("#pairRow");
  if (!pairEl) {
    const sec = $("#tab-files");
    const row = document.createElement("div"); row.className = "cards"; row.id = "pairRow";
    sec.insertBefore(row, sec.querySelector(".chart-grid"));
    const grid = sec.querySelector(".chart-grid");
    const dupCard = document.createElement("div"); dupCard.className = "chart-card";
    dupCard.innerHTML = `<h3>疑似重复文件</h3><p class="sub">大小、尺寸、拍摄时间完全一致的文件组 ·
      <a href="#" id="dupFilter">浏览这些文件</a></p><div id="dupTbl" style="max-height:300px;overflow:auto"></div>`;
    grid.appendChild(dupCard);
    const vdupCard = document.createElement("div"); vdupCard.className = "chart-card";
    vdupCard.innerHTML = `<h3>视觉重复(pHash)</h3>
      <p class="sub">跨文件夹、改尺寸/重新导出也能识别 · 距离阈值 ≤ 8</p>
      <p style="margin:6px 0"><button id="vdupBtn" style="border:1px solid var(--accent);color:var(--accent);background:none;border-radius:6px;padding:4px 14px;cursor:pointer">计算 / 刷新哈希</button>
      <span id="vdupStatus" class="sub"></span></p>
      <div id="vdupTbl" style="max-height:300px;overflow:auto"></div>`;
    grid.appendChild(vdupCard);
    const qCard = document.createElement("div"); qCard.className = "chart-card";
    qCard.innerHTML = `<h3>画质概览</h3>
      <p class="sub">模糊 / 过曝 / 欠曝 · 选片与清理"仅RAW"的决策辅助</p>
      <p style="margin:6px 0"><button id="qBtn" style="border:1px solid var(--accent);color:var(--accent);background:none;border-radius:6px;padding:4px 14px;cursor:pointer">计算 / 刷新指标</button>
      <button id="softBtn" style="border:1px solid var(--line);color:var(--text);background:none;border-radius:6px;padding:4px 14px;cursor:pointer">计算同簇对比</button>
      <button id="aesBtn" style="border:1px solid var(--line);color:var(--text);background:none;border-radius:6px;padding:4px 14px;cursor:pointer">计算美学分</button>
      <span id="qStatus" class="sub"></span></p>
      <div id="qCards" class="cards" style="grid-template-columns:repeat(4,1fr);gap:8px"></div>`;
    grid.appendChild(qCard);
    const extCard = document.createElement("div"); extCard.className = "chart-card";
    extCard.innerHTML = `<h3>其他文件</h3><p class="sub">未纳入统计的文件类型</p><div id="extTbl" style="max-height:300px;overflow:auto"></div>`;
    grid.appendChild(extCard);
    $("#dupFilter").addEventListener("click", e => { e.preventDefault(); setFilter("dup", "dup", "疑似重复文件"); });
    $("#vdupBtn").addEventListener("click", rebuildHashes);
    $("#qBtn").addEventListener("click", rebuildQuality);
    $("#softBtn").addEventListener("click", rebuildSoft);
    $("#aesBtn").addEventListener("click", async () => {
      const b = $("#aesBtn");
      b.disabled = true;
      try {
        await fetch("/api/aesthetic/rebuild");
        const poll = setInterval(async () => {
          const d = await fetch("/api/aesthetic").then(r => r.json());
          $("#qStatus").textContent = d.status.running
            ? `美学评分 ${d.status.done}/${d.status.total}…` : "";
          if (!d.status.running) {
            clearInterval(poll);
            b.disabled = false;
            aestheticMap = new Map(Object.entries(d.scores || {}));
            refreshQuality();
          }
        }, 1000);
      } catch (e) { b.disabled = false; }
    });
    pairEl = $("#pairRow");
  }
  pairEl.innerHTML = `
    <div class="stat-card" style="cursor:pointer" onclick='setFilter("pair","paired","RAW+JPG 配对")'>
      <small>RAW+JPG 成对</small><b>${pairGroups.paired.toLocaleString()} 组</b><small>点击查看</small></div>
    <div class="stat-card" style="cursor:pointer" onclick='setFilter("pair","raw_only","仅 RAW")'>
      <small>仅 RAW(无 JPG)</small><b>${pairGroups.raw_only.toLocaleString()} 组</b><small>点击查看</small></div>
    <div class="stat-card" style="cursor:pointer" onclick='setFilter("pair","jpg_only","仅 JPG")'>
      <small>仅 JPG(无 RAW)</small><b>${pairGroups.jpg_only.toLocaleString()} 组</b><small>点击查看</small></div>`;
  const inList = new Set(list.map(f => f.p));
  const dupInList = dupGroups.filter(g => g.files.some(p => inList.has(p)));
  $("#dupTbl").innerHTML = dupInList.length ? "<table class='tbl'><tr><th>拍摄时间</th><th>组内数量</th><th>文件</th></tr>" +
    dupInList.slice(0, 40).map(g => {
      const rel = LIB.files.find(f => f.p === g.files[0]);
      return `<tr><td>${rel ? (rel.dt || "").replace("T", " ") : "-"}</td><td class="num">${g.files.length}</td>
        <td>${g.files.map(p => p.replace("/", " / ")).join("<br>")}</td></tr>`;
    }).join("") + "</table>" +
    (dupInList.length > 40 ? `<p class="sub">仅显示前 40 组,共 ${dupInList.length} 组</p>` : "")
    : "<p class='sub'>未发现疑似重复文件 🎉</p>";
  const others = Object.entries(LIB.meta.extCounts || {})
    .filter(([k]) => !["jpg", "jpeg", "nef", "hif", "dng", "rw2", "mp4", "mov"].includes(k))
    .sort((a, b) => b[1] - a[1]).slice(0, 15);
  $("#extTbl").innerHTML = "<table class='tbl'><tr><th>扩展名</th><th class='num'>数量</th></tr>" +
    others.map(([k, v]) => `<tr><td>.${k}</td><td class="num">${v}</td></tr>`).join("") + "</table>";
  refreshVisualDups();
  refreshQuality();
}

/* ---------- 画质概览(B1) ---------- */
async function refreshQualityData() {
  try {
    const d = await fetch("/api/quality").then(r => r.json());
    qualityThresholds = d.thresholds;
    qualityMap = new Map(Object.entries(d.items || {}));
    softSet = new Set((d.soft && d.soft.flagged) || []);
    return d;
  } catch (e) { console.warn("quality 数据异常", e); return {}; }
}
async function refreshQuality() {
  const cards = $("#qCards"), status = $("#qStatus");
  if (!cards) return;
  try {
    const d = await refreshQualityData();
    if (d.status.running) {
      status.textContent = `计算中 ${d.status.done}/${d.status.total}…`;
      setTimeout(refreshQuality, 1500);
      return;
    }
    if (d.status.error) { status.textContent = "失败:" + d.status.error; return; }
    status.textContent = `已分析 ${d.summary.analyzed.toLocaleString()} 张`;
    const s = d.summary;
    const softN = d.soft ? d.soft.count : 0;
    const softHint = d.soft && d.soft.computedAt ? `组内最软帧 · ${d.soft.groups} 组` : "需先计算同簇对比";
    const blinkN = blinkSet.size;
    const bestN = bestpickBest.size;
    const cullN = bestpickCull.size;
    cards.innerHTML = [
      ["严重糊片", s.blur, "blur", "失焦/抖动/废片"],
      ["过曝", s.over, "over", "高光裁切"],
      ["欠曝", s.under, "under", "暗部裁切"],
      ["同簇偏软", softN, "soft", softHint],
      ["闭眼(实验)", blinkN, "blink", "眼部纹理启发式"],
      ["组内最佳", bestN, "best", "AI 推荐保留"],
      ["建议删除", cullN, "cull", "组内明显更差者"],
    ].map(c => `<div class="stat-card" style="cursor:pointer;padding:8px 10px" onclick='setFilter("quality","${c[2]}","画质:${c[0]}")'>
      <small>${c[0]}</small><b>${c[1].toLocaleString()}</b><small>${c[3]} · 点击查看</small></div>`).join("");
  } catch (e) { status.textContent = "加载失败:" + e; }
}
async function rebuildSoft() {
  const btn = $("#softBtn");
  btn.disabled = true;
  try {
    await fetch("/api/softness/rebuild");
    const poll = setInterval(async () => {
      const d = await fetch("/api/quality").then(r => r.json());
      $("#qStatus").textContent = d.soft.status.running
        ? `同簇对比 ${d.soft.status.done}/${d.soft.status.total}…` : "";
      if (!d.soft.status.running) {
        clearInterval(poll);
        btn.disabled = false;
        if (d.soft.status.error) $("#qStatus").textContent = "失败:" + d.soft.status.error;
        else { await refreshQualityData(); refreshQuality(); }
      }
    }, 1500);
  } catch (e) { btn.disabled = false; $("#qStatus").textContent = "启动失败:" + e; }
}
async function rebuildQuality() {
  const btn = $("#qBtn");
  btn.disabled = true;
  try {
    await fetch("/api/quality/rebuild");
    const poll = setInterval(async () => {
      const d = await fetch("/api/quality").then(r => r.json());
      $("#qStatus").textContent = d.status.running
        ? `计算中 ${d.status.done}/${d.status.total}…` : "";
      if (!d.status.running) {
        clearInterval(poll);
        btn.disabled = false;
        if (d.status.error) $("#qStatus").textContent = "失败:" + d.status.error;
        else refreshQuality();
      }
    }, 1500);
  } catch (e) { btn.disabled = false; $("#qStatus").textContent = "启动失败:" + e; }
}

/* ---------- 视觉重复(pHash) ---------- */
async function refreshVisualDups() {
  const tbl = $("#vdupTbl"), status = $("#vdupStatus");
  if (!tbl) return;
  try {
    const d = await fetch("/api/dups").then(r => r.json());
    if (d.status.running) {
      status.textContent = `计算中 ${d.status.done}/${d.status.total}…`;
      setTimeout(refreshVisualDups, 1500);
      return;
    }
    if (d.status.error) { status.textContent = "失败:" + d.status.error; return; }
    status.textContent = `已哈希 ${d.hashed} 张`;
    const gs = d.groups || [];
    tbl.innerHTML = gs.length ? "<table class='tbl'><tr><th>组内数量</th><th>文件(跨文件夹)</th></tr>" +
      gs.slice(0, 30).map(g => `<tr><td class="num">${g.length}</td>
        <td>${g.map(p => p.replace("/", " / ")).join("<br>")}</td></tr>`).join("") + "</table>" +
      (gs.length > 30 ? `<p class="sub">仅显示前 30 组,共 ${gs.length} 组</p>` : "")
      : (d.hashed ? "<p class='sub'>未发现视觉重复 🎉</p>" : "<p class='sub'>点击上方按钮开始计算</p>");
  } catch (e) { status.textContent = "加载失败:" + e; }
}
async function rebuildHashes() {
  const btn = $("#vdupBtn");
  btn.disabled = true;
  try {
    await fetch("/api/dups/rebuild");
    const poll = setInterval(async () => {
      const st = await fetch("/api/dups").then(r => r.json());
      $("#vdupStatus").textContent = st.status.running
        ? `计算中 ${st.status.done}/${st.status.total}…` : "";
      if (!st.status.running) {
        clearInterval(poll);
        btn.disabled = false;
        if (st.status.error) $("#vdupStatus").textContent = "失败:" + st.status.error;
        else refreshVisualDups();
      }
    }, 1500);
  } catch (e) { btn.disabled = false; $("#vdupStatus").textContent = "启动失败:" + e; }
}

/* ---------- 拍摄日历热力图 ---------- */
function drawCalendar(def, list) {
  const el = document.getElementById(def.id);
  if (!el || !el.offsetWidth) return;
  const perDay = {};
  for (const f of list) {
    if (f.dt) { const d = f.dt.slice(0, 10); perDay[d] = (perDay[d] || 0) + 1; }
  }
  const dates = Object.keys(perDay).sort();
  if (!dates.length) { el.innerHTML = "<p class='empty'>无时间数据</p>"; return; }
  const years = [...new Set(dates.map(d => d.slice(0, 4)))].sort();
  el.style.height = years.length * 165 + 50 + "px";
  const c = freshChart(def, el, p => { if (p.value) setFilter("date", p.value[0], "拍摄日历"); });
  const maxV = Math.max(...Object.values(perDay));
  const calendars = years.map((y, i) => ({
    range: y, cellSize: [13, 13], top: 36 + i * 165, left: 70,
    dayLabel: { firstDay: 1, fontSize: 10, nameMap: ["日", "一", "二", "三", "四", "五", "六"] },
    monthLabel: { fontSize: 10 },
    yearLabel: { show: true, position: "left", fontSize: 12, color: "#8792a8" },
    itemStyle: { color: "#eef1f6", borderColor: "#fff", borderWidth: 1 },
  }));
  const series = years.map((y, i) => ({
    type: "heatmap", coordinateSystem: "calendar", calendarIndex: i,
    data: dates.filter(d => d.startsWith(y)).map(d => [d, perDay[d]]),
  }));
  c.setOption({
    tooltip: { formatter: p => `${p.value[0]}: ${p.value[1]} 张` },
    visualMap: { min: 0, max: maxV, calculable: false, orient: "horizontal", left: "center", top: 0,
      inRange: { color: ["#eef1f6", "#9dc0ff", "#4f8cff"] }, textStyle: { fontSize: 10 } },
    calendar: calendars, series,
  }, true);
}

/* ---------- 题材统计 ---------- */
function drawSubjOrient(def, list) {
  const el = document.getElementById(def.id);
  if (!el || !el.offsetWidth) return;
  const rows = {};
  for (const f of list) {
    const s = subjOf(f);
    if (!s) continue;
    const o = orientOf(f) || "横构图";
    rows[s] = rows[s] || { 横构图: 0, 竖构图: 0, 方形: 0 };
    rows[s][o]++;
  }
  const names = Object.entries(rows).sort((a, b) => (b[1].横构图 + b[1].竖构图 + b[1].方形) - (a[1].横构图 + a[1].竖构图 + a[1].方形))
    .map(([k]) => k).reverse();
  const c = freshChart(def, el, p => setFilter("subj", p.name, "题材"));
  c.setOption({
    grid: { left: 8, right: 40, top: 30, bottom: 8, containLabel: true },
    tooltip: { trigger: "axis" },
    legend: { top: 0, right: 0, itemWidth: 12, textStyle: { fontSize: 11 } },
    xAxis: { type: "value", splitLine: { lineStyle: { color: "#f0f2f7" } } },
    yAxis: { type: "category", data: names, axisLabel: { fontSize: 11 } },
    series: [
      { name: "横构图", type: "bar", stack: "o", barMaxWidth: 18, data: names.map(n => rows[n].横构图), itemStyle: { color: ACCENT } },
      { name: "竖构图", type: "bar", stack: "o", data: names.map(n => rows[n].竖构图), itemStyle: { color: "#d63f3f" } },
      { name: "方形", type: "bar", stack: "o", data: names.map(n => rows[n].方形), itemStyle: { color: "#f5c542" } },
    ],
  }, true);
}

function renderSubjectsTab(list) {
  const photos = list.filter(f => f.t !== "video");
  const classified = photos.filter(f => subjMap.has(f.p)).length;
  const photosAll = LIB.files.filter(f => f.t !== "video").length;
  const classifiedAll = [...new Set([...subjMap.keys()])].length;
  let cardsEl = $("#subjCards");
  if (!cardsEl) {
    const sec = $("#tab-subjects");
    cardsEl = document.createElement("div");
    cardsEl.className = "cards"; cardsEl.id = "subjCards";
    const grid = document.createElement("div"); grid.className = "chart-grid";
    sec.appendChild(cardsEl); sec.appendChild(grid);
    const runCard = document.createElement("div"); runCard.className = "chart-card";
    runCard.innerHTML = `<h3>AI 题材识别</h3>
      <p class="sub" id="subjModelInfo">CLIP 零样本分类 · 本机显卡加速 · 照片不出电脑</p>
      <p style="margin:8px 0">模型包 <select id="packSel" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;background:#fff;color:var(--text)"></select></p>
      <p style="margin:8px 0"><button id="classifyBtn" style="border:1px solid var(--accent);color:var(--accent);background:none;border-radius:6px;padding:6px 18px;cursor:pointer">▶ 运行识别 / 增量更新</button></p>
      <p class="sub" id="classifyProgress"></p>`;
    grid.appendChild(runCard);
    $("#classifyBtn").addEventListener("click", startClassify);
    loadPackSelect();
    const xmpCard = document.createElement("div"); xmpCard.className = "chart-card";
    xmpCard.innerHTML = `<h3>XMP 标签回写</h3>
      <p class="sub">把 AI 题材写成 .xmp sidecar(Lightroom/digiKam 可读)。只创建缺失的
      sidecar,不覆盖已有文件,不碰原片。</p>
      <p style="margin:6px 0"><button id="xmpBtn" style="border:1px solid var(--accent);color:var(--accent);background:none;border-radius:6px;padding:4px 14px;cursor:pointer">回写缺失的 sidecar</button>
      <span id="xmpStatus" class="sub"></span></p>
      <div id="xmpInfo" class="sub"></div>`;
    grid.appendChild(xmpCard);
    $("#xmpBtn").addEventListener("click", writeXmp);
    refreshXmp();
  }
  cardsEl.innerHTML = [
    ["已识别", classified.toLocaleString() + " 张", `当前筛选中的照片(${photosAll.toLocaleString()} 张全库)`],
    ["未识别", (photos.length - classified).toLocaleString(), "点击上方按钮开始识别"],
    ["题材覆盖", photosAll ? Math.round(classifiedAll / photosAll * 100) + "%" : "-",
      `全库已识别 ${classifiedAll.toLocaleString()} 张`],
  ].map(c => `<div class="stat-card"><small>${c[0]}</small><b>${c[1]}</b><small>${c[2]}</small></div>`).join("");
}

async function refreshXmp() {
  const info = $("#xmpInfo"), status = $("#xmpStatus");
  if (!info) return;
  try {
    const d = await fetch("/api/xmp/preview").then(r => r.json());
    if (d.status.running) {
      status.textContent = `回写中 ${d.status.done}/${d.status.total}…`;
      setTimeout(refreshXmp, 1500);
      return;
    }
    const p = d.preview;
    info.textContent = `有题材 ${p.withSubject.toLocaleString()} 张 · 已回写 ${p.alreadyWritten.toLocaleString()} · 待写 ${p.toWrite.toLocaleString()}`;
    if (d.status.error) status.textContent = "失败:" + d.status.error;
  } catch (e) { info.textContent = "加载失败:" + e; }
}
async function writeXmp() {
  const btn = $("#xmpBtn");
  btn.disabled = true;
  try {
    await fetch("/api/xmp/write");
    const poll = setInterval(async () => {
      const d = await fetch("/api/xmp/preview").then(r => r.json());
      $("#xmpStatus").textContent = d.status.running
        ? `回写中 ${d.status.done}/${d.status.total}…` : "";
      if (!d.status.running) {
        clearInterval(poll);
        btn.disabled = false;
        if (d.status.error) $("#xmpStatus").textContent = "失败:" + d.status.error;
        else refreshXmp();
      }
    }, 1500);
  } catch (e) { btn.disabled = false; $("#xmpStatus").textContent = "启动失败:" + e; }
}

async function loadPackSelect() {
  try {
    const d = await fetch("/api/packs").then(r => r.json());
    const sel = $("#packSel");
    if (!sel) return;
    sel.innerHTML = (d.packs || []).map(p =>
      `<option value="${p.id}"${p.active ? " selected" : ""}>${p.name}</option>`).join("");
    sel.onchange = async () => {
      const r = await fetch(`/api/packs/select?id=${encodeURIComponent(sel.value)}`).then(x => x.json());
      $("#classifyProgress").textContent = r.ok
        ? `已切换模型包,嵌入空间已变化 —— 请点「运行识别」重建(全库约 10 分钟)`
        : "切换失败:未知的模型包";
      await loadSubjects();
      refreshAll();
    };
  } catch (e) { /* 模型包列表不可用时隐藏选项 */ }
}

async function startClassify() {  const btn = $("#classifyBtn"), prog = $("#classifyProgress");
  btn.disabled = true;
  try {
    await fetch("/api/classify/start");
    const poll = setInterval(async () => {
      const st = await fetch("/api/classify/status").then(r => r.json());
      prog.textContent = st.running
        ? `${st.phase === "extract" ? "提取 RAW 预览图" : "AI 识别中"} ${st.done}/${st.total}(${st.total ? Math.round(st.done / st.total * 100) : 0}%)…`
        : "";
      if (!st.running) {
        clearInterval(poll);
        btn.disabled = false;
        if (st.error) { prog.textContent = "识别失败:" + st.error; return; }
        prog.textContent = "识别完成 ✓ " + (st.finishedAt || "");
        await loadSubjects();
        refreshAll();
      }
    }, 1000);
  } catch (e) {
    btn.disabled = false;
    prog.textContent = "启动失败:" + e;
  }
}

async function loadSubjects() {
  const d = await fetch("/api/subjects").then(r => r.json());
  SUBJECTS = d.categories || [];
  subjMap = new Map(Object.entries(d.results || {}));
}

/* ---------- 视频统计卡片 ---------- */function renderVideoCards(list) {
  const vids = list.filter(f => f.t === "video");
  let el = $("#vidCards");
  if (!el) {
    el = document.createElement("div"); el.className = "cards"; el.id = "vidCards";
    const sec = $("#tab-video");
    sec.insertBefore(el, sec.querySelector(".chart-grid"));
  }
  const total = vids.reduce((s, f) => s + (f.dur || 0), 0);
  const uhd = vids.filter(f => f.w === 3840 && f.h === 2160).length;
  el.innerHTML = [
    ["视频数量", vids.length.toLocaleString() + " 个", "当前筛选范围"],
    ["总时长", fmtDur(total), "平均 " + (vids.length ? fmtDur(total / vids.length) : "-")],
    ["4K 占比", vids.length ? Math.round(uhd / vids.length * 100) + "%" : "-", `3840×2160 共 ${uhd} 个`],
    ["视频容量", fmtSize(vids.reduce((s, f) => s + (f.s || 0), 0)), "占用空间"],
  ].map(c => `<div class="stat-card"><small>${c[0]}</small><b>${c[1]}</b><small>${c[2]}</small></div>`).join("");
}

/* ---------- 照片浏览 ---------- */
function cardHTML(f, i) {
  const ext = f.p.split(".").pop().toUpperCase();
  const ph = f.t === "video" ? "🎬" : "🖼️";
  const tag = f.t === "video" ? `<span class="tag vid">${ext}</span>`
    : f.t === "raw" ? `<span class="tag raw">RAW</span>` : "";
  const name = f.p.split("/").pop();
  const subj = subjOf(f);
  const score = f._score != null ? ` · 匹配 ${Math.round(f._score * 100)}%` : "";
  const qtag = isBlur(f) ? `<span class="tag qblur">糊</span>` : "";
  return `<figure class="card" data-i="${i}">
    <img loading="lazy" src="/api/thumb?p=${encodeURIComponent(f.p)}" data-ph="${ph}" onerror="phErr(this)">${tag}${qtag}
    <figcaption title="${f.p}">${name} · ${fmtSize(f.s)}${score}${subj ? " · " + subj : ""}</figcaption></figure>`;
}
function phErr(el) {
  el.outerHTML = `<div class="ph">${el.dataset.ph || "🖼️"}</div>`;
}
function renderBrowse(reset) {
  const grid = $("#grid");
  if (reset) { gridPage = 1; renderedCount = 0; grid.innerHTML = ""; }
  if (!filteredList.length) {
    grid.innerHTML = "<p class='empty'>没有符合条件的文件</p>";
    $("#loadMore").hidden = true;
    return;
  }
  const upto = Math.min(gridPage * PAGE, filteredList.length);
  const items = filteredList.slice(renderedCount, upto)
    .map((f, k) => cardHTML(f, renderedCount + k)).join("");
  grid.insertAdjacentHTML("beforeend", items);
  renderedCount = upto;
  $("#loadMore").hidden = renderedCount >= filteredList.length;
}

/* ---------- 灯箱 ---------- */
let lbIndex = 0;
function openLb(i) {
  lbIndex = i;
  $("#lightbox").hidden = false;
  renderLb();
}
function renderLb() {
  const f = filteredList[lbIndex];
  if (!f) return;
  const enc = encodeURIComponent(f.p);
  $("#lbMedia").innerHTML = f.t === "video"
    ? `<video controls autoplay src="/api/file?p=${enc}"></video>`
    : `<img src="/api/preview?p=${enc}" onerror="if(!this.dataset.t){this.dataset.t=1;this.src='/api/thumb?p=${enc}'}">`;
  const ext = f.p.split(".").pop().toUpperCase();
  const lines = [
    ["文件", f.p], ["类型", ext + " · " + fmtSize(f.s)],
    ["尺寸", f.w ? `${f.w}×${f.h} (${(f.w * f.h / 1e6).toFixed(1)}MP)` : null],
    ["拍摄时间", f.dt ? f.dt.replace("T", " ") : null],
    ["机身", f.model], ["镜头", f.lens],
    ["焦距", f.fl ? `${f.fl}mm${f.fl35 && f.fl35 !== f.fl ? "(等效 " + f.fl35 + "mm)" : ""}` : null],
    ["光圈", f.ap ? "f/" + f.ap : null], ["快门", shutterLabel(f.exp)],
    ["ISO", f.iso ? Math.round(f.iso) : null],
    ["曝光补偿", f.ec ? (f.ec > 0 ? "+" : "") + Math.round(f.ec * 10) / 10 + "EV" : null],
    ["测光 / 模式", [meterLabel(f), progLabel(f)].filter(Boolean).join(" / ") || null],
    ["闪光灯", flashLabel(f)],
    ["画质", qualityLabel(f)],
    ["人物", (personMap.get(f.p) || []).map(pid => {
      const p = personList.find(x => x.id === pid);
      return p ? (p.name || `人物 ${pid + 1}`) : null;
    }).filter(Boolean).join(", ") || null],
    ["美学分", aestheticMap.get(f.p) != null ? `${aestheticMap.get(f.p)} / 10` : null],
    ["选片", bestpickBest.has(f.p) ? "组内最佳 ✓" : (bestpickCull.has(f.p) ? "建议删除 ✗" : null)],
    ["题材", subjOf(f) ? `${subjOf(f)}(置信度 ${Math.round(subjConf(f) * 100)}%)` : null],
    ["视频", f.dur ? `${fmtDur(f.dur)} · ${resLabel(f)} · ${fpsLabel(f) || ""} ${codecLabel(f) || ""}` : null],
    ["快门数", f.sc ? f.sc.toLocaleString() : null],
    ["GPS", f.lat != null && f.lon != null ? f.lat.toFixed(5) + ", " + f.lon.toFixed(5) : null],
  ].filter(l => l[1] != null && l[1] !== "");
  const stars = ratingsMap.get(f.p) || 0;
  $("#lbInfo").innerHTML = `<h4>${f.p.split("/").pop()}</h4>
    <div class="lbstars">${[1, 2, 3, 4, 5].map(n =>
      `<span class="st${n <= stars ? " on" : ""}" data-n="${n}" title="${n} 星">★</span>`).join("")}
      <button class="cmpbtn" id="cmpAdd">＋ 对比</button>
      <span id="cmpCount" class="cmpbtn" style="cursor:default">${compareArr.length ? "对比 " + compareArr.length : ""}</span></div>` +
    lines.map(l => `<p>${l[0]}:<b>${l[1]}</b></p>`).join("");
}
function lbMove(d) {
  if (!filteredList.length) return;
  lbIndex = (lbIndex + d + filteredList.length) % filteredList.length;
  renderLb();
}

/* ---------- 顶部摘要与筛选条 ---------- */
function renderSummary() {
  const m = LIB.meta;
  const photos = filteredList.filter(f => f.t !== "video").length;
  const vids = filteredList.length - photos;
  const size = filteredList.reduce((s, f) => s + (f.s || 0), 0);
  $("#summary").textContent =
    `${photos.toLocaleString()} 张照片 · ${vids} 个视频 · ${fmtSize(size)} · 共 ${LIB.files.length.toLocaleString()} 个文件`;
  $("#summary").title = `目录:${m.root}\n扫描时间:${m.scannedAt || "-"}`;
  const bar = $("#filterbar");
  if (searchState) {
    bar.hidden = false;
    $("#filterLabel").textContent = `${searchState.title} · ${searchState.list.length.toLocaleString()} 个文件`;
  } else if (filters.size) {
    bar.hidden = false;
    $("#filterLabel").textContent =
      `筛选 ${filterSummary()} · ${filteredList.length.toLocaleString()} 个文件`;
  } else bar.hidden = true;
}

/* ---------- 刷新与初始化 ---------- */
function refreshAll() {
  filteredList = currentList();
  renderSummary();
  renderTab(currentTab);
  renderBrowse(true);
  syncPanel();
}
function showTab(tab) {
  currentTab = tab;
  document.querySelectorAll("nav#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll("main .tab").forEach(s => s.classList.toggle("active", s.id === "tab-" + tab));
  renderTab(tab);
  if (tab === "browse") renderBrowse(false);
  for (const c of Object.values(charts)) { try { c.resize(); } catch (e) { /* 忽略 */ } }
}

async function init() {
  for (let i = 0; i < 3; i++) {  // 服务启动初期可能未就绪,重试 3 次
    try {
      LIB = await fetch("/api/library").then(r => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
      break;
    } catch (e) {
      if (i === 2) { document.body.innerHTML = "<p style='padding:40px'>数据加载失败:" + e + " —— 请刷新重试</p>"; return; }
      await new Promise(res => setTimeout(res, 1500));
    }
  }
  await loadSubjects();
  buildMaps();
  pathToFile = new Map(LIB.files.map(f => [f.p, f]));
  buildPanel();
  buildAlbumBar();
  await loadSearchInfo();
  await loadPersons();
  try { await refreshQualityData(); } catch (e) { console.warn("quality 加载失败", e); }
  await Promise.all([
    fetch("/api/ratings").then(r => r.json()).then(d => {
      ratingsMap = new Map(Object.entries(d.ratings || {}));
    }).catch(() => {}),
    fetch("/api/aesthetic").then(r => r.json()).then(d => {
      aestheticMap = new Map(Object.entries(d.scores || {}));
    }).catch(() => {}),
    fetch("/api/blink").then(r => r.json()).then(d => {
      blinkSet = new Set(Object.keys(d.photos || {}).filter(p => d.photos[p]));
    }).catch(() => {}),
    fetch("/api/bestpick").then(r => r.json()).then(d => {
      bestpickBest = new Set((d.groups || []).map(g => g.best));
      bestpickCull = new Set((d.groups || []).flatMap(g => g.cull));
    }).catch(() => {}),
  ]);
  try { await loadAlbums(); } catch (e) { console.warn("相册加载失败", e); }
  await refreshColorsData();
  window.setFilter = setFilter; // 供内联 onclick 使用
  refreshAll();
  showTab("overview");
  window.__ready = true;

  $("#tabs").addEventListener("click", e => {
    const b = e.target.closest("button"); if (b) showTab(b.dataset.tab);
  });
  $("#clearFilter").addEventListener("click", clearFilter);
  $("#loadMore").addEventListener("click", () => { gridPage++; renderBrowse(false); });
  $("#grid").addEventListener("click", e => {
    const c = e.target.closest(".card"); if (c) openLb(+c.dataset.i);
  });
  $("#rescanBtn").addEventListener("click", async () => {
    const btn = $("#rescanBtn"), hint = $("#rescanHint");
    btn.disabled = true; hint.textContent = "扫描中,请稍候(约 1-2 分钟)…";
    try {
      LIB = await fetch("/api/rescan").then(r => r.json());
      await loadSubjects();
      buildMaps(); refreshAll();
      hint.textContent = "扫描完成 ✓";
    } catch (err) { hint.textContent = "扫描失败:" + err; }
    btn.disabled = false;
    setTimeout(() => hint.textContent = "", 4000);
  });
  $("#lbClose").addEventListener("click", () => { $("#lightbox").hidden = true; $("#lbMedia").innerHTML = ""; });
  $("#lbPrev").addEventListener("click", () => lbMove(-1));
  $("#lbNext").addEventListener("click", () => lbMove(1));
  $("#lbInfo").addEventListener("click", e => {
    const st = e.target.closest(".st");
    if (st) {
      const n = +st.dataset.n;
      const f = filteredList[lbIndex];
      if (!f) return;
      ratingsMap.set(f.p, n);
      $("#lbInfo").querySelectorAll(".st").forEach(x =>
        x.classList.toggle("on", +x.dataset.n <= n));
      fetch(`/api/rating?path=${encodeURIComponent(f.p)}&stars=${n}`);
      return;
    }
    if (e.target.id === "cmpAdd") {
      const f = filteredList[lbIndex];
      if (!f) return;
      if (!compareArr.includes(f.p)) compareArr.push(f.p);
      if (compareArr.length > 4) compareArr.shift();
      $("#cmpCount").textContent = compareArr.length ? "对比 " + compareArr.length : "";
      return;
    }
    if (e.target.id === "cmpCount" && compareArr.length >= 2) {
      openCompare();
    }
  });
  $("#cmpClose").addEventListener("click", closeCompare);
  $("#cmpGrid").addEventListener("click", e => {
    const cell = e.target.closest(".cmp-cell");
    if (!cell) return;
    compareArr = compareArr.filter(p => p !== cell.dataset.path);
    if (compareArr.length < 2) closeCompare(); else openCompare();
  });
  $("#lbSimilar").addEventListener("click", () => {
    const f = filteredList[lbIndex];
    if (f) doSimilar(f.p);
  });
  $("#searchBox").addEventListener("keydown", e => {
    if (e.key === "Enter") doSearch(e.target.value);
  });
  $("#reportBtn").addEventListener("click", () => window.open("/api/report", "_blank"));
  $("#lightbox").addEventListener("click", e => { if (e.target === e.currentTarget) $("#lbClose").click(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#compareLayer").hidden) { closeCompare(); return; }
    if ($("#lightbox").hidden) return;
    if (e.key === "Escape") $("#lbClose").click();
    if (e.key === "ArrowLeft") lbMove(-1);
    if (e.key === "ArrowRight") lbMove(1);
  });
  window.addEventListener("resize", () => {
    for (const c of Object.values(charts)) { try { c.resize(); } catch (e) { /* 忽略已损坏实例 */ } }
  });
}
init();

window.addEventListener("error", e => {
  (window.__errs = window.__errs || []).push(e.message + " @" + (e.filename || "").split("/").pop() + ":" + e.lineno);
});
