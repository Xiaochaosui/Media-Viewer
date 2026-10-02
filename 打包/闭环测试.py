#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体浏览器 · 闭环测试（整理类功能：重命名 / 移动 / 新建文件夹）

跑法（服务得先起着）：
    python3 打包/闭环测试.py                     # 默认打 http://127.0.0.1:8777
    python3 打包/闭环测试.py --base http://127.0.0.1:8899
    python3 打包/闭环测试.py --keep              # 跑完不删临时目录（排查用）

它自己在临时目录里造一批小文件、加成一个素材目录，然后一步步走真实接口：
单张改名 / 前后缀 / 查找替换 / 连续编号 / 重名冲突 / 移动到已有文件夹 / 移到新文件夹 /
只读目录（chmod 不给你写）/ 真·只读挂载盘（macOS 上的 NTFS 外接盘，有就测）……
每一步都对着**磁盘上的真实结果**断言，最后把临时素材目录从配置里摘掉、临时文件删干净。

不碰你已有的素材：所有写操作都只发生在它自己造的临时目录里；
只读那两项只做「应该被拒绝」的断言，一个字节都不落盘。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PASS, FAIL, SKIP = [], [], []
BASE = "http://127.0.0.1:8777"
TMP = Path("/tmp/mb-closed-loop")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def call(path: str, body=None, method: str | None = None, timeout: float = 30.0) -> dict:
    """请求接口。返回 JSON；HTTP 错误也把 body 解析出来（错误信息就在里面）。"""
    url = BASE + path
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "HTTP %s" % e.code}


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("   ← " + detail) if detail and not cond else ""))
    return cond


def skip(name: str, why: str) -> None:
    SKIP.append(name)
    print("  ⏭  %s（%s）" % (name, why))


