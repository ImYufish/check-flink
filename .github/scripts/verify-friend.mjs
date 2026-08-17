// Playwright 验证友链申请：可访问性 + 反链核验
// 输入：verify_input.json（由 apply-friend.yml 的解析步骤写入）
// 输出：verify_result.json（含 pass / reachable / hasReciprocal / status / reason / engine）
import { chromium } from "playwright";
import fs from "fs";

const inputPath = process.env.VERIFY_INPUT || "verify_input.json";
const outputPath = process.env.VERIFY_RESULT || "verify_result.json";

const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const siteurl = input.siteurl;
const linkpage = input.linkpage || "";
const authorUrl = input.authorUrl || "https://x1anyu.cn";
const target = linkpage.trim() ? linkpage.trim() : siteurl;

const out = {
  reachable: false,
  hasReciprocal: false,
  status: 0,
  reason: "",
  pass: false,
  engine: "playwright",
};

// 判断页面是否含指向本站的真实超链接（兼容 <a href> / data-url / 跳转型内嵌 URL）
function containsAuthorLink(html, authorUrl) {
  const bare = authorUrl.replace(/^https?:\/\//i, "").replace(/\/+$/, "").toLowerCase();
  const variants = new Set([bare, "www." + bare]);
  if (bare.startsWith("www.")) variants.add(bare.slice(4));
  const attrRe = /(?:href|data-url|data-link|data-href)\s*=\s*["']([^"']+)["']/gi;
  let m, candidates = [];
  while ((m = attrRe.exec(html))) candidates.push(m[1]);
  const expanded = candidates.slice();
  for (const c of candidates) {
    for (const inner of c.matchAll(/https?:\/\/([^/?#\s"'>]+)/g)) expanded.push("https://" + inner[1]);
    for (const inner of c.matchAll(/(?<!:)\/\/([^/?#\s"'>]+)/g)) expanded.push("//" + inner[1]);
  }
  for (const u of expanded) {
    const host = u.replace(/^https?:\/\//i, "").replace(/^\/\//, "").split("/")[0].toLowerCase();
    if (variants.has(host)) return true;
  }
  return false;
}

// Playwright 不可用时的降级：纯 fetch + 正则（静态 HTML）
async function checkWithFetch(url, author) {
  const resp = await fetch(url, {
    method: "GET",
    redirect: "follow",
    headers: { "User-Agent": "Mozilla/5.0 (compatible; FriendLinkBot/1.0)" },
  });
  const status = resp.status;
  if (status < 200 || status >= 400) {
    return { reachable: false, hasReciprocal: false, status, reason: "站点不可访问（HTTP " + status + "）" };
  }
  const html = await resp.text();
  const has = containsAuthorLink(html, author);
  return {
    reachable: true,
    hasReciprocal: has,
    status,
    reason: has ? "" : "未检测到指向 " + author + " 的真实友链",
  };
}

try {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (launchErr) {
    // Playwright 启动失败（环境异常）→ 降级到 fetch 检测，不中断流程
    const r = await checkWithFetch(target, authorUrl);
    out.reachable = r.reachable;
    out.hasReciprocal = r.hasReciprocal;
    out.status = r.status;
    out.reason = r.reason;
    out.engine = "fetch-fallback";
    out.pass = out.reachable && out.hasReciprocal;
    fs.writeFileSync(outputPath, JSON.stringify(out, null, 2));
    process.exit(0);
  }

  const page = await browser.newPage({
    userAgent: "Mozilla/5.0 (compatible; FriendLinkBot/1.0)",
  });
  page.setDefaultTimeout(12000);
  try {
    const resp = await page.goto(target, { waitUntil: "domcontentloaded", timeout: 12000 });
    out.status = resp ? resp.status() : 0;
    if (resp && resp.ok()) {
      out.reachable = true;
      // 给 JS 渲染（SPA / VitePress 等）一点时间
      await page.waitForTimeout(1500);
      const html = await page.content();
      out.hasReciprocal = containsAuthorLink(html, authorUrl);
      if (!out.hasReciprocal) {
        out.reason = "未在你的友链页检测到指向 " + authorUrl + " 的真实友链";
      }
    } else {
      out.reason = "站点不可访问（HTTP " + out.status + "）";
    }
  } catch (e) {
    out.reason = "访问失败：" + e.message;
  } finally {
    await browser.close();
  }
} catch (e) {
  out.reason = "验证异常：" + e.message;
}

out.pass = out.reachable && out.hasReciprocal;
fs.writeFileSync(outputPath, JSON.stringify(out, null, 2));
