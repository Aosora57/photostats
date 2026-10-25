# PhotoStats 技术文档(交接用)

> 本地照片库统计查看器:为零依赖、离线运行设计,面向尼康照片库(JPG + NEF + MP4/MOV)。
> 面向使用者的说明见 `README.md`,本文面向后续开发者。

- 项目路径:`<安装目录>\`
- 照片库根目录:默认取 `PhotoStats` 的上级目录(即 `<照片库根目录>`),可用 `--root` 覆盖
- 运行环境:Windows 10/11 + Python 3.12(C:\Python312),核心功能仅用标准库
- 服务地址:`http://127.0.0.1:8765`(只监听本机)
- 当前规模:8,515 个文件(照片 8,297 / 视频 218 / 其他 92),约 184.7GB

---

## 1. 已实现功能清单

| 页签 | 功能 | 状态 |
|------|------|------|
| 总览 | 照片/视频总数、容量、拍摄跨度、机身/镜头数、RAW+JPG 成对组数、快门数(尼康机身内嵌 ShutterCount) | ✅ |
| 拍摄参数 | 机身、镜头、焦段(35mm 等效分桶)、光圈、快门、ISO、拍摄模式、测光、闪光灯、横竖构图、画幅比例 共 11 张分布图 | ✅ |
| 时间分布 | 按月堆叠图(照片+视频,滚轮缩放)、按年、按小时(0-23)、按星期 | ✅ |
| 文件与存储 | RAW+JPG 配对三卡(成对/仅RAW/仅JPG,可点击筛选)、文件夹分布、疑似重复文件(大小+尺寸+拍摄时间一致)、其他扩展名清单 | ✅ |
| 题材统计 | CLIP 零样本 AI 识别 11 类题材(人像/风景/动物/美食/建筑/夜景/街拍/微距/花卉/星空/其他):占比环形图、各题材横竖构图堆叠图、覆盖率卡片、识别按钮(带双阶段进度) | ✅ |
| 语义搜索 | 顶部搜索框,中文/英文自然语言找照片(多语言 CLIP 文本编码器 + 图像嵌入余弦 Top-N),照片墙显示匹配度 | ✅ |
| 相似照片 | 灯箱「🔍 相似」按钮,基于嵌入余弦找同场景连拍(选片辅助) | ✅ |
| 视觉去重 | pHash 感知哈希(64 位 DCT),抓改尺寸/重新导出的跨文件夹重复,并查集聚组 | ✅ |
| 画质概览 | 拉普拉斯清晰度 + 曝光裁切检测(严重糊片/过曝/欠曝,阈值校准 + 暗帧豁免)+ 同簇相对清晰度对比(嵌入同场景分组) | ✅ |
| 人物 | YuNet 检测 + SFace 嵌入聚类(cv2 官方实现),人物头像网格,点击按人筛选 | ✅ |
| 连拍统计 | 同文件夹 ≤1.5s 聚簇,连拍占比/最大簇,浏览面板按连拍筛选 | ✅ |
| XMP 回写 | 题材关键词写入 .xmp sidecar(只创建缺失,不碰原片),Lightroom/digiKam 可读 | ✅ |
| 年度报告 | 一键生成自包含 HTML 年报(代表作 base64 内嵌) | ✅ |
| 增量扫描 | 启动/重新扫描自动 diff(mtime+大小),仅提取新增/变化文件 | ✅ |
| 模型包 | 语义检索模型模块化(pack.json 注册表),换模型=放文件+切换+重算嵌入 | ✅ |
| 拍摄日历 | 时间分布页签的多年份日历热力图(GitHub 风格),点击某天筛选 | ✅ |
| 视频统计 | 数量/总时长/平均时长、4K 占比、分辨率、帧率、编码、时长分布 | ✅ |
| 照片浏览 | 缩略图墙(懒加载,每页 120)、RAW 缩略图提取、点击灯箱看大图+完整 EXIF+题材置信度、视频在线播放(Range 分片) | ✅ |
| 全局 | 多条件筛选(AND 叠加):图表点击柱条/扇区 + 照片浏览筛选面板(机身/镜头/题材/构图/像素/画质/GPS/日期范围),全页签联动重聚合;顶部筛选条;重新扫描按钮 | ✅ |
| 基建 | 中文路径批量元数据提取、按内容寻址的缩略图缓存、扫描缓存、增量识别 | ✅ |

**未实现**(讨论过的路线,见 §8):GPS 地图足迹、重复文件清理(目前只检测不删除)、打包 exe、年度报告导出、视频画面题材识别、按置信度过滤的 UI 控件。

