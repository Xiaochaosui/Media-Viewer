#!/usr/bin/env bash
# 给媒体浏览器装上「RAW 全尺寸解码」能力（富士 .RAF 等）
#
# 为什么需要它：.RAF 里除了 40MP 的感光数据，还塞了一张缩略预览图
#   （X-T50 的预览只有 4416×2944，比同目录的 JPG 还小），
#   所以想真正「看原图」必须把 RAW 数据解码出来 —— 靠 libraw。
#
# 本机没有 pip（Ubuntu 桌面默认不带），也尽量不动系统，所以这里直接把
# PyPI 上的 manylinux 轮子（纯 zip）解压到指定目录，程序启动时把它加进 sys.path。
# 不写系统目录、不需要 sudo、想撤掉就删掉那个目录。
#
# 用法：
#   ./安装RAW支持.sh              # 装到 1T 固态（默认）
#   ./安装RAW支持.sh --local      # 装到家目录（~/.local/lib/media-browser/pylibs）
#   ./安装RAW支持.sh --uninstall  # 删掉已装的库
#   ./安装RAW支持.sh --check      # 只检查现在能不能解 RAW

set -u
PYTHON="${PYTHON:-python3}"
SSD_DIR="/mnt/XCS_DATA/xcs-cache/pylibs"
LOCAL_DIR="$HOME/.local/lib/media-browser/pylibs"
MODE="ssd"

# ---- 平台：macOS / Windows 直接走 pip（那边本来就有 pip，装到用户目录最省事）----
PLATFORM="$(uname -s 2>/dev/null || echo unknown)"
case "$PLATFORM" in
  Darwin) IS_MAC=1 ;;
  MINGW*|MSYS*|CYGWIN*) IS_WIN=1 ;;
esac
: "${IS_MAC:=0}"; : "${IS_WIN:=0}"

for a in "$@"; do
  case "$a" in
    --local) MODE="local" ;;
    --ssd) MODE="ssd" ;;
    --uninstall) MODE="uninstall" ;;
    --check) MODE="check" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "不认识的参数：$a（用 --help 看用法）"; exit 2 ;;
  esac
done

if [ "$IS_MAC" = "1" ]; then
  echo "检测到 macOS：用 pip 装到用户目录（不需要 sudo、不动系统）"
  case "$MODE" in
    check)
      "$PYTHON" -c "import rawpy; print('  已装 rawpy', rawpy.__version__)" 2>/dev/null \
        || echo "  还没装（界面里不会出现「RAW 原图」）"
      exit 0 ;;
    uninstall)
      "$PYTHON" -m pip uninstall -y rawpy numpy && echo "已卸载" ; exit 0 ;;
    *)
      "$PYTHON" -m pip install --user --upgrade rawpy \
        && "$PYTHON" -c "import rawpy; print('装好了：rawpy', rawpy.__version__)" \
        && echo "现在重启媒体浏览器就能解 RAW 了"
      exit 0 ;;
  esac
fi
if [ "$IS_WIN" = "1" ]; then
  echo "检测到 Windows：请用 py -3 -m pip install --user rawpy 安装（或双击运行本脚本的 .bat 版）"
  exit 0
fi

TARGET="$SSD_DIR"
[ "$MODE" = "local" ] && TARGET="$LOCAL_DIR"

check() {
  echo "=== 现在能不能解 RAW ==="
  local found=""
  for d in "$SSD_DIR" "$LOCAL_DIR"; do
    if [ -d "$d/rawpy" ]; then echo "  已安装：$d"; found=1; fi
  done
  [ -z "$found" ] && echo "  还没装（界面里不会出现「RAW 原图」）"
  "$PYTHON" - <<'PY'
import os, sys
from pathlib import Path
for d in ("/mnt/XCS_DATA/xcs-cache/pylibs",
          str(Path.home() / ".local/lib/media-browser/pylibs")):
    if os.path.isdir(d):
        sys.path.insert(0, d)
try:
    import rawpy
    print("  ✓ rawpy 可用：%s（libraw %s）"
          % (rawpy.__version__, ".".join(str(x) for x in rawpy.libraw_version)))
except Exception as e:
    print("  ✗ rawpy 不可用：%s" % e)
PY
}

case "$MODE" in
  check) check; exit 0 ;;
  uninstall)
    for d in "$SSD_DIR" "$LOCAL_DIR"; do
      if [ -d "$d" ]; then
        echo "删除 $d …"
        rm -rf "$d"
      fi
    done
    echo "已卸载（界面里的「RAW 原图」会自动消失，改回用缩小版预览）"
    exit 0 ;;
esac

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "没有找到 $PYTHON"; exit 1
fi

mkdir -p "$TARGET" || { echo "建不了目录：$TARGET"; exit 1; }
# 固态没挂载时不要往上写
if [ "$MODE" = "ssd" ] && ! mountpoint -q /mnt/XCS_DATA 2>/dev/null; then
  echo "/mnt/XCS_DATA 没挂载，改用家目录安装"
  TARGET="$LOCAL_DIR"; mkdir -p "$TARGET"
fi

echo "安装到：$TARGET"
"$PYTHON" - "$TARGET" <<'PY'
import io, json, os, sys, time, urllib.request, zipfile

target = sys.argv[1]
# python 3.10（Ubuntu 22.04）用得到的版本
WANT = {"numpy": "2.2.6", "rawpy": "0.27.1"}

def pick_url(pkg, ver):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/{ver}/json", timeout=30) as r:
        data = json.load(r)
    for u in data["urls"]:
        f = u["filename"]
        if "cp310" in f and "x86_64" in f and "manylinux" in f:
            return u["url"], f, u["size"]
    raise SystemExit(f"{pkg} {ver} 没有 cp310/x86_64 的轮子")

for pkg, ver in WANT.items():
    url, name, size = pick_url(pkg, ver)
    print(f"  下载 {name}（{size/1048576:.1f} MB）…", flush=True)
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=600) as r:
        blob = r.read()
    print(f"    完成，用时 {time.time()-t0:.1f}s", flush=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(target)
print("解压完成")
PY

[ $? -ne 0 ] && { echo "下载/解压失败（检查网络）"; exit 1; }
echo
check
echo
echo "搞定。重启媒体浏览器（或在网页里点「⟳ 重新扫描」旁边的刷新）后，"
echo "图片条目上会出现「🖼 看 RAW 原图」，详情面板里也能直接开 40MP 全尺寸。"
