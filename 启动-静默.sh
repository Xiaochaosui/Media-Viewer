#!/usr/bin/env bash
# 媒体浏览器 · 静默启动（桌面 / 程序坞图标用这个：不弹终端窗口）
#
#   ./启动-静默.sh             # 后台起服务，起好后弹一条通知，浏览器自己打开
#   ./启动-静默.sh --lan       # 额外参数原样转给 启动.sh
#   ./启动-静默.sh --no-open   # 不自动开浏览器
#
# 和 ./启动.sh 的区别只有一个：**不留终端窗口**。
# 日志不往屏幕上打，而是写到日志文件里 —— 起不来时通知会告诉你去看它：
#   macOS : ~/Library/Logs/media-browser/启动.log
#   Linux : ${XDG_CACHE_HOME:-~/.cache}/media-browser/启动.log
#
# 想看得见日志和状态：用 ./启动.sh（macOS 桌面上另有「媒体浏览器（带日志窗口）」）。
#
# 跨平台小抄：macOS 没有 setsid / notify-send / zenity / stat -c，
# 所以后台起进程改用 nohup，通知改用 osascript 顶上。

set -u
HERE="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
cd "$HERE" || exit 1

IS_MAC=0
[ "$(uname -s 2>/dev/null)" = "Darwin" ] && IS_MAC=1

# 从别处拷/同步过来的文件可能丢了执行位，这里补一下（不然静默启动会静默失败）
[ -x "$HERE/启动.sh" ] || chmod +x "$HERE/启动.sh" "$HERE/启动.py" "$HERE/media_browser.py" 2>/dev/null

PORT="${MEDIA_BROWSER_PORT:-8777}"
if [ "$IS_MAC" = 1 ]; then
  LOG_DIR="$HOME/Library/Logs/media-browser"          # macOS 的日志惯例位置
else
  LOG_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/media-browser"
fi
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

# osascript / AppleScript 字符串里把引号和反斜杠转义掉
esc() { printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# 系统通知：macOS 用 osascript，Linux 用 notify-send → zenity，都没有就不弹（不影响启动）
notify() {
  if [ "$IS_MAC" = 1 ] && command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$(esc "$2")\" with title \"$(esc "$1")\"" >/dev/null 2>&1
    return 0
  fi
  command -v notify-send >/dev/null 2>&1 &&
    notify-send -a "媒体浏览器" -i multimedia-player "$1" "$2" 2>/dev/null && return 0
  command -v zenity >/dev/null 2>&1 &&
    zenity --info --title="媒体浏览器" --text="$1
$2" --timeout=6 >/dev/null 2>&1 && return 0
  return 0
}

# 起不来时的弹窗（比通知更不容易被忽略；只在 macOS 上有）
alert() {
  if [ "$IS_MAC" = 1 ] && command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"$(esc "$1")\" message \"$(esc "$2")\"" >/dev/null 2>&1
  fi
}

# 日志别无限长：超过 2MB 就留最后一份（stat -c 是 Linux 语法，所以用 wc -c）
SIZE="$(wc -c <"$LOG" 2>/dev/null | tr -d ' \n' || echo 0)"
case "$SIZE" in ''|*[!0-9]*) SIZE=0 ;; esac
if [ "$SIZE" -gt 2097152 ]; then
  mv -f "$LOG" "$LOG.1" 2>/dev/null
fi
printf '[%s] 静默启动：%s\n' "$(date '+%F %T')" "$*" >>"$LOG"

# 真正干活的启动器丢到后台，桌面图标立刻返回，不留终端
if command -v setsid >/dev/null 2>&1; then        # Linux
  setsid nohup "$HERE/启动.sh" "$@" >>"$LOG" 2>&1 </dev/null &
else                                              # macOS：没有 setsid，nohup 就够
  nohup "$HERE/启动.sh" "$@" >>"$LOG" 2>&1 </dev/null &
  disown 2>/dev/null || true
fi
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
alert "媒体浏览器没起来" "看看日志：$LOG"
printf '[%s] 启动失败或超时，日志见 %s\n' "$(date '+%F %T')" "$LOG" >>"$LOG"
exit 1
