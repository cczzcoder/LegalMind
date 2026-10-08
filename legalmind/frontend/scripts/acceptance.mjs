/**
 * 前端验收脚本（《前端界面说明》§4、§8 的**可重复**版本）。
 *
 * 此前这套检查是**仓外的临时脚本**（2026-10-07 / 10-08 各跑过一次，结论抄进了文档），
 * 跑完就没了——于是「触控目标有没有再退化」只能靠人记得。这里把它固化下来。
 *
 * **它不是 CI 门禁**：需要活的后端、真实的账号与语料，CI 里跑不起来（见 §7 已知债务）。
 * 定位是「改完界面手动跑一次」，和 §5 的人工验收清单并列。
 *
 * 跑法（需要 uvicorn 在 8000、vite 在 5173；后者**必须带 VITE_API_TARGET**）：
 *
 *   cd legalmind/frontend
 *   VITE_API_TARGET=http://127.0.0.1:8000 npm run dev     # 另开一个终端
 *   npm run acceptance
 *
 * 可用环境变量覆盖：
 *   ACCEPTANCE_BASE     默认 http://127.0.0.1:5173
 *   ACCEPTANCE_USER     默认 ui-version-reviewer
 *   ACCEPTANCE_PASSWORD 默认 UiVersion2026!
 *   ACCEPTANCE_ROUTES   默认见下面 ROUTES（逗号分隔）
 *   ACCEPTANCE_SHOTS    默认 1（截图落到 .acceptance/，已 gitignore）；置 0 关闭
 *
 * ⚠️ **默认账号是 `legal_reviewer`**（不是 `ui-editor`）：`legal_reviewer` 同时有
 * `document.read` 与 `review.decide`，于是**七个页面都能走到**（含待审队列与版本复核）；
 * `ui-editor` 没有 `review.decide`，那两个页面会 403 而被当成「失败请求」。
 * 代价是它没有 `document.write`，**文献管理页的上传按钮不会渲染**——那一个按钮不在本脚本覆盖内。
 *
 * 浏览器用**本机已装的 Chrome**（`channel: "chrome"`），不下载 Chromium。
 */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.ACCEPTANCE_BASE ?? "http://127.0.0.1:5173";
const USER = process.env.ACCEPTANCE_USER ?? "ui-version-reviewer";
const PASSWORD = process.env.ACCEPTANCE_PASSWORD ?? "UiVersion2026!";
const ROUTES = (
  process.env.ACCEPTANCE_ROUTES ?? "#/ask,#/review,#/versions,#/search,#/documents,#/wiki"
).split(",");
const SHOTS = process.env.ACCEPTANCE_SHOTS !== "0";

/** §4：这三档是验收口径，改这里等于改规格，别顺手加。 */
const WIDTHS = [375, 768, 1280];
/** §4：窄屏（< lg）可点击控件 ≥ 44×44；lg 以上是桌面，保持 antd 默认 32px。 */
const TOUCH_MIN = 44;
const LG = 992;

const failures = [];
const notes = [];

function fail(where, message) {
  failures.push(`${where}：${message}`);
}

/** 在页面里跑的采集函数（会被序列化送进浏览器，**不能引用外部变量**）。 */
const COLLECT = () => {
  const clickable = [];
  for (const el of document.querySelectorAll('a[href], button, [role="button"]')) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") continue;
    if (el.closest("[aria-hidden='true']")) continue;
    clickable.push({
      key: [
        el.tagName,
        String(el.className || ""),
        el.getAttribute("aria-label") || "",
        (el.textContent || "").replace(/\s+/g, " ").trim(),
      ].join("|"),
      label: el.getAttribute("aria-label") || (el.textContent || "").trim().slice(0, 16),
      // 定位用：只留外层标签与 class，够找到人就行，不要把正文灌进报告
      html: el.outerHTML.replace(/\s+/g, " ").slice(0, 110),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
    });
  }
  const sider = document.querySelector(".ant-layout-sider");
  return {
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    siderWidth: sider ? Math.round(sider.getBoundingClientRect().width) : 0,
    burger: Boolean(document.querySelector('button[aria-label="打开导航"]')),
    clickable,
  };
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const shotDir = SHOTS
  ? path.join(HERE, "..", ".acceptance", new Date().toISOString().replace(/[:.]/g, "-"))
  : null;
if (shotDir) await mkdir(shotDir, { recursive: true });

