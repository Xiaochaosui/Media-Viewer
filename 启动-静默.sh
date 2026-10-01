#!/usr/bin/env bash
# 媒体浏览器 · 静默启动（桌面图标用这个：不弹终端窗口）
#
#   ./启动-静默.sh             # 后台起服务，起好后弹一条桌面通知，浏览器自己打开
#   ./启动-静默.sh --lan       # 额外参数原样转给 启动.sh
#   ./启动-静默.sh --no-open   # 不自动开浏览器
#
# 和 ./启动.sh 的区别只有一个：**不留终端窗口**。
# 日志不往屏幕上打，而是写到缓存目录里的 启动.log —— 起不来时桌面通知会告诉你去看它。
#
# 想看得见日志和状态：用 ./启动.sh，或在应用菜单里选「媒体浏览器（带日志窗口）」。

set -u
HERE="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
cd "$HERE" || exit 1

PORT="${MEDIA_BROWSER_PORT:-8777}"
LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/media-browser"
LOG="$LOG_DIR/启动.log"
mkdir -p "$LOG_DIR" 2>/dev/null

# 探活：curl 优先，没有就用 python3
probe() {
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$PORT/api/ping" 2>/dev/null
  else
    python3 -c "import urllib.request,sys;urllib.request.urlopen('http://127.0.0.1:$PORT/api/ping',timeout=1)" 2>/dev/null
  fi
}

# 本机在局域网里的地址（拿不到就空着，通知里只提示本机地址）
lan_ip() {
  python3 - <<'PY' 2>/dev/null
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.settimeout(0.4)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
except Exception:
    ip = ""
print("" if ip.startswith("127.") or not ip else ip)
PY
}

# 桌面通知：没有 notify-send 就退回 zenity，再没有就什么都不弹（不影响启动）
notify() {
  command -v notify-send >/dev/null 2>&1 &&
    notify-send -a "媒体浏览器" -i multimedia-player "$1" "$2" 2>/dev/null && return 0
  command -v zenity >/dev/null 2>&1 &&
    zenity --info --title="媒体浏览器" --text="$1
$2" --timeout=6 >/dev/null 2>&1 && return 0
  return 0
}

# 日志别无限长：超过 2MB 就留最后一份
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 2097152 ]; then
  mv -f "$LOG" "$LOG.1" 2>/dev/null
fi
printf '[%s] 静默启动：%s\n' "$(date '+%F %T')" "$*" >>"$LOG"

# 真正干活的启动器丢到后台，桌面图标立刻返回，不留终端
setsid nohup "$HERE/启动.sh" "$@" >>"$LOG" 2>&1 </dev/null &
PID=$!

# 等它就绪（最多 ~20 秒）：起来了给条通知（不然"点了没反应"），起不来告诉你去看日志
for _ in $(seq 1 40); do
  sleep 0.5
  if probe; then
    IP="$(lan_ip)"
    if [ -n "$IP" ]; then
      notify "媒体浏览器已启动" "本机 http://127.0.0.1:$PORT/　　手机 http://$IP:$PORT/"
    else
      notify "媒体浏览器已启动" "http://127.0.0.1:$PORT/"
    fi
    printf '[%s] 已就绪 http://127.0.0.1:%s/\n' "$(date '+%F %T')" "$PORT" >>"$LOG"
    exit 0
  fi
  kill -0 "$PID" 2>/dev/null || break       # 进程已经退出了，不用再等
done

notify "媒体浏览器没起来" "看看日志：$LOG"
printf '[%s] 启动失败或超时，日志见 %s\n' "$(date '+%F %T')" "$LOG" >>"$LOG"
exit 1
