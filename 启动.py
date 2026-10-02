#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体浏览器 · 跨平台启动器（Linux / macOS / Windows 同一套逻辑）

用法（三种平台都一样，参数也一致）：

    ./启动.sh                      # Linux / macOS 命令行
    双击 启动.command               # macOS：访达里双击就能起
    双击 启动.bat                   # Windows
    python3 启动.py [目录...] [选项]

    ./启动.sh --lan                # 顺便开放局域网（手机 / 别的电脑能看，只读）
    ./启动.sh ~/Pictures --lan     # 指定目录 + 局域网
    python3 启动.py --help         # 看全部选项
    ./启动.sh --check              # 平台自检：看这台机器自动挑到了哪些系统工具

真正干活的还是 media_browser.py；这个文件只负责：
  ① 找到能用的 Python ② 判断端口上是不是已经有实例在跑 ③ 打印本机 / 局域网地址
  ④ 检查几个可选工具（ffmpeg / VLC / Pillow）并给出各平台的安装建议
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "media_browser.py"

# 不带目录启动时去哪儿找素材：按顺序取**真实存在**的那几个 ——
# 外接盘/移动盘没挂上就自动跳过，不会像写死一个路径那样直接报「目录不存在」。
DEFAULT_DIRS = {
    "linux": ["/mnt/MEDIA/个人生活", "/mnt/MEDIA",
              "/mnt/XCS_DATA/生活记录", str(Path.home() / "图片"), str(Path.home() / "Pictures")],
    "darwin": [str(Path.home() / "Pictures"), str(Path.home() / "Movies")],
    "win": [str(Path.home() / "Pictures"), str(Path.home() / "Videos")],
}


def default_dirs(limit: int = 3) -> list:
    """这台机器上真实存在、而且**里面确实有东西**的默认素材目录。

    空的目录（比如挂载点还在、盘没挂上）会被跳过 —— 免得一上来就是个空墙。
    """
    key = "win" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
    cands = DEFAULT_DIRS.get(key, [])
    found: list[str] = []

    def usable(p: Path) -> bool:
        try:
            if not p.is_dir():
                return False
            next(p.iterdir())          # 空的就跳过（挂载点没挂上通常是空的）
            return True
        except StopIteration:
            return False
        except OSError:
            return False

    for d in cands:
        p = Path(d).expanduser()
        sp = str(p).rstrip("/")
        # 已经选过它、或者跟已选中的目录有上下层关系 → 跳过，
        # 免得同一个文件在墙上出现两遍（/mnt/MEDIA 和 /mnt/MEDIA/个人生活 就是这种）
        if sp in found or any(sp.startswith(f + "/") for f in found) \
                or any(f.startswith(sp + "/") for f in found):
            continue
        if usable(p):
            found.append(sp)
        if len(found) >= limit:
            break
    return found or [str(Path.home())]


DEFAULT_DIR = default_dirs()[0]

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
PLATFORM = "Windows" if IS_WIN else ("macOS" if IS_MAC else "Linux")

# 需要单独跟一个值的选项（加新选项时记得也加到这里）
VALUED_OPTS = {
    "-p", "--port", "--host", "--token", "--thumb-px", "--cache", "--vlc",
    "--image-viewer", "--raw-viewer", "--raw-limit", "--raw-out", "--raw-demosaic",
    "--raw-wb", "--vlc-arg", "--launch-mode", "--warmup", "--watch", "--config",
}
FLAG_OPTS = {
    "--lan", "--no-recursive", "--no-pair-raw", "--no-open", "-v", "--verbose",
    "--print-config", "--check", "-h", "--help",
}


def _win_console() -> None:
    """Windows 双击 .bat 时的小照顾：中文别乱码、窗口标题写清楚。"""
    if not IS_WIN:
        return
    try:                                   # 输出被重定向（日志）时用 UTF-8，不然中文会抛异常
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("媒体浏览器 media_browser")
    except Exception:
        pass


