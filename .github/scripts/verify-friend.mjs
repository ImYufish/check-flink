// Playwright 验证友链申请：可访问性 + 反链核验
// 输入：verify_input.json（由 apply-friend.yml 的解析步骤写入）
// 输出：verify_result.json（含 pass / reachable / hasReciprocal / status / reason / engine）
import { chromium } from "playwright";
import fs from "fs";
import dns from "node:dns/promises";

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

// ---- SSRF 防护：仅允许 http/https，且目标主机不能是内网/保留地址 ----
// 公开仓库的公开 Issue 任何人可提交 URL，验证器若直接 fetch 任意地址，
// 可能被用来探测云元数据端点(169.254.169.254)或内网服务。故做 scheme 白名单 + 私有地址拦截。
function isHttpUrl(u) {
  try {
    const p = new URL(u);
    return p.protocol === "http:" || p.protocol === "https:";
  } catch {
    return false;
  }
}
function isPrivateIp(ip) {
  if (!ip) return true;
  if (ip.includes(":")) {
    const v = ip.toLowerCase();
    if (v === "::1" || v === "::" || v === "0:0:0:0:0:0:0:0") return true;
    if (v.startsWith("fe80")) return true; // 链路本地
    if (v.startsWith("fc") || v.startsWith("fd")) return true; // 唯一本地 fc00::/7
    return false;
  }
  const p = ip.split(".").map(Number);
  if (p.length !== 4 || p.some(Number.isNaN)) return true;
  if (p[0] === 0) return true;
  if (p[0] === 10) return true;
  if (p[0] === 127) return true;
  if (p[0] === 169 && p[1] === 254) return true;
  if (p[0] === 172 && p[1] >= 16 && p[1] <= 31) return true;
  if (p[0] === 192 && p[1] === 168) return true;
  return false;
}
async function isPrivateHost(hostname) {
  let addrs;
  try {
    addrs = await dns.lookup(hostname, { all: true });
  } catch {
    return true; // 解析失败 → 保守拒绝
  }
  for (const a of addrs) {
    if (isPrivateIp(a.address)) return true;
  }
  return false;
}
function getHostname(u) {
  try { return new URL(u).hostname; } catch { return ""; }
}

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

// === 安全闸门：任何网络访问前先校验 target ===
if (!isHttpUrl(target) || (await isPrivateHost(getHostname(target)))) {
  out.reason = "友链地址不合法或指向内网/保留地址，已拒绝访问（安全策略）";
  fs.writeFileSync(outputPath, JSON.stringify(out, null, 2));
  process.exit(0);
}

let browser;
try {
  browser = await chromium.launch();
  const page = await browser.newPage({
    userAgent: "Mozilla/5.0 (compatible; FriendLinkBot/1.0)",
  });
  page.setDefaultTimeout(12000);
  try {
    const resp = await page.goto(target, { waitUntil: "domcontentloaded", timeout: 12000 });
    out.status = resp ? resp.status() : 0;
    if (resp && resp.ok()) {
      out.reachable = true;
      // 给 JS 渲染（SPA / VitePress 等）时间，并滚动到底触发懒加载/无限滚动友链
      await page.waitForTimeout(2500);
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await page.waitForTimeout(800);
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
  }
} catch (e) {
  if (!browser) {
    // Playwright 启动失败（环境异常）→ 降级到 fetch 检测（target 已通过 SSRF 校验）
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
  out.reason = "验证异常：" + e.message;
} finally {
  if (browser) await browser.close();
}

out.pass = out.reachable && out.hasReciprocal;
fs.writeFileSync(outputPath, JSON.stringify(out, null, 2));
