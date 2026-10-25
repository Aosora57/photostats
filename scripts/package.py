# -*- coding: utf-8 -*-
"""组装跨平台绿色分发包(CI 与本地通用)。

用法:
  python scripts/package.py --triple x86_64-pc-windows-msvc
  python scripts/package.py --triple aarch64-apple-darwin
  python scripts/package.py --triple x86_64-unknown-linux-gnu

产出 dist/PhotoStats-<triple>/:
  python/            便携 Python(python-build-standalone)+ 依赖
  app/               应用代码与静态资源
  tools/models/      模型与 exiftool(从已构建环境复制或下载)
  start.bat / start.command

模型文件约 1.6GB:优先从本地 tools/models 复制,否则现场下载。
"""
import argparse
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = ["photostats.py", "scanner.py", "server.py", "classifier.py", "searcher.py",
             "encoders.py", "quality.py", "faces.py", "phash.py", "xmp.py",
             "report.py", "exporter.py", "colors.py", "requirements.txt",
             "README.md", "LICENSE"]
APP_DIRS = ["static"]

PBS_BASE = "https://github.com/astral-sh/python-build-standalone/releases/download"
PBS_TAG = "20241016"


def sh(cmd: list, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def fetch(url: str, dest: Path):
    print(f"  下载 {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--triple", required=True,
                    help="目标平台,如 x86_64-pc-windows-msvc / aarch64-apple-darwin")
    ap.add_argument("--pbs-tag", default=PBS_TAG)
    ap.add_argument("--no-models", action="store_true", help="跳过模型复制(减小产物)")
    args = ap.parse_args()

    out = ROOT / "dist" / f"PhotoStats-{args.triple}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    print(f"== 组装 {out} ==")

    # 1) 便携 Python(python-build-standalone install_only)
    is_win = "windows" in args.triple
    py_arch = "x86_64" if args.triple.startswith("x86_64") else "aarch64"
    kind = "windows" if is_win else "macos" if "apple" in args.triple else "linux"
    pbs_name = (f"cpython-3.12.7+{args.pbs_tag}-{py_arch}-{kind}-"
                + ("msvc-shared" if is_win else "default") + "-install_only.tar.gz")
    archive = ROOT / "dist" / pbs_name
    if not archive.exists():
        fetch(f"{PBS_BASE}/{args.pbs_tag}/{pbs_name}", archive)
    print("  解压便携 Python ...")
    import tarfile
    with tarfile.open(archive) as tf:
        tf.extractall(out)

    py_dir = next((out / p for p in ("python", "cpython-3.12.7") if (out / p).exists()), None)
    if py_dir is None:  # 解压根目录即 python
        candidates = [d for d in out.iterdir() if d.is_dir()]
        py_dir = candidates[0]
    py = py_dir / ("python.exe" if is_win else py_dir / "bin" / "python3")

    # 2) 依赖
    print("  安装依赖 ...")
    sh([py, "-m", "pip", "install", "-r", ROOT / "requirements.txt",
        "opencv-python-headless", "--no-cache-dir"])

    # 3) 应用代码
    print("  复制应用代码 ...")
    app = out / "app"
    app.mkdir(exist_ok=True)
    for f in APP_FILES:
        shutil.copy2(ROOT / f, app / f)
    for d in APP_DIRS:
        shutil.copytree(ROOT / d, app / d, dirs_exist_ok=True)
    (app / ".models_link").write_text("models 位于 ../../tools/models", encoding="utf-8")

    # 4) 模型与 exiftool
    if not args.no_models:
        print("  复制模型与工具 ...")
        tools_dst = out / "tools"
        tools_dst.mkdir(exist_ok=True)
        src_models = ROOT / "tools" / "models"
        if src_models.exists():
            shutil.copytree(src_models, tools_dst / "models", dirs_exist_ok=True)
        else:
            sh([sys.executable, ROOT / "scripts" / "download_models.py"])
        exif = ROOT / "tools" / "exiftool.exe"
        if exif.exists():
            shutil.copy2(exif, tools_dst / "exiftool.exe")
            ef = ROOT / "tools" / "exiftool_files"
            if ef.exists():
                shutil.copytree(ef, tools_dst / "exiftool_files", dirs_exist_ok=True)

    # 5) 启动脚本
    if is_win:
        launcher = out / "start.bat"
        launcher.write_text(
            "@echo off\r\n"
            "title PhotoStats\r\n"
            "cd /d %~dp0\r\n"
            "start \"\" python\\python.exe app\\photostats.py --root %~dp0..\r\n", encoding="utf-8")
    else:
        launcher = out / "start.command"
        launcher.write_text(
            "#!/bin/bash\n"
            "cd \"$(dirname \"$0\")\"\n"
            "./python/bin/python3 app/photostats.py --root \"$(dirname \"$PWD\")\"\n", encoding="utf-8")
        launcher.chmod(0o755)

    print(f"== 完成: {out} ==")


if __name__ == "__main__":
    main()