def say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:              # 个别老控制台
        print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def lan_ips() -> list[str]:
    """本机在局域网里的 IPv4 地址（优先问默认路由出口）。"""
    ips: list[str] = []

    def ok(ip: str) -> bool:
        return bool(ip) and not ip.startswith("127.") and not ip.startswith("169.254.") \
            and not ip.startswith("172.17.") and ip != "0.0.0.0"

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ok(ip):
            ips.append(ip)
    except Exception:
        pass
    if IS_MAC and not ips:                      # macOS：直接问 Wi-Fi 接口
        try:
            out = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True,
                                 text=True, timeout=4).stdout.strip()
            if ok(out):
                ips.append(out)
        except Exception:
            pass
    if not ips:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ok(ip) and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
    return ips


def server_alive(port: int, host: str = "127.0.0.1") -> bool:
    """端口上跑的是不是本工具（用服务自己的 /api/ping 探，避免误判别的程序）。"""
    try:
        with urllib.request.urlopen("http://%s:%d/api/ping" % (host, port), timeout=1.2) as r:
            return r.status == 200 and b"version" in r.read(400)
    except Exception:
        return False


def open_browser(url: str) -> None:
    try:
        if IS_MAC:
            subprocess.Popen(["open", url])
        elif IS_WIN:
            os.startfile(url)              # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        say("  （没能自动打开浏览器，手动复制上面的地址到浏览器里打开）")


def tool_report() -> list[str]:
    """检查可选外部工具，缺什么就给这个平台的安装建议。"""
    def has(name: str, extra_paths: tuple[str, ...] = ()) -> str:
        from shutil import which
        p = which(name)
        if p:
            return p
        for cand in extra_paths:
            if Path(cand).exists():
                return cand
        return ""

    lines = []
    ff = has("ffmpeg", ("/opt/homebrew/bin/ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe"))
    vlc = has("vlc", ("/Applications/VLC.app/Contents/MacOS/VLC",
                      r"C:\Program Files\VideoLAN\VLC\vlc.exe"))
    if IS_MAC:
        vlc = vlc or ("/Applications/VLC.app" if Path("/Applications/VLC.app").exists() else "")
    try:
        import PIL  # noqa: F401
        pil = "Pillow " + getattr(PIL, "__version__", "")
    except Exception:
        pil = ""
    lines.append("  图片引擎 : %s" % (pil or "系统自带工具（macOS 用 sips，Windows 用 .NET）"))
    lines.append("  视频引擎 : %s" % (ff or "没装 —— 视频缩略图会用系统工具 / 直接留空"))
    lines.append("  VLC      : %s" % (vlc or "没装"))
    if not ff:
        hint = {
            "macOS": "brew install ffmpeg",
            "Windows": "winget install Gyan.FFmpeg  或去 ffmpeg.org 下 zip 解到 C:\\ffmpeg",
        }.get(PLATFORM, "sudo apt install ffmpeg")
        lines.append("             （想要准确的视频时长/抽帧：%s）" % hint)
    if not vlc:
        hint = {
            "macOS": "brew install --cask vlc",
            "Windows": "winget install VideoLAN.VLC",
        }.get(PLATFORM, "sudo apt install vlc")
        lines.append("             （要能用 VLC 播放：%s）" % hint)
    if not pil:
        hint = {
            "macOS": "python3 -m pip install --user Pillow",
            "Windows": "py -3 -m pip install Pillow",
        }.get(PLATFORM, "sudo apt install python3-pil")
        lines.append("             （想要更快的缩略图：%s）" % hint)
    return lines