> **后续功能推进以 `目标1.md` 为准**(A→B→C→D 四级路线图,含验收标准与完成记录),本文 §8 只保留概要。

---

## 2. 架构总览

```
┌───────────────────────────  数据生产层(离线批处理) ───────────────────────────┐
│                                                                              │
│  photostats.py(入口)                                                        │
│    ├─ scanner.scan()      → exiftool 批量提取元数据 → cache/metadata.json    │
│    └─ classifier          → CLIP 零样本题材识别 → cache/classifications.json │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
                                   ↓  JSON 缓存
┌───────────────────────────  服务层(运行时) ──────────────────────────────────┐
│  server.py:ThreadingHTTPServer(仅 127.0.0.1:8765)                          │
│    静态页面  /  /static/*                                                     │
│    数据 API  /api/library /api/subjects /api/rescan                          │
│    识别 API  /api/classify/start /api/classify/status(后台线程 + 轮询)      │
│    媒体 API  /api/thumb(缓存) /api/preview /api/file(Range 206)           │
└──────────────────────────────────────────────────────────────────────────────┘
                                   ↓
┌───────────────────────────  前端(无框架原生 JS) ─────────────────────────────┐
│  static/app.js:拉取 /api/library + /api/subjects → 内存聚合 → ECharts 渲染   │
│  筛选 = 前端内存过滤(match 函数),无后端查询;图表点击 → setFilter() → 全页刷新│
└──────────────────────────────────────────────────────────────────────────────┘
```

设计要点:

1. **两层缓存、按需生成**:元数据/识别结果是"批处理产物"(JSON 文件);缩略图/预览图是"请求时产物"(md5 寻址落盘)。两者都可整体删除,程序自动重建。
2. **聚合全部在前端**:后端只吐原始记录(~8.5k 条 JSON,约 6MB),所有分桶统计由 `app.js` 完成,所以筛选可以瞬时联动。
3. **零 pip 依赖跑核心,ML 依赖可选**:没有 onnxruntime 时元数据统计照常工作,只有题材识别不可用(前端按钮会报缺少依赖)。

---

## 3. 代码地图(文件 → 职责 → 关键函数)

### `photostats.py` — 命令行入口

| 函数 | 职责 |
|------|------|
| `main()` | 参数解析(`--root --port --scan-only --rescan --no-open`)、缓存命中判断、启动 HTTP 服务、自动开浏览器 |
| `scanner.scan()` 调用 | 全量扫描,`on_progress` 打印进度 |
| `print_summary()` | 控制台摘要(照片/视频数、跨度、机身 Top3、配对统计) |
| `pair_stats()` | RAW+JPG 配对统计(同目录同名不同扩展名为一组) |

### `scanner.py` — 元数据提取(依赖 tools/exiftool.exe 13.59)

| 成员 | 说明 |
|------|------|
| `TARGET_EXTS` | raw: nef/hif/dng/rw2;jpg: jpg/jpeg;video: mp4/mov |
| `BASE_ARGS` | 请求的 exiftool 标签清单(增删统计字段从这里改) |
| `collect_files()` | os.walk 遍历(跳过 `$RECYCLE.BIN`、`System Volume Information`、排除目录),返回相对路径 + 扩展名计数 |
| `_run()` | **中文路径关键**:参数与文件清单写入 UTF-8 argfile,`exiftool -@ argfile` 调用,带 `-charset filename=utf8` |
| `parse_dt()` | EXIF 时间 `2025:02:01 10:30:45` → ISO `2025-02-01T10:30:45` |
| `_num()` | 数值清洗;处理 exiftool 的 `{Value, Scale}` 结构(**秒数 = Value × Scale**,不是除法,见 §7) |
| `normalize()` | 统一字段名(见 §5 数据契约);**竖拍换算**:Orientation ∈ 5-8 时交换 w/h |
| `scan()` | 按 `BATCH_SIZE=500` 分批调用 exiftool;无拍摄时间的文件用 mtime 兜底(`dt_src="mtime"`) |
| `extract_binary()` | `-b` 提取内嵌二进制(NEF 缩略图/预览图用) |

### `classifier.py` — CLIP 零样本题材识别

