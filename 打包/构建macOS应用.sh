#!/usr/bin/env bash
# 媒体浏览器 · 在 macOS 上做「一键启动」的图标（应用包 + 桌面 + 程序坞）
#
#   ./打包/构建macOS应用.sh              # 生成 /Applications/媒体浏览器.app，并铺桌面 / 程序坞图标
#   ./打包/构建macOS应用.sh --no-dock    # 不往程序坞里加（只做应用包 + 桌面图标）
#   ./打包/构建macOS应用.sh --app-only   # 只做应用包，桌面 / 程序坞都不动
#
# 双击桌面或程序坞里的「媒体浏览器」= 后台静默起服务 + 自动开浏览器（不弹终端窗口）；
# 已经在跑的话只会把浏览器叫到前台，不会起第二个。
# 真正的启动逻辑在 启动-静默.sh 里，这里只负责「包装」成 macOS 认识的 .app。
#
# 换地方了（工程搬家）怎么办：在新位置重新跑一遍本脚本即可 —— 程序目录会重新写进启动器。

set -u

WANT_DESKTOP=1
WANT_DOCK=1
for a in "$@"; do
  case "$a" in
    --no-dock) WANT_DOCK=0 ;;
    --app-only) WANT_DESKTOP=0; WANT_DOCK=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "不认识的参数：$a"; exit 2 ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "这个脚本只在 macOS 上有用（Linux 用仓库里的 媒体浏览器.desktop）。"
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
APP_NAME="媒体浏览器"
APP="/Applications/$APP_NAME.app"
BUNDLE_ID="com.xiaochaosui.media-viewer"

if [ ! -f "$PROJECT/media_browser.py" ] || [ ! -f "$PROJECT/启动-静默.sh" ]; then
  echo "✗ $PROJECT 里找不到 media_browser.py / 启动-静默.sh，这不是工程根目录？"
  exit 1
fi
echo "工程目录：$PROJECT"

# 顺手把执行位补齐（从别的机器拷过来常常会丢）
chmod +x "$PROJECT/启动.sh" "$PROJECT/启动.py" "$PROJECT/启动-静默.sh" \
         "$PROJECT/media_browser.py" 2>/dev/null

# --------------------------------------------------------------------------- #
# 1. 图标：Pillow 画一张 1024 的底图 → 各尺寸 → iconutil 打成 .icns
# --------------------------------------------------------------------------- #
WORK="$(mktemp -d "${TMPDIR:-/tmp}/media-browser-app.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"

if python3 -c 'import PIL' 2>/dev/null; then
  python3 - "$ICONSET" "$WORK/icon-preview.png" <<'PY'
import sys
from PIL import Image, ImageDraw

iconset, preview = sys.argv[1], sys.argv[2]
S = 1024

# 背景：对角渐变（蓝 → 紫）。先画 64×64 小图再放大，省得逐像素慢慢算。
c1, c2 = (41, 98, 255), (150, 62, 230)
small = Image.new("RGB", (64, 64))
px = small.load()
for y in range(64):
    for x in range(64):
        t = (x + y) / 126.0
        px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))
base = small.resize((S, S), Image.BICUBIC).convert("RGBA")

icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=228, fill=255)
icon.paste(base, (0, 0), mask)

# 图案：2×2 的缩略图墙，右下角那格是「播放」三角 —— 图片 + 视频，两个都说得清
d = ImageDraw.Draw(icon)
tile, gap = 262, 58
total = tile * 2 + gap
x0 = (S - total) // 2
y0 = (S - total) // 2
boxes = []
for r in range(2):
    for c in range(2):
        bx, by = x0 + c * (tile + gap), y0 + r * (tile + gap)
        boxes.append([bx, by, bx + tile, by + tile])
for i, b in enumerate(boxes):
    d.rounded_rectangle(b, radius=58, fill=(255, 255, 255, 232 if i < 3 else 255))
bx0, by0, bx1, by1 = boxes[3]
cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
h = 76
d.polygon([(cx - 30, cy - h), (cx - 30, cy + h), (cx + 66, cy)], fill=(112, 74, 236, 255))

