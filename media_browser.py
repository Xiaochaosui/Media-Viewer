#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
媒体浏览器 (Media Browser)
=========================

一个本地小工具：用缩略图墙快速浏览某个文件夹里的**所有图片和视频**，
点开看详情（尺寸/时长/编码/EXIF/路径等），需要细看时一键调用**本机 VLC** 播放。

特点
----
* 零第三方依赖（只用 Python 标准库；PIL / ffmpeg 存在时自动增强）
* 图片缩略图用 Pillow，视频缩略图用 ffmpeg（自动避开黑帧），本地缓存
* 支持 RAW（.raf/.cr2/.nef/.arw...）：自动取同名的 JPEG 或内嵌预览，并可与其配对合并显示
* 点击卡片 = 看详情；双击 / 回车 = 用 VLC 播放；还可把整个筛选结果作为 VLC 播放列表
* 服务只监听 127.0.0.1，媒体文件按需生成缩略图，浏览大目录也不卡

用法
----
    python3 media_browser.py                      # 浏览当前目录
    python3 media_browser.py /mnt/MEDIA/个人生活   # 浏览指定目录
    python3 media_browser.py ~/Pictures ~/Movies  # 多个目录
    python3 media_browser.py --help

作者：为 xcs 定制 · 仅供本机使用
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import mimetypes
import os
import plistlib
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.error
import urllib.request
import zipfile
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0"
# 缩略图算法版本：改动取图/缩放逻辑时 +1，可让旧缓存自动失效
THUMB_REV = 2

# --------------------------------------------------------------------------- #
# 文件类型
# --------------------------------------------------------------------------- #

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".jfif", ".ico", ".svg", ".jxl",
}
RAW_EXTS = {
    ".raf", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
    ".orf", ".rw2", ".pef", ".srw", ".raw", ".3fr", ".erf", ".mrw", ".x3f",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".f4v", ".webm",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".3gp", ".3g2", ".vob", ".rmvb",
    ".rm", ".asf", ".divx", ".ogv", ".mxf", ".insv", ".lrv",
}
# 跟照片同名的"伴随文件"：改名时一起改，免得 .xmp/.dop 跟照片分了家
SIDECAR_EXTS = {".xmp", ".dop", ".pp3", ".aae", ".thm", ".cos", ".wav"}

# 明显不是媒体、但可能出现在素材目录里的东西
SKIP_DIR_NAMES = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules", "$RECYCLE.BIN",
    "System Volume Information", "@eaDir", ".Trash", ".Trashes", ".Spotlight-V100",
    ".fseventsd", ".media_cache", ".cache", ".thumbnails",
    "RAW解码",          # 默认的 RAW 解码输出目录（可用 --raw-out 改名，见 RAW_OUT_NAME）
}
RAW_OUT_NAME = "RAW解码"        # 解出来的全尺寸图放哪：RAW 文件同级目录下的这个文件夹

VIDEO_MIME_FALLBACK = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv", ".ts": "video/mp2t",
    ".m2ts": "video/mp2t", ".mts": "video/mp2t", ".3gp": "video/3gpp",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".ogv": "video/ogg",
}


def now() -> float:
    return time.time()


def log(msg: str) -> None:
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# 平台
# --------------------------------------------------------------------------- #
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PLATFORM_NAME = "Windows" if IS_WIN else ("macOS" if IS_MAC else
                                          ("Linux" if IS_LINUX else sys.platform))


def _augment_path() -> None:
    """从 Finder / 快捷方式启动时 PATH 往往很短，补上常见的工具目录。"""
    extra = []
    if IS_MAC:
        extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/sbin",
                 "/Applications/VLC.app/Contents/MacOS", str(Path.home() / ".local/bin")]
    elif IS_WIN:
        extra = [r"C:\ffmpeg\bin", r"C:\Program Files\VideoLAN\VLC",
                 r"C:\Program Files (x86)\VideoLAN\VLC",
                 str(Path.home() / "scoop" / "shims"),
                 str(Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links")]
    else:
        extra = ["/usr/local/bin", "/snap/bin", str(Path.home() / ".local/bin")]
    cur = os.environ.get("PATH", "")
    parts = [p for p in cur.split(os.pathsep) if p]
    add = [p for p in extra if p not in parts and Path(p).is_dir()]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + parts)


_augment_path()


def cache_home() -> Path:
    """各平台的缓存根目录。"""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base)
    if IS_MAC:
        return Path.home() / "Library" / "Caches"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))


def find_exe(*names: str) -> str | None:
    """按名字找可执行文件；顺带支持几个平台的固定安装位置。"""
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    app_dirs = {
        "vlc": ["/Applications/VLC.app/Contents/MacOS/VLC",
                r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"],
        "ffmpeg": ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                   r"C:\ffmpeg\bin\ffmpeg.exe"],
        "ffprobe": ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe",
                    r"C:\ffmpeg\bin\ffprobe.exe"],
        "magick": ["/opt/homebrew/bin/magick", "/usr/local/bin/magick"],
        "convert": ["/opt/homebrew/bin/convert", "/usr/local/bin/convert"],
    }
    for n in names:
        for cand in app_dirs.get(Path(n).stem.lower(), []):
            if Path(cand).exists():
                return cand
    return None


FFMPEG = find_exe("ffmpeg")
FFPROBE = find_exe("ffprobe")
VLC = find_exe("vlc", "vlc.exe")
GDBUS = None if IS_WIN else find_exe("gdbus")
XDG_OPEN = None if IS_WIN else find_exe("xdg-open")
SYSTEMD_RUN = None if IS_WIN else find_exe("systemd-run")
MAC_OPEN = find_exe("open") if IS_MAC else None
SIPS = find_exe("sips") if IS_MAC else None            # macOS 自带的图片缩放工具
QLMANAGE = find_exe("qlmanage") if IS_MAC else None    # macOS 自带的缩略图工具
OSASCRIPT = find_exe("osascript") if IS_MAC else None
MAGICK = find_exe("magick", "convert")                 # ImageMagick（没 Pillow 时的备胎）
POWERSHELL = find_exe("powershell", "pwsh") if IS_WIN else None
EXPLORER = find_exe("explorer") if IS_WIN else None

# 本机图片查看器候选（按优先级）；看原图用它，视频才用 VLC
# macOS / Windows 没有这些命令行看图器，直接用系统默认程序（Preview / 照片）
if IS_MAC:
    IMAGE_VIEWER_CANDIDATES = ("open",)
elif IS_WIN:
    IMAGE_VIEWER_CANDIDATES = ("explorer",)
else:
    IMAGE_VIEWER_CANDIDATES = (
        "eog", "loupe", "gwenview", "eom", "nomacs", "gpicview", "ristretto",
        "shotwell", "feh", "xdg-open",
    )
IMAGE_VIEWER = find_exe(*IMAGE_VIEWER_CANDIDATES)
IMAGE_VIEWER_NAME = Path(IMAGE_VIEWER).name if IMAGE_VIEWER else ""

# macOS 的「视频播放器」：装了 VLC / IINA 就优先用（什么格式都认），
# 都没有就交给系统自带的 QuickTime Player，再兜底是系统默认程序（访达「显示简介」里设的打开方式）。
# 目的：在没装 VLC 的 Mac 上双击视频也有东西弹出来，而不是甩一句「没装 VLC」。
MAC_VIDEO_APPS = ("VLC", "IINA")                                 # 装了就用这两个
MAC_QT_EXT = {".mov", ".mp4", ".m4v", ".qt", ".3gp", ".3g2"}     # QuickTime 认的格式
MAC_APP_DIRS = (Path("/Applications"), Path("/System/Applications"),
                Path("/System/Applications/Utilities"), Path.home() / "Applications")


def mac_app_path(name: str) -> str:
    """macOS 上找一个 .app：装了返回路径，没装返回空串。"""
    if not IS_MAC:
        return ""
    for d in MAC_APP_DIRS:
        p = d / (name + ".app")
        try:
            if p.exists():
                return str(p)
        except OSError:
            pass
    return ""


def mac_video_player() -> str:
    """macOS 上准备用哪个播放器（给人看的名字）；都没有就返回空串＝系统默认程序。"""
    for name in MAC_VIDEO_APPS:
        if mac_app_path(name):
            return name
    return "QuickTime Player" if mac_app_path("QuickTime Player") else ""


def video_player_label() -> str:
    """这台机器现在「双击视频」会用谁（网页顶栏 / --check 里显示用）。"""
    if VLC:
        return "VLC"
    if IS_MAC:
        return mac_video_player() or "系统默认播放器"
    return ""


def friendly_viewer_name(path: str | None) -> str:
    """图片查看器给人看的名（macOS 的 open 其实就是「预览」，Windows 的 explorer 是「照片」）。"""
    name = Path(path).name if path else ""
    if IS_MAC and name == "open":
        return "预览"
    if IS_WIN and name.startswith("explorer"):
        return "照片（Windows 图片查看器）"
    return name


def write_problem(target) -> str:
    """这个位置能不能写？能写返回空串，不能写返回一句**给人看**的原因。

    整理文件（改名 / 移动 / 新建文件夹）之前先问一句这个，比让用户对着
    「[Errno 30] Read-only file system」发呆强得多 —— macOS 上外接 NTFS 盘
    就是只读挂载的，非常常见。
    """
    try:
        p = Path(target).expanduser()
        d = p if p.is_dir() else p.parent
    except OSError:
        d = Path(target).parent
    try:
        flag = getattr(os, "ST_RDONLY", 1)
        if os.statvfs(str(d)).f_flag & flag:
            extra = ""
            if IS_MAC:
                extra = ("（macOS 读 NTFS 盘默认只能读不能写）"
                         "。要么先把文件拷到本机磁盘（比如 ~/Pictures）再整理，"
                         "要么装个 NTFS 写入支持：Mounty / macFUSE + ntfs-3g / Paragon NTFS")
            return "%s 是只读挂载的盘%s" % (d, extra)
        if not os.access(str(d), os.W_OK):
            return ("%s 没有写权限（当前用户改不了这里）：换个目录，"
                    "或者改一下文件夹权限再来" % d)
    except OSError as e:
        return "检查写权限失败：%s" % e
    return ""


def os_error_text(e: Exception, target=None) -> str:
    """把 OSError 翻译成人话（只读盘 / 没权限是最常见的两种）。"""
    err = getattr(e, "errno", None)
    if err in (errno.EROFS, errno.EACCES, errno.EPERM):
        note = write_problem(target) if target else ""
        if note:
            return note
        return "没有写权限：%s" % e
    return str(e)

try:  # Pillow 可选
    from PIL import Image, ImageOps  # type: ignore
    from PIL import ExifTags  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    ExifTags = None  # type: ignore


# --------------------------------------------------------------------------- #
# 缩略图工具
# --------------------------------------------------------------------------- #

def _pil_thumb(src: Path, dst: Path, px: int) -> bool:
    """用 Pillow 生成缩略图。"""
    if Image is None:
        return False
    try:
        with Image.open(src) as im:
            try:
                im = ImageOps.exif_transpose(im)  # type: ignore[union-attr]
            except Exception:
                pass
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((px, px * 2), Image.LANCZOS)  # type: ignore[attr-defined]
            tmp = dst.with_suffix(".tmp.jpg")
            im.save(tmp, "JPEG", quality=84, optimize=True, progressive=True)
            os.replace(tmp, dst)
        return True
    except Exception:
        return False