| 成员 | 说明 |
|------|------|
| `SUBJECTS` | **题材定义唯一入口**:11 类,每类若干英文提示词(集成平均提升稳定性)。改这里 → `_sig()` 变化 → 旧结果自动失效 |
| `MODELS_DIR` | `tools/models/`:vision.onnx(605MB,完整 CLIP 模型)、text.onnx(64MB,量化文本编码器)、tokenizer.json |
| `_session()` | onnxruntime 会话;providers 优先 `DmlExecutionProvider`(任意 DX12 显卡)回落 CPU |
| `_text_embeds()` | 文本嵌入:分词(77 token,BOS 49406/EOS 49407)→ text.onnx → 按题材平均 + L2 归一化;结果按提示词签名缓存到 `cache/text_embeds.json` |
| `_preprocess()` | 短边缩放 → 中心裁剪 224 → CLIP mean/std 归一化 → NCHW |
| `ClassifyState` | 识别进度对象(`running/done/total/phase/error`),供服务端轮询;`phase`: extract(提取 NEF 预览)/ classify(模型推理) |
| `_extract_one()` | 单个 RAW 的 PreviewImage→ThumbnailImage 提取,md5 文件名落盘 `cache/cls_previews/` |
| `classify_all()` | 主流程:①计划任务(JPG 直接用;RAW 优先复用同组 JPG;无 JPG 的 RAW 标记提取)②6 线程并行提取预览 ③8 线程并行预处理(JPEG 用 `draft()` DCT 快速解码)+ 32 张/批 GPU 推理 ④softmax 取 argmax + 置信度 ⑤写 `cache/classifications.json` |
| `_sig()` / `_load_result()` | 结果缓存带题材定义签名;不匹配即整体作废 |

性能参考(本机 RTX 3060 Laptop):GPU 推理 202 张/s;全管线(含解码)约 13 张/s;全库 8,297 张约 7 分钟。

### `encoders.py` — 模型包注册表(模块化层,A6)

**设计**:模型模块化 = "模型包"。一个包 = 图像编码器 + 分类用文本编码器 + 查询用文本编码器 + 预处理参数,描述在 `pack.json`。查询与图像编码必须在同一向量空间,所以**换包 = 嵌入空间整体切换**,`embed_sig(包id)` 使 `cache/embeddings.npz` 自动失效,点「运行识别」重算(签名别名机制让历史缓存可无缝映射)。

| 成员 | 说明 |
|------|------|
| `list_packs()` | 扫描 `tools/models/pack.json`(默认包,文件在根目录)+ `tools/models/packs/*/pack.json`(未来新包,文件相对各自目录) |
| `active_pack()` / `set_active()` | 当前包读取/切换(持久化到 `tools/models/active_pack.json`),pack.json 改动按 mtime 自动重载 |
| `vision_session()` | 图像编码会话(DML 优先);支持 needs_dummy_text(联合模型喂哑文本)与自定义 output |
| `encode_texts(cfg, slot, texts)` | 统一文本编码入口,支持两种 kind:`native-clip`(BOS/EOS 77 token)与 `st-multilingual`(mean pooling + Dense 投影) |
| `query_embed()` / `embed_sig()` | 单条查询向量 / 嵌入缓存签名(`pack:<id>`,别名表 EMB_SIG_ALIASES 兼容历史 v1) |

**新增更强模型的步骤**:① 模型 ONNX 放进 `tools/models/packs/<新id>/`;② 写 pack.json(vision 的 input/output/image_size/mean/std + 两个文本槽 kind/onnx/tokenizer);③ 在题材页签下拉切换 → 点「运行识别」重建全库嵌入;④ 若新模型支持中文提示词,可在 pack 加 `prompts` 覆盖文件并配 `prompt_lang: "zh"`(SUBJECTS 标签不变)。候选升级:OpenAI CLIP ViT-L/14、SigLIP、Chinese-CLIP。

### `searcher.py` — 语义搜索与相似照片(A2/A3)

| 成员 | 说明 |
|------|------|
| `_init_session()` | 查询编码器:优先 `tools/models/multilingual.onnx`(sentence-transformers clip-ViT-B-32-multilingual-v1 fp32,**中文可用**);缺文件回落 text.onnx(仅英文) |
| `_load_dense()` | 手工解析 `dense.safetensors`(8字节头长+JSON头+张量),取 linear.weight(512×768,无 bias) |
| `_query_embed()` | 多语言路径:BERT 分词 → mean pooling(按 mask)→ Dense 投影 → L2;英文路径:BOS/EOS CLIP 文本编码 |
| `_matrix()` | 图像嵌入矩阵缓存(`cache/embeddings.npz`),按 mtime 失效 |
| `search()` / `similar()` | 余弦 Top-N;`similar` 排除自身 |

**注意**:quint8 量化版(135MB)中文表示严重退化,必须用 fp32(539MB)。嵌入向量空间 = OpenAI CLIP ViT-B/32 图像空间(该模型即蒸馏到该空间),与视觉侧嵌入直接可比。

### `phash.py` — 感知哈希视觉去重(A4)

