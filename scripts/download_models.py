# -*- coding: utf-8 -*-
"""下载 PhotoStats 所需的模型与工具(GitHub 克隆后运行一次)。

用法:
  python scripts/download_models.py            # 全部下载
  python scripts/download_models.py --skip-exiftool   # 跳过 exiftool(已装的可跳)

模型总大小约 1.6GB,存放在 tools/models/(被 .gitignore 排除,不进仓库)。
可选走代理:设置环境变量 HTTPS_PROXY / HTTP_PROXY。
"""
import argparse
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "tools" / "models"
FACE = MODELS / "face"
TOOLS = ROOT / "tools"

# (相对路径, 下载地址, 说明)
ITEMS = [
    ("vision.onnx",
     "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/model.onnx",
     "CLIP 视觉编码器(约 605MB)"),
    ("text.onnx",
     "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model_quantized.onnx",
     "CLIP 文本编码器·量化(约 64MB)"),
    ("tokenizer.json",
     "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json",
     "CLIP 分词器"),
    ("multilingual.onnx",
     "https://huggingface.co/sentence-transformers/clip-ViT-B-32-multilingual-v1/resolve/main/onnx/model.onnx",
     "多语言查询编码器·fp32(约 539MB,中文搜索必需)"),
    ("multilingual_tokenizer.json",
     "https://huggingface.co/sentence-transformers/clip-ViT-B-32-multilingual-v1/resolve/main/tokenizer.json",
     "多语言分词器"),
    ("dense.safetensors",
     "https://huggingface.co/sentence-transformers/clip-ViT-B-32-multilingual-v1/resolve/main/2_Dense/model.safetensors",
     "多语言投影矩阵"),
    ("aesthetic_head.safetensors",
     "https://huggingface.co/shunk031/aesthetics-predictor-v1-vit-base-patch32/resolve/main/model.safetensors",
     "美学评分头(约 351MB)"),
    ("face/yunet.onnx",
     "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
     "YuNet 人脸检测(约 0.2MB)"),
    ("face/sface.onnx",
     "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
     "SFace 人脸嵌入(约 37MB)"),
]

EXIFTOOL_URL = "https://exiftool.org/exiftool-13.59_64.zip"


def fetch(url: str, dest: Path, desc: str) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  已存在,跳过: {dest.name}")
        return True
    print(f"  下载 {desc} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlretrieve(url, dest)
        print(f"  ✓ {dest.name} ({dest.stat().st_size // 1024 // 1024}MB)")
        return True
    except Exception as e:
        print(f"  ✗ 失败: {e}")
        return False


def download_exiftool() -> bool:
    import tempfile
    dest = TOOLS / "exiftool.exe"
    if dest.exists():
        print("  已存在,跳过: exiftool.exe")
        return True
    print("  下载 exiftool ...")
    try:
        with tempfile.TemporaryDirectory() as td:
            zp = Path(td) / "exiftool.zip"
            urllib.request.urlretrieve(EXIFTOOL_URL, zp)
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(td)
            src = next(Path(td).rglob("exiftool(-k).exe"))
            TOOLS.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
            # exiftool_files 目录
            files_src = next(Path(td).rglob("exiftool_files"))
            if not (TOOLS / "exiftool_files").exists():
                files_src.rename(TOOLS / "exiftool_files")
        print("  ✓ exiftool.exe")
        return True
    except Exception as e:
        print(f"  ✗ 失败: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-exiftool", action="store_true")
    args = ap.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    ok = True
    print("== 下载模型与工具 ==")
    for rel, url, desc in ITEMS:
        if not fetch(url, MODELS / rel, desc):
            ok = False
    if not args.skip_exiftool:
        if not download_exiftool():
            ok = False
    print("== 完成 ==" if ok else "== 有失败项,请重跑本脚本重试 ==")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