def _ffmpeg_thumb(src: Path, dst: Path, px: int, seek: float) -> bool:
    """用 ffmpeg 在 seek 秒处截一帧；px 为最长边。先写临时文件再原子替换。"""
    if not FFMPEG:
        return False
    vf = "scale=w=min(%d\\,iw):h=-2" % px
    tmp = dst.with_name(dst.stem + ".part.jpg")
    cmd = [
        FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
        "-ss", "%.3f" % max(0.0, seek),
        "-i", str(src),
        "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
        "-vf", vf, "-q:v", "4", "-f", "image2",
        "-y", str(tmp),
    ]
    try:
        # 40 秒上限：正常一帧几秒就出来了；坏文件/转不动的也绝不能拖住整个界面
        # （超时就当失败，记进 _failed，之后同一条不再重试）
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=40)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, dst)
            return True
        return False
    except Exception:
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _sys_thumb(src: Path, dst: Path, px: int) -> bool:
    """没有 Pillow 时的备胎：借系统自带的图片工具缩图。

    macOS 用 sips、有 ImageMagick 就用 magick/convert、
    Windows 用 PowerShell 的 System.Drawing。都是"能用就行"，不追求画质。
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp.jpg")
        if tmp.exists():
            tmp.unlink()
        if SIPS:
            r = subprocess.run([SIPS, "-s", "format", "jpeg", "-Z", str(px),
                                str(src), "--out", str(tmp)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
            ok = r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0
        elif MAGICK:
            r = subprocess.run([MAGICK, str(src), "-auto-orient",
                                "-resize", "%dx%d>" % (px, px * 2),
                                "-quality", "84", str(tmp)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
            ok = r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0
        elif IS_WIN and POWERSHELL:
            ps = (
                "Add-Type -AssemblyName System.Drawing;"
                "$i=[System.Drawing.Image]::FromFile('%s');"
                "$m=[Math]::Max($i.Width,$i.Height);$k=[Math]::Min(1.0,%d/$m);"
                "$w=[int]($i.Width*$k);$h=[int]($i.Height*$k);"
                "$b=New-Object System.Drawing.Bitmap $w,$h;"
                "$g=[System.Drawing.Graphics]::FromImage($b);"
                "$g.InterpolationMode='HighQualityBicubic';"
                "$g.DrawImage($i,0,0,$w,$h);"
                "$b.Save('%s',[System.Drawing.Imaging.ImageFormat]::Jpeg);"
                "$i.Dispose();$b.Dispose()"
                % (str(src).replace("'", "''"), px, str(tmp).replace("'", "''"))
            )
            r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120)
            ok = r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0
        else:
            return False
        if ok:
            os.replace(tmp, dst)
            return True
        return False
    except Exception:
        return False


def _ql_thumb(src: Path, dst: Path, px: int) -> bool:
    """macOS 自带 QuickLook 抽帧（没有 ffmpeg 时给视频/图片出封面）。"""
    if not QLMANAGE:
        return False
    outdir = dst.parent / ("ql-" + hashlib.sha1(str(src).encode()).hexdigest()[:8])
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        # 15 秒上限：QuickLook 只是最后备胎，坏文件/怪格式不该让请求挂在那儿
        # （实测某些损坏的 .heic 能让 qlmanage 干等一分半，把浏览器的连接占满）
        r = subprocess.run([QLMANAGE, "-t", "-s", str(px), "-o", str(outdir), str(src)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        if r.returncode != 0:
            return False
        made = sorted(outdir.glob(src.name + "*"))
        if not made:
            made = sorted(outdir.iterdir())
        if not made:
            return False
        png = made[0]
        if SIPS:                                    # QuickLook 出的是 png，转成 jpg
            tmp = dst.with_suffix(".tmp.jpg")
            r2 = subprocess.run([SIPS, "-s", "format", "jpeg", str(png), "--out", str(tmp)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=60)
            if r2.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                os.replace(tmp, dst)
                return True
        if Image is not None:
            with Image.open(png) as im:
                im.convert("RGB").save(dst, "JPEG", quality=84)
            return True
        return False
    except Exception:
        return False
    finally:
        try:
            for f in outdir.iterdir():
                f.unlink()
            outdir.rmdir()
        except Exception:
            pass


def _frame_stats(path: Path) -> tuple[float, float]:
    """返回 (平均亮度, 灰度标准差)；标准差小 = 纯黑/纯色画面。"""
    if Image is None:
        return (99.0, 99.0)
    try:
        with Image.open(path) as im:
            g = im.convert("L").resize((48, 48))
            px = list(g.getdata())
        mean = sum(px) / len(px)
        var = sum((p - mean) ** 2 for p in px) / len(px)
        return (mean, var ** 0.5)
    except Exception:
        return (99.0, 99.0)


def _find_embedded_jpeg(src: Path) -> bytes | None:
    """把 RAW 里内嵌的那张 JPEG 原样抠出来（富士 RAF 走文件头偏移，其它走扫描）。"""
    try:
        size = src.stat().st_size
        with open(src, "rb") as f:
            head = f.read(96)
            if head[:16] == b"FUJIFILMCCD-RAW " and len(head) >= 92:
                off, length = struct.unpack(">II", head[84:92])
                if 0 < off < size and 0 < length <= size - off:
                    f.seek(off)
                    blob = f.read(length)
                    if blob[:2] == b"\xff\xd8":
                        return blob
            f.seek(0)
            data = f.read(min(size, 64 * 1024 * 1024))
        best = b""
        i = 0
        while True:
            st = data.find(b"\xff\xd8\xff", i)
            if st < 0:
                break
            en = data.find(b"\xff\xd9", st)
            if en < 0:
                break
            if en - st > len(best):
                best = data[st:en + 2]
            i = en + 2
        return best or None
    except Exception:
        return None


def _extract_embedded_jpeg(src: Path, dst: Path, px: int) -> bool:
    """从 RAW 文件里抠出内嵌的 JPEG 预览（富士 RAF 走文件头偏移，其它走扫描）。"""
    if Image is None:
        # 没有 Pillow：把内嵌 JPEG 原样写出来，能缩就借系统工具缩一下
        blob = _find_embedded_jpeg(src)
        if not blob:
            return False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            raw_file = dst.with_suffix(".embedded.jpg")
            raw_file.write_bytes(blob)
            if px > 0 and _sys_thumb(raw_file, dst, px):
                raw_file.unlink(missing_ok=True)
                return True
            os.replace(raw_file, dst)
            return True
        except Exception:
            return False
    try:
        size = src.stat().st_size
        with open(src, "rb") as f:
            head = f.read(96)
            blob = None
            if head[:16] == b"FUJIFILMCCD-RAW " and len(head) >= 92:
                off, length = struct.unpack(">II", head[84:92])
                if 0 < off < size and 0 < length <= size - off:
                    f.seek(off)
                    blob = f.read(length)
            if blob is None:
                # 通用扫描：找最大的 JPEG 数据块（前 64MB 足够）
                f.seek(0)
                data = f.read(min(size, 64 * 1024 * 1024))
                best = b""
                i = 0
                while True:
                    s = data.find(b"\xff\xd8\xff", i)
                    if s < 0:
                        break
                    e = data.find(b"\xff\xd9", s)
                    if e < 0:
                        break
                    if e - s > len(best):
                        best = data[s:e + 2]
                    i = e + 2
                blob = best or None
        if not blob:
            return False
        import io
        with Image.open(io.BytesIO(blob)) as im:  # type: ignore[union-attr]
            im = im.convert("RGB")
            if px > 0:
                im.thumbnail((px, px * 2), Image.LANCZOS)  # type: ignore[attr-defined]
            tmp = dst.with_suffix(".tmp.jpg")
            im.save(tmp, "JPEG", quality=84 if px > 0 else 92, optimize=True)
            os.replace(tmp, dst)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# RAW 全尺寸解码（可选能力）
# --------------------------------------------------------------------------- #
# 富士 .RAF 里除了 40MP 的感光数据，还塞了一张缩略预览图（X-T50 只有 4416×2944，
# 比同目录的 .JPG 还小），所以"看原图"必须把 RAW 数据真正解码出来 —— 靠 libraw。
# 本机 Ubuntu 桌面默认没有 pip，源里的 darktable/libraw 又太老（不认识 X-T50），
# 所以支持从自带目录里加载 PyPI 上的 manylinux 轮子（见 安装RAW支持.sh）。
RAW_REV = 2                 # 解码逻辑一改就 +1，旧缓存自动失效
RAW_QUALITY = 92
RAW_DEMOSAIC = "auto"       # auto|linear|vng|ppg|ahd|dcb|amaze（不同算法细节/伪色不同）
RAW_WB = "camera"           # camera|auto|none|daylight|shade|cloudy|tungsten|fluorescent|flash

RAW_PYLIBS = [
    os.environ.get("MEDIA_BROWSER_PYLIBS", ""),
    "/mnt/XCS_DATA/xcs-cache/pylibs",
    str(Path.home() / ".local/lib/media-browser/pylibs"),
    str(Path(__file__).resolve().parent / "pylibs"),
]

# 万一装了别的 RAW 软件，也能直接交给它打开 .RAF
RAW_APP_CANDIDATES = ("darktable", "rawtherapee", "digikam", "gimp", "ufraw", "xnview")

RAWPY = None
_RAWPY_TRIED = False


def rawpy_module():
    """按需加载 rawpy（先从自带库目录里找）。拿不到就返回 None。"""
    global RAWPY, _RAWPY_TRIED
    if _RAWPY_TRIED:
        return RAWPY
    _RAWPY_TRIED = True
    for d in RAW_PYLIBS:
        if d and os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
    try:
        import rawpy as _rp          # type: ignore
        RAWPY = _rp
        log("RAW 解码：已启用 rawpy %s（libraw %s）"
            % (_rp.__version__, ".".join(str(x) for x in _rp.libraw_version)))
    except Exception as e:
        RAWPY = None
        log("RAW 全尺寸解码未启用（%s）—— 需要时跑 ./安装RAW支持.sh" % e)
    return RAWPY


def find_raw_app() -> str | None:
    for name in RAW_APP_CANDIDATES:
        p = find_exe(name)
        if p:
            return p
    return None


def decode_raw_full(src: Path, dst: Path, quality: int = RAW_QUALITY) -> tuple[bool, str]:
    """把 RAW 解成全尺寸 JPEG。返回 (是否成功, 尺寸说明)。

    解码参数（去马赛克算法 / 白平衡）会影响成片效果，见 raw_settings()。
    注意：这里只做"中性"解码 —— 富士机内的胶片模拟是相机独有的一套处理，
    libraw 不实现，所以解出来的颜色不会跟你那张直出 JPG 完全一样。
    """
    rp = rawpy_module()
    if rp is None:
        return False, "没有 RAW 解码库"
    dem, wb_mode = RAW_DEMOSAIC, RAW_WB

    def run(demosaic: str):
        with rp.imread(str(src)) as raw:
            kw = {"output_bps": 8, "no_auto_bright": False}
            algo = _demosaic_enum(rp, demosaic)
            if algo is not None:
                kw["demosaic_algorithm"] = algo
            if wb_mode == "auto":
                kw["use_auto_wb"] = True
            elif wb_mode == "camera":
                kw["use_camera_wb"] = True
            elif wb_mode == "none":
                pass
            else:                                   # 预设白平衡：日光/阴天/白炽灯…
                wb = _preset_wb(wb_mode)
                if wb:
                    kw["user_wb"] = wb
                else:
                    kw["use_camera_wb"] = True
            return raw.postprocess(**kw)

    try:
        try:
            rgb = run(dem)
        except Exception as e:
            # 有些算法（比如 AMAZE）要额外的 GPL3 解码包，没编进去就退回默认算法
            if dem not in ("", "auto"):
                log("去马赛克算法 %s 用不了（%s），退回默认算法" % (dem, e))
                rgb = run("auto")
            else:
                raise
        if rgb is None or not getattr(rgb, "shape", None) or rgb.shape[0] < 2:
            return False, "解码结果为空"
        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        tmp = dst.with_suffix(".tmp.jpg")
        Image.fromarray(rgb).save(tmp, "JPEG", quality=quality, subsampling=0)  # type: ignore[union-attr]
        os.replace(tmp, dst)
        return True, "%d×%d" % (w, h)
    except Exception as e:
        return False, "解码失败：%s" % e


def _demosaic_enum(rp, name: str):
    """去马赛克算法：不同算法解出来的细节/伪色不一样（AHD 快、DCB/AMAZE 细节多）。"""
    name = (name or "auto").strip().lower()
    if name in ("", "auto"):
        return None                      # 交给 libraw 自己挑（默认 AHD/PPG）
    table = {
        "linear": "LINEAR", "vng": "VNG", "ppg": "PPG",
        "ahd": "AHD", "dcb": "DCB", "amaze": "AMAZE",
    }
    attr = table.get(name)
    if not attr:
        return None
    try:
        return getattr(rp.DemosaicAlgorithm, attr)
    except Exception:
        return None


def _preset_wb(name: str):
    """日光 / 阴天 / 白炽灯… 的近似倍率（RAW 的白平衡随时可换，这是它最大的好处之一）。"""
    table = {
        "daylight":    [2.05, 1.0, 1.55, 1.0],
        "shade":       [2.25, 1.0, 1.35, 1.0],
        "cloudy":      [2.15, 1.0, 1.45, 1.0],
        "tungsten":    [1.35, 1.0, 2.60, 1.0],
        "fluorescent": [1.75, 1.0, 2.05, 1.0],
        "flash":       [2.30, 1.0, 1.60, 1.0],
    }
    return table.get((name or "").strip().lower())


def available_demosaic() -> list[str]:
    """本机 libraw 实际支持的算法（AMAZE 之类要额外的 GPL3 包，可能没有）。"""
    rp = rawpy_module()
    if rp is None:
        return []
    out = []
    for name, attr in (("linear", "LINEAR"), ("vng", "VNG"), ("ppg", "PPG"),
                       ("ahd", "AHD"), ("dcb", "DCB")):
        if hasattr(rp.DemosaicAlgorithm, attr):
            out.append(name)
    # AMAZE 要额外的 GPL3 demosaic pack，默认的 libraw 轮子里没有，就不列了
    return out


def raw_settings() -> dict:
    return {"quality": RAW_QUALITY, "demosaic": RAW_DEMOSAIC, "wb": RAW_WB,
            "out": RAW_OUT_NAME, "rev": RAW_REV,
            "demosaic_available": available_demosaic()}


def raw_settings_text() -> str:
    s = raw_settings()
    dem = "自动" if s["demosaic"] in ("", "auto") else s["demosaic"].upper()
    wb = {"camera": "相机记录", "auto": "自动判断", "none": "不调（原始）"}.get(s["wb"], s["wb"])
    return "解全尺寸（%s 去马赛克，白平衡 %s，JPEG q%d）" % (dem, wb, s["quality"])


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #

def mdls_info(path: Path) -> dict:
    """macOS 自带 Spotlight 元数据（没有 ffprobe 时用它取时长/分辨率/编码）。

    用 `mdls -plist -` + plistlib 解析：这个格式是本机的键值字典，
    数组类字段（比如编码列表）也能正确读出来。
    """
    mdls = shutil.which("mdls")
    if not mdls:
        return {}
    try:
        r = subprocess.run([mdls, "-plist", "-", str(path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=20)
        if r.returncode != 0:
            return {}
        data = plistlib.loads(r.stdout or b"")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    def num(x):
        try:
            return float(x)
        except Exception:
            return None

    info: dict = {}
    info["duration"] = num(data.get("kMDItemDurationSeconds"))
    w, h = num(data.get("kMDItemPixelWidth")), num(data.get("kMDItemPixelHeight"))
    if w and h:
        info["width"], info["height"] = int(w), int(h)
    codecs = data.get("kMDItemCodecs")
    if isinstance(codecs, (list, tuple)) and codecs:
        info["codec"] = " / ".join(str(c) for c in codecs)
    elif codecs:
        info["codec"] = str(codecs)
    created = data.get("kMDItemContentCreationDate")
    if hasattr(created, "isoformat"):
        created = created.isoformat()
    info["created"] = str(created) if created else None
    br = num(data.get("kMDItemTotalBitRate")) or num(data.get("kMDItemVideoBitRate"))
    if br:
        info["bit_rate"] = br
    return {k: v for k, v in info.items() if v}


def ffprobe_info(path: Path) -> dict:
    """读取视频/音频元信息，失败返回 {}。"""
    if not FFPROBE:
        return mdls_info(path) if IS_MAC else {}
    cmd = [
        FFPROBE, "-hide_banner", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=45)
        if r.returncode != 0:
            return {}
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return mdls_info(path) if IS_MAC else {}

    info: dict = {}
    fmt = data.get("format") or {}
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic", 0) != 1), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

    def _f(x):
        try:
            return float(x)
        except Exception:
            return None

    dur = _f(fmt.get("duration"))
    if dur is None and v:
        dur = _f(v.get("duration"))
    info["duration"] = dur
    info["bit_rate"] = _f(fmt.get("bit_rate")) or (_f(v.get("bit_rate")) if v else None)
    info["format_name"] = fmt.get("format_long_name") or fmt.get("format_name")
    if v:
        info["width"] = v.get("width")
        info["height"] = v.get("height")
        info["codec"] = v.get("codec_long_name") or v.get("codec_name")
        info["pix_fmt"] = v.get("pix_fmt")
        info["fps"] = v.get("avg_frame_rate") or v.get("r_frame_rate")
        info["rotate"] = (v.get("tags") or {}).get("rotate")
    if a:
        info["audio_codec"] = a.get("codec_long_name") or a.get("codec_name")
        info["audio_channels"] = a.get("channels")
        info["audio_rate"] = a.get("sample_rate")
    tags = fmt.get("tags") or {}
    for k in ("creation_time", "com.apple.quicktime.creationdate", "title", "comment"):
        if tags.get(k):
            info["created"] = tags[k]
            break
    return info


def human_duration(sec) -> str:
    if not sec or sec <= 0:
        return ""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def human_size(n) -> str:
    try:
        n = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return ""


# --------------------------------------------------------------------------- #
# 外部程序启动器（关键：既能从桌面终端启动，也能从沙箱/服务里启动）
# --------------------------------------------------------------------------- #

class Launcher:
    """负责把 VLC / 文件管理器弹到用户的桌面上。

    依次尝试（auto 模式）：
      1. direct —— 当前进程已有 DISPLAY/WAYLAND_DISPLAY，直接起子进程
      2. direct(探测) —— 没有图形变量时，扫描 X11 抽象套接字（:0~:9）找到桌面，
                        并补上 XAUTHORITY。这样即使本工具跑在容器/沙箱里也能弹窗
      3. systemd —— 交给 `systemd-run --user`，适配某些受限环境
      4. xdg —— 最后兜底交给 xdg-open

    每一步都会验证是否真的启动成功，失败就换下一种。
    """

    MODES = ("auto", "direct", "systemd", "xdg")

    def __init__(self, mode: str = "auto"):
        self.mode = mode if mode in self.MODES else "auto"
        self._last_ok: str | None = None
        self._probed: tuple[str | None, str | None] | None = None

    # -- 探测 ---------------------------------------------------------------
    @staticmethod
    def _abstract_x_ok(n: int) -> bool:
        """X 服务器同时监听抽象套接字 @/tmp/.X11-unix/X<n>，跨 /tmp 隔离也能连上。"""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(0.35)
            s.connect("\0/tmp/.X11-unix/X%d" % n)
            return True
        except Exception:
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _xauthority_candidates(self) -> list[str]:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        c = []
        if os.environ.get("XAUTHORITY"):
            c.append(os.environ["XAUTHORITY"])
        c += [
            os.path.expanduser("~/.Xauthority"),
            "/run/user/%d/gdm/Xauthority" % uid,
            "/run/user/%d/xauth/%s" % (uid, os.environ.get("USER", "")),
        ]
        alt = os.environ.get("XDG_RUNTIME_DIR")
        if alt:
            c.insert(1, os.path.join(alt, "gdm", "Xauthority"))
        seen, out = set(), []
        for p in c:
            if p and p not in seen and os.path.exists(p):
                seen.add(p)
                out.append(p)
        return out

    def display_env(self) -> dict | None:
        """构造一个能连上桌面的环境变量字典，找不到返回 None。

        Windows / macOS 不需要 DISPLAY，进程本来就在图形会话里，直接返回环境。
        """
        env = dict(os.environ)
        if IS_WIN or IS_MAC:
            return env
        if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
            return env
        if self._probed is None:
            found = (None, None)
            for n in range(0, 10):
                if self._abstract_x_ok(n):
                    auths = self._xauthority_candidates()
                    found = (":%d" % n, auths[0] if auths else None)
                    break
            self._probed = found
        disp, auth = self._probed
        if not disp:
            return None
        env["DISPLAY"] = disp
        if auth:
            env["XAUTHORITY"] = auth
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d"
                       % (os.getuid() if hasattr(os, "getuid") else 0))
        return env

    def describe(self) -> str:
        if IS_WIN:
            return "直接启动（%s）" % PLATFORM_NAME
        if IS_MAC:
            return "直接启动（macOS 图形会话）"
        env = self.display_env() if self.mode in ("auto", "direct") else None
        if env:
            return "直接启动（桌面 %s）" % env.get("DISPLAY", "?")
        if self._last_ok == "systemd":
            return "systemd 用户会话启动"
        if self._last_ok == "xdg":
            return "xdg-open 启动"
        return {
            "systemd": "systemd 用户会话启动",
            "xdg": "xdg-open 启动",
        }.get(self.mode, "自动（未检测到桌面，将改用系统命令）")

    # -- 执行 ---------------------------------------------------------------
    def _try(self, argv: list[str], env: dict | None, wait: float) -> tuple[bool, str]:
        """启动一次。wait>0 时短暂等待：还活着＝成功，秒退＝失败（附 stderr 摘要）。

        注意：这里绝不使用 stderr=PIPE —— 父进程一旦丢弃管道读端，仍在运行的
        VLC 再往 stderr 写就会收到 SIGPIPE 被静默杀掉；改用临时文件收集诊断信息。
        """
        errfile = tempfile.TemporaryFile() if wait > 0 else None
        try:
            kw = {}
            if IS_WIN:
                # 让子进程脱离本进程的控制台，关掉窗口也不影响播放器
                kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                        | getattr(subprocess, "DETACHED_PROCESS", 0))
            else:
                kw["start_new_session"] = True
            p = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errfile if errfile is not None else subprocess.DEVNULL,
                env=env,
                **kw,
            )
        except Exception as e:
            if errfile:
                errfile.close()
            return False, "启动失败：%s" % e
        if wait <= 0:
            if errfile:
                errfile.close()
            return True, "已启动"
        try:
            rc = p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            return True, "已启动"          # 还活着 = 成功
        detail = ""
        try:
            if errfile:
                errfile.seek(0)
                lines = errfile.read(4096).decode("utf-8", "replace").strip().splitlines()
                if lines:
                    detail = "：" + lines[-1][:200]
        except Exception:
            pass
        finally:
            if errfile:
                errfile.close()
        if rc == 0:
            # 退出码 0 = 程序把活干完了。典型场景：VLC 带 --one-instance 时
            # 把文件交给已经运行的窗口后自己退出，这属于成功。
            return True, "已交给正在运行的窗口"
        return False, "退出码 %s%s" % (rc, detail)

    def spawn(self, argv: list[str], wait: float = 0.0) -> tuple[bool, str]:
        """启动外部程序，返回 (是否成功, 说明)。"""
        order = {
            "auto": ["direct", "systemd", "xdg"],
            "direct": ["direct"],
            "systemd": ["systemd"],
            "xdg": ["xdg"],
        }[self.mode]
        errors = []
        for m in order:
            if m == "direct":
                env = self.display_env()
                if env is None:
                    errors.append("direct：没有找到可用的桌面")
                    continue
                ok, msg = self._try(argv, env, wait)
            elif m == "systemd":
                if not SYSTEMD_RUN:
                    continue
                unit = "media-browser-%d-%d" % (int(now()), os.getpid() % 1000)
                sargv = [SYSTEMD_RUN, "--user", "--collect", "--quiet",
                         "--unit=" + unit, "--description=媒体浏览器启动的程序"] + argv
                ok, msg = self._try(sargv, dict(os.environ), max(wait, 0.6))
            else:  # xdg-open 兜底：交给系统默认程序（可能不是 VLC）
                if not XDG_OPEN:
                    continue
                target = next((a for a in reversed(argv)
                               if not a.startswith("-") and a != XDG_OPEN), None)
                if not target:
                    continue
                ok, msg = self._try([XDG_OPEN, target], self.display_env(), 1.0)
                if ok:
                    msg = "已交给系统默认程序（未使用 VLC）"
            if ok:
                self._last_ok = m
                return True, msg
            errors.append("%s：%s" % (m, msg))
        return False, "；".join(errors) or "没有可用的启动方式"

    # -- 具体动作 -----------------------------------------------------------
    def vlc_play(self, files: list[Path], extra: list[str] | None = None) -> tuple[bool, str]:
        """打开视频（图片走 view_image）。名字沿用 vlc_play，装了什么就用什么。"""
        if not files:
            return False, "没有可播放的文件"
        if VLC:
            argv = [VLC] + list(extra or []) + [str(f) for f in files]
            ok, msg = self.spawn(argv, wait=1.2)
            if ok:
                return True, "已用 VLC 打开 %d 个文件（%s）" % (len(files), self.describe())
            return False, "VLC 启动失败：%s" % msg
        # 没装 VLC：macOS 交给系统播放器 —— QuickTime 认的格式直接用它，
        # 别的（.mkv/.avi/.wmv…）交给系统默认程序（访达里设的打开方式，装了 IINA/VLC 自然就是它们）。
        if IS_MAC and MAC_OPEN:
            exts = {f.suffix.lower() for f in files}
            tries: list[tuple[list[str], str]] = []
            if exts <= MAC_QT_EXT and mac_app_path("QuickTime Player"):
                tries.append(([MAC_OPEN, "-a", "QuickTime Player"], "QuickTime Player"))
            tries.append(([MAC_OPEN], "系统默认播放器"))
            errs: list[str] = []
            for argv, shown in tries:
                ok, msg = self.spawn(argv + [str(f) for f in files], wait=1.2)
                if ok:
                    note = "" if shown == "QuickTime Player" else \
                        "（这个格式系统没默认程序，装 VLC / IINA 就能放）"
                    return True, "已用 %s 打开 %d 个文件%s" % (shown, len(files), note)
                errs.append("%s：%s" % (shown, msg))
            return False, "打不开视频：%s" % "；".join(errs)
        hint = {
            "macOS": "装一个 VLC（https://www.videolan.org/）或 brew install --cask vlc",
            "Windows": "装一个 VLC（https://www.videolan.org/）",
        }.get(PLATFORM_NAME, "sudo apt install vlc")
        return False, "没有找到 VLC，请安装（%s）或在配置里指定 --vlc 路径" % hint

    def view_image(self, files: list[Path], viewer: str | None = None) -> tuple[bool, str]:
        """用本机图片查看器打开原图（不是缩略图）。"""
        viewer = viewer or IMAGE_VIEWER
        if not viewer:
            return False, "没有找到图片查看器（可用 --image-viewer 指定）"
        if not files:
            return False, "没有可查看的图片"
        name = Path(viewer).name.lower()
        shown = Path(viewer).name
        if IS_MAC and name == "open":
            # macOS：图片交给系统自带的「预览」，一次可以带多张；
            # 预览也打不开的（个别 RAW / 生僻格式）再退回系统默认看图程序。
            shown = "预览"
            ok, msg = self.spawn([viewer, "-a", "Preview"] + [str(f) for f in files], wait=1.2)
            if not ok:
                shown = "系统默认看图程序"
                ok, msg = self.spawn([viewer] + [str(f) for f in files], wait=1.2)
        elif IS_WIN and name.startswith("explorer"):
            files = files[:1]
            ok, msg = self.spawn([viewer] + [str(f) for f in files], wait=1.2)
        else:
            if name == "xdg-open":
                files = files[:1]      # xdg-open 只接受一个文件
            ok, msg = self.spawn([viewer] + [str(f) for f in files], wait=1.2)
        if ok:
            return True, "已打开 %d 张原图（%s）" % (len(files), shown)
        return False, "图片查看器启动失败：%s" % msg

    def reveal(self, path: Path) -> tuple[bool, str]:
        """在文件管理器里定位文件（macOS 用 Finder、Windows 用资源管理器）。"""
        try:
            is_dir = path.is_dir()
        except OSError:
            is_dir = False
        if IS_MAC and MAC_OPEN:
            ok, msg = self.spawn([MAC_OPEN, str(path)] if is_dir
                                 else [MAC_OPEN, "-R", str(path)], 0.8)
            if ok:
                return True, ("已打开文件夹 %s" % path.name) if is_dir else "已在访达中定位"
            return False, "打不开访达：%s" % msg
        if IS_WIN and EXPLORER:
            arg = str(path) if is_dir else "/select," + str(path)
            ok, msg = self.spawn([EXPLORER, arg], 0.8)
            if ok:
                return True, ("已打开文件夹 %s" % path.name) if is_dir else "已在资源管理器中定位"
            return False, "打不开资源管理器：%s" % msg
        if is_dir and XDG_OPEN:                 # 目录就直接打开它，别去父目录里定位
            ok, msg = self.spawn([XDG_OPEN, str(path)])
            if ok:
                return True, "已打开文件夹 %s" % path.name
        uri = "file://" + urllib.parse.quote(str(path))
        if GDBUS:
            try:
                r = subprocess.run(
                    [GDBUS, "call", "--session",
                     "--dest", "org.freedesktop.FileManager1",
                     "--object-path", "/org/freedesktop/FileManager1",
                     "--method", "org.freedesktop.FileManager1.ShowItems",
                     "['%s']" % uri, ""],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=6,
                )
                if r.returncode == 0:
                    return True, "已在文件管理器中定位"
            except Exception:
                pass
        if XDG_OPEN:
            ok, msg = self.spawn([XDG_OPEN, str(path.parent)])
            if ok:
                return True, "已打开所在文件夹"
        return False, "找不到可用的文件管理器接口"

    def open_url(self, url: str) -> tuple[bool, str]:
        if XDG_OPEN:
            ok, msg = self.spawn([XDG_OPEN, url])
            if ok:
                return True, "已打开浏览器"
        try:
            webbrowser.open(url)
            return True, "已打开浏览器"
        except Exception as e:
            return False, str(e)


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #

def lan_ips() -> list[str]:
    """本机在局域网里的 IPv4 地址（不含回环、docker 网桥）。"""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:                                  # 顺着默认路由问一下出口 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(i for i in ips
                  if not i.startswith("127.") and not i.startswith("172.17.")
                  and not i.startswith("172.18.") and i != "0.0.0.0")


def ufw_enabled() -> bool:
    """粗查 ufw 是否开着（只读配置文件，不需要 root；只有 Linux 有 ufw）。"""
    if not IS_LINUX:
        return False
    try:
        for line in Path("/etc/ufw/ufw.conf").read_text("utf-8").splitlines():
            if line.strip().upper().startswith("ENABLED="):
                return line.split("=", 1)[1].strip().lower() in ("yes", "true", "1")
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# 远程媒体源：把另一台机器上的「媒体浏览器」当成一个素材目录
# --------------------------------------------------------------------------- #
REMOTE_TIMEOUT = 25.0          # 单个远程请求的超时（秒）
REMOTE_MARK = "@"              # 远程条目 id 的前缀，一眼能认出来


class RemoteSource:
    """一台远程媒体浏览器（http://主机:端口），只读地当素材目录用。"""

    def __init__(self, url: str, name: str = "", token: str = ""):
        u = (url or "").strip().rstrip("/")
        if u and not u.startswith(("http://", "https://")):
            u = "http://" + u
        self.url = u
        self.token = token or ""
        self.key = root_key(Path(u or "remote"))
        self._name = name
        self.online = False
        self.note = ""
        self.items: dict = {}
        self.dirs: dict = {}
        self.count = 0
        self.scanned_at = 0.0

    # -- 名字 ---------------------------------------------------------------
    @property
    def name(self) -> str:
        if self._name:
            return self._name
        try:
            return urllib.parse.urlsplit(self.url).hostname or self.url
        except Exception:
            return self.url

    def to_dict(self) -> dict:
        return {"url": self.url, "name": self.name, "token": self.token}

    # -- 请求 ---------------------------------------------------------------
    def _url(self, path: str, **params) -> str:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if self.token:
            q = (q + "&" if q else "") + "t=" + urllib.parse.quote(self.token)
        return "%s%s%s" % (self.url, path, ("?" + q) if q else "")

    def get_json(self, path: str, **params) -> dict:
        req = urllib.request.Request(self._url(path, **params),
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    def open_stream(self, path: str, **params):
        """打开一个流（缩略图 / 原文件代理用）。调用方负责 close。"""
        req = urllib.request.Request(self._url(path, **params))
        return urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT)

    def ping(self) -> bool:
        try:
            j = self.get_json("/api/ping")
            self.online = bool(j.get("ok", True))
            self.note = ""
            return self.online
        except Exception as e:
            self.online = False
            self.note = str(e)
            return False

    def fetch_items(self) -> tuple[dict, dict, str]:
        """拉取这台机器上的全部条目（分页）。返回 (items, dirs, 错误说明)。"""
        out: dict = {}
        dirs: dict = {}
        general = ""            # 不带目录的筛选
        try:
            first = self.get_json("/api/list", limit=1, offset=0)
            total = int(first.get("total") or 0)
        except Exception as e:
            return {}, {}, "连不上：%s" % e
        limit = 500
        try:
            for off in range(0, max(total, 1), limit):
                j = self.get_json("/api/list", limit=limit, offset=off, raw=1)
                batch = j.get("items") or []
                if not batch:
                    break
                for it in batch:
                    rid = str(it.get("id") or "")
                    if not rid:
                        continue
                    iid = "%s%s:%s" % (REMOTE_MARK, self.key, rid)
                    parent = str(it.get("dir") or "")
                    item = {
                        "id": iid,
                        "path": str(Path(self.url) / rid),        # 仅用于显示
                        "rel": rid,
                        "name": str(it.get("name") or ""),
                        "dir": parent,
                        "root": -1,                               # 稍后由 scan 填
                        "root_name": self.name,
                        "kind": str(it.get("kind") or "image"),
                        "ext": str(it.get("ext") or "").lower(),
                        "size": int(it.get("size") or 0),
                        "mtime": float(it.get("mtime") or 0),
                        "remote": self.key,
                        "remote_url": self.url,
                        "remote_id": rid,
                    }
                    for k in ("duration", "width", "height", "raw"):
                        if it.get(k) is not None:
                            item[k] = it[k]
                    out[iid] = item
                    if not it.get("paired_hidden"):
                        dirs[parent] = dirs.get(parent, 0) + 1
                if len(batch) < limit:
                    break
        except Exception as e:
            general = "拉取出错：%s" % e
        self.items = out
        self.dirs = dirs
        self.count = len([i for i in out.values() if not i.get("paired_hidden")])
        self.scanned_at = now()
        self.online = not general
        self.note = general
        return out, dirs, general