| 成员 | 说明 |
|------|------|
| `phash_of()` | 32×32 灰度 → 正交 DCT(`_DCT32` 矩阵)→ 左上 8×8 低频 → 中位数阈值 → 64bit |
| `compute_hashes()` | 8 线程并行补算缺失哈希(JPG draft 解码/RAW 复用 cls_previews),存 `cache/phashes.json` |
| `find_visual_dups()` | 分块异或 + 查表 popcount 汉明距离 ≤ 8;并查集聚组;排除同文件夹与同名 RAW/JPG 伴生对的直连(跨文件夹仍会经传递聚进同组) |

### `quality.py` — 画质检测(B1)

| 成员 | 说明 |
|------|------|
| `metrics_of()` | 512px 灰度图:拉普拉斯方差(切片卷积免 scipy)+ 8×8 分块方差最大值 + 高光(>250)/暗部(<5)像素占比 |
| `compute_quality()` | 8 线程并行补算(图片来源同 phash),原始值存 `cache/quality.json`(阈值后调不重算) |
| `flags_of()` / `summary()` | 原始值 → 标记:严重糊片(lap_max<60 且非暗帧)、过曝(over>0.10)、欠曝(under>0.35 或暗帧 under>0.5) |

**校准教训**:lap_max 60-180 区间被天然低纹理场景(云/雾/星空/日落渐变)主导,单图盲测精度 <80%;暗帧(星空)拉普拉斯值天然极低。最终阈值 60 + 暗帧豁免,12 张抽样一致率 83%。扩召回需"同簇相对清晰度"对比(嵌入相似 + 同组内排序)。

### `faces.py` — 人脸检测与人物聚类(B2)

| 成员 | 说明 |
|------|------|
| `detect()` | letterbox 到 640×640 → YuNet(OpenCV Zoo)→ **原始头解码**(cls×obj 为分数,bbox/kps 为锚点距离×stride,stride 8/16/32)→ 坐标还原 |
| `_similarity_transform()` / `_warp()` | 5 点 Umeyama 相似变换 + 逆映射双线性采样,对齐到 ArcFace 112×112 模板 |
| `align_and_embed()` | SFace(128 维)逐脸推理(导出为静态 batch=1),BGR 0-255 输入 |
| `compute_faces()` | 6 线程并行全库检测;人脸记录存 `cache/faces.json`,嵌入存 `cache/face_embs.npz`,对齐裁剪图存 `cache/face_crops/` |
| `_cluster()` | 贪心聚类(余弦 ≥0.363,SFace 官方阈值);<3 张脸的散簇归为未分配;`cache/persons.json` 存人物列表与分配 |

**注意**:YuNet 仅检测人脸;SFace 导出为静态 batch=1。onnxruntime 警告可用 `ort.set_default_logger_severity(3)` 抑制。cv2 DNN 对象**非线程安全**,多线程必须 thread-local 会话。

**人物编辑**:`cache/person_edits.json` 存 names(id→名)与 mergeInto(id→id 链式合并映射),`resolve_persons()` 把聚类结果 + 编辑解析为界面视图(计数按去重照片);聚类重跑不丢命名。API:`/api/persons/rename?id=&name=`、`/api/persons/merge?ids=a,b,c&to=`(合并到 to)、`/api/persons/reset`。

**星级(E1)**:`xmp.set_rating` — ratings.json 为主存储,XMP sidecar Rating 由 exiftool 写入(已有 sidecar 原位更新,缺失则 `-o` 创建连带照片标签)。**闭眼(E7)**:`quality.compute_blink` — 对齐脸眼部区域灰度标准差启发式(实验性,阈值 EYE_STD_OPEN)。**组内最佳(E8)**:`quality.compute_bestpick` — 同场景组内美学分最高者为最佳,低 1.0 分以上为建议删除。

### `colors.py` — 色彩分析(E10)

64px 缩略图 → PIL HSV → 12 色相 bin(按饱和度 ≥0.12 过滤无彩色)+ 暖/冷/无彩色像素占比 + 主导色族。
`compute_colors` 8 线程并行(图片来源同 phash),存 `cache/colors.json`;`monthly_warmth` 按月聚合暖色占比。
色族:红/橙/黄/黄绿/绿/青绿/青/蓝/蓝紫/紫/品红/红紫(30° 一档);暖 = 红/橙/黄/品红端。
**坑**:指标里的 np.float32 必须显式 float() 转换,否则 JSON 序列化崩溃丢全部结果。

### `aesthetic.py` — 美学评分(E4)

