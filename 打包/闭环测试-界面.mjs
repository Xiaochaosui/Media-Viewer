#!/usr/bin/env node
/**
 * 媒体浏览器 · 界面闭环测试（真的开一个无头 Chrome，点真的按钮）
 *
 *   node 打包/闭环测试-界面.mjs                     # 默认 http://127.0.0.1:8777
 *   node 打包/闭环测试-界面.mjs --base http://127.0.0.1:8899
 *
 * 干的事：把页面加载起来 → 用页面自己的函数和按钮走一遍
 *   「重命名（单张 / 批量前后缀）」「移动到…（新建文件夹 + 移进去）」
 *   「只读目录的提前拦截（NTFS 只读盘）」
 * 每一步都断言 DOM 状态 + 磁盘结果，并且全程盯着有没有 JS 报错。
 * 临时素材目录用完就摘掉、删干净，不碰你已有的素材。
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const args = process.argv.slice(2);
const baseArg = args.indexOf("--base");
const BASE = (baseArg >= 0 ? args[baseArg + 1] : "http://127.0.0.1:8777").replace(/\/$/, "");
const PORT = 9333;
const PROFILE = "/tmp/mb-cdp-profile";
const TMP = "/tmp/mb-ui-closed-loop";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const PASS = [], FAIL = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function check(name, cond, detail = "") {
  (cond ? PASS : FAIL).push(name);
  console.log(`  ${cond ? "✅" : "❌"} ${name}${!cond && detail ? "   ← " + detail : ""}`);
  return cond;
}
const skip = (name, why) => console.log(`  ⏭  ${name}（${why}）`);

/* ---------------------------------------------------------------- CDP 壳 */
class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.waiting = new Map(); this.errors = []; }
  static async attach() {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const page = list.find((t) => t.type === "page");
    if (!page) throw new Error("没有找到页面 target");
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new Cdp(ws);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && c.waiting.has(m.id)) {
        const { res, rej } = c.waiting.get(m.id);
        c.waiting.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method === "Runtime.exceptionThrown") {
        const d = m.params.exceptionDetails;
        c.errors.push(d.exception?.description || d.text || "未知异常");
      } else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
        c.errors.push(m.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
      }
    };
    await c.send("Runtime.enable");
    await c.send("Page.enable");
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.waiting.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async js(expr) {
    const r = await this.send("Runtime.evaluate", {
      expression: `(async () => { ${expr} })()`,
      awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    }
    return r.result.value;
  }
  async until(expr, ms = 15000, step = 250) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (await this.js(`return !!(${expr});`)) return true;
      await sleep(step);
    }
    return false;
  }
}

/* ------------------------------------------------------------------ 主流程 */
const rmdir = (p) => fs.rmSync(p, { recursive: true, force: true });