def remote_key_of(url: str) -> str:
    return root_key(Path((url or "").strip().rstrip("/")))


def root_key(p: Path) -> str:
    """根目录的稳定短标识：用绝对路径的 sha1 前 8 位。

    条目 id 里带它（多目录时），这样**增删目录、调整顺序都不会让已有条目的 id 变**，
    缩略图 / 元信息缓存不会因为目录顺序变化而整体失效。
    """
    return hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:8]


def _same_file(a: Path, b: Path) -> bool:
    """a 和 b 是不是同一个文件（NTFS 这类大小写不敏感的盘上，改大小写时很有用）。"""
    try:
        return a.samefile(b)
    except OSError:
        return False


def _unique_path(dest_dir: Path, name: str) -> Path:
    """目标重名时给个不冲突的名字：照片.jpg → 照片 (1).jpg → 照片 (2).jpg"""
    p = Path(name)
    stem, suffix = p.stem, p.suffix
    for i in range(1, 1000):
        cand = dest_dir / ("%s (%d)%s" % (stem, i, suffix))
        if not cand.exists():
            return cand
    return dest_dir / ("%s-%d%s" % (stem, int(now()), suffix))


class Library:
    """扫描目录、维护条目、生成缩略图。"""

    def __init__(self, roots: list[Path], cache_dir: Path, thumb_px: int = 480,
                 pair_raw: bool = True, recursive: bool = True,
                 sources: list[str] | None = None,
                 remotes: list[dict] | None = None):
        self.roots = [r.resolve() for r in roots]
        self.root_keys = [root_key(r) for r in self.roots]
        self.root_sources = list(sources) if sources else ["cli"] * len(self.roots)
        # 远程源排在本地目录后面，root 下标 = len(self.roots) + i
        self.remotes: list[RemoteSource] = []
        for r in (remotes or []):
            if isinstance(r, str):
                r = {"url": r}
            url = str(r.get("url") or "")
            if url:
                self.remotes.append(RemoteSource(url, str(r.get("name") or ""),
                                                 str(r.get("token") or "")))
        self.cache_dir = cache_dir
        self.thumb_dir = cache_dir / "thumbs"
        self.meta_file = cache_dir / "meta.json"
        self.thumb_px = thumb_px
        self.pair_raw = pair_raw
        self.recursive = recursive

        self.items: dict[str, dict] = {}
        self.dirs: dict[str, int] = {}
        self.lock = threading.RLock()
        self.scanning = False
        self.scan_started_at = 0.0
        self.scan_again = False
        self.scan_note = ""
        self.root_timeout = 45.0        # 单个根目录最长扫描时间（秒）
        self.version = 1                # 索引版本号：内容一变就 +1，前端据此自动刷新
        self.watch_running = False
        self.scanned_at = 0.0

        self.meta_total = 0
        self.meta_done = 0
        self.meta_running = False
        self.warm_running = False
        self.warmed = 0

        self._inflight: dict[str, threading.Event] = {}
        self._failed: set[str] = set()
        self._err_lock = threading.Lock()

        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="thumb")
        self.meta_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="meta")
        self._meta_cache: dict = {}
        self._load_meta_cache()
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

    # -- id 编解码 ----------------------------------------------------------
    @property
    def multi(self) -> bool:
        return len(self.roots) > 1

    def item_id(self, root_idx: int, rel: str) -> str:
        return ("%s:%s" % (self.root_keys[root_idx], rel)) if self.multi else rel

    # -- 根目录增删（界面里「添加目录」用） -----------------------------------
    def root_index(self, ident: str) -> int:
        """按 key 或路径找到根目录下标，找不到返回 -1。"""
        with self.lock:
            for i, k in enumerate(self.root_keys):
                if ident == k:
                    return i
            for i, r in enumerate(self.roots):
                if ident == str(r):
                    return i
        try:
            p = Path(ident).expanduser().resolve()
        except Exception:
            return -1
        with self.lock:
            for i, r in enumerate(self.roots):
                if p == r:
                    return i
        return -1

    def add_root(self, path: str | Path, source: str = "extra") -> tuple[bool, str]:
        raw = Path(str(path)).expanduser()
        try:
            p = raw.resolve()
        except Exception:
            return False, "路径看不懂：%s" % raw
        if not p.exists():
            return False, "目录不存在：%s" % p
        if not p.is_dir():
            return False, "这不是文件夹：%s" % p
        with self.lock:
            if p in self.roots:
                return False, "这个目录已经在列表里了：%s" % p
            inside = next((r for r in self.roots if r in p.parents), None)
            covers = next((r for r in self.roots if p in r.parents), None)
            self.roots.append(p)
            self.root_keys.append(root_key(p))
            self.root_sources.append(source)
            if inside:
                note = "（它在 %s 里面，同一个文件可能会列两遍）" % inside
            elif covers:
                note = "（它包含已有的 %s，里面的文件会出现两遍）" % covers
            else:
                note = ""
            return True, "已添加目录 %s%s" % (p, note)

    def remove_root(self, ident: str) -> tuple[bool, str, str]:
        """返回 (是否成功, 提示, 被移除的绝对路径/地址)。"""
        if self.remote_index(ident) >= 0:            # 远程源
            ok, msg = self.remove_remote(ident)
            return ok, msg, ident if ok else ""
        i = self.root_index(ident)
        if i < 0:
            return False, "找不到这个目录：%s" % ident, ""
        with self.lock:
            if len(self.roots) <= 1 and not self.remotes:
                return False, "至少要保留一个目录", ""
            path = self.roots.pop(i)
            self.root_keys.pop(i)
            self.root_sources.pop(i)
        return True, "已移除目录 %s" % path, str(path)

    # -- 远程源 -------------------------------------------------------------
    def remote_index(self, ident: str) -> int:
        for i, rr in enumerate(self.remotes):
            if ident == rr.key or ident == rr.url or ident == rr.name:
                return i
        return -1

    def add_remote(self, url: str, name: str = "", token: str = "") -> tuple[bool, str]:
        u = (url or "").strip()
        if not u:
            return False, "远程地址是空的"
        key = remote_key_of(u)
        with self.lock:
            if any(rr.key == key for rr in self.remotes):
                return False, "这个服务器已经在列表里了：%s" % u
        rr = RemoteSource(u, name, token)
        if not rr.ping():
            return False, "连不上这个服务器：%s（%s）" % (rr.url, rr.note or "没有响应")
        with self.lock:
            self.remotes.append(rr)
        return True, "已添加远程服务器 %s（%s）" % (rr.name, rr.url)

    def remove_remote(self, ident: str) -> tuple[bool, str]:
        i = self.remote_index(ident)
        if i < 0:
            return False, "找不到这个远程服务器：%s" % ident
        with self.lock:
            rr = self.remotes.pop(i)
        return True, "已移除远程服务器 %s" % rr.name

    def roots_info(self) -> list[dict]:
        with self.lock:
            items = list(self.items.values())
            out = []
            for i, r in enumerate(self.roots):
                visible = [x for x in items if x["root"] == i and not x.get("paired_hidden")]
                note = write_problem(r)          # 只读挂载（macOS 上的 NTFS 盘）要提前告诉界面
                out.append({
                    "key": self.root_keys[i],
                    "path": str(r),
                    "name": r.name or str(r),
                    "source": self.root_sources[i],
                    "count": len(visible),
                    "videos": len([x for x in visible if x["kind"] == "video"]),
                    "images": len([x for x in visible if x["kind"] == "image"]),
                    "kind": "local",
                    "writable": not note,
                    "write_note": note,
                })
            for i, rr in enumerate(self.remotes):
                ri = len(self.roots) + i
                visible = [x for x in items if x["root"] == ri and not x.get("paired_hidden")]
                out.append({
                    "key": rr.key,
                    "path": rr.url,
                    "name": rr.name,
                    "source": "remote",
                    "count": len(visible),
                    "videos": len([x for x in visible if x["kind"] == "video"]),
                    "images": len([x for x in visible if x["kind"] == "image"]),
                    "kind": "remote",
                    "online": rr.online,
                    "note": rr.note,
                })
        return out

    def resolve_id(self, iid: str) -> tuple[Path, int, str]:
        """id -> (绝对路径, root_idx, rel)。只接受索引里存在的 id。"""
        with self.lock:
            it = self.items.get(iid)
        if it is None:
            raise KeyError(iid)
        return Path(it["path"]), it["root"], it["rel"]

    # -- 目录浏览 / 移动（分类） ---------------------------------------------
    def root_of_path(self, path: Path) -> int:
        """路径落在哪个根目录里（-1 表示不在任何根目录下）。"""
        p = Path(path).resolve()
        best = -1
        for i, r in enumerate(self.roots):
            try:
                p.relative_to(r)
            except ValueError:
                continue
            if best < 0 or len(str(r)) > len(str(self.roots[best])):
                best = i
        return best

    def folder_list(self, path: str = "") -> dict:
        """列出一个目录下的子文件夹（只允许根目录范围内），给「移动到…」用。"""
        with self.lock:
            roots = list(self.roots)
            counts: dict[str, int] = {}
            for it in self.items.values():
                if it.get("paired_hidden"):
                    continue
                counts[str(Path(it["path"]).parent)] = counts.get(str(Path(it["path"]).parent), 0) + 1
        if path:
            cur = Path(path).expanduser()
            try:
                cur = cur.resolve()
            except OSError:
                cur = Path(path).expanduser().absolute()
        else:
            cur = roots[0] if roots else Path(".")
        ri = self.root_of_path(cur)
        if not cur.is_dir() or ri < 0:
            raise ValueError("这个路径不在已加入的目录里：%s" % cur)
        dirs = []
        try:
            for e in sorted(cur.iterdir(), key=lambda x: x.name.lower()):
                if e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                dirs.append({"name": e.name, "path": str(e), "files": counts.get(str(e), 0)})
        except OSError as e:
            raise ValueError("读不了这个目录：%s（%s）" % (cur, e))
        parent = str(cur.parent) if cur != roots[ri] else ""
        note = write_problem(cur)
        return {"path": str(cur), "parent": parent, "dirs": dirs,
                "roots": [{"key": self.root_keys[i], "path": str(r), "name": r.name or str(r)}
                          for i, r in enumerate(roots)],
                "here_files": counts.get(str(cur), 0),
                "is_root": cur == roots[ri],
                "writable": not note, "write_note": note}

    def move_items(self, ids: list[str], dest: str, on_conflict: str = "rename",
                   mkdir: bool = False, progress=None) -> dict:
        """把条目移到 dest（本机执行）。配套的 RAW / JPEG 会跟着一起走。

        on_conflict: rename（自动改名）| skip（跳过）| overwrite（覆盖）
        """
        destp = Path(dest).expanduser()
        try:
            destp = destp.resolve()
        except OSError:
            destp = destp.absolute()
        ri = self.root_of_path(destp)
        if ri < 0:
            raise ValueError("目标文件夹不在已加入的目录里，先把这个目录加进来：%s" % destp)
        if mkdir:
            destp.mkdir(parents=True, exist_ok=True)
        if not destp.is_dir():
            raise ValueError("目标不是个文件夹：%s" % destp)

        jobs: list[tuple[str, Path]] = []      # (来源 id, 文件路径)
        seen: set[str] = set()
        missing: list[str] = []
        for iid in ids:
            with self.lock:
                it = self.items.get(iid)
                twin = self.items.get(it.get("paired_with")) if it and it.get("paired_with") else None
                raw_of_twin = (it or {}).get("raw") or {}
            if not it:
                missing.append(iid)
                continue
            cand: list[Path] = [Path(it["path"])]
            if twin:                                    # 这是个 RAW，配对 JPEG 一起走
                cand.append(Path(twin["path"]))
            elif raw_of_twin.get("path"):               # 这是个 JPEG，同名 RAW 一起走
                cand.append(Path(raw_of_twin["path"]))
            for p in cand:
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                jobs.append((iid, p))

        moved, renamed, skipped, errors = [], [], [], []
        total = len(jobs)
        for n, (iid, src) in enumerate(jobs, 1):
            if progress:
                progress(n - 1, total, src.name)
            if not src.exists():
                errors.append("%s：源文件不在了" % src.name)
                continue
            try:
                if src.parent == destp:
                    skipped.append("%s：已经在这个文件夹里" % src.name)
                    continue
            except OSError:
                pass
            target = destp / src.name
            try:
                if target.exists():
                    if on_conflict == "skip":
                        skipped.append("%s：目标已有同名文件" % src.name)
                        continue
                    if on_conflict == "overwrite":
                        if target.is_dir():
                            errors.append("%s：目标是个文件夹，没动" % src.name)
                            continue
                        target.unlink()
                    else:
                        target = _unique_path(destp, src.name)
                        renamed.append("%s → %s" % (src.name, target.name))
                shutil.move(str(src), str(target))
                moved.append(src.name)
            except Exception as e:
                errors.append("%s：%s" % (src.name, os_error_text(e, destp)))
        if progress:
            progress(total, total, "")
        if moved or renamed:
            self.scan()            # 索引跟着刷新（和 rename_items 一样，免得列表还指着老路径）
        return {"moved": len(moved), "renamed": renamed, "skipped": skipped,
                "missing": missing, "errors": errors, "dest": str(destp), "total": total}

    # -- 重命名（本机执行；RAW 配对与 sidecar 一起改，缩略图缓存跟着搬） ------
    @staticmethod
    def _check_new_stem(stem: str) -> str:
        """检查新文件名（不含扩展名）能不能用：返回错误说明，没问题返回空串。"""
        if not stem or not stem.strip():
            return "名字不能是空的"
        if stem in (".", ".."):
            return "名字不能是 . 或 .."
        if stem != stem.strip():
            return "名字首尾不能有空格"
        # 素材盘多是 NTFS：这些字符要么非法、要么跨平台会出问题，统一拦掉
        bad = sorted({c for c in stem if c in '/\\:*?"<>|' or ord(c) < 32})
        if bad:
            return "名字里不能有这些字符：%s" % " ".join(bad)
        if stem.endswith("."):
            return "名字不能以 . 结尾（Windows 上存不下来）"
        if stem.startswith("."):
            return "名字不能以 . 开头（会被当成隐藏文件跳过）"
        if len(stem.encode("utf-8")) > 200:
            return "名字太长了，换个短点的"
        return ""

    def _group_files(self, it: dict) -> list:
        """改名要一起动的文件：条目本身 + 配对的另一张（RAW↔JPEG）+ 同名 sidecar。"""
        paths: list[Path] = []
        seen: set = set()

        def add(p) -> None:
            if p and str(p) not in seen:
                seen.add(str(p))
                paths.append(Path(p))

        add(it.get("path"))
        with self.lock:
            twin = self.items.get(it["paired_with"]) if it.get("paired_with") else None
        if twin:
            add(twin.get("path"))
        if (it.get("raw") or {}).get("path"):
            add(it["raw"]["path"])
        src = Path(it["path"])                    # sidecar 不进索引，只能自己看目录
        try:
            for sib in src.parent.iterdir():
                if sib.is_file() and sib.stem == src.stem \
                        and sib.suffix.lower() in SIDECAR_EXTS:
                    add(sib)
        except OSError:
            pass
        return paths

    def rename_plan(self, ids: list[str], spec: dict) -> list[dict]:
        """算出每个条目改名后叫什么（只看索引，不落盘）。预览和真正执行都用它。

        spec 的字段都作用在**主文件名**上，扩展名不动：
          name            直接给新名字（单张改名）
          find / replace  查找替换
          prefix / suffix 加前缀 / 后缀
          seq,start,digits,seq_sep   连续编号（如 001、002）
        """
        name = str(spec.get("name") or "")
        find = str(spec.get("find") or "")
        repl = str(spec.get("replace") or "")
        prefix = str(spec.get("prefix") or "")
        suffix = str(spec.get("suffix") or "")
        use_seq = bool(spec.get("seq"))
        try:
            start = int(spec.get("start") or 1)
        except (TypeError, ValueError):
            start = 1
        try:
            digits = max(1, min(6, int(spec.get("digits") or 3)))
        except (TypeError, ValueError):
            digits = 3
        sep = str(spec.get("seq_sep") or "_")

        plan: list[dict] = []
        n = 0
        for iid in ids:
            with self.lock:
                it = dict(self.items.get(iid) or {}) or None
            if not it:
                plan.append({"id": iid, "status": "error", "detail": "条目不在了（可能刚被移动/删掉）"})
                continue
            if it.get("remote"):
                plan.append({"id": iid, "old": it.get("name", ""), "status": "error",
                             "detail": "远程服务器上的文件不能改名（先下载到本机）"})
                continue
            n += 1
            src = Path(it["path"])
            stem, ext = src.stem, src.suffix
            if name:
                new_stem = name
            elif use_seq:
                new_stem = "%s%0*d%s" % (prefix, digits, start + n - 1, suffix)
            else:
                new_stem = stem.replace(find, repl) if find else stem
                new_stem = "%s%s%s" % (prefix, new_stem, suffix)
            err = self._check_new_stem(new_stem)
            if not err and new_stem == stem:
                err = "名字没变"
            plan.append({"id": iid, "path": str(src), "old": src.name, "ext": ext,
                         "new_stem": new_stem, "new_name": new_stem + ext,
                         "status": "error" if err else "ok", "detail": err})
        return plan

    def rename_items(self, ids: list[str], spec: dict, on_conflict: str = "rename",
                     dry_run: bool = False, progress=None) -> dict:
        """按 spec 改名（本机执行）。dry_run=True 时只算名字和冲突，一个字都不落盘。"""
        plan = self.rename_plan(ids, spec)
        used: set = set()          # 这一批已经占掉的目标路径
        srcs: set = set()          # 这一批会挪走的源文件（判断冲突时要排除它们，才能"互换名字"）

        for row in plan:
            if row.get("status") == "ok":
                with self.lock:
                    it = dict(self.items.get(row["id"]) or {})
                row["_group"] = self._group_files(it)
                srcs.update(str(f) for f in row["_group"])

        # ---- 先给整组算好目标名；任何一条算不出来，这一条就整组不动 ----
        for row in plan:
            if row.get("status") != "ok":
                continue
            moves: list = []
            problem = ""
            for f in row["_group"]:
                t = f.parent / (row["new_stem"] + f.suffix)
                while str(t) in used and not _same_file(t, f):
                    t = _unique_path(f.parent, t.name)
                clash = t.exists() and not _same_file(t, f) and str(t) not in srcs
                if clash:
                    if on_conflict == "skip":
                        problem = "目标已有同名文件：%s" % t.name
                        break
                    if on_conflict == "overwrite":
                        if t.is_dir():
                            problem = "%s 是个文件夹，没动" % t.name
                            break
                    else:
                        t = _unique_path(f.parent, t.name)
                used.add(str(t))
                moves.append((f, t))
            row["_moves"] = moves
            if problem:
                row["status"] = "skipped"
                row["detail"] = problem
            else:
                row["conflict"] = any(str(t) != str(f) and t.exists() and str(t) not in srcs
                                      for f, t in moves)

        done_rows = [r for r in plan if r.get("_moves")]
        if dry_run:
            for row in plan:
                row.pop("_group", None)
            return {"dry_run": True, "total": len(plan),
                    "ok_count": sum(1 for r in plan if r.get("status") == "ok"),
                    "skipped": sum(1 for r in plan if r.get("status") == "skipped"),
                    "failed": sum(1 for r in plan if r.get("status") == "error"),
                    "conflicts": sum(1 for r in plan if r.get("conflict")),
                    "results": [{k: v for k, v in r.items() if not k.startswith("_")}
                                for r in plan]}

        # ---- 落盘：两阶段（先临时名再目标名），这样"互换名字"和大小写改动都不会撞 ----
        ok = skipped = failed = 0
        total = len(done_rows)
        temp: list = []            # (临时路径, 目标路径)
        for i, row in enumerate(done_rows):
            if progress:
                progress(i, total, row["old"])
            pair_list = []
            try:
                for j, (f, t) in enumerate(row["_moves"]):
                    if str(f) == str(t):
                        continue
                    tmp = f.parent / (".mb-rename-%d-%d-%s" % (os.getpid(), j, f.name))
                    os.rename(str(f), str(tmp))
                    temp.append((tmp, t, f))
                    pair_list.append((f, t))
            except Exception as e:
                for tmp, t, f in reversed(temp):        # 这一条回滚
                    try:
                        os.rename(str(tmp), str(f))
                    except OSError:
                        pass
                temp = [x for x in temp if x[2] not in [p[0] for p in pair_list]]
                row["status"] = "error"
                row["detail"] = os_error_text(e, f)
                failed += 1
                continue
            for f, t in pair_list:
                pass
            row["_pairs"] = pair_list

        for row in done_rows:
            if row.get("status") != "ok":
                continue
            try:
                for tmp, t, f in [x for x in temp if x[2] in [p[0] for p in row.get("_pairs", [])]]:
                    if t.exists() and not _same_file(t, tmp):
                        if on_conflict == "overwrite":
                            t.unlink()
                        else:
                            t = _unique_path(t.parent, t.name)
                    os.rename(str(tmp), str(t))
                    self._move_cache(f, t)
                names = [t.name for _, t in row.get("_pairs", [])]
                row["status"] = "done"
                row["to"] = row["_pairs"][0][1].name if row.get("_pairs") else row["old"]
                row["files"] = names or [row["old"]]
                row["new_id"] = self._id_after(row["id"], row["to"])
                ok += 1
            except Exception as e:
                row["status"] = "error"
                row["detail"] = os_error_text(e, row.get("path"))
                failed += 1

        skipped = sum(1 for r in plan if r.get("status") == "skipped")
        failed += sum(1 for r in plan if r.get("status") == "error")
        if progress:
            progress(total, total, "")
        if ok:
            self.scan()                                  # 重建索引（几毫秒）
        out = []
        for r in plan:
            r.pop("_group", None)
            r.pop("_moves", None)
            r.pop("_pairs", None)
            out.append(r)
        return {"dry_run": False, "ok_count": ok, "skipped": skipped, "failed": failed,
                "total": len(plan), "results": out}

    def _id_after(self, old_id: str, new_name: str) -> str:
        """改名后的新条目 id：只换掉最后一段文件名，根目录前缀（xxxx:）和中间路径都留着。"""
        if not old_id or not new_name:
            return old_id
        head, sep, _ = old_id.rpartition("/")
        if sep:
            return head + sep + new_name
        root, colon, _ = old_id.partition(":")      # 直接在素材目录根下：id 形如 "9becdbd3:名字.jpg"
        return (root + colon + new_name) if colon else new_name

    def _move_cache(self, old: Path, new: Path) -> None:
        """文件改名后把缩略图和元信息缓存也挪过去（省得重新生成一遍）。"""
        with self.lock:
            old_it = next((dict(v) for v in self.items.values()
                           if v.get("path") == str(old)), None)
        if not old_it:
            return
        root_i = old_it.get("root", 0)
        try:
            old_rel = str(old.relative_to(self.roots[root_i]))
            new_rel = str(new.relative_to(self.roots[root_i]))
        except (ValueError, IndexError):
            return
        old_id = self.item_id(root_i, old_rel)
        new_id = self.item_id(root_i, new_rel)
        if old_id == new_id:
            return
        # 缩略图 key 只跟 id/大小/修改时间有关，而改名不动这两样 → 缓存文件直接搬
        fake = dict(old_it, id=new_id, path=str(new), rel=new_rel, name=new.name)
        old_thumb = self.thumb_path_for(self.thumb_key(old_it))
        new_thumb = self.thumb_path_for(self.thumb_key(fake))
        try:
            if old_thumb.exists():
                new_thumb.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(old_thumb), str(new_thumb))
        except OSError:
            pass
        with self.lock:
            meta = self._meta_cache.pop(old_id, None)
            if meta is not None:
                self._meta_cache[new_id] = meta

    # -- 元信息缓存（缩略图/时长跨次启动复用） -------------------------------
    def _load_meta_cache(self) -> None:
        try:
            if self.meta_file.exists():
                self._meta_cache = json.loads(self.meta_file.read_text("utf-8"))
        except Exception:
            self._meta_cache = {}

    def _save_meta_cache(self) -> None:
        try:
            data = {}
            for iid, it in self.items.items():
                rec = {k: it[k] for k in ("duration", "width", "height") if it.get(k) is not None}
                if rec:
                    data[iid] = rec
            self.meta_file.write_text(json.dumps(data), "utf-8")
        except Exception:
            pass

    # -- 扫描 ---------------------------------------------------------------
    def scan(self, timeout: float | None = None) -> bool:
        """扫描所有根目录，返回是否真的扫了（已经在扫 → False，不阻塞调用方）。

        两条保命措施（本机 /mnt/MEDIA 是 ntfs-3g，属于 FUSE，实测会卡死单个请求）：
          1. **每个根目录单独一个线程扫 + 超时**：一个盘卡住，其它盘照样出结果；
          2. **看门狗**：如果上一次扫描已经卡了很久，允许强行重新开始，
             否则一次磁盘打嗝就会让这个进程永远"扫描中"，之后加什么目录都读不出来。
        """
        tmo = float(self.root_timeout if timeout is None else timeout)
        with self.lock:
            if self.scanning:
                if now() - self.scan_started_at < max(tmo * 3, 180):
                    self.scan_again = True          # 记一笔，等这轮完了再扫一次
                    return False
                log("上次扫描已卡住 %.0f 秒（多半是磁盘/网络盘不给响应），强行重新开始"
                    % (now() - self.scan_started_at))
            self.scanning = True
            self.scan_started_at = now()
            self.scan_again = False
        try:
            roots = list(self.roots)
            box: dict[int, tuple[dict, dict]] = {}

            def work(ri: int, root: Path) -> None:
                found: dict = {}
                dirs: dict = {}
                box[ri] = (found, dirs)           # 先放进去，超时也能拿到已扫到的部分
                try:
                    files = self._walk(root) if self.recursive else self._iter_files_flat(root)
                    self._collect(files, ri, found, dirs)
                except Exception as e:
                    log("扫描 %s 出错：%s" % (root, e))

            workers = []
            for ri, root in enumerate(roots):
                if not root.exists():
                    log("跳过不存在的目录：%s" % root)
                    continue
                t = threading.Thread(target=work, args=(ri, root),
                                     name="scan-%d" % ri, daemon=True)
                t.start()
                workers.append((ri, root, t))

            # 远程源同时拉（放后台线程，一台机器没响应不影响别的）
            remotes = list(self.remotes)
            rbox: dict[int, tuple[dict, dict, str]] = {}

            def work_remote(ri: int, rr: "RemoteSource") -> None:
                try:
                    items, rdirs, err = rr.fetch_items()
                    rbox[ri] = (items, rdirs, err)
                except Exception as e:                      # 兜底，别让线程炸掉
                    rbox[ri] = ({}, {}, str(e))

            rworkers = []
            for i, rr in enumerate(remotes):
                t = threading.Thread(target=work_remote, args=(i, rr),
                                     name="scan-remote-%d" % i, daemon=True)
                t.start()
                rworkers.append((i, rr, t))
            deadline = now() + tmo
            slow = []
            for ri, root, t in workers:
                t.join(max(0.5, deadline - now()))
                if t.is_alive():
                    slow.append(str(root))
                    log("扫描超时：%s（%.0f 秒没扫完，先用已扫到的部分，后台线程仍在等磁盘）"
                        % (root, tmo))

            found: dict[str, dict] = {}
            dirs: dict[str, int] = {}
            for ri, _root, _t in workers:
                got = box.get(ri)
                if not got:
                    continue
                try:
                    found.update(got[0])
                    for k, v in list(got[1].items()):
                        dirs[k] = dirs.get(k, 0) + v
                except RuntimeError:               # 后台线程还在往里写，跳过这轮
                    pass

            # 远程源：给条目的 root 下标填好（本地目录个数 + i），再并进总表
            rnotes = []
            for i, rr, t in rworkers:
                t.join(max(0.5, deadline - now()))
                if t.is_alive():
                    rnotes.append("%s 响应太慢" % rr.name)
                    continue
                got = rbox.get(i)
                if not got:
                    continue
                ritems, rdirs, err = got
                if err:
                    rnotes.append("%s：%s" % (rr.name, err))
                    continue
                ri = len(roots) + i
                for it in ritems.values():
                    it["root"] = ri
                found.update(ritems)
                for k, v in rdirs.items():
                    dirs[k] = dirs.get(k, 0) + v
            with self.lock:
                changed = (len(found) != len(self.items)
                           or set(found) != set(self.items))
                self.items = found
                self.dirs = dirs
                self.scanned_at = now()
                if self.pair_raw:
                    self._pair_raw()
                self.meta_total = len([i for i in self.items.values()
                                       if not i.get("paired_hidden")])
                notes = []
                if slow:
                    notes.append("这些目录扫描超时，可能磁盘卡住了：%s" % "、".join(slow))
                if rnotes:
                    notes.append("远程源：%s" % "；".join(rnotes))
                self.scan_note = "；".join(notes)
                if changed:
                    self.version += 1
            hidden = len([i for i in found.values() if i.get("paired_hidden")])
            log("扫描完成：%d 个媒体文件%s，%d 个文件夹，用时 %.2f 秒"
                % (len(found) - hidden,
                   "（另有 %d 个 RAW 与同名 JPEG 配对，默认折叠）" % hidden if hidden else "",
                   len(self.dirs), now() - self.scan_started_at))
            return True
        finally:
            again = False
            with self.lock:
                self.scanning = False
                if self.scan_again:
                    self.scan_again = False
                    again = True
            if again:
                # 扫描期间又有人要求扫（比如刚加完目录），补一次
                threading.Thread(target=self.scan, daemon=True, name="scan-again").start()

    # -- 目录监听（新文件/新文件夹自动出现） --------------------------------
    def tree_signature(self) -> tuple:
        """目录树指纹：只看结构（路径 + 每个目录的条目数），不 stat 文件，很快。"""
        sig = []
        for root in list(self.roots):
            try:
                if not root.is_dir():
                    continue
            except OSError:
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames
                                     if not d.startswith(".") and d not in SKIP_DIR_NAMES)
                sig.append((dirpath, len(filenames), len(dirnames)))
        return tuple(sig)

    def start_watch(self, interval: float = 20.0) -> None:
        """后台线程：每隔 interval 秒看一眼目录树有没有变，变了就自动重扫。"""
        with self.lock:
            if self.watch_running or interval <= 0:
                return
            self.watch_running = True

        def runner():
            last = None
            while True:
                time.sleep(interval)
                # 指纹放在临时线程里算并加超时：网络盘卡住也不会让监听线程一起完蛋
                res: list = []
                t = threading.Thread(target=lambda: res.append(self.tree_signature()),
                                     daemon=True, name="sig")
                t.start()
                t.join(interval * 2)
                if t.is_alive() or not res:
                    log("目录监听：检查超时（磁盘没响应），跳过这一轮")
                    continue
                sig = res[0]
                if last is None:
                    last = sig
                    continue
                if sig != last:
                    last = sig
                    log("目录监听：发现变化，自动重新扫描")
                    self.scan()

        threading.Thread(target=runner, name="watch", daemon=True).start()

    # 工具自己的目录（放着 media_browser.py / ui.html / docs 的那个文件夹）不是素材，
    # 但它的上级往往就是素材根目录 —— 扫到它就跳过，免得 README 里的截图
    # 、图标之类的东西混进缩略图墙。
    TOOL_DIR = Path(__file__).resolve().parent

    def _walk(self, root: Path):
        skip_self = self.TOOL_DIR != Path(root).resolve()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if skip_self:
                dirnames[:] = [d for d in dirnames
                               if (Path(dirpath) / d).resolve() != self.TOOL_DIR]
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d not in SKIP_DIR_NAMES)
            for fn in sorted(filenames):
                yield Path(dirpath) / fn

    def _iter_files_flat(self, root: Path):
        for e in sorted(os.scandir(root), key=lambda e: e.name):
            if e.is_file(follow_symlinks=False):
                yield Path(e.path)

    def _collect(self, files, ri: int, found: dict, dirs: dict) -> None:
        root = self.roots[ri]
        for p in files:
            ext = p.suffix.lower()
            if ext in IMAGE_EXTS:
                kind = "image"
            elif ext in VIDEO_EXTS:
                kind = "video"
            elif ext in RAW_EXTS:
                kind = "raw"
            else:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                continue
            iid = self.item_id(ri, rel)
            parent = str(Path(rel).parent)
            parent = "" if parent == "." else parent
            item = {
                "id": iid,
                "path": str(p),
                "rel": rel,
                "name": p.name,
                "dir": parent,
                "root": ri,
                "root_name": root.name or str(root),
                "kind": kind,
                "ext": ext.lstrip("."),
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
            cached = self._meta_cache.get(iid)
            if cached:
                for k in ("duration", "width", "height"):
                    if cached.get(k) is not None:
                        item[k] = cached[k]
            found[iid] = item
            dirs[parent] = dirs.get(parent, 0) + 1

    def _pair_raw(self) -> None:
        """把 RAW 和同名的 JPEG 合并成一条：默认只显示 JPEG，但保留 RAW 信息。"""
        by_dir_stem: dict[tuple, dict] = {}
        for it in self.items.values():
            if it["kind"] == "image":
                by_dir_stem[(it["root"], it["dir"], Path(it["name"]).stem.lower())] = it
        for it in self.items.values():
            if it["kind"] != "raw":
                continue
            key = (it["root"], it["dir"], Path(it["name"]).stem.lower())
            twin = by_dir_stem.get(key)
            if twin is not None:
                twin["raw"] = {"id": it["id"], "name": it["name"], "size": it["size"],
                               "path": it["path"], "ext": it["ext"]}
                it["paired_with"] = twin["id"]
                it["paired_hidden"] = True  # 默认不在列表里重复出现
            else:
                it["raw_only"] = True

    # -- 元信息补齐 ----------------------------------------------------------
    def start_meta_pass(self) -> None:
        with self.lock:
            if self.meta_running:
                return
            self.meta_running = True
            self.meta_done = 0
            todo = [it for it in self.items.values() if not it.get("paired_hidden")]
            self.meta_total = len(todo)
        def work(it):
            try:
                self.fill_meta(it)
            finally:
                with self.lock:
                    self.meta_done += 1
        def runner():
            try:
                list(self.meta_pool.map(work, todo))
                self._save_meta_cache()
                log("元信息读取完成（%d 项）" % len(todo))
            finally:
                with self.lock:
                    self.meta_running = False
        threading.Thread(target=runner, name="meta-pass", daemon=True).start()

    def fill_meta(self, it: dict) -> None:
        if it.get("kind") == "video":
            info = ffprobe_info(Path(it["path"]))
            if info:
                it["duration"] = info.get("duration")
                it["width"] = info.get("width")
                it["height"] = info.get("height")
                it["codec"] = info.get("codec")
                it["fps"] = info.get("fps")
                it["bit_rate"] = info.get("bit_rate")
                it["audio_codec"] = info.get("audio_codec")
                it["probe"] = info
        elif it.get("kind") == "image":
            if Image is not None:
                try:
                    with Image.open(it["path"]) as im:
                        it["width"], it["height"] = im.size
                except Exception:
                    pass
        elif it.get("kind") == "raw":
            twin = it.get("paired_with")
            if twin and twin in self.items:
                src = self.items[twin]
                if src.get("width"):
                    it["width"], it["height"] = src["width"], src["height"]

    # -- 缩略图 -------------------------------------------------------------
    def thumb_key(self, it: dict) -> str:
        raw = "%s|%d|%d|%d|r%d" % (it["id"], it["size"], int(it["mtime"]),
                                   self.thumb_px, THUMB_REV)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def thumb_path_for(self, key: str) -> Path:
        return self.thumb_dir / key[:2] / (key + ".jpg")

    # -- RAW 全尺寸解码结果的存放 -------------------------------------------
    def raw_out_dir(self, src: Path) -> Path:
        """解码结果放哪：**RAW 文件同级目录**下新建一个文件夹（默认叫 RAW解码）。

        这样照片和解出来的图在一起，拷走/备份不会分家；扫描时会跳过这个目录，
        不会把解出来的 JPG 又当成新素材重复显示。
        那个目录建不出来（只读盘等）就退回缩略图缓存目录。
        """
        d = src.parent / RAW_OUT_NAME
        try:
            d.mkdir(exist_ok=True)
            probe = d / ".w"
            probe.write_bytes(b"1")
            probe.unlink()
            return d
        except OSError:
            fallback = self.cache_dir / "raw"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def raw_cache_path(self, src: Path, kind: str = "full") -> Path:
        """kind="full" 是全尺寸解码结果（放 RAW 同级目录），"preview" 是内嵌预览 ——
        两者绝不共用缓存，否则哪天库没了、只能出预览，之后库回来了会把那张小图当成全尺寸。"""
        st = None
        try:
            st = src.stat()
        except OSError:
            pass
        # 指纹里带上解码参数：换了去马赛克算法/白平衡/质量，不会误用旧结果
        fingerprint = "%s|%s|q%d" % (RAW_DEMOSAIC, RAW_WB, RAW_QUALITY)
        key = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:6]
        if kind == "full":
            # 放同级目录，文件名保留原 RAW 的名字（看图器标题栏里一眼认得出）
            return self.raw_out_dir(src) / ("%s.jpg" % src.stem)
        raw = "%s|%s|%d|r%d|%s" % (src, fingerprint, int(st.st_mtime) if st else 0,
                                   RAW_REV, kind)
        k = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        d = self.cache_dir / "raw"
        d.mkdir(parents=True, exist_ok=True)
        return d / ("%s-RAW-preview-%s-%s.jpg" % (src.stem, key, k[:8]))

    def ensure_thumb(self, it: dict, block: bool = True) -> Path | None:
        key = self.thumb_key(it)
        dst = self.thumb_path_for(key)
        if dst.exists() and dst.stat().st_size > 0:
            self._touch(dst)
            return dst
        with self._err_lock:
            if key in self._failed:
                return None
            ev = self._inflight.get(key)
            mine = ev is None
            if mine:
                ev = threading.Event()
                self._inflight[key] = ev
        if not mine:
            if not block:
                return None
            ev.wait(timeout=120)  # type: ignore[union-attr]
            return dst if dst.exists() else None
        try:
            ok = self._make_thumb(it, dst, key)
            return dst if ok else None
        finally:
            ev.set()  # type: ignore[union-attr]
            with self._err_lock:
                self._inflight.pop(key, None)

    def remote_by_key(self, key: str) -> "RemoteSource | None":
        with self.lock:
            for rr in self.remotes:
                if rr.key == key:
                    return rr
        return None

    def _remote_thumb(self, it: dict, dst: Path) -> bool:
        """远程条目的缩略图：直接把它那台机器生成好的图取回来存本地。"""
        rr = self.remote_by_key(str(it.get("remote") or ""))
        if rr is None:
            return False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".tmp.jpg")
            with rr.open_stream("/api/thumb", id=it.get("remote_id")) as r:
                data = r.read()
            if len(data) < 100:
                return False
            tmp.write_bytes(data)
            os.replace(tmp, dst)
            return True
        except Exception as e:
            log("远程缩略图失败 %s：%s" % (it.get("name"), e))
            return False

    def _make_thumb(self, it: dict, dst: Path, key: str) -> bool:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if it.get("remote"):
            ok = self._remote_thumb(it, dst)
            if not ok:
                with self._err_lock:
                    self._failed.add(key)
            else:
                self.warmed += 1
            return ok
        src = Path(it["path"])
        px = self.thumb_px
        try:
            if it["kind"] == "video":
                cand = self._video_frame(src, dst, px)
            elif it["kind"] == "raw":
                cand = self._raw_thumb(it, src, dst, px)
            else:
                # macOS 上先试系统自带的 sips（原生支持 HEIC/RAW，比 Pillow 还全），
                # 其余平台先用自己的 Pillow，最后都能退到 ffmpeg / QuickLook
                if IS_MAC:
                    cand = (_sys_thumb(src, dst, px)
                            or _pil_thumb(src, dst, px)
                            or _ffmpeg_thumb(src, dst, px, 0)
                            or _ql_thumb(src, dst, px))
                else:
                    cand = (_pil_thumb(src, dst, px) or _ffmpeg_thumb(src, dst, px, 0)
                            or _sys_thumb(src, dst, px))
            if not cand or not dst.exists() or dst.stat().st_size == 0:
                with self._err_lock:
                    self._failed.add(key)
                return False
            self.warmed += 1
            return True
        except Exception:
            log("缩略图失败 %s\n%s" % (it["path"], traceback.format_exc(limit=2)))
            with self._err_lock:
                self._failed.add(key)
            return False

    def _video_frame(self, src: Path, dst: Path, px: int) -> bool:
        """在多个时间点取帧，挑“最有内容”的一帧（避开纯黑开场/纯色画面）。"""
        dur = None
        try:
            dur = ffprobe_info(src).get("duration")
        except Exception:
            dur = None
        if not dur:
            seeks = [5.0, 1.0, 0.0]
        elif dur <= 3:
            seeks = [0.0, dur * 0.5]
        else:
            seeks = [dur * 0.05, dur * 0.2, dur * 0.35, dur * 0.55, dur * 0.8]
            seeks = [max(1.0, min(s, dur - 0.5)) for s in seeks]
        best: tuple[float, Path] | None = None
        try:
            for i, s in enumerate(seeks):
                cand = dst.with_name(dst.stem + ".c%d.jpg" % i)
                if not _ffmpeg_thumb(src, cand, px, s):
                    continue
                mean, sd = _frame_stats(cand)
                score = sd + mean * 0.5           # 明亮且细节多 = 更好的封面
                if best is None or score > best[0]:
                    if best is not None:
                        best[1].unlink(missing_ok=True)
                    best = (score, cand)
                    if sd >= 12 and mean >= 40:   # 已经足够好，不用再看
                        break
                else:
                    cand.unlink(missing_ok=True)
        except Exception:
            pass
        if best is None:
            # 没有 ffmpeg（macOS 常见）→ 用系统 QuickLook 抽一张封面
            return _ql_thumb(src, dst, px)
        if best[1] != dst:
            os.replace(best[1], dst)
        return dst.exists() and dst.stat().st_size > 0

    def _raw_thumb(self, it: dict, src: Path, dst: Path, px: int) -> bool:
        twin = it.get("paired_with")
        if twin and twin in self.items:
            tp = Path(self.items[twin]["path"])
            if _pil_thumb(tp, dst, px) or _ffmpeg_thumb(tp, dst, px, 0) \
                    or _sys_thumb(tp, dst, px):
                return True
        if _extract_embedded_jpeg(src, dst, px):
            return True
        if _ql_thumb(src, dst, px):
            return True
        return _ffmpeg_thumb(src, dst, px, 0)

    @staticmethod
    def _touch(p: Path) -> None:
        try:
            os.utime(p, None)
        except OSError:
            pass

    def start_warmup(self) -> None:
        """后台预热缩略图（按默认排序，先热最可能被看到的前面部分）。"""
        with self.lock:
            if self.warm_running:
                return
            self.warm_running = True
            todo = [it for it in self.items.values() if not it.get("paired_hidden")]
        todo.sort(key=lambda i: i["mtime"], reverse=True)
        queue = list(todo)
        qlock = threading.Lock()

        def worker():
            while True:
                with qlock:
                    if not queue:
                        return
                    it = queue.pop(0)
                self.ensure_thumb(it)

        def runner():
            try:
                t0 = now()
                threads = [threading.Thread(target=worker, name="warm-%d" % i, daemon=True)
                           for i in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                log("缩略图预热完成：%d 张，用时 %.1f 秒" % (len(todo), now() - t0))
            finally:
                with self.lock:
                    self.warm_running = False
        threading.Thread(target=runner, name="warmup", daemon=True).start()

    # -- 查询 ---------------------------------------------------------------
    def query(self, dir_filter: str = "", kind: str = "", q: str = "",
              sort: str = "mtime", order: str = "desc", show_raw: bool = False,
              offset: int = 0, limit: int = 120, root_filter: str = "") -> tuple[int, list[dict]]:
        with self.lock:
            items = list(self.items.values())
        if not show_raw:
            items = [i for i in items if not i.get("paired_hidden")]
        if root_filter:
            ri = self.root_index(root_filter)
            items = [i for i in items if i["root"] == ri] if ri >= 0 else []
        if dir_filter:
            if dir_filter == "\x00root":
                items = [i for i in items if i["dir"] == ""]
            else:
                items = [i for i in items if i["dir"] == dir_filter
                         or i["dir"].startswith(dir_filter.rstrip("/") + "/")]
        if kind in ("image", "video", "raw"):
            items = [i for i in items if i["kind"] == kind]
        if q:
            ql = q.lower()
            terms = [t for t in ql.split() if t]
            def match(i):
                hay = (i["name"] + " " + i["rel"]).lower()
                return all(t in hay for t in terms)
            items = [i for i in items if match(i)]

        keymap = {
            "mtime": lambda i: i["mtime"],
            "name": lambda i: i["name"].lower(),
            "size": lambda i: i["size"],
            "duration": lambda i: i.get("duration") or 0,
            "dir": lambda i: (i["dir"], i["name"].lower()),
        }
        key = keymap.get(sort, keymap["mtime"])
        items.sort(key=key, reverse=(order == "desc"))
        total = len(items)
        page = items[offset:offset + limit]
        out = []
        for i in page:
            d = dict(i)
            d.pop("probe", None)
            d["duration_text"] = human_duration(d.get("duration"))
            d["size_text"] = human_size(d.get("size"))
            out.append(d)
        return total, out

    def dirs_list(self, show_raw: bool = False) -> list[dict]:
        with self.lock:
            items = list(self.items.values())
        counts: dict[tuple[int, str], dict] = {}

        def bump(ri: int, root_name: str, d: str, kind: str) -> None:
            parts = [p for p in d.split("/") if p]
            names = [""] + [ "/".join(parts[:n]) for n in range(1, len(parts) + 1) ]
            for name in names:  # 目录自身 + 所有上级，各自累计
                rec = counts.setdefault(
                    (ri, name),
                    {"dir": name, "root": self.root_keys[ri], "root_name": root_name,
                     "total": 0, "image": 0, "video": 0, "raw": 0})
                rec["total"] += 1
                rec[kind] += 1

        for it in items:
            if it.get("paired_hidden") and not show_raw:
                continue
            bump(it["root"], it.get("root_name") or "", it["dir"],
                 it["kind"] if it["kind"] in ("image", "video", "raw") else "image")
        out = list(counts.values())
        out.sort(key=lambda r: (r["root_name"], r["dir"]))
        return out

    def stats(self) -> dict:
        t = time.time()
        memo = getattr(self, "_stats_memo", None)
        if memo and t - memo[0] < 1.0:
            return memo[1]
        with self.lock:
            items = list(self.items.values())
            visible = [i for i in items if not i.get("paired_hidden")]
            n_img = len([i for i in visible if i["kind"] == "image"])
            n_vid = len([i for i in visible if i["kind"] == "video"])
            n_raw = len([i for i in items if i["kind"] == "raw"])
            cached = 0
            for it in visible:
                p = self.thumb_path_for(self.thumb_key(it))
                if p.exists():
                    cached += 1
            res = {
                "scanning": self.scanning,
                "scan_started_at": self.scan_started_at,
                "scan_note": self.scan_note,
                "version": self.version,
                "watch": self.watch_running,
                "scanned_at": self.scanned_at,
                "total": len(visible),
                "images": n_img,
                "videos": n_vid,
                "raws": n_raw,
                "raw_pairs": len([i for i in items if i.get("paired_hidden")]),
                "meta_done": self.meta_done,
                "meta_total": self.meta_total,
                "meta_running": self.meta_running,
                "warming": self.warm_running,
                "thumb_cached": cached,
                "roots": [str(r) for r in self.roots],
                "cache_dir": str(self.cache_dir),
            }
        self._stats_memo = (t, res)
        return res

    # -- 详情 ---------------------------------------------------------------
    def remote_detail(self, it: dict) -> dict:
        """远程条目的详情：本地 probe 不了，就用对方列表里的信息和直观说明。"""
        rr = self.remote_by_key(str(it.get("remote") or ""))
        d = dict(it)
        d["path"] = "%s/%s" % (it.get("remote_url") or "", it.get("rel") or "")
        d["mtime_text"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(it.get("mtime") or 0))
        d["size_text"] = human_size(it.get("size"))
        d["duration_text"] = human_duration(it.get("duration"))
        d["viewer_name"] = APP.viewer_name
        d["remote_name"] = rr.name if rr else "远程服务器"
        d["remote_online"] = bool(rr and rr.online)
        d["has_raw"] = bool(it.get("raw")) or it.get("kind") == "raw"
        d["raw_mode"] = ""
        d["raw_mode_text"] = "远程 RAW：点「下载原件」把 .RAF 拿下来，或用那台机器上的界面解码"
        d["raw_decoded"] = {}
        d["play_target"] = d["path"]
        d["view_target"] = d["path"]
        d["play_note"] = ("这是 %s 上的文件：本机可以直接在浏览器里播（服务器端边转边发），"
                          "也可以用「📥 下载到本机并打开」拿原文件用本机播放器看。"
                          % (d["remote_name"]))
        d["note"] = d["play_note"]
        return d

    def detail(self, iid: str) -> dict:
        with self.lock:
            it = self.items.get(iid)
        if it is None:
            raise KeyError(iid)
        if it.get("remote"):
            return self.remote_detail(it)
        d = dict(it)
        d.pop("probe", None)
        p = Path(it["path"])
        d["duration_text"] = human_duration(d.get("duration"))
        d["size_text"] = human_size(d.get("size"))
        d["mtime_text"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it["mtime"]))
        d["play_target"] = it["path"]
        d["view_target"] = it["path"]
        d["play_note"] = ""
        d["viewer_name"] = APP.viewer_name
        # RAW 相关：这条有没有 RAW、能不能解全尺寸、界面该给什么按钮
        d["raw_path"] = ""
        d["raw_name"] = ""
        d["raw_size_text"] = ""
        d["raw_mode"] = APP.raw_mode()
        d["raw_mode_text"] = APP.raw_mode_text()
        d["has_raw"] = False
        if it["kind"] == "raw":
            d["has_raw"] = True
            d["raw_path"] = it["path"]
            d["raw_name"] = it["name"]
            d["raw_size_text"] = human_size(it.get("size"))
        elif it.get("raw"):
            d["has_raw"] = True
            d["raw_path"] = it["raw"]["path"]
            d["raw_name"] = it["raw"]["name"]
            d["raw_size_text"] = human_size(it["raw"].get("size"))
        # 解出来的全尺寸图（放在 RAW 同级目录）在不在？在的话界面上直接给"打开解码结果"
        d["raw_decoded"] = {}
        if d["has_raw"] and d["raw_path"] and APP.raw_mode() == "rawpy":
            dec = APP.lib.raw_cache_path(Path(d["raw_path"]), "full")
            try:
                if dec.exists() and dec.stat().st_size > 0:
                    with Image.open(dec) as im:  # type: ignore[union-attr]
                        d["raw_decoded"] = {"path": str(dec), "name": dec.name,
                                            "size": dec.stat().st_size,
                                            "size_text": human_size(dec.stat().st_size),
                                            "pixels": "%d×%d" % im.size,
                                            "dir": str(dec.parent)}
            except Exception:
                d["raw_decoded"] = {}

        if it["kind"] == "raw":
            twin = it.get("paired_with")
            if twin and twin in self.items:
                d["play_target"] = self.items[twin]["path"]
                d["view_target"] = self.items[twin]["path"]
                d["play_note"] = ("RAW 原文件（%s）交给 VLC 多半打不开，「打开」"
                                  "会打开配套的 %s。" % (it["name"], Path(self.items[twin]["path"]).name))
        elif it.get("raw"):
            d["note"] = ("这条有配套 RAW：%s（%s）。"
                         "「🖼 看 RAW 原图」会把它解成全尺寸再看（%s）。"
                         % (it["raw"]["name"], human_size(it["raw"].get("size")),
                            APP.raw_mode_text()))

        if it["kind"] == "video":
            info = ffprobe_info(p)
            if info:
                d.update({k: v for k, v in info.items() if v is not None})
                d["duration_text"] = human_duration(info.get("duration"))
                d["fps_text"] = _fps_text(info.get("fps"))
                d["bit_rate_text"] = ("%.1f Mbps" % (info["bit_rate"] / 1e6)) if info.get("bit_rate") else ""
                d["audio_text"] = _audio_text(info)
        elif it["kind"] in ("image", "raw"):
            d.update(self._image_exif(it))
        return d

    def _image_exif(self, it: dict) -> dict:
        out: dict = {}
        src = it["path"]
        twin = it.get("paired_with")
        if it["kind"] == "raw" and twin and twin in self.items:
            src = self.items[twin]["path"]
        if Image is None:
            return out
        try:
            with Image.open(src) as im:
                out["width"], out["height"] = im.size
                out["mode"] = im.mode
                exif = im.getexif()
                if not exif:
                    return out
                def g(tag, ifd=None):
                    try:
                        return (ifd or exif).get(tag)
                    except Exception:
                        return None
                make, model = g(271), g(272)
                out["camera"] = " ".join(str(x) for x in (make, model) if x).strip()
                sub = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
                out["lens"] = _clean(g(0xA434, sub) or g(42036, sub))
                out["datetime"] = _clean(g(0x9003, sub) or g(306))
                iso = g(0x8827, sub) or g(34855, sub)
                if iso:
                    out["iso"] = iso
                fn = g(0x829D, sub) or g(33437, sub)
                if fn:
                    out["aperture"] = "f/%s" % (float(fn) if float(fn) % 1 else int(float(fn)))
                et = g(0x829A, sub) or g(33434, sub)
                if et:
                    try:
                        f = float(et)
                        out["exposure"] = ("1/%d s" % round(1 / f)) if f and f < 1 else ("%g s" % f)
                    except Exception:
                        out["exposure"] = str(et)
                fl = g(0x920A, sub) or g(37386, sub)
                if fl:
                    out["focal"] = "%g mm" % float(fl)
                out["software"] = _clean(g(305))
        except Exception:
            pass
        return out