LAION aesthetic predictor v1 线性头(512→1,兼容 OpenAI CLIP ViT-B/32 嵌入空间),权重从
`tools/models/aesthetic_head.safetensors` 提取(手工 safetensors 解析)。`score_embeddings` 直接对
embeddings.npz 打分(秒级);`compute_all` 增量补分。输出约 0-10 分,存 `cache/aesthetic.json`。

### `exporter.py` — 批量导出(E5)

`build_zip(root, files_by, ids, maxpx)` → ZIP(RAW 用预览/内嵌图 + PIL 缩放,JPEG q88,
STORED 存储),输出 `cache/export_<ts>.zip`。上限 2000 张。

### `xmp.py` — XMP 标签回写(C4/E1)

| 成员 | 说明 |
|------|------|
| `sidecar_path()` / `xmp_xml()` | sidecar 同目录同名 .xmp;最小化 XMP 模板(dc:subject 关键词 = 题材 + "PhotoStats") |
| `preview()` / `write_sidecars()` | 统计可写数量;4 线程创建缺失 sidecar(已存在一律跳过,写入记录存 `cache/xmp_state.json`) |

**安全边界**:不覆盖已有 sidecar(Lightroom 修图参数在内)、不改原片、评分不自动写。

### `report.py` — 年度报告(C5)

`generate(year, root, files)` → 自包含 HTML(`cache/report_<year>.html`,代表作缩略图 base64 内嵌)。聚合:总量/月度节奏/题材构成/器材 Top3/年度之最/画质提醒。

### `server.py` — 本地 HTTP 服务(纯标准库)

| 成员 | 说明 |
|------|------|
| `AppState` | 持有 root、library、classify_state、缓存路径;`rescan()` 加锁重建;`start_classify()` 拉起后台识别线程(防重入) |
| `make_handler()` 路由 | 见 §6 API 清单 |
| `_safe_file()` | **路径安全**:拒绝 `..`,resolve 后必须位于 root 之下 |
| `_thumb()` | 缩略图:JPG 优先内嵌 ThumbnailImage(无则回原图);RAW 取 ThumbnailImage→PreviewImage;md5(路径+mtime) 做缓存键,`Cache-Control: max-age=604800` |
| `_preview()` | 灯箱大图:JPG 原图;RAW 取 PreviewImage(约 1MP JPEG) |
| `_serve_file()` | 原文件下载/播放,实现 `Range` 请求(206 分片,1MB 块),视频拖动进度条依赖它 |

### `static/` — 前端

| 文件/成员 | 说明 |
|------|------|
| `index.html` | 页签 nav + 各 tab section + 灯箱;**静态资源用 `?v=N` 版本参数防缓存**,改 JS/CSS 后记得递增 |
| `app.js` 数据 | `LIB`(全量元数据)、`subjMap`(路径→[题材,置信度])、`pairMap`/`dupSet`(配对/重复)、`filter`(当前筛选) |
| `app.js` 分桶 | `focalBucket`(35mm 等效 9 桶)、`apBucket`/`shutterBucket`/`isoBucket`(档位就近匹配)、`orientOf`/`ratioOf`(构图与画幅)、`subjOf`、`monthOf/hourOf/weekdayOf`、`resLabel/fpsLabel/codecLabel/durBucket`(视频) |
| `app.js` 图表 | `CHART_DEFS` 注册表(id/tab/类型/取数函数/筛选字段);`drawBar`(点击柱条筛选)、`drawStack`(按月堆叠)、`drawDonut`、`drawSubjOrient`(题材×构图) |
| `freshChart()` | **关键模式**:每次渲染 dispose 旧实例重建(避免隐藏容器中 init 出 width=0 的损坏实例,见 §7) |
| `renderTab()` → 各页签渲染 | `renderOverview` / `renderFilesCards` / `renderSubjectsTab` / `renderVideoCards` |
| 筛选系统 | `match(f)` 按字段分发;`setFilter(field, value, title)`;筛选后 `refreshAll()` 全页联动 |
| 照片墙 | `renderBrowse` 每页 120 张懒加载缩略图;卡片角标 RAW/视频;figcaption 附题材 |
| 灯箱 | 大图(NEF 用 preview)+ EXIF 全文 + 题材置信度;←/→/Esc 键盘导航 |
| `startClassify()` | 调 `/api/classify/start` 后每秒轮询 status,完成后 `loadSubjects()` + `refreshAll()` |
| `style.css` | 卡片/网格/灯箱样式;注意 `#filterbar[hidden]{display:none}` 需显式写(flex 会覆盖 hidden 属性) |

### 数据文件与缓存