sizes = [(16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
         (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
         (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
         (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
         (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png")]
for px_size, name in sizes:
    icon.resize((px_size, px_size), Image.LANCZOS).save(f"{iconset}/{name}")
icon.save(preview)
PY
  ICON_OK=1
else
  echo "⚠️  这台机器的 python3 没有 Pillow，跳过自定义图标（用系统通用图标）"
  ICON_OK=0
fi

if [ "$ICON_OK" = 1 ] && iconutil -c icns "$ICONSET" -o "$WORK/AppIcon.icns" 2>/dev/null; then
  PREVIEW="${TMPDIR:-/tmp}/$APP_NAME-图标预览.png"
  cp "$WORK/icon-preview.png" "$PREVIEW" 2>/dev/null
  echo "✓ 图标已生成（预览：${PREVIEW}）"
else
  ICON_OK=0
fi

# --------------------------------------------------------------------------- #
# 2. 组装 .app（Contents/MacOS/launcher 是个 bash 脚本，LSUIElement 让它不占程序坞）
# --------------------------------------------------------------------------- #
rm -rf "$WORK/$APP_NAME.app"
mkdir -p "$WORK/$APP_NAME.app/Contents/MacOS" "$WORK/$APP_NAME.app/Contents/Resources"

cat > "$WORK/$APP_NAME.app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key><string>$APP_NAME</string>
	<key>CFBundleDisplayName</key><string>$APP_NAME</string>
	<key>CFBundleExecutable</key><string>launcher</string>
	<key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
	<key>CFBundleIconFile</key><string>AppIcon</string>
	<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>1.0</string>
	<key>CFBundleVersion</key><string>1</string>
	<key>LSMinimumSystemVersion</key><string>11.0</string>
	<key>NSHighResolutionCapable</key><true/>
	<key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

cat > "$WORK/$APP_NAME.app/Contents/MacOS/launcher" <<'LAUNCHER'
#!/bin/bash
# 媒体浏览器 · macOS 一键启动器（由 打包/构建macOS应用.sh 生成，别手改，改那个脚本）
# 点图标 = 后台静默起服务 + 自动开浏览器；已经在跑就把浏览器叫到前台。
set -u
# 从程序坞启动时拿不到登录 shell 的 PATH，这里补上常见的几个位置
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# 程序目录：按顺序找第一个「看着像」的，工程搬过家也能认出来
PROJECT=""
for cand in \
  "__PROJECT__" \
  "$HOME/xcs/projects/Media-Viewer" \
  "$HOME/媒体浏览器-mac" \
  "$HOME/Projects/Media-Viewer" \
  "/data/Projects/媒体浏览器"; do
  if [ -f "$cand/media_browser.py" ] && [ -f "$cand/启动-静默.sh" ]; then PROJECT="$cand"; break; fi
done

if [ -z "$PROJECT" ]; then
  osascript -e 'display alert "找不到「媒体浏览器」工程目录" message "工程可能搬家了。打开工程里的 打包/构建macOS应用.sh 重跑一次，图标就认新位置了。"' >/dev/null 2>&1
  echo "找不到工程目录（期望在 __PROJECT__）" >&2
  exit 1
fi

cd "$PROJECT" || exit 1
[ -x "./启动-静默.sh" ] || chmod +x ./启动-静默.sh ./启动.sh ./启动.py ./media_browser.py 2>/dev/null
exec ./启动-静默.sh "$@"
LAUNCHER

# 把工程路径写死进启动器（sed 而不是展开，免得 $HOME 之类被吃掉）
python3 - "$WORK/$APP_NAME.app/Contents/MacOS/launcher" "$PROJECT" <<'PY'
import sys
path, project = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    s = f.read()
with open(path, "w", encoding="utf-8") as f:
    f.write(s.replace("__PROJECT__", project))
PY
chmod +x "$WORK/$APP_NAME.app/Contents/MacOS/launcher"

[ "$ICON_OK" = 1 ] && cp "$WORK/AppIcon.icns" "$WORK/$APP_NAME.app/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "$WORK/$APP_NAME.app/Contents/PkgInfo"

# 装到 /Applications（可写就不用 sudo；不行就退回 ~/Applications）
if [ -w /Applications ]; then
  DEST_DIR="/Applications"
else
  DEST_DIR="$HOME/Applications"
  mkdir -p "$DEST_DIR"
fi
APP="$DEST_DIR/$APP_NAME.app"
rm -rf "$APP"
cp -R "$WORK/$APP_NAME.app" "$APP"
# 让系统重新登记一下，免得程序坞 / 访达里还是旧图标
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APP" 2>/dev/null
echo "✓ 应用包：$APP"

# --------------------------------------------------------------------------- #
# 3. 桌面图标
#    这里放的是**真·拷贝**而不是软链：软链在访达里会被当成「替身」（一个带小箭头的
#    通用图标），看着不像应用，容易找不到。拷贝只多占几百 KB，图标就是原样的。
# --------------------------------------------------------------------------- #
if [ "$WANT_DESKTOP" = 1 ]; then
  DESK="$HOME/Desktop/$APP_NAME.app"
  rm -rf "$DESK"
  cp -R "$APP" "$DESK" && echo "✓ 桌面图标：$DESK"
  [ -x "$LSREG" ] && "$LSREG" -f "$DESK" 2>/dev/null
fi

# --------------------------------------------------------------------------- #
# 4. 程序坞图标
#    直接读 / 写 Dock 的整份 plist（plistlib），比 defaults write 拼字符串靠谱：
#    已经在了就不动，重复了（手滑跑了两遍）就清成一份。
#
#    ⚠️ 两个坑都踩过了：
#      · `_CFURLStringType` 必须是 15（URL 字符串）。写 0 的话 Dock 会把它当成
#        普通路径、把里面的 % 再转义一次（%E5 → %25E5），图标就变成一个问号 / 空白。
#      · 所以匹配时得把百分号编码反复解码（unquote 到稳定）再比，不然重复项认不出来。
# --------------------------------------------------------------------------- #
if [ "$WANT_DOCK" = 1 ]; then
  DOCK_ACTION="$(python3 - "$APP" "$APP_NAME" "$WORK/dock.plist" <<'PY'
import plistlib, subprocess, sys, urllib.parse

app, label, out = sys.argv[1], sys.argv[2], sys.argv[3]
raw = subprocess.run(["defaults", "export", "com.apple.dock", "-"],
                     capture_output=True).stdout
try:
    dock = plistlib.loads(raw)
except Exception:
    print("error")
    raise SystemExit(0)

url = "file://" + urllib.parse.quote(app)


def decode_all(s):
    """把 %E5 → %25E5 这种重复编码解到底，用来判断「是不是同一个 app」"""
    prev = None
    while prev != s:
        prev, s = s, urllib.parse.unquote(s)
    return s.rstrip("/")


def url_of(tile):
    try:
        return tile["tile-data"]["file-data"]["_CFURLString"]
    except Exception:
        return ""


def healthy(tile):
    u = url_of(tile)
    if not u or decode_all(u) != decode_all(url):
        return False
    try:
        return int(tile["tile-data"]["file-data"].get("_CFURLStringType", 0)) == 15 \
            and "%25" not in u
    except Exception:
        return False


apps = list(dock.get("persistent-apps") or [])
hits = [t for t in apps if url_of(t) and decode_all(url_of(t)) == decode_all(url)]
if len(hits) == 1 and healthy(hits[0]):
    print("same")
    raise SystemExit(0)

apps = [t for t in apps if not (url_of(t) and decode_all(url_of(t)) == decode_all(url))]
apps.append({"tile-type": "file",
             "tile-data": {
                 "file-data": {"_CFURLString": url, "_CFURLStringType": 15},
                 "file-label": label}})
dock["persistent-apps"] = apps
with open(out, "wb") as f:
    plistlib.dump(dock, f)
print("fix" if hits else "add")
PY
)"
  case "$DOCK_ACTION" in
    same)
      echo "• 程序坞里已经有了，不重复添加" ;;
    add)
      defaults import com.apple.dock "$WORK/dock.plist" && killall Dock 2>/dev/null \
        && echo "✓ 已加入程序坞（Dock 刚重启了一下，图标会闪一下）" ;;
    fix)
      defaults import com.apple.dock "$WORK/dock.plist" && killall Dock 2>/dev/null \
        && echo "✓ 程序坞里的旧项 / 重复项已清掉，重写了正确的条目" ;;
    *)
      echo "⚠️  程序坞没动成（读不到 com.apple.dock？）—— 把 $APP 直接拖进程序坞也一样" ;;
  esac
fi

echo
echo "搞定。点桌面或程序坞里的「${APP_NAME}」就会：后台静默起服务 → 自动开浏览器。"
echo "看日志：tail -f \"$HOME/Library/Logs/media-browser/启动.log\""