def _clean(v):
    """EXIF 字符串常常带尾部空格/\\x00，清掉。"""
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    s = str(v).replace("\x00", "").strip()
    return s or None


def _fps_text(fps) -> str:
    if not fps:
        return ""
    try:
        if isinstance(fps, str) and "/" in fps:
            n, d = fps.split("/")
            v = float(n) / float(d) if float(d) else 0
        else:
            v = float(fps)
        return ("%.0f fps" % v) if abs(v - round(v)) < 0.05 else ("%.2f fps" % v)
    except Exception:
        return str(fps)


def _audio_text(info: dict) -> str:
    parts = []
    if info.get("audio_codec"):
        parts.append(str(info["audio_codec"]).split(" ")[0])
    if info.get("audio_channels"):
        ch = int(info["audio_channels"])
        parts.append({1: "单声道", 2: "立体声"}.get(ch, "%d 声道" % ch))
    if info.get("audio_rate"):
        parts.append("%g kHz" % (float(info["audio_rate"]) / 1000.0))
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, lib: Library, launcher: Launcher, ui_path: Path,
                 verbose: bool = False, vlc_args: list[str] | None = None,
                 playlist_limit: int = 300, image_viewer: str | None = None,
                 viewer_limit: int = 20, cfg_path: Path | None = None,
                 cfg_roots: list[str] | None = None,
                 extra_roots: list[str] | None = None,
                 cli_roots: list[str] | None = None,
                 raw_app: str | None = None, raw_limit: int = 6,
                 token: str = "", lan: bool = False):
        self.lib = lib
        self.launcher = launcher
        self.ui_path = ui_path
        self.verbose = verbose
        self.vlc_args = vlc_args or []
        self.playlist_limit = playlist_limit
        self.image_viewer = image_viewer or IMAGE_VIEWER
        self.viewer_name = friendly_viewer_name(self.image_viewer)
        self.viewer_limit = viewer_limit
        self.ui_bytes: bytes | None = None
        self.ui_stamp: int | None = None
        # 目录管理：哪些来自启动参数 / 配置文件 / 界面添加
        self.cfg_path = cfg_path
        self.cfg_roots = list(cfg_roots) if cfg_roots is not None else None
        self.extra_roots = list(extra_roots or [])
        self.cli_roots = list(cli_roots or [])
        self.raw_app = raw_app          # 外部 RAW 软件（有的话优先用它）
        self.raw_limit = raw_limit      # 一次最多解几张 RAW（每张要好几秒）
        self.token = token              # 局域网里「打开/播放」这类会弹窗的动作需要它
        self.lan = lan                  # 是否绑定在 0.0.0.0
        self.jobs: dict[str, dict] = {}          # 长任务（移动/整理）进度表
        self.jobs_lock = threading.Lock()
        self.tickets: dict[str, dict] = {}       # 批量下载的临时票据
        self.tickets_lock = threading.Lock()
        self.play_lock = threading.Lock()        # 实时转码并发数
        self.play_now = 0
        self.play_max = 3

    # -- 长任务（移动文件这种要跑一阵的） ------------------------------------
    def start_job(self, kind: str, total: int = 0, note: str = "") -> str:
        jid = hashlib.sha1(("%s|%s|%s" % (kind, now(), os.urandom(6))).encode()).hexdigest()[:12]
        with self.jobs_lock:
            self.jobs[jid] = {"id": jid, "kind": kind, "total": total, "done": 0,
                              "current": "", "state": "running", "note": note,
                              "message": "", "result": None, "started": now(),
                              "finished": 0.0}
            # 顺手清掉 30 分钟前的旧任务，别让字典无限长大
            if len(self.jobs) > 40:
                cut = now() - 1800
                for k in [k for k, v in self.jobs.items()
                          if v["state"] != "running" and v["finished"] < cut]:
                    self.jobs.pop(k, None)
        return jid

    def job_update(self, jid: str, **kw) -> None:
        with self.jobs_lock:
            j = self.jobs.get(jid)
            if j:
                j.update(kw)

    def job_get(self, jid: str) -> dict | None:
        with self.jobs_lock:
            j = self.jobs.get(jid)
            return dict(j) if j else None

    # -- 批量下载票据 --------------------------------------------------------
    def new_ticket(self, paths: list[Path], name: str) -> str:
        tid = hashlib.sha1(os.urandom(12)).hexdigest()[:16]
        with self.tickets_lock:
            self.tickets[tid] = {"paths": [str(p) for p in paths], "name": name,
                                 "created": now()}
            cut = now() - 3600
            for k in [k for k, v in self.tickets.items() if v["created"] < cut]:
                self.tickets.pop(k, None)
        return tid

    def get_ticket(self, tid: str) -> dict | None:
        with self.tickets_lock:
            t = self.tickets.get(tid)
            return dict(t) if t else None

    # -- 目录管理 -----------------------------------------------------------
    # -- RAW 原图 -----------------------------------------------------------
    def raw_app_bin(self) -> str | None:
        """优先用 --raw-viewer 指定的；没指定就自己找一个装了的 RAW 软件。"""
        return getattr(self, "raw_app", None) or find_raw_app()

    def raw_mode(self) -> str:
        """当前能用什么方式打开 RAW：rawpy（全尺寸解码）/ 外部软件 / 只能看内嵌预览。"""
        if getattr(self, "raw_app", None):
            return "app"            # 用户明确指定了外部程序，就用它
        if rawpy_module() is not None:
            return "rawpy"
        return "app" if find_raw_app() else "preview"

    def raw_mode_text(self) -> str:
        m = self.raw_mode()
        if m == "rawpy":
            return "内置 libraw 解全尺寸（%s 去马赛克 / 白平衡 %s）" % (
                "自动" if RAW_DEMOSAIC in ("", "auto") else RAW_DEMOSAIC.upper(),
                {"camera": "按相机", "auto": "自动", "none": "原始"}.get(RAW_WB, RAW_WB))
        if m == "app":
            return "交给 %s 打开" % Path(self.raw_app_bin() or "RAW 软件").name
        return "只能看内嵌预览图（跑 ./安装RAW支持.sh 可解全尺寸）"

    def prepare_raw_view(self, raf: Path) -> tuple[str, Path | None, str]:
        """把 .RAF 变成"能看的图"。返回 (模式, 文件, 说明)。

        模式：decoded = 已解成全尺寸 JPEG；app = 交给外部 RAW 软件打开；
              preview = 只抠出内嵌预览（降级方案）。
        """
        if not raf.exists():
            return "missing", None, "RAW 文件不存在"
        mode = self.raw_mode()
        if mode == "app":
            return "app", raf, "交给 %s 打开" % Path(self.raw_app_bin() or "RAW 软件").name
        dst = self.lib.raw_cache_path(raf, "full" if mode == "rawpy" else "preview")
        if dst.exists() and dst.stat().st_size > 0:
            try:
                with Image.open(dst) as im:  # type: ignore[union-attr]
                    return "decoded" if mode == "rawpy" else "preview", dst, "%d×%d" % im.size
            except Exception:
                pass
        if mode == "rawpy":
            ok, note = decode_raw_full(raf, dst)
            if ok:
                return "decoded", dst, note
            log("RAW 解码失败 %s：%s" % (raf, note))
            dst = self.lib.raw_cache_path(raf, "preview")
        # 降级：抠内嵌的 JPEG 预览（比 JPG 小，但至少能看）
        if _extract_embedded_jpeg(raf, dst, 0):
            try:
                with Image.open(dst) as im:  # type: ignore[union-attr]
                    return "preview", dst, "%d×%d" % im.size
            except Exception:
                pass
        return "failed", None, "解不开这个 RAW 文件"

    def raw_path_of(self, it: dict) -> Path | None:
        """条目对应的 RAW 文件路径（本身是 RAW 就取自己，配对条目取配套 RAW）。"""
        if it.get("kind") == "raw":
            return Path(it["path"])
        raw = it.get("raw")
        if raw and raw.get("path"):
            return Path(raw["path"])
        return None

    def save_roots(self) -> str:
        """把「界面添加的目录」写回配置文件，返回写成功的路径（失败返回空串）。"""
        p = self.cfg_path
        if p is None:
            return ""
        data: dict = {}
        try:
            if p.exists():
                old = json.loads(p.read_text("utf-8"))
                if isinstance(old, dict):
                    data = old
        except Exception:
            data = {}
        if not self.extra_roots and self.cfg_roots is None and not data:
            return ""      # 本来就没有配置文件、也没东西要记，就别凭空造一个
        data["extra_roots"] = list(self.extra_roots)
        data["remote_roots"] = [rr.to_dict() for rr in self.lib.remotes]
        if self.cfg_roots is not None:
            data["roots"] = list(self.cfg_roots)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            txt = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(txt, "utf-8")
            os.replace(tmp, p)
            return str(p)
        except Exception as e:
            log("写配置文件失败 %s：%s" % (p, e))
            return ""

    def add_remote(self, url: str, name: str = "", token: str = "") -> tuple[bool, str]:
        ok, msg = self.lib.add_remote(url, name=name, token=token or self.token)
        if ok:
            self.save_roots()
            self.lib.version += 1
        return ok, msg

    def remove_remote(self, ident: str) -> tuple[bool, str]:
        ok, msg = self.lib.remove_remote(ident)
        if ok:
            self.save_roots()
            self.lib.version += 1
        return ok, msg

    def add_root(self, path: str) -> tuple[bool, str]:
        ok, msg = self.lib.add_root(path, source="extra")
        if not ok:
            return False, msg
        new = str(self.lib.roots[-1])
        if new not in self.extra_roots:
            self.extra_roots.append(new)
        wrote = self.save_roots()
        if wrote:
            msg += "，已记到 %s（下次启动还在）" % wrote
        else:
            msg += "（没写进配置文件，重启后不保留）"
        return True, msg

    def remove_root(self, ident: str) -> tuple[bool, str]:
        ok, msg, path = self.lib.remove_root(ident)
        if not ok:
            return False, msg
        from_cli = path in self.cli_roots
        if path in self.extra_roots:
            self.extra_roots.remove(path)
        if self.cfg_roots is not None and path in self.cfg_roots:
            self.cfg_roots = [r for r in self.cfg_roots if r != path]
        if from_cli:
            msg += "；它来自启动参数，重启后会再出现"
        wrote = self.save_roots()
        if wrote and not from_cli:
            msg += "，已从 %s 里去掉" % wrote
        return True, msg

    def suggest_dirs(self) -> list[dict]:
        """给界面用的「快速选目录」候选：家目录常用文件夹 + /mnt 等挂载点。"""
        have = {str(r) for r in self.lib.roots}
        out: list[dict] = []
        seen: set[str] = set()

        def push(p: Path, label: str = "") -> None:
            try:
                key = str(p)
                if key in seen or key in have or not p.is_dir():
                    return
                if os.access(str(p), os.R_OK) is False:
                    return
                seen.add(key)
                out.append({"path": key, "name": p.name or key,
                            "label": label or (p.name or key)})
            except OSError:
                pass

        home = Path.home()
        for n in ("图片", "视频", "桌面", "下载", "文档", "Pictures", "Videos", "Downloads"):
            push(home / n)
        for base in (Path("/mnt"), Path("/media") / os.environ.get("USER", ""),
                     Path("/run/media") / os.environ.get("USER", ""), home):
            try:
                subs = sorted([d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")],
                              key=lambda d: d.name)
            except OSError:
                continue
            for d in subs[:12]:
                push(d, "%s（%s）" % (d.name, d.parent))
        return out[:16]



APP: App  # 单例，供 Handler 使用


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaBrowser/" + VERSION
    protocol_version = "HTTP/1.1"

    # -- 基础工具 -----------------------------------------------------------
    def log_message(self, fmt, *args):
        if APP.verbose:
            log("%s %s" % (self.address_string(), fmt % args))

    def _send(self, body: bytes, ctype: str, status: int = 200,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if getattr(self, "_set_token_cookie", False):
            self.send_header("Set-Cookie",
                             "mb_token=%s; Path=/; SameSite=Lax; Max-Age=31536000"
                             % urllib.parse.quote(APP.token, safe=""))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, status: int = 200) -> None:
        # default=str 兜底：某个平台返回 datetime 之类的特殊类型时，
        # 顶多这一个字段变成字符串，不至于整个请求 500
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def _err(self, msg: str, status: int = 400) -> None:
        self._json({"ok": False, "error": msg}, status)

    def _body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    @staticmethod
    def _fix_utf8(s: str) -> str:
        """URL 里的中文如果没做百分号编码，会被 http.server 按 latin-1 读成乱码，救回来。"""
        try:
            if any(ord(c) > 127 for c in s):
                return s.encode("latin-1").decode("utf-8")
        except (UnicodeError, ValueError):
            pass
        return s

    # -- 访问控制（局域网只读；会弹窗/改配置的动作要 token） ----------------
    READ_ONLY = {
        "/", "/index.html", "/api/ping", "/api/list", "/api/dirs", "/api/stats",
        "/api/detail", "/api/thumb", "/api/media", "/api/roots", "/api/browse",
        "/api/play",
        "/api/download", "/api/zip", "/api/job",
    }
    # 这些接口会在本机桌面上弹东西（VLC / 看图工具 / 文件夹选择框）或改本机文件，
    # 局域网来的客户端一律不准用 —— 不是权限问题，是它们的效果落在这台电脑上，
    # 别人点一下你的屏幕上就冒窗口。局域网只能「在浏览器里播」和「下载」。
    LOCAL_ONLY = {
        "/api/open",           # 弹 VLC / 看图工具
        "/api/reveal",         # 在本机文件管理器里定位
        "/api/roots/pick",     # 在本机桌面弹文件夹选择框
        "/api/move",           # 移动文件（一定在本机执行）
        "/api/mkdir",          # 在素材目录里新建文件夹
        "/api/decode",         # 批量解码 RAW（会在素材目录里写文件）
        "/api/rename",         # 重命名（改的是素材目录里的文件名）
    }
    REMOTE_GUI_MSG = ("局域网客户端不能做这个：它会在服务器那台电脑上弹出 VLC / 看图工具，"
                      "或者改动素材目录里的文件。想看请在浏览器里播放，想存下来请用「下载」"
                      "（支持批量下载原图 / 原视频）。")

    def _client_local(self) -> bool:
        ip = (self.client_address[0] if self.client_address else "") or ""
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or ip.startswith("127.")

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return urllib.parse.unquote(v)
        return ""

    def _remote_token_ok(self, qs) -> bool:
        tok = APP.token
        if not tok:
            return False
        given = qs("t") or self._cookie("mb_token")
        if not given:
            return False
        # 口令可能是中文，compare_digest 只吃 ASCII 的 str，统一转成 bytes 比
        try:
            return hmac.compare_digest(given.encode("utf-8"), tok.encode("utf-8"))
        except Exception:
            return False

    def _access(self, path: str, qs) -> tuple[bool, bool]:
        """返回 (是否允许, 是否只读)。远程且没带 token 时只放行只读接口。"""
        if self._client_local():
            return True, False
        if path in self.LOCAL_ONLY:          # 不管有没有 token，远程都不许
            return False, True
        tok_ok = self._remote_token_ok(qs)
        if tok_ok:
            self._set_token_cookie = True
            return True, False
        if path in self.READ_ONLY:
            return True, True
        return False, True

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)

        def qs(name, default=""):
            v = q.get(name)
            return self._fix_utf8(v[0]) if v else default

        try:
            if method == "POST" and path.startswith("/api/"):
                body = self._body_json()
            else:
                body = {}

            self._set_token_cookie = False
            self._read_only = False
            allowed, ro = self._access(path, qs)
            self._read_only = ro
            if not allowed:
                if path in self.LOCAL_ONLY:
                    return self._json({"ok": False, "error": self.REMOTE_GUI_MSG,
                                       "code": "remote_no_local_gui"}, 403)
                return self._json({"ok": False, "code": "readonly",
                                   "error": "局域网访问只能浏览，不能「改目录」这类操作。"
                                            "要完全控制，请用带 token 的网址"
                                            "（启动时终端里打印了）。"}, 403)

            if path in ("/", "/index.html"):
                self._serve_ui()
            elif path == "/api/list":
                self._api_list(qs, body)
            elif path == "/api/dirs":
                self._api_dirs(qs)
            elif path == "/api/roots":
                self._api_roots()
            elif path == "/api/roots/add":
                self._api_roots_add(body)
            elif path == "/api/roots/remove":
                self._api_roots_remove(body)
            elif path == "/api/roots/pick":
                self._api_roots_pick(body)
            elif path == "/api/stats":
                self._json(APP.lib.stats())
            elif path == "/api/detail":
                self._api_detail(qs, body)
            elif path == "/api/thumb":
                self._api_thumb(qs, body)
            elif path == "/api/media":
                self._api_media(qs, body)
            elif path == "/api/play":
                self._api_play(qs, body)
            elif path == "/api/open":
                self._api_open(body)
            elif path == "/api/reveal":
                self._api_reveal(body)
            elif path == "/api/browse":
                self._api_browse(qs, body)
            elif path == "/api/move":
                self._api_move(body)
            elif path == "/api/rename":
                self._api_rename(body)
            elif path == "/api/mkdir":
                self._api_mkdir(body)
            elif path == "/api/decode":
                self._api_decode(body)
            elif path == "/api/job":
                self._api_job(qs)
            elif path == "/api/download":
                self._api_download(body)
            elif path == "/api/zip":
                self._api_zip(qs)
            elif path == "/api/rescan":
                threading.Thread(target=self._rescan, daemon=True).start()
                self._json({"ok": True, "message": "已开始重新扫描"})
            elif path == "/api/clear-cache":
                self._api_clear_cache()
            elif path == "/api/ping":
                self._json({"ok": True, "version": VERSION,
                            "viewer": APP.viewer_name, "vlc": bool(VLC),
                            "video_player": video_player_label(),
                            "raw": APP.raw_mode(), "raw_text": APP.raw_mode_text(),
                            "raw_app": Path(APP.raw_app).name if APP.raw_app else "",
                            "remote": not self._client_local(),
                            "readonly": bool(getattr(self, "_read_only", False)),
                            "has_token": bool(APP.token)})
            else:
                self._err("未知路径 %s" % path, 404)
        except KeyError as e:
            self._err("找不到条目 %s" % e, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("请求出错 %s\n%s" % (self.path, traceback.format_exc(limit=4)))
            try:
                self._err("服务器错误：%s" % e, 500)
            except Exception:
                pass

    def _rescan(self) -> None:
        if APP.lib.scan() is False:
            log("已有扫描在进行，这次请求排队等它结束")
            return
        APP.lib.start_meta_pass()
        APP.lib.start_warmup()

    def _rescan_async(self) -> None:
        """扫描放到后台线程：请求立刻返回，前端靠 /api/stats 的 version 自动刷新。"""
        threading.Thread(target=self._rescan, daemon=True, name="rescan").start()

    # -- 静态界面 -----------------------------------------------------------
    def _serve_ui(self) -> None:
        # ui.html 改过就重新读一次（不用重启服务；每次请求只多一个 stat）
        try:
            stamp = APP.ui_path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        if APP.ui_bytes is None or stamp != getattr(APP, "ui_stamp", None):
            try:
                APP.ui_bytes = APP.ui_path.read_bytes()
                APP.ui_stamp = stamp
            except OSError:
                APP.ui_bytes = (b"<h1>ui.html not found</h1>")
        self._send(APP.ui_bytes, "text/html; charset=utf-8",
                   extra={"Cache-Control": "no-cache"})

    # -- 列表 ---------------------------------------------------------------
    def _api_list(self, qs, body) -> None:
        args = {**body} if body else {}
        def g(k, d=""):
            return args.get(k, qs(k, d))
        try:
            offset = max(0, int(g("offset", "0") or 0))
            limit = min(500, max(1, int(g("limit", "120") or 120)))
        except ValueError:
            offset, limit = 0, 120
        show_raw = str(g("raw", "")).lower() in ("1", "true", "yes")
        d = str(g("dir", ""))
        if d == "/":
            d = "\x00root"
        total, items = APP.lib.query(
            dir_filter=d,
            kind=str(g("kind", "")),
            q=str(g("q", "")),
            sort=str(g("sort", "mtime")),
            order=str(g("order", "desc")),
            show_raw=show_raw,
            offset=offset,
            limit=limit,
            root_filter=str(g("root", "")),
        )
        self._json({"ok": True, "total": total, "offset": offset,
                    "limit": limit, "items": items})

    def _api_dirs(self, qs) -> None:
        show_raw = str(qs("raw", "")).lower() in ("1", "true", "yes")
        dirs = APP.lib.dirs_list(show_raw)
        self._json({"ok": True, "root": "\x00root", "dirs": dirs,
                    "multi": APP.lib.multi})

    # -- 目录管理 -----------------------------------------------------------
    def _api_roots(self) -> None:
        self._json({
            "ok": True,
            "roots": APP.lib.roots_info(),
            "multi": APP.lib.multi,
            "config": str(APP.cfg_path or ""),
            "suggestions": APP.suggest_dirs(),
            "sources": {"cli": "启动参数", "config": "配置文件", "extra": "界面添加"},
        })

    def _api_roots_add(self, body) -> None:
        path = str(body.get("path") or "").strip().strip('"').strip("'")
        if not path:
            return self._err("没有给路径")
        # 服务器地址（另一台机器上的媒体浏览器）→ 当远程素材源
        if re.match(r"^(https?://|\d{1,3}(\.\d{1,3}){3}(:\d+)?$)", path):
            ok, msg = APP.add_remote(path, token=str(body.get("token") or ""))
            if not ok:
                return self._err(msg)
            self._rescan_async()
            return self._json({"ok": True, "message": msg + "，正在拉取它的目录…",
                               "roots": APP.lib.roots_info(), "multi": True,
                               "scanning": True, "remote": True})
        # 网络共享（smb/afp/nfs/webdav）→ 交给系统挂载，拿回本地路径再加
        if re.match(r"^(smb|afp|nfs|dav|davs|webdav)://", path, re.I):
            ok, msg, local = self._mount_share(path)
            if not ok:
                return self._err(msg)
            path = local
        ok, msg = APP.add_root(path)
        if not ok:
            return self._err(msg)
        self._rescan_async()
        self._json({"ok": True, "message": msg + "，正在扫描…",
                    "roots": APP.lib.roots_info(), "multi": APP.lib.multi,
                    "scanning": True})

    def _mount_share(self, url: str) -> tuple[bool, str, str]:
        """把网络共享挂成本地路径：Linux 用 gio，macOS 告诉用户去访达挂，Windows 用 net use。

        返回 (是否成功, 说明, 本地路径)。已经挂过的直接复用。
        """
        # 已经挂到本机某个位置了？（gvfs / /Volumes / 盘符）
        if IS_LINUX:
            gvfs_root = Path("/run/user/%d/gvfs" % (os.getuid() if hasattr(os, "getuid") else 1000))
            if gvfs_root.is_dir():
                want = url.rstrip("/").split("//")[-1]
                for d in gvfs_root.iterdir():
                    if want.split("/")[0] in d.name:
                        for sub in [d] + [x for x in d.iterdir() if x.is_dir()]:
                            if sub.is_dir() and any(sub.iterdir()):
                                return True, "已经在 %s" % sub, str(sub)
            gio = shutil.which("gio")
            if not gio:
                return False, "这台机器没有 gio，挂不了网络共享；可以先手动挂载再加本地路径", ""
            try:
                r = subprocess.run([gio, "mount", url], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, timeout=120)
            except Exception as e:
                return False, "挂载失败：%s" % e, ""
            if r.returncode != 0:
                tail = (r.stdout or "").strip().splitlines()
                return False, "挂载失败：%s（可以先在文件管理器里连一次，再回来加本地路径）" \
                    % (tail[-1] if tail else "未知错误"), ""
            try:
                gio_root = Path("/run/user/%d/gvfs" % (os.getuid() if hasattr(os, "getuid") else 1000))
                want = url.rstrip("/").split("//")[-1].split("/")[0]
                for d in gio_root.iterdir():
                    if want in d.name:
                        return True, "已挂载", str(d)
            except Exception:
                pass
            return False, "挂上了但没找到挂载点，请手动填写本地路径（一般在 /run/user/*/gvfs 下）", ""
        if IS_MAC:
            return False, ("macOS 上请先在「访达 → 前往 → 连接服务器」里连一次 %s，"
                           "挂好之后把 /Volumes 下的本地路径加进来就行（这样所有功能都能用）" % url), ""
        if IS_WIN:
            return False, ("Windows 上请先映射网络驱动器（资源管理器 → 映射网络驱动器，填 %s），"
                           "再把盘符加进来（例如 Z:\\）" % url), ""
        return False, "这个平台暂时不支持自动挂载网络共享", ""

    def _api_roots_remove(self, body) -> None:
        ident = str(body.get("key") or body.get("path") or "").strip()
        if not ident:
            return self._err("没有指定要移除的目录")
        ok, msg = APP.remove_root(ident)
        if not ok:
            return self._err(msg)
        self._rescan_async()
        self._json({"ok": True, "message": msg + "，正在重新扫描…",
                    "roots": APP.lib.roots_info(), "multi": APP.lib.multi,
                    "scanning": True})

    def _api_roots_pick(self, body) -> None:
        """在桌面上弹系统文件夹选择框，把选中的路径带回来。

        Linux 用 zenity/kdialog，macOS 用 osascript 的 choose folder，
        Windows 用 PowerShell 的 FolderBrowserDialog。
        """
        zen = shutil.which("zenity")
        kdl = shutil.which("kdialog")
        env = APP.launcher.display_env()
        if not env:
            return self._json({"ok": False, "error": "没找到桌面（DISPLAY），请直接手输路径"})
        start = str(body.get("start") or "").strip()
        if not start or not Path(start).is_dir():
            start = str(APP.lib.roots[0]) if APP.lib.roots else str(Path.home())
        if IS_MAC and OSASCRIPT:
            if start:
                script = ('POSIX path of (choose folder with prompt "选择要加入的文件夹" '
                          'default location POSIX file "%s")' % start.replace('"', '\\"'))
            else:
                script = 'POSIX path of (choose folder with prompt "选择要加入的文件夹")'
            argv = [OSASCRIPT, "-e", script]
        elif IS_WIN and POWERSHELL:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                "$d.Description='选择要加入的文件夹';"
                "$d.SelectedPath='%s';"
                "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}"
                % start.replace("'", "''")
            )
            argv = [POWERSHELL, "-NoProfile", "-STA", "-Command", ps]
        elif zen:
            argv = [zen, "--file-selection", "--directory",
                    "--title=选择要加入的文件夹", "--filename=%s/" % start]
        elif kdl:
            argv = [kdl, "--getexistingdirectory", start]
        else:
            return self._json({"ok": False, "error": "本机没有可用的选择框（zenity/kdialog/osascript），"
                                                     "请直接手输路径"})
        try:
            p = subprocess.run(argv, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return self._json({"ok": False, "cancelled": True, "error": "选择框等待超时"})
        except Exception as e:
            return self._json({"ok": False, "error": "打开选择框失败：%s" % e})
        path = (p.stdout or "").strip()
        if p.returncode != 0 or not path:
            return self._json({"ok": False, "cancelled": True, "error": "已取消选择"})
        self._json({"ok": True, "path": path})

    # -- 详情 ---------------------------------------------------------------
    def _api_detail(self, qs, body) -> None:
        iid = body.get("id") or qs("id")
        if not iid:
            return self._err("缺少 id")
        d = APP.lib.detail(iid)
        with APP.lib.lock:
            it = APP.lib.items.get(iid)
        d["thumb_cached"] = bool(it) and APP.lib.thumb_path_for(
            APP.lib.thumb_key(it)).exists()
        self._json({"ok": True, "item": d})

    # -- 缩略图 -------------------------------------------------------------
    def _api_thumb(self, qs, body) -> None:
        iid = body.get("id") or qs("id")
        if not iid:
            return self._err("缺少 id")
        with APP.lib.lock:
            it = APP.lib.items.get(iid)
        if it is None:
            return self._err("找不到条目", 404)
        regen = str(qs("regen", "") or body.get("regen", "")).lower() in ("1", "true", "yes")
        if regen:
            key = APP.lib.thumb_key(it)
            try:
                APP.lib.thumb_path_for(key).unlink(missing_ok=True)
            except OSError:
                pass
            with APP.lib._err_lock:
                APP.lib._failed.discard(key)
        p = APP.lib.ensure_thumb(it)
        if not p or not p.exists():
            return self._err("缩略图生成失败", 404)
        key = p.stem
        etag = '"%s"' % key
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            data = p.read_bytes()
        except OSError:
            return self._err("读取缩略图失败", 500)
        self._send(data, "image/jpeg", extra={
            "ETag": etag,
            "Cache-Control": "public, max-age=86400",
        })

    # -- 原始媒体（支持 Range，浏览器内可直接播放/预览） ---------------------
    def _api_media(self, qs, body) -> None:
        iid = body.get("id") or qs("id")
        if not iid:
            return self._err("缺少 id")
        with APP.lib.lock:
            it = APP.lib.items.get(iid)
        if it and it.get("remote"):
            return self._proxy_media(it, qs)
        path, _root, rel = APP.lib.resolve_id(iid)
        if not path.exists():
            return self._err("文件不存在", 404)
        download = str(qs("dl", "")).lower() in ("1", "true", "yes")
        ctype = mimetypes.guess_type(path.name)[0] or ""
        if not ctype or ctype == "application/octet-stream":
            ctype = VIDEO_MIME_FALLBACK.get(path.suffix.lower(), "application/octet-stream")
        if path.suffix.lower() == ".svg":
            ctype = "image/svg+xml"
        disp = "attachment" if download else "inline"
        self._serve_file(path, ctype, disp)

    def _proxy_media(self, it: dict, qs) -> None:
        """远程条目的原文件：本机浏览器不方便直接连那台机器时，由我们中转。

        支持 Range 透传（视频拖动进度、断点续传都靠它）。
        """
        rr = APP.lib.remote_by_key(str(it.get("remote") or ""))
        if rr is None:
            return self._err("远程服务器已经不在了", 404)
        download = str(qs("dl", "")).lower() in ("1", "true", "yes")
        rng = self.headers.get("Range")
        try:
            req = urllib.request.Request(rr._url("/api/media", id=it.get("remote_id"),
                                                 dl="1" if download else ""))
            if rng and str(rng).startswith("bytes="):
                req.add_header("Range", str(rng))
            with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT) as r:
                ctype = r.headers.get("Content-Type") or "application/octet-stream"
                cd = r.headers.get("Content-Disposition") or ""
                length = r.headers.get("Content-Length")
                status = r.status
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                if length:
                    self.send_header("Content-Length", length)
                if cd:
                    self.send_header("Content-Disposition", cd)
                elif download:
                    self.send_header("Content-Disposition",
                                     "attachment; filename*=UTF-8''%s"
                                     % urllib.parse.quote(str(it.get("name") or "file")))
                if r.headers.get("Accept-Ranges"):
                    self.send_header("Accept-Ranges", r.headers["Accept-Ranges"])
                if r.headers.get("Content-Range"):
                    self.send_header("Content-Range", r.headers["Content-Range"])
                self.end_headers()
                if self.command == "HEAD":
                    return
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("远程文件中转失败 %s：%s" % (it.get("name"), e))
            try:
                self._err("从远程服务器取文件失败：%s" % e, 502)
            except Exception:
                pass

    def _serve_file(self, path: Path, ctype: str, disp: str) -> None:
        try:
            st = path.stat()
        except OSError:
            return self._err("文件不存在", 404)
        size = st.st_size
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            try:
                if spec.startswith("-"):
                    n = int(spec[1:])
                    start, end = max(0, size - n), size - 1
                else:
                    a, _, b = spec.partition("-")
                    start = int(a) if a else 0
                    end = int(b) if b else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
        length = end - start + 1
        fname = urllib.parse.quote(path.name.encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(int(st.st_mtime)))
        self.send_header("Content-Disposition",
                         "%s; filename*=UTF-8''%s" % (disp, fname))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- 用 VLC 打开 --------------------------------------------------------
    def _fetch_remote_file(self, it: dict) -> Path | None:
        """把远程条目下到本机缓存目录，返回本地文件路径（已经下过就直接用）。"""
        rr = APP.lib.remote_by_key(str(it.get("remote") or ""))
        if rr is None:
            return None
        safe = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", str(it.get("name") or "file"))
        d = APP.lib.cache_dir / "remote-open" / rr.key
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        dst = d / ("%s-%s" % (str(it.get("id"))[-8:].replace(":", "_"), safe))
        try:
            if dst.exists() and dst.stat().st_size == int(it.get("size") or 0) > 0:
                return dst
            tmp = dst.with_suffix(dst.suffix + ".part")
            with rr.open_stream("/api/media", id=it.get("remote_id")) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dst)
            return dst
        except Exception as e:
            log("下载远程文件失败 %s：%s" % (it.get("name"), e))
            return None

    def _api_open(self, body) -> None:
        """打开文件：视频交给 VLC，图片交给本机图片查看器（看原图）。

        远程条目会先下到本机缓存（~/.../media-browser/remote-open/）再打开 ——
        本地播放器读不了 http 地址，而且那台机器上不许我们弹程序。
        target = auto（默认，按类型分流） | vlc（全部用 VLC）
                 viewer（只用看图工具） | raw（图片开 RAW 原图，全尺寸解码）
        """
        ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
        if not ids:
            return self._err("缺少 id")

        # 远程条目：先落到本地再按类型打开
        with APP.lib.lock:
            remote_items = [APP.lib.items.get(i) for i in ids]
        remote_items = [x for x in remote_items if x and x.get("remote")]
        if remote_items:
            return self._open_remote(remote_items, body)
        target = str(body.get("target") or "auto").lower()
        fullscreen = bool(body.get("fullscreen"))
        one_instance = bool(body.get("one_instance"))
        extra = list(APP.vlc_args)
        if one_instance and "--one-instance" not in extra:
            extra.append("--one-instance")
        if fullscreen and "--fullscreen" not in extra:
            extra.append("--fullscreen")

        if target == "raw":
            return self._open_raw(ids)

        videos: list[Path] = []
        images: list[Path] = []
        missing = 0
        for iid in ids[:APP.playlist_limit]:
            try:
                path, _, _ = APP.lib.resolve_id(iid)
            except KeyError:
                continue
            with APP.lib.lock:
                it = APP.lib.items.get(iid) or {}
            kind = it.get("kind", "image")
            if kind == "raw":
                twin = APP.lib.items.get(it.get("paired_with")) if it.get("paired_with") else None
                src = Path(twin["path"]) if twin else path
                if not src.exists():
                    src = path
                if src.exists():
                    images.append(src)
                else:
                    missing += 1
                continue
            if not path.exists():
                missing += 1
                continue
            (videos if kind == "video" else images).append(path)

        parts: list[str] = []
        errors: list[str] = []
        truncated = len(ids) > APP.playlist_limit

        if target in ("auto", "vlc") and videos:
            ok, msg = APP.launcher.vlc_play(videos, extra)
            if ok:
                parts.append("%s 打开 %d 个视频" % (video_player_label() or "播放器", len(videos)))
            else:
                errors.append(msg)
        if target == "vlc" and images:
            ok, msg = APP.launcher.vlc_play(images, extra)
            if ok:
                parts.append("%s 打开 %d 张图片" % (video_player_label() or "播放器", len(images)))
            else:
                errors.append(msg)
        elif target in ("auto", "viewer") and images:
            use = images[:APP.viewer_limit]
            ok, msg = APP.launcher.view_image(use, APP.image_viewer)
            if ok:
                parts.append("%s 打开 %d 张原图%s"
                             % (APP.viewer_name or "图片查看器", len(use),
                                "（超出的 %d 张未打开）" % (len(images) - len(use))
                                if len(images) > len(use) else ""))
            else:
                errors.append(msg)

        if not parts:
            if errors:
                return self._err("；".join(errors), 500)
            if target == "viewer":
                return self._err("选中的里面没有可查看的图片")
            return self._err("没有可打开的文件")
        msg = "已用 " + "，".join(parts)
        if truncated:
            msg += "（已截断到前 %d 个）" % APP.playlist_limit
        if missing:
            msg += "；%d 个文件不存在" % missing
        if errors:
            msg += "；部分失败：" + "；".join(errors)
        self._json({"ok": True, "message": msg, "videos": len(videos),
                    "images": len(images), "target": target,
                    "mode": APP.launcher.describe()})

    def _open_remote(self, items: list, body: dict) -> None:
        """远程文件：下到本机缓存，再交给本机的看图工具 / VLC。"""
        target = str(body.get("target") or "auto").lower()
        limit = max(1, min(len(items), APP.viewer_limit))
        files: list[Path] = []
        videos: list[Path] = []
        errors: list[str] = []
        for it in items[:limit]:
            p = self._fetch_remote_file(it)
            if not p:
                errors.append("%s：下载失败" % it.get("name"))
                continue
            (videos if it.get("kind") == "video" else files).append(p)
        parts: list[str] = []
        if videos and target in ("auto", "vlc"):
            ok, msg = APP.launcher.vlc_play(videos, list(APP.vlc_args))
            parts.append("%s 打开 %d 个视频" % (video_player_label() or "播放器", len(videos))) if ok else errors.append(msg)
        if files and target in ("auto", "viewer", "raw"):
            use = files[:APP.viewer_limit]
            ok, msg = APP.launcher.view_image(use, APP.image_viewer)
            if ok:
                parts.append("%s 打开 %d 张原图" % (APP.viewer_name or "图片查看器", len(use)))
            else:
                errors.append(msg)
        if not parts:
            if errors:
                return self._err("；".join(errors), 500)
            return self._err("没有可打开的文件")
        msg = "已用 " + "，".join(parts) + "（远程文件已下到本机缓存）"
        if len(items) > limit:
            msg += "；一次最多打开 %d 个" % limit
        self._json({"ok": True, "message": msg, "remote": True, "files": len(files) + len(videos)})

    def _open_raw(self, ids: list) -> None:
        """图片 → 打开它的 RAW 原图（全尺寸解码后交给图片查看器）。"""
        rafs: list[Path] = []
        plain: list[Path] = []          # 没有 RAW 的普通图片，还是按老办法看
        skipped = 0
        for iid in ids[:APP.raw_limit]:
            with APP.lib.lock:
                it = APP.lib.items.get(iid)
            if not it:
                continue
            raf = APP.raw_path_of(it)
            if raf is None:
                try:
                    path, _, _ = APP.lib.resolve_id(iid)
                except KeyError:
                    continue
                if it.get("kind") != "video":
                    plain.append(path)
                continue
            if raf.exists():
                rafs.append(raf)
            else:
                skipped += 1
        too_many = len(ids) > APP.raw_limit

        files: list[Path] = []
        notes: list[str] = []
        app_mode: str | None = None
        for raf in rafs:
            mode, path, note = APP.prepare_raw_view(raf)
            if mode == "app" and path:
                app_mode = APP.raw_app_bin()
                files.append(path)
                notes.append(note)
            elif path:
                files.append(path)
                mode_txt = "全尺寸" if mode == "decoded" else "内嵌预览"
                notes.append("%s %s" % (mode_txt, note))
            else:
                skipped += 1

        parts: list[str] = []
        errors: list[str] = []
        if files:
            viewer = app_mode if app_mode else APP.image_viewer
            ok, msg = APP.launcher.view_image(files[:APP.viewer_limit], viewer)
            if ok:
                name = Path(viewer).name if viewer else "图片查看器"
                head = "RAW 原图" if app_mode is None else "RAW"
                parts.append("%s 打开 %d 张 %s（%s）"
                             % (name, len(files[:APP.viewer_limit]), head, "，".join(notes[:3])))
            else:
                errors.append(msg)
        if plain:
            ok, msg = APP.launcher.view_image(plain[:APP.viewer_limit], APP.image_viewer)
            if ok:
                parts.append("%s 打开 %d 张没有 RAW 的图片"
                             % (APP.viewer_name or "图片查看器", len(plain[:APP.viewer_limit])))
            else:
                errors.append(msg)

        if not parts:
            if errors:
                return self._err("；".join(errors), 500)
            if skipped:
                return self._err("这些条目里没有能打开的 RAW（%d 个跳过）" % skipped)
            return self._err("选中的里面没有 RAW 可看")

        msg = "已用 " + "，".join(parts)
        if too_many:
            msg += "；一次最多解 %d 张 RAW，其余未打开" % APP.raw_limit
        if skipped:
            msg += "；%d 个跳过" % skipped
        if errors:
            msg += "；部分失败：" + "；".join(errors)
        self._json({"ok": True, "message": msg, "target": "raw",
                    "raw_mode": APP.raw_mode(), "files": len(files),
                    "mode": APP.launcher.describe()})

    def _api_reveal(self, body) -> None:
        target = str(body.get("path") or "")
        if target:                      # 直接给路径（限素材目录范围内，如解码结果文件夹）
            p = Path(target).expanduser()
            try:
                p = p.resolve()
            except OSError:
                pass
            if not p.exists():
                return self._err("这个路径不存在：%s" % p, 404)
            if APP.lib.root_of_path(p) < 0:
                return self._err("只能定位素材目录里面的东西")
            ok, msg = APP.launcher.reveal(p)
            if not ok:
                return self._err(msg, 500)
            return self._json({"ok": True, "message": msg})
        iid = body.get("id")
        if not iid:
            return self._err("缺少 id")
        with APP.lib.lock:
            it = APP.lib.items.get(iid)
        if it and it.get("remote"):
            local = APP.lib.cache_dir / "remote-open" / str(it["remote"])
            if local.is_dir():
                ok, msg = APP.launcher.reveal(local)
                if ok:
                    return self._json({"ok": True,
                                       "message": "远程文件在本机的缓存目录（%s）" % local})
            return self._err("这是远程服务器上的文件，本机没有实体路径；"
                             "可以用「📥 下载到本机并打开」先存下来")
        path, _, _ = APP.lib.resolve_id(iid)
        if not path.exists():
            return self._err("文件不存在", 404)
        ok, msg = APP.launcher.reveal(path)
        if not ok:
            return self._err(msg, 500)
        self._json({"ok": True, "message": msg})

    # -- 目录浏览 / 移动 / 下载 ---------------------------------------------
    def _api_browse(self, qs, body) -> None:
        """给「移动到…」弹窗列子文件夹（只允许已加入目录范围内的路径）。"""
        path = (body.get("path") if body else "") or qs("path")
        try:
            self._json({"ok": True, **APP.lib.folder_list(path)})
        except ValueError as e:
            self._err(str(e))

    def _api_play(self, qs, body) -> None:
        """边转码边播：浏览器解不了的编码（HEVC / H.265 之类）在服务器上实时转成 H.264。

        局域网客户端点「在浏览器中播放」时最需要它 —— 既不弹本机播放器，
        也不用先把几十 GB 的原片下下来。用 ?t=秒 可以从中间开始（进度条拖不动，
        因为边转边发没有文件长度，想拖就用「⬇ 下载原件」）。
        """
        iid = (body.get("id") if body else "") or qs("id")
        if not iid:
            return self._err("缺少 id")
        try:
            path, _root, _rel = APP.lib.resolve_id(iid)
        except KeyError:
            return self._err("找不到条目", 404)
        if not path.exists():
            return self._err("文件不存在", 404)
        try:
            start = max(0.0, float(qs("t") or 0))
        except ValueError:
            start = 0.0
        if not FFMPEG:
            return self._err("服务器上没有 ffmpeg，转不了码；请用「⬇ 下载原件」", 500)

        with APP.play_lock:
            if APP.play_now >= APP.play_max:
                return self._err("服务器正在给别人转码，稍等一下再试（或直接下载原件）", 503)
            APP.play_now += 1
        proc = None
        try:
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
            if start > 0:
                cmd += ["-ss", "%.2f" % start]
            cmd += [
                "-i", str(path),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-sn", "-dn",
                "-vf", "scale='min(1280,iw)':-2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-pix_fmt", "yuv420p", "-maxrate", "4M", "-bufsize", "8M",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4", "pipe:1",
            ]
            log("转码播放 %s（从 %.1fs 开始）" % (path.name, start))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    bufsize=0)
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            sent = 0
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("转码播放出错：%s" % e)
        finally:
            if proc is not None:
                try:
                    proc.stdout.close()
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    err = proc.stderr.read(2000).decode("utf-8", "replace").strip()
                    if err:
                        # 客户端关掉播放时 ffmpeg 必然会报 Broken pipe / trailer，
                        # 以及某些素材 duration 为未知的固有警告 —— 这些不用刷屏
                        noise = ("Broken pipe", "Error writing trailer",
                                 "out of range for mov/mp4")
                        lines = [ln for ln in err.splitlines()
                                 if ln.strip() and not any(n in ln for n in noise)]
                        if lines:
                            log("ffmpeg：%s" % lines[-1][:200])
                    proc.stderr.close()
                except Exception:
                    pass
            with APP.play_lock:
                APP.play_now = max(0, APP.play_now - 1)

    def _api_decode(self, body) -> None:
        """批量解码 RAW：给选中的条目（或全库）把 .RAF 解成全尺寸 JPEG。

        解出来的图放在 **RAW 同级目录**的「RAW解码」文件夹里（可用 --raw-out 改名），
        已经有结果的不会重复解；返回一个后台任务，进度用 /api/job 看。
        """
        ids = [str(x) for x in (body.get("ids") or [])]
        scope = str(body.get("scope") or ("ids" if ids else "all"))
        force = bool(body.get("force"))
        with APP.lib.lock:
            items = [x for x in APP.lib.items.values() if not x.get("remote")]
        jobs = []                                   # [(条目 id, RAW 路径, 显示名)]
        seen = set()
        for it in items:
            if scope == "ids" and it["id"] not in ids:
                continue
            raf = APP.raw_path_of(it)
            if raf is None:
                continue
            key = str(raf)
            if key in seen:
                continue
            seen.add(key)
            jobs.append((it["id"], raf, it["name"]))
        if not jobs:
            return self._err("选中的里面没有带 RAW 的照片")
        already = 0
        if not force:
            rest = []
            for j in jobs:
                dst = APP.lib.raw_cache_path(j[1], "full")
                if dst.exists() and dst.stat().st_size > 0:
                    already += 1
                else:
                    rest.append(j)
            jobs = rest
        out_dir = str(APP.lib.raw_out_dir(jobs[0][1])) if jobs \
            else str(APP.lib.raw_out_dir(Path(items[0]["path"]))) if items else ""
        jid = APP.start_job("decode", len(jobs), note="解码 %d 张 RAW" % len(jobs))
        APP.job_update(jid, already=already)
        self._json({"ok": True, "job": jid, "total": len(jobs), "already": already,
                    "out_dir": out_dir})

        def work():
            done = ok_n = fail_n = 0
            errs = []
            for _iid, raf, name in jobs:
                APP.job_update(jid, done=done, current=name)
                mode, path, note = APP.prepare_raw_view(raf)
                done += 1
                if mode in ("decoded", "app") and path:
                    ok_n += 1
                elif mode == "preview":
                    errs.append("%s：只能出内嵌预览（%s）" % (name, note))
                    fail_n += 1
                else:
                    errs.append("%s：%s" % (name, note))
                    fail_n += 1
                APP.job_update(jid, done=done)
            msg = "解码完成：%d 张" % ok_n
            if already:
                msg += "（另有 %d 张已有结果，跳过）" % already
            if fail_n:
                msg += "，%d 张失败" % fail_n
            APP.job_update(jid, state="done", done=done, message=msg, finished=now(),
                           result={"ok": ok_n, "failed": fail_n, "already": already,
                                   "errors": errs[:20], "out_dir": out_dir})

        threading.Thread(target=work, daemon=True, name="raw-decode").start()

    def _api_mkdir(self, body) -> None:
        """在某个已有的素材目录里新建一个文件夹（分类用），只建目录不搬文件。"""
        base = str(body.get("path") or "").strip()
        name = str(body.get("name") or "").strip()
        if not name:
            return self._err("给新文件夹起个名字")
        if "/" in name or "\\" in name or name in (".", ".."):
            return self._err("名字里不能有 / 或 ..")
        lib = APP.lib
        if not base:
            return self._err("先选一个位置")
        newp = Path(base).expanduser() / name
        if lib.root_of_path(newp) < 0:
            return self._err("只能建在已加入的素材目录里面：%s" % base)
        note = write_problem(newp)                 # 只读盘 / 没权限先拦下来，别抛 errno
        if note:
            return self._err("在这儿建不了文件夹：%s" % note)
        try:
            if newp.exists():
                return self._err("已经有同名文件夹了：%s" % name)
            newp.mkdir(parents=True)
        except OSError as e:
            return self._err("新建失败：%s" % os_error_text(e, newp))
        self._json({"ok": True, "path": str(newp), "name": name,
                    "message": "已新建文件夹 %s" % name})

    def _api_rename(self, body) -> None:
        """重命名（本机执行）。RAW 配对与 .xmp/.dop 这类 sidecar 会跟着一起改。

        单张：{"id": "...", "name": "新名字"}
        批量：{"ids": [...], "find": …, "replace": …, "prefix": …, "suffix": …,
               "seq": true, "start": 1, "digits": 3}
        预览：加 "dry_run": true —— 只算新旧名字和冲突，一个字都不落盘。
        """
        ids = [str(x) for x in (body.get("ids") or ([body["id"]] if body.get("id") else []))]
        ids = [i for i in ids if i]
        if not ids:
            return self._err("先选一个（或几个）文件再改名")
        spec = {k: body.get(k) for k in
                ("name", "find", "replace", "prefix", "suffix",
                 "seq", "start", "digits", "seq_sep")}
        if not any(v not in (None, "") for v in spec.values()):
            return self._err("没有给出新的文件名规则")
        dry = bool(body.get("dry_run"))
        on_conflict = str(body.get("on_conflict") or "rename").lower()
        if on_conflict not in ("rename", "skip", "overwrite"):
            on_conflict = "rename"
        # 只读盘（macOS 上的 NTFS 外接盘就是这样）提前说清楚，别等用户点了「改名」才报 errno
        blocked = ""
        dirs: set = set()
        with APP.lib.lock:
            for i in ids:
                it = APP.lib.items.get(i) or {}
                if it.get("path"):
                    dirs.add(str(Path(it["path"]).parent))
        for d in sorted(dirs):
            blocked = write_problem(d)
            if blocked:
                break
        if blocked:
            return self._err("改不了名：%s" % blocked)
        try:
            res = APP.lib.rename_items(ids, spec, on_conflict=on_conflict, dry_run=dry)
        except Exception as e:
            return self._err("改名失败：%s" % os_error_text(e))

        rows = res.get("results") or []
        if dry:
            conflicts = [r for r in rows if r.get("conflict")]
            bad = [r for r in rows if r.get("status") == "error"]
            return self._json({"ok": True, "dry_run": True, "results": rows,
                               "ok_count": res["ok_count"], "skipped": res["skipped"],
                               "failed": res["failed"], "total": res["total"],
                               "conflicts": len(conflicts),
                               "message": "预览：%d 个会改名%s%s"
                                          % (res["ok_count"],
                                             "，%d 个目标重名会另行处理" % len(conflicts) if conflicts else "",
                                             "，%d 个不能改" % len(bad) if bad else "")})

        ok = res["ok_count"]
        skipped, failed = res["skipped"], res["failed"]
        parts = []
        if ok:
            names = [r.get("to") or r.get("old") for r in rows if r.get("status") == "done"]
            if len(names) == 1:
                parts.append("已改名为 %s" % names[0])
            else:
                parts.append("已改名 %d 个" % ok)
        if skipped:
            parts.append("跳过 %d 个（目标重名）" % skipped)
        if failed:
            first = next((r for r in rows if r.get("status") == "error"), None)
            parts.append("失败 %d 个%s" % (failed, "：" + str(first.get("detail")) if first else ""))
        msg = "；".join(parts) or "没有需要改的"
        new_id = next((r.get("new_id") for r in rows if r.get("status") == "done"), None)
        self._json({"ok": True, "dry_run": False, "results": rows, "message": msg,
                    "ok_count": ok, "skipped": skipped, "failed": failed,
                    "total": res["total"], "new_id": new_id,
                    "renamed": [{"from": r.get("old"), "to": r.get("to"),
                                 "files": r.get("files")} for r in rows
                                if r.get("status") == "done"]})

    def _api_move(self, body) -> None:
        """把选中的文件移到某个文件夹（本机执行），可选新建文件夹。"""
        ids = [str(x) for x in (body.get("ids") or ([body["id"]] if body.get("id") else []))]
        if not ids:
            return self._err("先选几个文件再移动")
        with APP.lib.lock:
            rids = [i for i in ids
                    if (APP.lib.items.get(i) or {}).get("remote")]
        if rids:
            return self._err("选中的里面有远程服务器上的文件（%d 个），"
                             "移动只在本地目录里做：先把它们下载到本机再整理"
                             % len(rids))
        dest = str(body.get("dest") or "").strip()
        new_folder = str(body.get("new_folder") or "").strip()
        on_conflict = str(body.get("on_conflict") or "rename").lower()
        if on_conflict not in ("rename", "skip", "overwrite"):
            on_conflict = "rename"
        try:
            if new_folder:
                if "/" in new_folder or new_folder in (".", ".."):
                    return self._err("新建文件夹的名字不能带 / 或 ..")
                base = dest or str(APP.lib.roots[0] if APP.lib.roots else "")
                if not base:
                    return self._err("没有可用的目标目录")
                dest = str(Path(base).expanduser() / new_folder)
            if not dest:
                return self._err("请选择目标文件夹")
            note = write_problem(dest)             # 只读盘 / 没权限：直接告诉用户为什么
            if note:
                return self._err("移不过去：%s" % note)
            # 源头也得能写：只读盘上的文件搬走要「删源文件」，一样会被系统挡回来
            src_dirs: dict = {}
            with APP.lib.lock:
                for i in ids:
                    it = APP.lib.items.get(i) or {}
                    if it.get("path"):
                        src_dirs.setdefault(str(Path(it["path"]).parent), it["path"])
            for d in sorted(src_dirs):
                note = write_problem(d)
                if note:
                    return self._err("搬不走：%s" % note)
            jid = APP.start_job("move", len(ids), note="移动到 %s" % dest)
        except ValueError as e:
            return self._err(str(e))
        self._json({"ok": True, "job": jid, "total": len(ids), "dest": dest})

        def work():
            def prog(done, total, cur):
                APP.job_update(jid, done=done, total=total, current=cur or "")
            try:
                res = APP.lib.move_items(ids, dest, on_conflict=on_conflict,
                                         mkdir=bool(new_folder), progress=prog)
                msg = "已移动 %d 个文件到 %s" % (res["moved"], res["dest"])
                if res["renamed"]:
                    msg += "；%d 个重名已自动改名" % len(res["renamed"])
                if res["skipped"]:
                    msg += "；跳过 %d 个" % len(res["skipped"])
                if res.get("missing"):
                    msg += ("；%d 个在列表里找不到了（多半是刚才已经被移动/删除，"
                            "刷新一下列表就好）" % len(res["missing"]))
                if res["errors"]:
                    msg += "；%d 个失败" % len(res["errors"])
                APP.job_update(jid, state="done", done=res["total"], message=msg,
                               result=res, finished=now())
            except Exception as e:
                APP.job_update(jid, state="error", message=str(e), finished=now())
            finally:
                try:
                    APP.lib.scan()
                    APP.lib.start_meta_pass()
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True, name="move").start()

    def _api_job(self, qs) -> None:
        j = APP.job_get(qs("id") or "")
        if not j:
            return self._err("没有这个任务", 404)
        self._json({"ok": True, **j})

    def _api_download(self, body) -> None:
        """批量下载：多选打成一个 zip，单个就直接下原文件（远程也能用）。"""
        ids = [str(x) for x in (body.get("ids") or ([body["id"]] if body.get("id") else []))]
        if not ids:
            return self._err("先选几个文件再下载")
        entries: list[dict] = []
        for iid in ids[:600]:
            with APP.lib.lock:
                it = APP.lib.items.get(iid)
            if not it:
                continue
            if it.get("remote"):                    # 远程源上的文件：打包时现场拉
                rr = APP.lib.remote_by_key(str(it["remote"]))
                if rr is None:
                    continue
                entries.append({"remote": rr.key, "url": rr.url,
                                "rid": it.get("remote_id"), "name": str(it.get("name") or ""),
                                "rel": str(it.get("rel") or it.get("name") or ""),
                                "size": int(it.get("size") or 0)})
                continue
            p = Path(it["path"])
            if not p.exists():
                continue
            entries.append({"path": str(p), "name": p.name,
                            "rel": str(it.get("rel") or p.name),
                            "size": p.stat().st_size})
        if not entries:
            return self._err("这些文件都不在了")
        if len(entries) == 1:
            e = entries[0]
            if e.get("url"):                        # 单个远程文件：代理直下（保原文件名）
                return self._json({"ok": True, "count": 1,
                                   "url": "/api/media?id=%s&dl=1" % urllib.parse.quote(ids[0]),
                                   "name": e["name"], "zip": False, "size": e["size"]})
            return self._json({"ok": True, "count": 1,
                               "url": "/api/media?id=%s&dl=1" % urllib.parse.quote(ids[0]),
                               "name": e["name"], "zip": False, "size": e["size"]})
        # 压缩包内部用「目录名/文件名」当路径，不同目录里的同名文件不会互相覆盖
        rels = []
        for e in entries:
            if e.get("url"):                        # 远程：用对方给的相对路径
                rels.append(re.sub(r"^[^:]+:", "", str(e["rel"])))
                continue
            p = Path(e["path"])
            ri = APP.lib.root_of_path(p.parent)
            prefix = APP.lib.roots[ri].name if ri >= 0 else ""
            rels.append(str(Path(prefix) / p.name) if prefix else p.name)
        used: dict[str, int] = {}
        out_rels = []
        for r in rels:
            if r in used:
                used[r] += 1
                rp = Path(r)
                r = str(rp.with_name("%s-%d%s" % (rp.stem, used[r] + 1, rp.suffix)))
            else:
                used[r] = 0
            out_rels.append(r)
        stamp = time.strftime("%m%d-%H%M%S")
        name = "媒体-%d个-%s.zip" % (len(entries), stamp)
        tid = APP.new_ticket([e.get("path") or e.get("url") for e in entries], name)
        with APP.tickets_lock:
            APP.tickets[tid]["rels"] = out_rels
            APP.tickets[tid]["entries"] = entries
        total = sum(int(e.get("size") or 0) for e in entries)
        self._json({"ok": True, "count": len(entries), "url": "/api/zip?ticket=%s" % tid,
                    "name": name, "zip": True, "size": total})

    def _api_zip(self, qs) -> None:
        """边读边打的 zip（不落临时文件），浏览器直接下载。"""
        t = APP.get_ticket(qs("ticket") or "")
        if not t:
            return self._err("下载链接已过期，重新点一次下载", 404)
        paths = [Path(p) for p in t["paths"]]
        rels = t.get("rels") or [p.name for p in paths]
        entries = t.get("entries") or []
        fname = urllib.parse.quote(t["name"].encode("utf-8"))
        # 边打边发，事先不知道总长度 —— HTTP/1.1 下只能靠关连接标记结束
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % fname)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                if entries:
                    for e, rel in zip(entries, rels):
                        try:
                            if e.get("url"):        # 远程：边拉边写进压缩包
                                rr = APP.lib.remote_by_key(str(e.get("remote") or "")) \
                                    or RemoteSource(str(e["url"]))
                                with rr.open_stream("/api/media", id=e.get("rid")) as r:
                                    with z.open(rel, "w") as zf:
                                        while True:
                                            chunk = r.read(262144)
                                            if not chunk:
                                                break
                                            zf.write(chunk)
                            else:
                                p = Path(e["path"])
                                if p.exists():
                                    z.write(str(p), arcname=rel)
                        except (BrokenPipeError, ConnectionResetError):
                            raise
                        except Exception as ex:
                            log("打包跳过 %s：%s" % (e.get("name"), ex))
                else:
                    for p, rel in zip(paths, rels):
                        if not p.exists():
                            continue
                        try:
                            z.write(str(p), arcname=rel)
                        except (BrokenPipeError, ConnectionResetError):
                            raise
                        except OSError as e:
                            log("打包跳过 %s：%s" % (p, e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("打包下载出错：%s" % e)

    def _api_clear_cache(self) -> None:
        lib = APP.lib
        n = 0
        raw_n = 0
        try:
            for p in lib.thumb_dir.rglob("*.jpg"):
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
            raw_dir = lib.cache_dir / "raw"
            if raw_dir.is_dir():
                for p in raw_dir.rglob("*.jpg"):
                    try:
                        p.unlink()
                        raw_n += 1
                    except OSError:
                        pass
            with lib._err_lock:
                lib._failed.clear()
        except Exception as e:
            return self._err("清理失败：%s" % e, 500)
        msg = "已清理 %d 张缩略图缓存" % n
        if raw_n:
            msg += "，%d 张 RAW 全尺寸解码结果" % raw_n
        self._json({"ok": True, "message": msg, "removed": n, "raw_removed": raw_n})


# --------------------------------------------------------------------------- #
# 配置 / 启动
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "roots": [],
    "port": 8777,
    "host": "",
    "thumb_px": 480,
    "cache": "",
    "vlc": "",
    "vlc_args": [],
    "image_viewer": "",
    "raw_viewer": "",
    "remote_roots": [],
    "raw_out": "RAW解码",
    "raw_demosaic": "auto",
    "raw_wb": "camera",
    "launch_mode": "auto",
    "recursive": True,
    "pair_raw": True,
    "warmup": "auto",
    "watch": 20,
    "lan": False,
    "token": "",
    "open_browser": True,
}


def load_config(path: Path | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    candidates = []
    if path:
        candidates.append(path)
    else:
        here = Path(__file__).resolve().parent
        candidates += [here / "config.json", Path.cwd() / "media-browser.json"]
    for c in candidates:
        try:
            if c and c.exists():
                data = json.loads(c.read_text("utf-8"))
                if isinstance(data, dict):
                    cfg.update({k: v for k, v in data.items() if v is not None})
                    log("已读取配置：%s" % c)
                    break
        except Exception as e:
            log("配置文件解析失败 %s：%s" % (c, e))
    return cfg


def pick_cache_dir(requested: str, tool_dir: Path) -> Path:
    if requested:
        p = Path(requested).expanduser()
    else:
        p = cache_home() / "media-browser"     # Linux: ~/.cache, mac: ~/Library/Caches, Win: %LOCALAPPDATA%
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_text("x")
        probe.unlink()
        return p
    except Exception:
        alt = tool_dir / ".cache"
        alt.mkdir(parents=True, exist_ok=True)
        log("缓存目录 %s 不可写，改用 %s" % (p, alt))
        return alt


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media_browser.py",
        description="用缩略图墙快速浏览图片/视频，一键调用本机 VLC 播放。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python3 media_browser.py                       # 浏览当前目录
  python3 media_browser.py /mnt/MEDIA/个人生活     # 浏览指定目录
  python3 media_browser.py ~/Pictures ~/Movies -p 9000
  python3 media_browser.py --no-warmup --no-open  # 不预热、不自动开浏览器
""")
    p.add_argument("roots", nargs="*", help="要浏览的目录（默认当前目录）")
    p.add_argument("-p", "--port", type=int, default=None, help="HTTP 端口（默认 8777）")
    p.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，仅本机）")
    p.add_argument("--lan", action="store_true",
                   help="开放给局域网（等于 --host 0.0.0.0），启动时会打印手机能访问的网址")
    p.add_argument("--token", default=None,
                   help="局域网里「打开/播放/改目录」需要的口令；--lan 时不给就随机生成一个")
    p.add_argument("--thumb-px", type=int, default=None, help="缩略图最长边像素（默认 480）")
    p.add_argument("--cache", default=None, help="缩略图缓存目录")
    p.add_argument("--vlc", default=None, help="VLC 可执行文件路径")
    p.add_argument("--image-viewer", default=None,
                   help="看原图用的图片查看器（默认自动找 eog/loupe/gwenview 等）")
    p.add_argument("--raw-viewer", default=None,
                   help="打开 RAW 用的外部程序（如 darktable）；默认用内置的 libraw 解全尺寸")
    p.add_argument("--raw-limit", type=int, default=None,
                   help="一次最多解码几张 RAW（默认 6，每张要几秒；批量解码不受此限）")
    p.add_argument("--remote", action="append", default=None,
                   help="把另一台机器上的媒体浏览器当素材目录：--remote http://主机:端口"
                        "（可重复；带口令写 --remote http://主机:端口 --remote-token 口令）")
    p.add_argument("--remote-token", default=None,
                   help="访问远程服务器用的口令（对应那台的 --token）")
    p.add_argument("--raw-out", default=None,
                   help="RAW 解码结果放哪个文件夹（RAW 同级目录下的名字，默认 RAW解码）")
    p.add_argument("--raw-demosaic", default=None,
                   choices=["auto", "linear", "vng", "ppg", "ahd", "dcb", "amaze"],
                   help="去马赛克算法（默认 auto；dcb 细节更多也更慢。"
                        "amaze 需要额外的 GPL3 解码包，没有时会自动退回 auto）")
    p.add_argument("--raw-wb", default=None,
                   choices=["camera", "auto", "none", "daylight", "shade", "cloudy",
                            "tungsten", "fluorescent", "flash"],
                   help="RAW 白平衡（默认 camera=按相机记录的来）")
    p.add_argument("--vlc-arg", action="append", default=None,
                   help="附加给 VLC 的参数，可多次使用，如 --vlc-arg=--one-instance")
    p.add_argument("--launch-mode", choices=["auto", "direct", "systemd", "xdg"], default=None,
                   help="外部程序启动方式（默认 auto）")
    p.add_argument("--no-recursive", action="store_true", help="只扫描顶层目录")
    p.add_argument("--no-pair-raw", action="store_true", help="RAW 与同名 JPEG 不合并显示")
    p.add_argument("--warmup", choices=["auto", "all", "none"], default=None,
                   help="是否后台预热全部缩略图（默认 auto：条目不多时预热）")
    p.add_argument("--watch", type=int, default=None,
                   help="每隔多少秒检查一次目录树有没有新内容（默认 20，0 表示关掉自动刷新）")
    p.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    p.add_argument("--config", default=None, help="指定 JSON 配置文件")
    p.add_argument("-v", "--verbose", action="store_true", help="打印访问日志")
    p.add_argument("--print-config", action="store_true", help="打印生效配置后退出")
    p.add_argument("--check", action="store_true",
                   help="平台自检：打印这台机器上自动挑到的外部工具（缩略图/抽帧/VLC/RAW 等）后退出")
    return p


def _pick(names, fallback_ok=False):
    for n in names:
        p = find_exe(n)
        if p:
            return p
    return ""


def platform_report(port: int, host: str, cache_dir: Path, tool_dir: Path) -> int:
    """--check：把这台机器上"平台自动适配"的结果摊开给你看。

    同一套代码在 Linux / macOS / Windows 上会自动挑不同的系统工具，这个命令就是
    让你确认"我这台上挑到的是哪个"（部署到新机器上先跑它，最省事）。
    """
    import platform as _pf

    say = print
    say("=" * 68)
    say(" 媒体浏览器 · 平台自检")
    say("=" * 68)
    say("  系统     : %s %s (%s)" % (_pf.system(), _pf.release(), _pf.machine()))
    say("  Python   : %s  (%s)" % (_pf.python_version(), sys.executable))
    say("  程序目录 : %s" % tool_dir)
    say("  缓存目录 : %s" % cache_dir)
    eff_host = host or "127.0.0.1"
    say("  端口     : %d   监听 %s%s" % (port, eff_host,
        "（局域网可看）" if eff_host in ("0.0.0.0", "::") else "（只有本机能看）"))
    say("")

    def w(text: str) -> int:                    # 中文按两格宽算，好对齐
        return sum(2 if ord(c) > 0x2E80 else 1 for c in text)

    def row(what: str, got: str, note: str = "") -> None:
        pad = " " * max(1, 16 - w(what))
        say("  %s%s: %s%s" % (what, pad, got or "（没有，功能会降级）",
                              ("   ← %s" % note) if note else ""))

    # 图片缩略图：macOS 优先 sips（原生认识 HEIC/RAW），其它平台优先 Pillow
    if IS_MAC and SIPS:
        row("图片缩略图", SIPS, "macOS 自带 sips，HEIC/RAW 都能出图")
    elif Image is not None:
        row("图片缩略图", "Pillow %s" % getattr(Image, "__version__", ""), "跨平台主力")
    elif IS_WIN and POWERSHELL:
        row("图片缩略图", POWERSHELL, "Windows 自带 PowerShell + System.Drawing")
    elif MAGICK:
        row("图片缩略图", MAGICK, "ImageMagick")
    else:
        row("图片缩略图", "")
    if IS_MAC and QLMANAGE:
        row("缩略图备用", QLMANAGE, "QuickLook，能出 RAW/视频的首帧")

    row("视频抽帧", FFMPEG, "多帧评分选最好的一帧")
    row("媒体信息", FFPROBE or (shutil.which("mdls") if IS_MAC else ""),
        "macOS 没 ffprobe 时用系统 mdls 读时长/尺寸/编码（macOS 自带，无需安装）")
    row("视频播放", VLC or (mac_video_player() if IS_MAC else ""),
        "macOS 用系统自带播放器，装了 VLC / IINA 会优先用它们；别的机器只能在浏览器里转码看"
        if IS_MAC else "本机用 VLC；别的机器只能在浏览器里转码看")
    row("看图打开", IMAGE_VIEWER,
        "macOS 图片交给系统自带的「预览」，视频交给上面的播放器"
        if IS_MAC else "看原图用它，视频才走 VLC")
    row("文件定位", (MAC_OPEN if IS_MAC else (EXPLORER if IS_WIN else (GDBUS or XDG_OPEN))),
        "在文件管理器里定位文件")
    row("选文件夹", (OSASCRIPT if IS_MAC else (POWERSHELL if IS_WIN else _pick(["zenity", "kdialog"]))),
        "网页里点「弹出文件夹选择框」时用")
    rp = rawpy_module()
    row("RAW 全尺寸解码", ("rawpy %s / libraw %s" % (rp.__version__,
        ".".join(str(x) for x in rp.libraw_version))) if rp else "",
        "没有也能看内嵌预览、认配对；要解全尺寸就跑 安装RAW支持.sh（mac/Windows 用 pip）")
    say("")
    say("  说明：以上都是「自动挑」的结果 —— 缺哪个不影响启动，只是那一项功能降级；")
    say("        要装的话每个平台一条命令，见 README 里对应平台那一节。")
    say("=" * 68)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.config).expanduser() if args.config else None)

    def choose(cli, key):
        return cli if cli not in (None, []) else cfg.get(key)

    # ---- 目录：启动参数 / 配置文件 / 界面添加过的，合并去重 -----------------
    cli_roots_arg = list(args.roots or [])
    cfg_roots_arg = [str(r) for r in (cfg.get("roots") or [])]
    extra_arg = [str(r) for r in (cfg.get("extra_roots") or [])]
    base = cli_roots_arg or cfg_roots_arg
    base_src = "cli" if cli_roots_arg else ("config" if cfg_roots_arg else "cli")
    if not base:
        base = [os.getcwd()]

    def norm(r: str) -> Path | None:
        p = Path(r).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p)
        if not p.exists():
            log("目录不存在，已忽略：%s" % p)
            return None
        if p.is_file():
            p = p.parent
        try:
            return p.resolve()
        except OSError:
            return p

    roots: list[Path] = []
    root_sources: list[str] = []
    for r, src in [(r, base_src) for r in base] + [(r, "extra") for r in extra_arg]:
        p = norm(r)
        if p is None or p in roots:
            continue
        roots.append(p)
        root_sources.append(src)
    if not roots:
        log("没有任何有效目录")
        return 2

    tool_dir = Path(__file__).resolve().parent
    # 界面里添加的目录要写回配置文件，先确定写哪个文件
    if args.config:
        cfg_path = Path(args.config).expanduser()
    elif (tool_dir / "config.json").exists():
        cfg_path = tool_dir / "config.json"
    elif (Path.cwd() / "media-browser.json").exists():
        cfg_path = Path.cwd() / "media-browser.json"
    else:
        cfg_path = tool_dir / "config.json"
    cache_dir = pick_cache_dir(choose(args.cache, "cache") or "", tool_dir)
    if getattr(args, "check", False):
        return platform_report(int(choose(args.port, "port") or 8777),
                              choose(args.host, "host") or "",
                              cache_dir, tool_dir)
    thumb_px = int(choose(args.thumb_px, "thumb_px") or 480)
    recursive = (not args.no_recursive) and bool(cfg.get("recursive", True))
    pair_raw = (not args.no_pair_raw) and bool(cfg.get("pair_raw", True))
    warmup = choose(args.warmup, "warmup") or "auto"
    watch = choose(getattr(args, "watch", None), "watch")
    watch = 20 if watch is None else int(watch)
    vlc_args = args.vlc_arg if args.vlc_arg else list(cfg.get("vlc_args") or [])
    launch_mode = choose(args.launch_mode, "launch_mode") or "auto"
    port = int(choose(args.port, "port") or 8777)
    lan = bool(args.lan) or bool(cfg.get("lan")) or \
        str(os.environ.get("MEDIA_BROWSER_LAN", "")).lower() in ("1", "true", "yes")
    host = choose(args.host, "host") or ("0.0.0.0" if lan else "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        lan = True
    token = str(choose(getattr(args, "token", None), "token") or "").strip()
    if lan and not token:
        token = hashlib.sha1(os.urandom(16)).hexdigest()[:10]
    open_browser = (not args.no_open) and bool(cfg.get("open_browser", True))

    global VLC, IMAGE_VIEWER, IMAGE_VIEWER_NAME
    vlc_cfg = choose(args.vlc, "vlc")
    if vlc_cfg:
        VLC = vlc_cfg if os.path.exists(vlc_cfg) else find_exe(vlc_cfg)
    raw_cfg = choose(getattr(args, "raw_viewer", None), "raw_viewer")
    RAW_APP = None
    if raw_cfg:
        RAW_APP = raw_cfg if os.path.exists(raw_cfg) else find_exe(raw_cfg)
        log("RAW 外部程序：%s" % (RAW_APP or "没找到，改用内置解码"))
    raw_limit = int(choose(getattr(args, "raw_limit", None), "raw_limit") or 6)
    # RAW 解码设置：解码方式不同，出来的照片确实不一样（见 README「RAW 解码」一节）
    global RAW_OUT_NAME, RAW_DEMOSAIC, RAW_WB
    RAW_OUT_NAME = str(choose(getattr(args, "raw_out", None), "raw_out") or "RAW解码").strip() \
        or "RAW解码"
    if "/" in RAW_OUT_NAME or RAW_OUT_NAME in (".", ".."):
        print("  --raw-out 只能是文件夹名字，不能带 /，用默认的 RAW解码")
        RAW_OUT_NAME = "RAW解码"
    SKIP_DIR_NAMES.add(RAW_OUT_NAME)
    RAW_DEMOSAIC = str(choose(getattr(args, "raw_demosaic", None), "raw_demosaic")
                       or "auto").strip().lower()
    if RAW_DEMOSAIC not in ("", "auto"):
        avail = available_demosaic()
        if avail and RAW_DEMOSAIC not in avail:
            print("  --raw-demosaic %s 在这台机器上不可用（可用：%s，"
                  "amaze 需要额外装 GPL3 解码包），改用 auto"
                  % (RAW_DEMOSAIC, "/".join(avail)))
            RAW_DEMOSAIC = "auto"
    RAW_WB = str(choose(getattr(args, "raw_wb", None), "raw_wb") or "camera").strip().lower()
    viewer_cfg = choose(getattr(args, "image_viewer", None), "image_viewer")
    if viewer_cfg:
        IMAGE_VIEWER = viewer_cfg if os.path.exists(viewer_cfg) else find_exe(viewer_cfg)
        IMAGE_VIEWER_NAME = Path(IMAGE_VIEWER).name if IMAGE_VIEWER else ""

    if args.print_config:
        print(json.dumps({
            "roots": [str(r) for r in roots], "root_sources": root_sources,
            "remote_roots": remote_list,
            "extra_roots": extra_arg, "config": str(cfg_path),
            "port": port, "host": host, "lan": lan,
            "token": token if lan else "",
            "thumb_px": thumb_px, "cache": str(cache_dir), "recursive": recursive,
            "pair_raw": pair_raw, "warmup": warmup, "watch": watch, "vlc": VLC,
            "vlc_args": vlc_args, "launch_mode": launch_mode,
            "image_viewer": IMAGE_VIEWER,
            "raw_app": RAW_APP or find_raw_app(),
            "raw_decode": bool(rawpy_module()), "raw_limit": raw_limit,
            "raw_settings": raw_settings(),
            "ffmpeg": FFMPEG, "ffprobe": FFPROBE, "pillow": Image is not None,
        }, ensure_ascii=False, indent=2))
        return 0

    # 远程源：命令行 --remote + 配置文件 remote_roots（界面里加的记在这里）
    remote_list: list[dict] = []
    remote_token = str(choose(getattr(args, "remote_token", None), "remote_token") or "")
    for item in (cfg.get("remote_roots") or []):
        if isinstance(item, str):
            item = {"url": item}
        if isinstance(item, dict) and item.get("url"):
            remote_list.append({"url": str(item["url"]),
                                "name": str(item.get("name") or ""),
                                "token": str(item.get("token") or remote_token)})
    for u in (getattr(args, "remote", None) or []):
        remote_list.append({"url": str(u), "name": "", "token": remote_token})

    lib = Library(roots, cache_dir, thumb_px=thumb_px, pair_raw=pair_raw,
                  recursive=recursive, sources=root_sources, remotes=remote_list)
    launcher = Launcher(launch_mode)
    ui_path = tool_dir / "ui.html"
    app = App(lib, launcher, ui_path, verbose=args.verbose, vlc_args=vlc_args,
              image_viewer=IMAGE_VIEWER, cfg_path=cfg_path,
              raw_app=RAW_APP, raw_limit=raw_limit, token=token, lan=lan,
              cfg_roots=cfg_roots_arg if base_src == "config" else None,
              extra_roots=extra_arg,
              cli_roots=[str(p) for p, s in zip(roots, root_sources) if s == "cli"])
    global APP
    APP = app

    print("=" * 68)
    print("  媒体浏览器 Media Browser v%s" % VERSION)
    print("=" * 68)
    src_label = {"cli": "启动参数", "config": "配置文件", "extra": "界面添加"}
    for p, s in zip(roots, root_sources):
        print("  浏览目录 : %s（%s）" % (p, src_label.get(s, s)))
    for rr in lib.remotes:
        print("  远程目录 : %s（%s）" % (rr.url, rr.name))
    print("  目录配置 : %s（界面里加的目录记在这里）" % cfg_path)
    print("  缩略图   : %s（最长边 %dpx）" % (cache_dir, thumb_px))
    if IS_MAC and SIPS:
        eng = "macOS 自带 sips（原生支持 HEIC / RAW）"
        if Image is not None:
            eng += " + Pillow %s" % getattr(Image, "__version__", "")
    elif Image is not None:
        eng = "Pillow %s" % getattr(Image, "__version__", "")
    elif MAGICK:
        eng = "ImageMagick（%s）" % Path(MAGICK).name
    elif IS_WIN and POWERSHELL:
        eng = "Windows 自带 .NET（没装 Pillow，建议 pip install Pillow）"
    else:
        eng = "没找到可用的图片引擎 —— 建议装 Pillow"
    print("  图片引擎 : %s" % eng)
    print("  视频引擎 : %s" % (FFMPEG or ("macOS 自带 QuickLook（qlmanage）"
                                        if QLMANAGE else "未找到 ffmpeg（视频缩略图不可用）")))
    print("  VLC      : %s（视频）" % (VLC or "未找到！"))
    print("  看图工具 : %s（图片看原图，可用 --image-viewer 改）"
          % (IMAGE_VIEWER or "未找到！"))
    if rawpy_module() is not None:
        print("  RAW 原图 : 已启用 libraw 全尺寸解码（图片条目可「看 RAW 原图」）")
        print("  解码设置 : %s" % raw_settings_text())
        print("  解码结果 : 存到每张 RAW 同级目录的「%s」文件夹（扫描时自动跳过）"
              % RAW_OUT_NAME)
    elif RAW_APP or find_raw_app():
        print("  RAW 原图 : 交给 %s 打开" % Path(RAW_APP or find_raw_app()).name)
    else:
        print("  RAW 原图 : 未启用（只能看内嵌预览）—— 跑 ./安装RAW支持.sh 可解全尺寸")
    print("  启动方式 : %s" % launcher.describe())
    print("  自动刷新 : %s" % ("每 %d 秒检查一次目录变化" % watch if watch > 0 else "已关闭（--watch 0）"))
    print("-" * 68)

    t0 = now()
    lib.scan()
    lib.start_meta_pass()
    total = lib.stats()["total"]
    if warmup == "all" or (warmup == "auto" and total <= 1500):
        lib.start_warmup()
    if watch > 0:
        lib.start_watch(watch)

    handler = Handler
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        log("无法监听 %s:%d —— %s" % (host, port, e))
        log("换成别的端口试试：--port 8899")
        return 1
    httpd.daemon_threads = True
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    ips = lan_ips() if lan else []
    print("  扫描用时 : %.2f 秒，%d 个媒体文件" % (now() - t0, total))
    print("  打开地址 : %s" % url)
    if lan:
        if ips:
            print("  局域网   : http://%s:%d/   ← 手机 / 别的电脑直接开这个" % (ips[0], port))
            for extra_ip in ips[1:]:
                print("             http://%s:%d/" % (extra_ip, port))
        else:
            print("  局域网   : 没检测到局域网 IP，用 `ip addr` 自己看一下")
        print("  别的机器 : 能浏览 / 搜索 / 在浏览器里播放（服务器实时转码）/ 批量下载原图原视频")
        print("             不能在别的机器上弹本机程序（VLC、看图工具）或移动文件 ——")
        print("             这些动作的落点是你这台电脑的桌面。")
        print("  只读模式 : 想让别的机器「改目录 / 重新扫描」，用带口令的网址（口令 %s）：" % token)
        if ips:
            print("             http://%s:%d/?t=%s"
                  % (ips[0], port, urllib.parse.quote(token, safe="")))
        if IS_MAC:
            print("  防火墙   : macOS 若开着防火墙，系统设置 → 网络 → 防火墙 → 选项里")
            print("             允许 Python 接受传入连接；首次运行时系统也会弹窗问一次。")
        elif IS_WIN:
            print("  防火墙   : Windows 首次运行会弹「是否允许 Python 访问网络」，点允许；")
            print("             手动放行： netsh advfirewall firewall add rule name=MediaBrowser ^")
            print("                       dir=in action=allow protocol=TCP localport=%d" % port)
        else:
            if ufw_enabled():
                print("  防火墙   : 检测到 ufw 开着 —— 别的机器连不上就先执行：")
            else:
                print("  防火墙   : 别的机器连不上时先看一眼：sudo ufw status，要放行就执行：")
            print("             sudo ufw allow %d/tcp" % port)
            print("             （只想给自己网段开：sudo ufw allow from 192.168.0.0/16 to any port %d proto tcp）" % port)
    else:
        print("  局域网   : 未开放（只监听本机）。要给手机/别的电脑看：./启动.sh --lan")
    print("  停止服务 : Ctrl+C")
    print("=" * 68)
    sys.stdout.flush()

    if open_browser:
        threading.Timer(0.6, lambda: launcher.open_url(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        lib._save_meta_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