| 路径 | 内容 | 可否删除 |
|------|------|----------|
| `cache/metadata.json` | 全库元数据(~6MB),扫描产物 | ✅ 自动重建(全库扫描约 3.5 分钟) |
| `cache/classifications.json` | `{sig, results:{路径:[题材,置信度]}}` | ✅ 识别按钮重建 |
| `cache/text_embeds.json` | 提示词嵌入(改 SUBJECTS 后自动失效) | ✅ |
| `cache/thumbs/` | 浏览用缩略图,md5(路径+mtime).jpg | ✅ 按需重建 |
| `cache/cls_previews/` | 无 JPG 配对的 NEF 预览图(md5(路径).jpg) | ✅ 但重建要重跑 exiftool(约 2,700 张 × 0.15s) |
| `cache/embeddings.npz` | 图像嵌入(sig=EMB_SIG + paths + 8297×512 float32 矩阵) | ✅ 识别按钮重建(全库约 10 分钟) |
| `cache/phashes.json` | pHash 表 {路径: 16位十六进制} | ✅「计算/刷新哈希」按钮重建(约 2 分钟) |
| `tools/models/multilingual.onnx` + `dense.safetensors` | 多语言查询编码器 + Dense 投影(A2) | 可删,搜索回落英文模式 |
| `server.log` | 最近一次后台启动的输出 | 可删 |

---

## 4. 外部依赖与资产

| 项 | 版本/大小 | 说明 |
|----|-----------|------|
| Python | 3.12.0(C:\Python312) | pip 包用 `--user` 安装(系统 Scripts 目录无写权限) |
| onnxruntime-directml | 1.24.4 | GPU 加速:DmlExecutionProvider(RTX 3060 实测可用);换 NVIDIA 专用 CUDA 版可再提速,但需装 cuDNN |
| pillow / numpy / tokenizers | 最新稳定 | tokenizers 只用于加载 tokenizer.json(避免装 transformers/torch) |
| exiftool | 13.59 | `tools/exiftool.exe` + `exiftool_files/`(绿色版,SourceForge 下载,本机需走代理 127.0.0.1:7897) |
| ECharts | 5.5.1 | `static/echarts.min.js` 本地化,不依赖 CDN |
| opencv-python-headless | 5.0.0 | B2 人脸管线(FaceDetectorYN/FaceRecognizerSF) |
| CLIP 模型 | 约 1.2GB | 图像侧+英文文本:`Xenova/clip-vit-base-patch32`;中文查询:`sentence-transformers/clip-ViT-B-32-multilingual-v1`(fp32 ONNX + Dense safetensors) |

---

## 5. 关键数据契约

**元数据记录**(metadata.json / `/api/library`,字段名为短码省体积):

```
p 相对路径(POSIX斜杠)  t 类型(raw/jpg/video)  s 字节
w,h 视觉宽高(已按Orientation换算)      make,model,lens 机身镜头
fl 实际焦距  fl35 35mm等效  ap 光圈F值  exp 快门秒  iso  ec 曝光补偿
flash 0/1  meter 测光枚举  prog 曝光模式枚举  dt ISO时间
dur 视频秒  fps  codec(avc1/hvc1…)  sc 快门数  rating  lat,lon GPS
dt_src 存在且为"mtime"表示时间来自文件修改时间
```

**识别记录**(classifications.json / `/api/subjects`):

```
{ "sig": "题材定义签名", "results": { "2025/.../DSC_1234.NEF": ["星空", 0.85], ... } }
```

**RAW 分类取图优先级**:同组 JPG 原图 > NEF PreviewImage > ThumbnailImage(全部落盘 cls_previews,一次提取永久复用)。

---