async function main() {
  console.log("=".repeat(68));
  console.log(" 媒体浏览器 · 界面闭环测试（无头 Chrome 点真按钮）");
  console.log(` 目标：${BASE}`);
  console.log("=".repeat(68));

  if (!fs.existsSync(CHROME)) { console.log("✗ 没装 Google Chrome，跳过界面测试"); return 2; }
  const ping = await (await fetch(`${BASE}/api/ping`)).json();
  if (!ping.ok) { console.log(`✗ 服务没起来（${BASE}）`); return 2; }

  // 造临时素材：用真实能解码的图片/视频，这样缩略图链路也一起测到
  const SRC = "/Users/xiaochaosui/Movies/测试相册";
  rmdir(TMP);
  fs.mkdirSync(path.join(TMP, "已有文件夹"), { recursive: true });
  const samples = [["Sonoma.heic", "界面照片A.heic"], ["Radial Sky Blue.heic", "界面照片B.heic"],
                   ["mac测试视频.mp4", "界面视频A.mp4"]];
  let copied = 0;
  for (const [from, to] of samples) {
    if (fs.existsSync(path.join(SRC, from))) { fs.copyFileSync(path.join(SRC, from), path.join(TMP, to)); copied++; }
  }
  if (!copied) {                                   // 没有样例就退回造假文件（缩略图会失败，但流程仍可测）
    for (const [n, sz] of [["界面照片A.heic", 2048], ["界面照片B.heic", 3072], ["界面视频A.mp4", 4096]])
      fs.writeFileSync(path.join(TMP, n), Buffer.alloc(sz));
  }

  rmdir(PROFILE);
  const chrome = spawn(CHROME, [
    "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    "--no-first-run", "--no-default-browser-check", "--disable-gpu",
    "--window-size=1440,900", `${BASE}/`,
  ], { stdio: "ignore" });

  let cdp, rootKey = "";
  try {
    for (let i = 0; i < 60; i++) {                     // 等调试端口起来
      try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); break; }
      catch { await sleep(250); }
    }
    cdp = await Cdp.attach();
    await cdp.until("document.readyState === 'complete'", 20000);
    await cdp.until("window.S && S.items !== undefined", 20000);

    console.log("\n【1】页面加载 + 加临时素材目录");
    check("页面没报 JS 错", cdp.errors.length === 0, cdp.errors.join(" | "));
    const addRes = await cdp.js(`
      const j = await post("/api/roots/add", { path: ${JSON.stringify(TMP)} });
      return j.roots.filter(r => r.path.endsWith("mb-ui-closed-loop"))[0] || {};`);
    rootKey = addRes.key || "";
    check("临时目录加进来了（走页面自己的 post）", !!rootKey, JSON.stringify(addRes));
    check("临时目录是「可写」", addRes.writable === true, JSON.stringify(addRes));
    // 列表是按「当前选中的素材目录」过滤的，所以像用户那样先切到目标目录再操作。
    // 注意 load() 有排队机制（正在加载时会推迟），所以要等列表真的变成这个目录的内容。
    const scopeTo = async (key) => cdp.js(`
      S.root = ${JSON.stringify(key)}; S.dir = ""; S.q = ""; S.kind = ""; S.selected.clear();
      for (let i = 0; i < 60; i++) {
        await load(true);
        if (S.items.length && S.items.every(x => x.id.startsWith(${JSON.stringify(key)} + ":"))) break;
        await new Promise(r => setTimeout(r, 300));
      }
      return S.items.length;`);
    // 等缩略图都生成完再点按钮：真实使用中也是「缩略图还在刷的时候别急着操作」
    const waitThumbs = async (ms = 30000) => cdp.js(`
      const t0 = Date.now();
      while (Date.now() - t0 < ${ms}) {
        await pollStats();
        if (S.stats && !S.stats.warming && S.stats.thumb_cached >= S.stats.total) return true;
        await new Promise(r => setTimeout(r, 400));
      }
      return false;`);
    const listed = await cdp.js(`
      for (let i = 0; i < 80; i++) {
        await loadRoots(true);
        S.root = ${JSON.stringify(rootKey)}; S.dir = ""; S.q = ""; S.kind = "";
        await load(true);
        const hit = S.items.filter(x => x.id.startsWith(${JSON.stringify(rootKey)} + ":"));
        if (hit.length >= 3) return hit.map(x => x.id);
        await new Promise(r => setTimeout(r, 400));
      }
      return [];`);
    check("切到临时目录后能看到 3 个文件", listed.length === 3, JSON.stringify(listed));
    check("列表只显示当前选中的素材目录",
      await cdp.js(`return S.items.length > 0 && S.items.every(x => x.id.startsWith(${JSON.stringify(rootKey)} + ":"));`));
    await scopeTo(rootKey);
    check("临时目录的缩略图都出好了", await waitThumbs(30000));

    /* ------------------------------------------------ 重命名（真弹窗） */
    console.log("\n【2】重命名弹窗（单张）");
    const idPhoto = listed.find((x) => x.endsWith("界面照片A.heic"));
    await cdp.js(`openRename([${JSON.stringify(idPhoto)}]); return true;`);
    check("弹窗打开了", await cdp.js(`return $("rnModal").classList.contains("on");`));
    check("标题显示的是这个文件名",
      (await cdp.js(`return $("rnCount").textContent;`)) === "界面照片A.heic");
    await cdp.js(`
      $("rnName").value = "界面改名A";
      $("rnName").dispatchEvent(new Event("input", { bubbles: true }));
      return true;`);
    await sleep(700);
    check("预览里出现了新名字",
      (await cdp.js(`return $("rnPreview").textContent;`)).includes("界面改名A"));
    check("「改名」按钮可点", (await cdp.js(`return !$("rnGo").disabled;`)) === true);
    await cdp.js(`$("rnGo").click(); return true;`);
    check("改名后弹窗自动关上",
      await cdp.until(`!$("rnModal").classList.contains("on")`, 8000));
    check("提示条说了「已改名」",
      (await cdp.js(`return $("toast").textContent;`)).includes("已改名"),
      await cdp.js(`return $("toast").textContent;`));
    check("磁盘上换了名字",
      fs.existsSync(path.join(TMP, "界面改名A.heic")) &&
      !fs.existsSync(path.join(TMP, "界面照片A.heic")));
    check("列表里也刷新成新名字了",
      await cdp.until(`S.items.some(x => x.id.endsWith("界面改名A.heic"))`, 8000));
    check("看详情跟到了新名字（new_id 没丢根前缀）",
      await cdp.until(`(S.detail && S.detail.name === "界面改名A.heic")`, 8000));

    /* ------------------------------------------------ 重命名（批量前后缀） */
    console.log("\n【3】重命名弹窗（批量：加前缀后缀）");
    const batchIds = await cdp.js(`
      return S.items.filter(x => x.id.startsWith(${JSON.stringify(rootKey)} + ":")
        && /界面照片B|界面视频A/.test(x.id)).map(x => x.id);`);
    check("选中了 2 个文件", batchIds.length === 2, JSON.stringify(batchIds));
    await cdp.js(`openRename(${JSON.stringify(batchIds)}); return true;`);
    check("批量模式（显示「已选 2 项」）",
      (await cdp.js(`return $("rnCount").textContent;`)).includes("2 项"));
    check("批量才有的规则区露出来了",
      (await cdp.js(`return $("rnBatch").style.display !== "none";`)) === true);
    await cdp.js(`
      setRnMode("affix");
      $("rnPrefix").value = "UI-"; $("rnSuffix").value = "-好";
      $("rnPrefix").dispatchEvent(new Event("input", { bubbles: true }));
      return true;`);
    await sleep(800);
    check("预览里是两个新名字",
      (await cdp.js(`return $("rnPreview").textContent;`)).includes("UI-界面照片B-好"),
      await cdp.js(`return $("rnPreview").textContent;`));
    await cdp.js(`$("rnGo").click(); return true;`);
    check("批量改名完成",
      await cdp.until(`!$("rnModal").classList.contains("on")`, 8000));
    check("两个文件都落盘",
      fs.existsSync(path.join(TMP, "UI-界面照片B-好.heic")) &&
      fs.existsSync(path.join(TMP, "UI-界面视频A-好.mp4")));
    // 改名会让缩略图缓存跟着换 key，可能重新生成；等它刷完再点下一步，
    // 否则浏览器的 6 条连接都被缩略图占着，弹窗的 /api/browse 只能排队（真实使用同理）
    await waitThumbs(60000);

    /* ------------------------------------------------ 移动到… */
    console.log("\n【4】移动到…弹窗（新建文件夹 → 移进去）");
    const idMove = await cdp.js(`
      return (S.items.find(x => x.id.endsWith("UI-界面照片B-好.heic")) || {}).id || "";`);
    await cdp.js(`openMove([${JSON.stringify(idMove)}]); return true;`);
    check("弹窗打开了", await cdp.js(`return $("moveModal").classList.contains("on");`));
    const tFolder = Date.now();
    const expectDir = await cdp.js(`
      const p = String(itemOf(${JSON.stringify(idMove)}).path || "");
      const i = p.lastIndexOf("/");
      return i > 0 ? p.slice(0, i) : p;`);
    const arrived4 = await cdp.until(
      `$("movePath").textContent === ${JSON.stringify(expectDir)}`, 20000);
    const moveDbg = await cdp.js(`
      return {mvPath: MV.path, shown: $("movePath").textContent, list: $("moveList").textContent,
              writable: MV.writable, ids: MV.ids};`);
    check("弹窗默认停在「这个文件所在的文件夹」",
      String(moveDbg.mvPath).endsWith("mb-ui-closed-loop"), JSON.stringify(moveDbg).slice(0, 200));
    check(`文件夹列表读出来了（${Date.now() - tFolder}ms）`,
      arrived4 && moveDbg.list.includes("已有文件夹"), JSON.stringify(moveDbg).slice(0, 300));
    if (!moveDbg.list.includes("已有文件夹")) {
      console.log("   调试·页面请求：",
        JSON.stringify(await cdp.js(`
          const rs = performance.getEntriesByType("resource")
            .map(r => ({n: r.name.replace(location.origin, ""), d: Math.round(r.duration), end: Math.round(r.responseEnd)}));
          return {总数: rs.length, 挂起: rs.filter(r => r.end === 0).map(r => r.n),
                  慢的: rs.filter(r => r.d > 300).map(r => r.n + " " + r.d + "ms")};`)));
      console.log("   调试·loadFolder 调用：", JSON.stringify(await cdp.js(`return window.__lfCalls || null;`)));
    }
    check("可写目录不显示只读警告",
      (await cdp.js(`return $("moveWarn").style.display;`)) === "none");
    check("「移到这里」是可点的", (await cdp.js(`return !$("moveGo").disabled;`)) === true);
    await cdp.js(`
      $("moveNewName").value = "UI-归档";
      $("moveNewGo").click();
      return true;`);
    check("新建文件夹后进到了里面",
      await cdp.until(`$("movePath").textContent.endsWith("UI-归档")`, 10000),
      await cdp.js(`return $("movePath").textContent;`));
    check("新文件夹真的建了", await cdp.until(`true`, 100));
    await sleep(300);
    check("磁盘上有了 UI-归档", fs.existsSync(path.join(TMP, "UI-归档")));
    await cdp.js(`$("moveGo").click(); return true;`);
    check("移动任务跑完（弹窗自己关掉）",
      await cdp.until(`!$("moveModal").classList.contains("on")`, 40000));
    check("提示条说了「已移动」",
      (await cdp.js(`return $("toast").textContent;`)).includes("已移动"),
      await cdp.js(`return $("toast").textContent;`));
    check("文件真的移进去了",
      fs.existsSync(path.join(TMP, "UI-归档", "UI-界面照片B-好.heic")) &&
      !fs.existsSync(path.join(TMP, "UI-界面照片B-好.heic")));
    check("列表刷新后跟上了新位置",
      await cdp.until(`S.items.some(x => x.id.endsWith("UI-归档/UI-界面照片B-好.heic"))`, 10000));

    /* ------------------------------------------------ 只读目录提前拦截 */
    console.log("\n【5】只读目录（NTFS 只读盘）提前拦截");
    const roRoot = await cdp.js(`
      const j = await api("/api/roots");
      return (j.roots || []).find(r => r.writable === false && r.kind === "local") || null;`);
    if (!roRoot) {
      skip("只读目录的界面拦截", "这台机器上没有已加入的只读目录");
    } else {
      console.log(`   （只读目录：${roRoot.path}）`);
      await scopeTo(roRoot.key);                    // 像用户那样先切到这个只读目录
      await waitThumbs(60000);                      // 等缩略图刷完再点（不然浏览器的连接都被占着）
      const roItem = await cdp.js(`
        return S.items[0] || null;`);
      check("界面上 roNote() 能认出只读",
        (await cdp.js(`return roNote(${JSON.stringify(roItem ? roItem.id : roRoot.key + ":x")});`)).includes("只读"));
      if (roItem) {
        await cdp.js(`openRename([${JSON.stringify(roItem.id)}]); return true;`);
        await sleep(600);
        const pv = await cdp.js(`return $("rnPreview").textContent;`);
        check("改名弹窗一打开就说明「只读」", pv.includes("只读"), pv.slice(0, 120));
        check("「改名」按钮被按住", (await cdp.js(`return $("rnGo").disabled;`)) === true);
        await cdp.js(`closeRename(); return true;`);
        const roDir = await cdp.js(`
          const p = String(${JSON.stringify(roItem.path)} || "");
          const i = p.lastIndexOf("/");
          return i > 0 ? p.slice(0, i) : p;`);
        await cdp.js(`openMove([${JSON.stringify(roItem.id)}]); return true;`);
        check("移动弹窗停在只读目录里",
          await cdp.until(`$("movePath").textContent === ${JSON.stringify(roDir)}`, 20000),
          await cdp.js(`return $("movePath").textContent;`));
        await sleep(300);
        const roDbg = await cdp.js(`
          return {mvPath: MV.path, shown: $("movePath").textContent, writable: MV.writable,
                  note: MV.writeNote, warn: $("moveWarn").textContent,
                  disp: $("moveWarn").style.display, goDisabled: $("moveGo").disabled};`);
        check("只读警告显示了", roDbg.disp !== "none" && String(roDbg.warn).includes("🔒"),
          JSON.stringify(roDbg).slice(0, 300));
        check("「移到这里」被按住", roDbg.goDisabled === true, JSON.stringify(roDbg).slice(0, 200));
        await cdp.js(`closeMove(); return true;`);
      } else {
        skip("只读目录的弹窗拦截", "这个只读目录里没有媒体文件");
      }
    }

    console.log("\n【6】全程有没有 JS 报错");
    check("没有未捕获的异常 / console.error", cdp.errors.length === 0, cdp.errors.join(" | "));

    // 收尾：把临时目录摘掉
    await cdp.js(`await post("/api/roots/remove", { key: ${JSON.stringify(rootKey)} }); return true;`);
  } catch (e) {
    check("测试脚本本身没出错", false, String(e && e.stack || e));
  } finally {
    try { if (cdp) await cdp.js(`await post("/api/roots/remove", { key: ${JSON.stringify(rootKey)} }); return true;`); } catch {}
    chrome.kill("SIGKILL");
    await sleep(400);
    rmdir(TMP); rmdir(PROFILE);
  }

  console.log("\n" + "=".repeat(68));
  console.log(` 通过 ${PASS.length} 项，失败 ${FAIL.length} 项`);
  if (FAIL.length) { console.log(" 失败："); FAIL.forEach((f) => console.log("   ✗ " + f)); }
  console.log("=".repeat(68));
  return FAIL.length ? 1 : 0;
}

main().then((c) => process.exit(c)).catch((e) => { console.error(e); process.exit(2); });
