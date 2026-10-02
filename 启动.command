#!/usr/bin/env bash
# macOS：在访达里双击本文件即可启动（首次可能要右键 → 打开）
# 等价于 ./启动.sh
cd "$(dirname "$0")" || exit 1
exec ./启动.sh "$@"