## 6. HTTP API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` `/static/*` | 页面与静态资源(无强缓存) |
| GET | `/api/library` | 全量元数据 `{meta:{root,scannedAt,totalFiles,extCounts}, files:[...]}` |
| GET | `/api/subjects` | `{categories:[题材名], results:{路径:[题材,置信度]}}` |
| GET | `/api/rescan` | **增量**重扫(与现有记录 diff,仅提取新增/变化文件,秒级);全量用 CLI `--rescan` |
| GET | `/api/xmp/preview` `/api/xmp/write` `/api/xmp/status` | XMP 回写:预览统计 / 后台回写 / 进度 |
| GET | `/api/classify/start` | 启动后台识别线程(已运行则幂等返回当前进度) |
| GET | `/api/classify/status` | `{running, done, total, phase, error, finishedAt}` |
| GET | `/api/search/info` | `{mode: multilingual/english, count: 嵌入数}` |
| GET | `/api/packs` | 模型包列表 `{packs: [{id,name,dim,active}], active}` |
| GET | `/api/packs/select?id=` | 切换激活模型包(嵌入自动标记失效) |
| GET | `/api/search?q=&top=200` | 语义搜索:`{results: [[路径, 相似度], ...]}` 按相似度降序 |
| GET | `/api/similar?p=&top=20` | 相似照片(不含自身) |
| GET | `/api/dups/rebuild` | 后台计算感知哈希(幂等) |
| GET | `/api/dups` | `{status, threshold: 8, hashed, groups: [[路径,...],...]}` |
| GET | `/api/quality/rebuild` | 后台计算画质指标(幂等) |
| GET | `/api/quality` | `{status, thresholds, summary: {analyzed, blur, over, under}, items: {路径: [lap_var, lap_max, over, under]}}` |
| GET | `/api/thumb?p=相对路径` | 缩略图 JPEG(长缓存) |
| GET | `/api/preview?p=相对路径` | 灯箱大图(RAW 返回内嵌预览) |
| GET | `/api/file?p=相对路径` | 原文件,支持 Range(视频播放) |
| GET | `/api/faces` `/api/faces/rebuild` | 人物列表/进度;后台人脸检测与聚类(B2) |
| GET | `/api/faceimg?k=` | 人脸对齐裁剪图(cache/face_crops) |
| GET | `/api/persons/rename?id=&name=` `/api/persons/merge?ids=a,b&to=` `/api/persons/reset` | 人物重命名/合并/重置编辑 |
| GET | `/api/softness/rebuild` | 同簇相对清晰度计算(B1.5) |
| GET | `/api/ratings` `/api/rating?path=&stars=` | 星级读取 / 设置(E1,XMP 双向) |
| GET | `/api/aesthetic` `/api/aesthetic/rebuild` | 美学分数据 / 增量计算(E4) |
| GET | `/api/blink/rebuild` `/api/blink` | 闭眼检测(实验性,E7) |
| GET | `/api/bestpick` | 组内最佳/建议删除(E8,实时计算) |
| GET | `/api/albums` `/api/albums/save?name=&filters=` `/api/albums/delete?name=` | 智能相册(E2) |
| POST | `/api/export` | `{ids:[...], maxpx}` → ZIP 附件(E5) |
| GET | `/api/colors` `/api/colors/rebuild` | 色彩数据(暖/冷/无彩色/色族)/ 后台计算(E10) |
| GET | `/api/faces` `/api/faces/rebuild` | 人物列表/进度;后台人脸检测与聚类(B2) |
| GET | `/api/faceimg?k=` | 人脸对齐裁剪图(cache/face_crops) |
| GET | `/api/persons/rename?id=&name=` `/api/persons/merge?ids=a,b&to=` `/api/persons/reset` | 人物重命名/合并/重置编辑 |
| GET | `/api/softness/rebuild` | 同簇相对清晰度计算(B1.5) |
| GET | `/api/ratings` `/api/rating?path=&stars=` | 星级读取 / 设置(E1,XMP 双向) |
| GET | `/api/aesthetic` `/api/aesthetic/rebuild` | 美学分数据 / 增量计算(E4) |
| GET | `/api/blink/rebuild` `/api/blink` | 闭眼检测(实验性,E7) |
| GET | `/api/bestpick` | 组内最佳/建议删除(E8,实时计算) |
| GET | `/api/albums` `/api/albums/save?name=&filters=` `/api/albums/delete?name=` | 智能相册(E2) |
| POST | `/api/export` | `{ids:[...], maxpx}` → ZIP 附件(E5) |

---

## 7. 踩过的坑(必读,防止回归)