def port_busy(port: int) -> bool:
    """端口有人在听，但不是本工具。"""
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sk:
        sk.settimeout(0.6)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str]) -> int:
    _win_console()
    args = list(argv)
    port = int(os.environ.get("MEDIA_BROWSER_PORT") or 8777)
    lan = str(os.environ.get("MEDIA_BROWSER_LAN", "")).lower() in ("1", "true", "yes", "on")
    dirs: list[str] = []
    rest: list[str] = []

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            say(__doc__.strip())
            return 0
        if a == "--lan":
            lan = True
            rest.append(a)
            i += 1
            continue
        if a in ("-p", "--port"):
            if i + 1 < len(args):
                port = int(args[i + 1])
            i += 2
            continue
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
            i += 1
            continue
        if a.startswith("-"):
            rest.append(a)
            if a in VALUED_OPTS and "=" not in a and i + 1 < len(args):
                rest.append(args[i + 1])
                i += 2
            else:
                i += 1
            continue
        dirs.append(a)
        i += 1

    if lan and "--lan" not in rest:
        rest.append("--lan")
    if not dirs:
        dirs = default_dirs()
    missing = [d for d in dirs if not Path(d).expanduser().exists()]
    if missing:
        say("目录不存在：%s" % "、".join(missing))
        say("（现在会自动找这几个：%s）" % "、".join(default_dirs()))
        say(" 要浏览别的地方就带上路径，例如："
            + ("./启动.sh ~/Pictures ~/Movies" if IS_MAC
               else "./启动.sh /mnt/XCS_DATA/某目录 ~/图片"))
        return 1

    ips = lan_ips()

    # 只要自检：不查目录、不起服务，直接交给 media_browser.py --check
    if "--check" in args:
        if not TOOL.exists():
            say("找不到 media_browser.py（应该在 %s 旁边）" % HERE)
            return 1
        return subprocess.call([sys.executable, str(TOOL)] + dirs + rest
                               + ["-p", str(port)], cwd=str(HERE))

    say("媒体浏览器 · %s" % PLATFORM)

    if port_busy(port) and not server_alive(port):
        say("端口 %d 被别的程序占着（不是本工具），换一个端口就行：" % port)
        say("  %s" % ("启动.bat -p %d" % (port + 1) if IS_WIN else
                      "./启动.sh -p %d" % (port + 1)))
        say("（或者把占端口的那个程序关掉）")
        return 1

    # 已经在跑？直接把浏览器叫到前台
    if server_alive(port):
        say("已经在运行，直接打开浏览器窗口…")
        say("（想彻底停掉：在那个终端按 Ctrl+C / 关掉那个窗口）")
        open_browser("http://127.0.0.1:%d/" % port)
        say()
        say("  本机地址 : http://127.0.0.1:%d/" % port)
        if ips and server_alive(port, ips[0]):
            say("  局域网   : http://%s:%d/   ← 手机 / 别的电脑开这个（只读）" % (ips[0], port))
            say("             （带口令地址见启动时那个终端打印的内容）")
        elif ips:
            say("  局域网   : 未开放。要开就先停掉，再 ./启动.sh --lan")
        if IS_WIN:
            time.sleep(3)          # 双击 .bat 时窗口别一闪而过
        return 0

    if sys.version_info < (3, 8):
        say("需要 Python 3.8 以上，当前是 %s" % sys.version.split()[0])
        return 1
    if not TOOL.exists():
        say("找不到 media_browser.py（应该在 %s 旁边）" % HERE)
        return 1

    say("  目录     : %s" % "  ".join(dirs))
    say("  端口     : %d" % port)
    say("  网页里也能加别的目录：顶部「📚 目录」按钮")
    for line in tool_report():
        say(line)
    if lan and ips:
        say("  局域网   : http://%s:%d/   ← 手机 / 别的电脑直接开这个（只能浏览，只读）"
            % (ips[0], port))
    if IS_MAC:
        say("  提示     : 首次运行若弹「是否允许 Python 接受传入连接」，要开局域网就点允许")
    elif IS_WIN:
        say("  提示     : 首次运行若弹 Windows 防火墙提示，要给手机看就点「允许访问」")
    say("  停止服务 : Ctrl+C")
    say("-" * 68)

    cmd = [sys.executable, str(TOOL)] + dirs + rest + ["-p", str(port)]
    try:
        return subprocess.call(cmd, cwd=str(HERE))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
