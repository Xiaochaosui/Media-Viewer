#!/usr/bin/env bash
# 把 ~/.cache 迁到 1TB 固态（/mnt/XCS_DATA = /dev/sdb1, ext4）并在原位置留软链接
#
# 用法：
#   ./迁移缓存到固态.sh                    # 迁移整个 ~/.cache（约 2.0G）
#   ./迁移缓存到固态.sh pnpm typescript node node-gyp   # 只迁指定子目录（推荐，风险最低）
#   ./迁移缓存到固态.sh --dry-run          # 只打印将要做什么，不动任何文件
#   ./迁移缓存到固态.sh --rollback         # 撤销：数据搬回根分区，删掉软链接
#
# 建议：注销图形会话后在 TTY（Ctrl+Alt+F3 登录）里执行最稳；
#       在桌面里执行也可以，但请先关掉 Chrome / Cursor / VS Code 等正在写缓存的程序。

set -euo pipefail

TARGET_ROOT="/mnt/XCS_DATA/xcs-cache"     # 固态上的存放目录
EXPECT_DEV="/dev/sdb1"                    # 期望的 1TB 固态
SRC="$HOME/.cache"
MODE="move"; DRY=0; SUBS=()

for a in "$@"; do
  case "$a" in
    --dry-run)  DRY=1 ;;
    --rollback) MODE="rollback" ;;
    -h|--help)  sed -n '2,13p' "$0"; exit 0 ;;
    -*)         echo "未知参数：$a" >&2; exit 2 ;;
    *)          SUBS+=("$a") ;;
  esac
done

die() { echo "❌ $*" >&2; exit 1; }
say() { echo "▶ $*"; }
run() { if [ "$DRY" = 1 ]; then echo "   [dry-run] $*"; else "$@"; fi; }

# --------------------------------------------------------------------------- #
# 0. 环境检查
# --------------------------------------------------------------------------- #
mountpoint -q /mnt/XCS_DATA || die "/mnt/XCS_DATA 没挂载（固态没插好？）"
dev="$(findmnt -no SOURCE /mnt/XCS_DATA)"
[ "$dev" = "$EXPECT_DEV" ] || echo "⚠️  注意：/mnt/XCS_DATA 对应的是 $dev，不是预期的 $EXPECT_DEV，请确认这是那块 1TB 固态"

avail_kb="$(df -Pk /mnt/XCS_DATA | awk 'NR==2{print $4}')"
need_kb="$(du -sk "$SRC" 2>/dev/null | awk '{print $1}')"
[ "${avail_kb:-0}" -gt "$(( ${need_kb:-0} + 524288 ))" ] \
  || die "固态可用空间不足（可用 $((avail_kb/1024))M，需要 $((need_kb/1024))M + 512M 余量）"
say "目标：$dev 挂载于 /mnt/XCS_DATA，可用 $((avail_kb/1024/1024))G；待迁移 $((need_kb/1024))M"

busy="$(pgrep -c -x chrome 2>/dev/null || true)"
[ "${busy:-0}" -gt 0 ] && echo "⚠️  检测到 Chrome 正在运行，它的缓存（约 950M）建议迁移后重启浏览器"

command -v rsync >/dev/null || die "缺少 rsync，请先 sudo apt install rsync"

# --------------------------------------------------------------------------- #
# 回滚模式
# --------------------------------------------------------------------------- #
if [ "$MODE" = "rollback" ]; then
  if [ ! -L "$SRC" ] && [ ${#SUBS[@]} -eq 0 ]; then
    die "$SRC 不是软链接，没什么可回滚的"
  fi
  if [ ${#SUBS[@]} -eq 0 ]; then
    say "回滚整个 ~/.cache"
    run rsync -aHAX --delete "$(readlink -f "$SRC")/" "$SRC.real/"
    run rm -f "$SRC"
    run mv "$SRC.real" "$SRC"
  else
    for s in "${SUBS[@]}"; do
      [ -L "$SRC/$s" ] || { echo "   跳过 $s（不是软链接）"; continue; }
      say "回滚 $s"
      run rsync -aHAX --delete "$(readlink -f "$SRC/$s")/" "$SRC/$s.real/"
      run rm -f "$SRC/$s"
      run mv "$SRC/$s.real" "$SRC/$s"
    done
  fi
  ok_done="回滚完成"
  echo "✅ $ok_done"
  exit 0
fi

run mkdir -p "$TARGET_ROOT"

# --------------------------------------------------------------------------- #
# 模式 A：只迁指定子目录（~/.cache 仍是真目录，里面几个大件变成软链）
# --------------------------------------------------------------------------- #
if [ ${#SUBS[@]} -gt 0 ]; then
  for s in "${SUBS[@]}"; do
    src="$SRC/$s"
    dst="$TARGET_ROOT/$s"
    if [ -L "$src" ]; then echo "   跳过 $s（已经是软链接）"; continue; fi
    [ -d "$src" ] || { echo "   跳过 $s（不存在）"; continue; }
    size="$(du -sh "$src" 2>/dev/null | awk '{print $1}')"
    say "迁移 $s（$size） → $dst"
    run mkdir -p "$dst"
    run rsync -aHAX --info=progress2 "$src/" "$dst/"
    run rm -rf "$src.old"
    run mv "$src" "$src.old"
    run ln -s "$dst" "$src"
    run rm -rf "$src.old"
  done
  echo "✅ 完成。检查一下： ls -l ~/.cache/"
  exit 0
fi

# --------------------------------------------------------------------------- #
# 模式 B：整体迁移 ~/.cache
# --------------------------------------------------------------------------- #
[ -L "$SRC" ] && { echo "✅ $SRC 已经是软链接 → $(readlink -f "$SRC")，无需再迁"; exit 0; }
[ -d "$SRC" ] || die "$SRC 不存在"

DST="$TARGET_ROOT/cache"
say "整体迁移 ~/.cache（$((need_kb/1024))M） → $DST"
run mkdir -p "$DST"
say "第 1 步：复制数据（rsync 保留权限/扩展属性）"
run rsync -aHAX --info=progress2 "$SRC/" "$DST/"
say "第 1.5 步：再同步一次增量（缩小切换瞬间的差异，改动的文件都还在备份里）"
run rsync -aHAX --delete "$SRC/" "$DST/"
say "第 2 步：原目录改名备份、建立软链接"
stamp="$(date +%Y%m%d-%H%M%S)"
run mv "$SRC" "$SRC.old-$stamp"
run ln -s "$DST" "$SRC"
say "第 3 步：校验软链接可用"
if [ "$DRY" = 0 ]; then
  ls "$SRC/" >/dev/null 2>&1 || die "软链接读不到内容，请手动检查（原目录还留着 $SRC.old-$stamp）"
fi
echo
echo "✅ 完成。"
echo "   新位置：$DST   原目录备份：$SRC.old-$stamp"
echo "   根分区可用空间：$(df -h / | awk 'NR==2{print $4}')"
echo
echo "   确认一切正常后（建议重启一次图形会话再观察一天），可删备份腾空间："
echo "     rm -rf $SRC.old-$stamp"