1. **exiftool Duration 结构**:部分 MP4(如"导出"文件夹)返回 `{Value, Scale}` 结构,**秒数 = Value × Scale**。曾按除法算出 3.2 亿小时。批量请求时 exiftool 还会走 Composite 路径返回结构,单文件请求却是正常数值——修复后重扫过"导出"文件夹。
2. **视频编码标签必须加命名空间**:`-Compression` 会匹配到照片的 TIFF Compression(数值 6/34713),要写 `-QuickTime:Compression`。
3. **中文路径**:exiftool 命令行直接传中文参数会乱码,必须用 `-@ argfile` + `-charset filename=utf8`,argfile 以 UTF-8 无 BOM 写入。
4. **尼康竖拍**:文件按传感器横向存储,视觉方向在 EXIF Orientation(5-8 需交换 w/h)。横竖构图统计依赖此换算,否则 8,274 横 vs 22 竖。
5. **ECharts 隐藏容器**:`echarts.init` 在 `display:none` 的容器里会创建 width=0 的损坏实例,后续 `setOption` 内部 dispose 崩溃(`__ec_inner` 错误)。采用 `freshChart()`:渲染前 dispose 旧实例重建。同理 `itemStyle.color` 回调在 progressive 渲染下只收到 1 个参数,**颜色要预计算成 data item**,不要用回调函数。
6. **PIL 不接受 bytes 对象**:`Image.open(b"...")` 会被当成路径解码报 UnicodeDecodeError,必须 `Image.open(io.BytesIO(data))`。
7. **Xenova 的 model.onnx 是图文联合模型**:输入要求 input_ids + pixel_values + attention_mask 三者齐备。方案:喂一条哑文本("a photo"),只取 `image_embeds` 输出;文本嵌入用量化的 text.onnx(仅 input_ids 输入,无需 attention_mask)。
8. **CLIP 提示词偏差**:人举花束的照片会被视觉上更突出的"花卉"吸走。v2 提示词给人像加了婚礼/毕业/合影场景描述后,婚礼文件夹人像 54→177、毕业 105→228。调类别时同样要用真实文件夹抽查。
9. **进度计数**:识别的 total = 推理数 + RAW 预览提取数(两个阶段共用一个计数器),不要用"照片数"当分母。
10. **静态资源缓存**:服务端对 /static 不发强缓存,前端用 `?v=N` 递增破坏缓存;改了 JS/CSS 记得同时改 index.html 的版本号。
11. **pip 权限**:本机 `C:\Python312\Scripts` 不可写,pip 必须 `--user`(f2py.exe 写入失败的 WinError 2 即此因)。
12. **同文件重复定义**:classifier.py 曾因编辑残留出现两个 `classify_all`,Python 后定义覆盖前定义导致"改了没生效"。排查此类问题先 `grep -n "^def"` 看是否有重名。
13. **缩进回归**:并行化改造时曾把分类代码块错误地留在批处理循环外(缩进少一层),运行"成功"但只保存了最后一批结果。长任务一定要核对产物数量与输入数量一致(如嵌入 8,297 条)。
14. **量化模型的多语言退化**:clip-ViT-B-32-multilingual-v1 的 quint8_avx2 ONNX 中文查询严重失准(英文正常),换 fp32 后正常。多语言文本编码器必须用 fp32。
15. **ECharts 日历坐标**:option 键名是 `calendar`(可传数组做多年份),写成 `calendars` 会静默渲染失败报 "reading 'type'"。
16. **单图盲测模糊阈值**:lap_max 在 60-180 区间被天然低纹理场景(云/雾/星空/日落)主导,精度 <80%;星空等暗帧拉普拉斯值天然极低(需暗帧豁免,改判"欠曝")。阈值必须配合真实抽拼图人工校准,极保守区间(<60)才有高精度。

---

## 8. 路线图(按建议优先级)

1. **重复文件清理**:重复检测已就绪(`dupGroups`),缺"对比预览 + 勾选删除"UI;已发现"2025-02-01-文成"与"视频"文件夹之间有整批双卡重复。
2. **GPS 地图足迹**:lat/lon 已在元数据里(lat/lon 字段),前端接一个离线地图库(如 Leaflet + 本地瓦片)即可;注意大部分照片无 GPS。
3. **打包 exe**:PyInstaller 打包 photostats.py;需把 tools/、static/ 打进包,模型 670MB 建议仍放外部目录。
4. **年度报告导出**:复用前端聚合逻辑生成静态 HTML。
5. **视频题材识别**:需抽帧工具(ffmpeg,本机未装);或先只对视频首帧做分类。
6. **置信度过滤 UI**:数据里已有置信度,前端加个滑块过滤 `subjConf < x` 的照片。
7. **预处理再提速**:当前 13 张/s 瓶颈在单盘顺序读 + 解码;若要更快可加读缓存或把 cls_previews 铺到全量照片。

---

## 9. 常用操作速查

```bash
cd <安装目录>

python photostats.py                # 启动(缓存存在则秒开;自动开浏览器)
python photostats.py --rescan       # 强制重扫全库
python photostats.py --scan-only    # 只扫描不启动服务
python photostats.py --root D:\xx   # 统计别的照片目录
python photostats.py --port 8888    # 换端口

# 题材识别:界面「题材统计」页签 → ▶ 运行识别 / 增量更新
# 或 API:curl http://127.0.0.1:8765/api/classify/start

# 改题材类别:编辑 classifier.py 的 SUBJECTS → 重启服务 → 点「运行识别」(旧结果自动作废)

# 前端改动后:递增 static/index.html 里的 ?v=N,刷新页面即可
```