try {
  for (const width of WIDTHS) {
    const where = `${width}px`;
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    const page = await context.newPage();

    const bad = [];
    page.on("response", (response) => {
      // 首帧的 /auth/me 401 是「还没登录」的正常探测，不算失败
      if (response.status() >= 400 && !response.url().endsWith("/auth/me")) {
        bad.push(`${response.status()} ${response.url()}`);
      }
    });

    await page.goto(`${BASE}/#/login`, { waitUntil: "networkidle" });
    await page.fill('input[placeholder="用户名"]', USER);
    await page.fill('input[placeholder="密码"]', PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForTimeout(1500);
    if (page.url().includes("/login")) {
      fail(where, `登录失败（${USER}）——先确认后端在跑、账号存在`);
      await context.close();
      continue;
    }

    const seen = new Map();
    let sawSider = 0;
    let sawBurger = false;
    let maxOverflow = 0;

    for (const route of ROUTES) {
      await page.goto(`${BASE}/${route.trim()}`, { waitUntil: "networkidle" });
      await page.waitForTimeout(1000);

      const shot = await page.evaluate(COLLECT);
      maxOverflow = Math.max(maxOverflow, shot.overflow);
      sawSider = Math.max(sawSider, shot.siderWidth);
      sawBurger = sawBurger || shot.burger;
      for (const item of shot.clickable) if (!seen.has(item.key)) seen.set(item.key, item);

      // 窄屏还有抽屉：把菜单项与关闭按钮也量进去（它们只在抽屉打开时存在）
      if (shot.burger) {
        await page.click('button[aria-label="打开导航"]');
        await page.waitForTimeout(500);
        const inDrawer = await page.evaluate(COLLECT);
        for (const item of inDrawer.clickable) if (!seen.has(item.key)) seen.set(item.key, item);
        const drawer = await page.evaluate(() => {
          const node = document.querySelector(".ant-drawer-content-wrapper");
          return node ? Math.round(node.getBoundingClientRect().width) : 0;
        });
        if (drawer !== 248) fail(where, `抽屉宽度 ${drawer}，§4 规定 248`);
        if (shotDir) {
          await page.screenshot({ path: path.join(shotDir, `${width}-drawer.png`) });
        }
        await page.keyboard.press("Escape");
        await page.waitForTimeout(300);
      }

      if (shotDir) {
        const name = route.trim().replace(/[#/]/g, "") || "root";
        await page.screenshot({ path: path.join(shotDir, `${width}-${name}.png`) });
      }
    }

    // §4：窄屏走抽屉、lg 以上是固定侧栏 208px
    if (width < LG) {
      if (sawSider !== 0) fail(where, `窄屏不该出现固定侧栏，实测 ${sawSider}px`);
      if (!sawBurger) fail(where, '窄屏应有汉堡按钮（aria-label="打开导航"）');
    } else if (sawSider !== 208) {
      fail(where, `lg 以上固定侧栏应为 208px，实测 ${sawSider}px`);
    }

    if (maxOverflow !== 0) fail(where, `出现横向溢出 ${maxOverflow}px`);

    const small = [...seen.values()].filter((item) => item.w < TOUCH_MIN || item.h < TOUCH_MIN);
    if (width < LG && small.length) {
      for (const item of small) {
        fail(
          where,
          `触控目标 ${item.w}×${item.h} < 44×44：${item.label || "(无标签)"}\n      ${item.html}`,
        );
      }
    } else if (width >= LG) {
      notes.push(`${where} 有 ${small.length} 个控件小于 44×44——**预期**（§4 的规则只覆盖 < lg）`);
    }

    if (bad.length) {
      for (const line of bad.slice(0, 5)) fail(where, `失败请求 ${line}`);
    }

    notes.push(
      `${where}：可点击控件 ${seen.size} 个、横向溢出 ${maxOverflow}px、侧栏 ${sawSider}px`,
    );
    await context.close();
  }
} finally {
  await browser.close();
}

const report = [
  "前端验收（《前端界面说明》§4、§8）",
  `目标 ${BASE} · 账号 ${USER} · 视口 ${WIDTHS.join(" / ")}`,
  ...notes.map((line) => `  · ${line}`),
  failures.length ? `\n✗ ${failures.length} 项未通过：` : "\n✓ 全部通过",
  ...failures.map((line) => `  ✗ ${line}`),
  shotDir ? `\n截图：${path.relative(process.cwd(), shotDir)}` : "",
].join("\n");

console.log(report);
if (shotDir) await writeFile(path.join(shotDir, "report.txt"), `${report}\n`, "utf8");
process.exit(failures.length ? 1 : 0);