def wait_scan(need: int, timeout: float = 40.0) -> list:
    """等后台扫描把临时目录里的文件收进来。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        items = call("/api/list?limit=500").get("items") or []
        mine = [it for it in items if it["id"].startswith(ROOT_KEY)]
        if len(mine) >= need:
            return mine
        time.sleep(0.4)
    return []


def wait_job(jid: str, timeout: float = 60.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = call("/api/job?id=" + urllib.parse.quote(jid))
        if j.get("state") != "running":
            return j
        time.sleep(0.3)
    return {"state": "timeout"}


def media(root: Path) -> dict:
    """临时目录里现在有哪些文件（按名字）。"""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            out[str(p.relative_to(root))] = p.stat().st_size
    return out


ROOT_KEY = ""


# --------------------------------------------------------------------------- #
# 各项测试
# --------------------------------------------------------------------------- #
def t_setup() -> list:
    global ROOT_KEY
    print("\n【准备】临时素材目录")
    shutil.rmtree(TMP, ignore_errors=True)
    (TMP / "已有文件夹").mkdir(parents=True)
    (TMP / "只读文件夹").mkdir()
    for name, size in [("照片A.heic", 2048), ("照片B.heic", 3072),
                       ("视频A.mp4", 4096), ("视频B.MP4", 5120)]:
        (TMP / name).write_bytes(b"\0" * size)
    r = call("/api/roots/add", {"path": str(TMP)})
    check("加素材目录", bool(r.get("ok")), str(r.get("error")))
    # /tmp 在 macOS 上是 /private/tmp 的软链，服务器会返回解析后的路径
    real = r.get("roots") and [x for x in r["roots"] if x["path"].endswith(TMP.name)]
    if real:
        ROOT_KEY = real[0]["key"]
    items = wait_scan(4)
    check("扫描到 4 个文件", len(items) == 4, "拿到 %d 个" % len(items))
    return items


def t_roots_writable() -> None:
    print("\n【1】目录可写性上报（界面靠它提前提示「只读」）")
    roots = call("/api/roots").get("roots") or []
    mine = next((r for r in roots if r["key"] == ROOT_KEY), None)
    check("临时目录 writable=true", bool(mine and mine.get("writable") is True))
    check("可写目录不带 write_note", bool(mine and not mine.get("write_note")))
    b = call("/api/browse?path=" + urllib.parse.quote(str(TMP)))
    check("/api/browse 也带 writable", b.get("writable") is True, str(b)[:200])


def t_mkdir() -> None:
    print("\n【2】新建文件夹")
    r = call("/api/mkdir", {"path": str(TMP), "name": "2026-10 新分类"})
    check("新建成功", bool(r.get("ok")), str(r.get("error")))
    check("磁盘上真的出现了", (TMP / "2026-10 新分类").is_dir())
    r2 = call("/api/mkdir", {"path": str(TMP), "name": "2026-10 新分类"})
    check("重名会被挡住", not r2.get("ok") and "同名" in str(r2.get("error")), str(r2.get("error")))


def t_rename_single(items: list) -> str:
    print("\n【3】单张改名")
    the_id = ROOT_KEY + ":照片A.heic"
    dry = call("/api/rename", {"id": the_id, "name": "假期A", "dry_run": True})
    check("预览不改盘", bool(dry.get("ok")) and (TMP / "照片A.heic").exists()
          and not (TMP / "假期A.heic").exists(), str(dry)[:200])
    check("预览算出新名字", (dry.get("results") or [{}])[0].get("new_name") == "假期A.heic",
          str(dry.get("results"))[:200])
    r = call("/api/rename", {"id": the_id, "name": "假期A"})
    check("改名成功", bool(r.get("ok")) and r.get("ok_count") == 1, str(r.get("error") or r.get("message")))
    check("磁盘上换了名字", (TMP / "假期A.heic").exists() and not (TMP / "照片A.heic").exists())
    # 回归：素材目录根下的文件，改名后的 new_id 必须还带着 "根key:" 前缀
    check("new_id 带根目录前缀", str(r.get("new_id", "")).startswith(ROOT_KEY + ":"),
          "new_id=%r" % r.get("new_id"))
    return ROOT_KEY + ":假期A.heic"


def t_rename_affix() -> None:
    print("\n【4】批量：加前缀后缀")
    ids = [ROOT_KEY + ":照片B.heic", ROOT_KEY + ":视频A.mp4"]
    r = call("/api/rename", {"ids": ids, "prefix": "2026-", "suffix": "-成片"})
    check("两个都改了", bool(r.get("ok")) and r.get("ok_count") == 2, str(r.get("error") or r.get("message")))
    check("照片B → 2026-照片B-成片.heic", (TMP / "2026-照片B-成片.heic").exists())
    check("视频A → 2026-视频A-成片.mp4", (TMP / "2026-视频A-成片.mp4").exists())


def t_rename_replace() -> None:
    print("\n【5】批量：查找替换")
    ids = [ROOT_KEY + ":2026-照片B-成片.heic", ROOT_KEY + ":2026-视频A-成片.mp4"]
    r = call("/api/rename", {"ids": ids, "find": "2026-", "replace": "2027-"})
    check("两个都改了", bool(r.get("ok")) and r.get("ok_count") == 2, str(r.get("error") or r.get("message")))
    check("替换结果落盘", (TMP / "2027-照片B-成片.heic").exists() and (TMP / "2027-视频A-成片.mp4").exists())


def t_rename_seq() -> None:
    print("\n【6】批量：连续编号")
    ids = [ROOT_KEY + ":2027-照片B-成片.heic", ROOT_KEY + ":2027-视频A-成片.mp4"]
    r = call("/api/rename", {"ids": ids, "seq": True, "prefix": "编号", "start": 1, "digits": 2})
    check("编号改名成功", bool(r.get("ok")) and r.get("ok_count") == 2, str(r.get("error") or r.get("message")))
    names = sorted(p.name for p in TMP.iterdir() if p.is_file() and p.name.startswith("编号"))
    check("编号按顺序落盘", names == ["编号01.heic", "编号02.mp4"], str(names))


def t_rename_conflict() -> None:
    print("\n【7】重名冲突（自动改名 / 跳过）")
    (TMP / "撞名.heic").write_bytes(b"\0" * 100)
    r0 = call("/api/mkdir", {"path": str(TMP), "name": "撞名.heic"})   # 目录不会建，只是占位
    ids = [ROOT_KEY + ":编号01.heic"]
    r = call("/api/rename", {"ids": ids, "name": "撞名", "on_conflict": "rename"})
    check("自动改名没覆盖", bool(r.get("ok")) and r.get("ok_count") == 1, str(r.get("error") or r.get("message")))
    check("原文件还在", (TMP / "撞名.heic").exists() and (TMP / "撞名.heic").stat().st_size == 100)
    others = sorted(p.name for p in TMP.iterdir() if p.is_file() and p.name.startswith("撞名"))
    check("新文件用了 (1) 后缀", len(others) == 2, str(others))
    # 跳过模式：拿那个「撞名 (1)」再去改成「撞名」，目标已存在 → 应该跳过而不是覆盖
    dup = next((n for n in others if "(1)" in n), others[-1])
    ids = [ROOT_KEY + ":" + dup]
    r2 = call("/api/rename", {"ids": ids, "name": "撞名", "on_conflict": "skip"})
    check("skip 模式报跳过", bool(r2.get("ok")) and r2.get("skipped") == 1,
          "%s / %s" % (r2.get("message"), r2.get("error")))
    check("skip 模式没动盘", (TMP / dup).exists())


def t_move_existing() -> None:
    print("\n【8】移动到已有文件夹（异步任务）")
    src = next(p for p in TMP.iterdir() if p.is_file() and p.name.startswith("编号02"))
    r = call("/api/move", {"ids": [ROOT_KEY + ":" + src.name], "dest": str(TMP / "已有文件夹")})
    check("任务已创建", bool(r.get("ok")) and r.get("job"), str(r.get("error")))
    if not r.get("job"):
        return
    j = wait_job(r["job"])
    check("任务跑完", j.get("state") == "done", str(j)[:200])
    check("任务消息是「已移动」", "已移动" in str(j.get("message")), str(j.get("message")))
    check("文件真的过去了", (TMP / "已有文件夹" / src.name).exists() and not src.exists())
    check("任务进度字段齐全", {"done", "total", "state", "message"} <= set(j.keys()), str(j.keys()))


def fresh_id(name: str) -> str:
    """按文件名（相对素材目录）拿一个最新的 id —— 移动/改名之后老 id 就不作数了。"""
    for it in (call("/api/list?limit=500").get("items") or []):
        if it["id"].startswith(ROOT_KEY + ":") and it["id"].endswith(":" + name):
            return it["id"]
    return ROOT_KEY + ":" + name


def t_move_new_folder() -> None:
    print("\n【9】移到「新文件夹」（顺便建目录）")
    src = next((p for p in TMP.iterdir() if p.is_file() and p.name.startswith("编号01")), None)
    if not src:
        src = next(p for p in TMP.iterdir() if p.is_file())
    r = call("/api/move", {"ids": [ROOT_KEY + ":" + src.name], "dest": str(TMP),
                           "new_folder": "2027-归档"})
    check("任务已创建", bool(r.get("ok")) and r.get("job"), str(r.get("error")))
    j = wait_job(r["job"]) if r.get("job") else {}
    check("任务跑完", j.get("state") == "done", str(j)[:200])
    check("新文件夹建好且文件在里面", (TMP / "2027-归档" / src.name).exists())
    # 已经在目标里 → 跳过（用移动之后的新 id）
    r2 = call("/api/move", {"ids": [fresh_id("2027-归档/" + src.name)],
                            "dest": str(TMP / "2027-归档")})
    j2 = wait_job(r2["job"]) if r2.get("job") else {}
    check("同一文件夹会跳过", "跳过" in str(j2.get("message")), str(j2.get("message")))
    # 拿已经过期/不存在的 id 去移 → 应该给一句人话，而不是静默 0 个
    r3 = call("/api/move", {"ids": [ROOT_KEY + ":根本不存在的文件.mp4"], "dest": str(TMP)})
    j3 = wait_job(r3["job"]) if r3.get("job") else {}
    check("过期 id 会说明白", "找不到" in str(j3.get("message")), str(j3.get("message")))


def t_readonly_chmod() -> None:
    print("\n【10】没有写权限的目录（chmod 500）")
    ro = TMP / "只读文件夹"
    (ro / "里面的东西.heic").write_bytes(b"\0" * 64)
    call("/api/roots/remove", {"key": ROOT_KEY})         # 重新加一次，把新文件收进索引
    call("/api/roots/add", {"path": str(TMP)})
    items = wait_scan(1)
    if not any(it["id"].endswith("只读文件夹/里面的东西.heic") for it in items):
        time.sleep(3)
        items = call("/api/list?limit=500").get("items") or []
    inner = next((it for it in items if it["id"].endswith("只读文件夹/里面的东西.heic")), None)
    os.chmod(ro, 0o500)
    try:
        r1 = call("/api/mkdir", {"path": str(ro), "name": "建不了"})
        check("新建被挡住且话说得明白",
              not r1.get("ok") and ("没有写权限" in str(r1.get("error")) or "只读" in str(r1.get("error"))),
              str(r1.get("error")))
        if inner:
            r2 = call("/api/rename", {"id": inner["id"], "name": "也改不了"})
            check("改名被挡住且话说得明白",
                  not r2.get("ok") and ("没有写权限" in str(r2.get("error")) or "只读" in str(r2.get("error"))),
                  str(r2.get("error")))
            r3 = call("/api/move", {"ids": [inner["id"]], "dest": str(ro)})
            check("移动被挡住且话说得明白",
                  not r3.get("ok") and ("没有写权限" in str(r3.get("error")) or "只读" in str(r3.get("error"))),
                  str(r3.get("error")))
        else:
            skip("只读目录里的改名/移动", "索引里没找到那个文件")
    finally:
        os.chmod(ro, 0o700)


def t_readonly_mount() -> None:
    print("\n【11】真·只读挂载盘（macOS 上的 NTFS 外接盘）")
    roots = call("/api/roots").get("roots") or []
    root = next((r for r in roots if r.get("writable") is False and r.get("kind") == "local"), None)
    if root is None:
        # 没有已经加进来的只读目录：看看这台机器上到底有没有只读挂载的卷
        ro_vols = []
        if Path("/Volumes").is_dir():
            for p in sorted(Path("/Volumes").iterdir()):
                try:
                    if p.is_dir() and (os.statvfs(str(p)).f_flag & getattr(os, "ST_RDONLY", 1)):
                        ro_vols.append(str(p))
                except OSError:
                    continue
        if ro_vols:
            skip("只读盘上的整理测试", "只读的 %s 没加进素材目录（界面上「📚 目录」加进来就能测）"
                 % "、".join(ro_vols))
        else:
            skip("只读盘上的整理测试", "这台机器上没有只读挂载的盘")
        return

    print("   （只读目录：%s）" % root["path"])
    check("只读盘在 /api/roots 里标了 writable=false",
          root.get("writable") is False and bool(root.get("write_note")), str(root)[:200])
    items = [it for it in (call("/api/list?limit=500").get("items") or [])
             if it["id"].startswith(root["key"] + ":")]
    before = len(items)
    r1 = call("/api/mkdir", {"path": root["path"], "name": "闭环测试别建"})
    check("新建被挡住且说清是只读盘",
          not r1.get("ok") and "只读挂载" in str(r1.get("error")), str(r1.get("error")))
    if items:
        iid = items[0]["id"]
        r2 = call("/api/rename", {"id": iid, "name": "闭环测试别改"})
        check("改名被挡住且说清是只读盘",
              not r2.get("ok") and "只读挂载" in str(r2.get("error")), str(r2.get("error")))
        r3 = call("/api/move", {"ids": [iid], "dest": root["path"]})
        check("移动被挡住且说清是只读盘",
              not r3.get("ok") and "只读挂载" in str(r3.get("error")), str(r3.get("error")))
    else:
        skip("只读盘上的改名/移动", "这个盘里没有媒体文件")
    after = len([it for it in (call("/api/list?limit=500").get("items") or [])
                 if it["id"].startswith(root["key"] + ":")])
    check("只读盘上一个文件都没动", before == after, "%d → %d" % (before, after))
    check("没有被误建的文件",
          not any("闭环测试" in p.name for p in Path(root["path"]).iterdir()))


def t_cleanup(keep: bool) -> None:
    print("\n【收尾】")
    call("/api/roots/remove", {"key": ROOT_KEY})
    roots = [r["path"] for r in (call("/api/roots").get("roots") or [])]
    check("临时素材目录已从配置里摘掉", not any(str(TMP).endswith(Path(p).name) and TMP.name in p for p in roots)
          if roots else False, str(roots))
    if not keep:
        shutil.rmtree(TMP, ignore_errors=True)
        check("临时文件已删除", not TMP.exists())
    else:
        print("  （--keep：临时目录留在 %s）" % TMP)


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    BASE = a.base.rstrip("/")

    print("=" * 68)
    print(" 媒体浏览器 · 整理功能闭环测试")
    print(" 目标：%s" % BASE)
    print("=" * 68)
    ping = call("/api/ping")
    if not ping.get("ok"):
        print("✗ 服务没起来（%s）" % BASE)
        return 2
    print(" 服务版本 v%s，播放器 %s，看图 %s" % (ping.get("version"), ping.get("video_player"), ping.get("viewer")))

    items = t_setup()
    if not items:
        print("✗ 临时素材没扫进来，后面没法测")
        return 2
    t_roots_writable()
    t_mkdir()
    t_rename_single(items)
    t_rename_affix()
    t_rename_replace()
    t_rename_seq()
    t_rename_conflict()
    t_move_existing()
    t_move_new_folder()
    t_readonly_chmod()
    t_readonly_mount()
    t_cleanup(a.keep)

    print("\n" + "=" * 68)
    print(" 通过 %d 项，失败 %d 项，跳过 %d 项" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print(" 失败：")
        for f in FAIL:
            print("   ✗ %s" % f)
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
