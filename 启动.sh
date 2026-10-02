#!/usr/bin/env bash
# 媒体浏览器启动脚本（Linux / macOS 通用）
#   用法： ./启动.sh                     # 浏览默认目录
#          ./启动.sh ~/Pictures ~/下载   # 浏览指定目录（可以多个）
#          ./启动.sh --lan               # 开放局域网（手机 / 别的电脑只读浏览）
#          ./启动.sh --lan --token 我的口令
#          ./启动.sh --help              # 看全部选项
# 关闭：在本窗口按 Ctrl+C
#
# 真正的逻辑在 启动.py 里（Linux/macOS/Windows 共用），这个脚本只负责
# 找到能用的 Python 再交给它。macOS 也可以在访达里双击 启动.command；
# Windows 用 启动.bat。

set -u
HERE="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"

pick_python() {
  # 优先用能跑本工具的 Python（3.8+）；macOS 上优先 Homebrew 的 python3
  for cand in \
      "${MEDIA_BROWSER_PYTHON:-}" \
      /opt/homebrew/bin/python3 /usr/local/bin/python3 \
      "$(command -v python3 2>/dev/null || true)" \
      "$(command -v python 2>/dev/null || true)"; do
    [ -n "$cand" ] || continue
    [ -x "$cand" ] || continue
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  echo "没找到 Python 3.8 以上的解释器。"
  echo "  macOS : brew install python    （或去 python.org 下载）"
  echo "  Ubuntu: sudo apt install python3"
  exit 1
fi

exec "$PY" "$HERE/启动.py" "$@"
