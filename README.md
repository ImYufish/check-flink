<div align="center">

# 🔗 友情链接自动检查 + 主页截图

**基于 [willow-god/check-flink](https://github.com/willow-god/check-flink) + [thun888/Python-WebSite-Screenshot](https://github.com/thun888/Python-WebSite-Screenshot) 深度整合版**

自动检测博客友链可达性 + 自动截取友链主页，一站式部署。

![示例](https://raw.githubusercontent.com/willow-god/check-flink/main/static/pic-doc/show.png)

---

**核心功能** · **部署教程** · **截图机制** · **常见问题**

</div>

---

## 📑 目录

- [✨ 核心特性](#-核心特性)
- [🆚 与原版差异](#-与原版差异)
- [📁 项目结构](#-项目结构)
- [🚀 部署教程](#-部署教程)
  - [Step 1 · Fork 仓库](#step-1--fork-仓库)
  - [Step 2 · 配置 GitHub Secrets](#step-2--配置-github-secrets)
  - [Step 3 · 开启 Actions 写权限](#step-3--开启-actions-写权限)
  - [Step 4 · 准备友链数据源](#step-4--准备友链数据源)
  - [Step 5 · 准备图床（截图上传）](#step-5--准备图床截图上传)
  - [Step 6 · 首次触发 Workflow](#step-6--首次触发-workflow)
  - [Step 7 · 部署到 Vercel](#step-7--部署到-vercel)
  - [Step 8 · 博客侧接入](#step-8--博客侧接入)
- [📸 截图工作机制](#-截图工作机制)
- [🎯 触发方式详解](#-触发方式详解)
- [⚙️ 高级配置](#-高级配置)
- [🐛 常见问题](#-常见问题)
- [📜 二次开发](#-二次开发)
- [📄 License](#-license)

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| **🔍 友链状态检测** | 每天 2 次自动巡检，统计延迟 + 失败次数 + 反链存在 |
| **📸 主页截图自动生成** | 每 6 天一次 Selenium 截图，自动上传图床 |
| **🎯 智能兜底链** | Selenium 失败 → thum.io 在线截图 → 无白屏 |
| **🔌 单一数据源** | 友链配置只在博客侧维护，截图仓库自动读取 |
| **📊 result.json 统一输出** | 状态 + 截图 URL 合并到一个 JSON，前端一次拉取 |
| **🎯 灵活触发 + 增量处理** | 定时全自动 + 手动按需，可只处理单个友链，其余保留历史状态 |
| **🔗 精准反链检测** | 仅识别真实 `<a href>` 超链接，纯文本提及不算反链 |
| **🚀 GitHub Action 全自动** | 无服务器，零运维 |
| **🧹 图床自动清理** | 上传前自动调用 cfbed 删除 API 清除同名旧图，避免资源堆积 |

---

## 🆚 与原版差异

> 本项目由两个开源项目深度整合而成

### 1. 上游项目

| 项目 | 角色 | 关键功能 |
|------|------|----------|
| [willow-god/check-flink](https://github.com/willow-god/check-flink) | 状态检测核心 | 多线程延迟检测、xxapi 兜底、反链检测 |
| [thun888/Python-WebSite-Screenshot](https://github.com/thun888/Python-WebSite-Screenshot) | 截图核心 | Selenium + Chrome + 隐藏滚动条 + thum.io 兜底 |

### 2. 本项目在原版基础上的修改

#### 📝 `main.py`（**多处改动**）

- 保留 `link_status` 中的历史 `siteshot` 字段，避免每次状态检测时把截图清空
- 新增 `TARGET_LINK` 环境变量支持：只检测指定友链，其余保留历史状态（增量合并）
- 重写反链检测函数 `check_author_link_in_page`：用正则提取真实 `<a href>` 并精确比对主机名，纯文本出现不再误判为反链

```python
'siteshot': prev_entry.get('siteshot', ''),  # 保留历史截图，由 screenshot_runner 更新
```

#### ➕ `screenshot.py`（**新增 186 行**）

完整截图模块，**移植自 thun888**，但做了以下适配：

| 项 | 原 thun888 | 本项目 |
|----|-----------|--------|
| 上传逻辑 | 推送到 `page` 分支 + GitHub Pages | 推送到 **cfbed.sanyue.de 兼容 API** |
| 兜底逻辑 | 404.png 本地占位 | **thum.io 在线截图**（永远可用） |
| 失败回退 | 直接失败 | **Selenium 失败 → thum.io**；**上传失败 → thum.io** |
| 触发方式 | 独立 cron | **从 `result.json` 读取已检测的友链**，只对 `latency > 0` 的友链截图 |

#### ➕ `screenshot_runner.py`（**新增 93 行**）

独立入口，**解耦截图与状态检测**，支持 `TARGET_LINK` 过滤（只截指定友链）：

```text
check_links job (每天 2 次) ─┐
                            ├→ result.json (含 status + 历史 siteshot)
take_screenshots job (6 天) ┘

TARGET_LINK="某友链" → 只检测/截图该友链，其余保留上次结果
```

#### ➕ `inject.css`（**新增 31 行**）

截图时注入到目标页面，**移植自 thun888**：

- 隐藏滚动条
- 触发懒加载图片
- 移除 cookie 横幅 / 广告弹窗

#### ✏️ `requirements.txt`（**改 3 行**）

新增依赖：
```diff
requests==2.32.3
+ selenium==4.34.2
+ webdriver-manager==4.0.2
+ Pillow==11.3.0
```

#### ✏️ `.github/workflows/check_links.yml`（**大幅改造**）

最关键的改动 —— **2 个独立 Job + 条件触发 + 手动输入参数**：

| Job | 触发条件 | 用途 |
|-----|----------|------|
| `check_links` | 每日定时 / 手动（除「仅截图」外） | 状态检测，**轻量**（仅 requests） |
| `take_screenshots` | 6 天定时 / 手动选「截图」或「全部」 | 主页截图，**重型**（Selenium + Chrome） |

**手动触发参数**（workflow_dispatch）：
- `task`：选择执行内容（status_only / screenshots_only / both）
- `target_link`：只处理指定友链（填名称或 URL 关键词，留空 = 全部）

**为什么不合并成一个 job**？
- Selenium + Chrome 安装耗时 ~2-3 分钟
- 截图跑完 ~5-10 分钟
- 分开后状态检测每次只需 ~1 分钟，结果更新更及时

> 📌 两个 Job 现已支持**条件触发**与**手动按需运行**（含只处理单个友链），详见 [🎯 触发方式详解](#-触发方式详解)。

#### ✏️ `.env.example`（**改 10 行**）

新增 3 个截图相关环境变量：

```env
IMG_UPLOAD_URL=https://tu.fqzlr.com/upload
IMG_AUTH_CODE=
IMG_UPLOAD_FOLDER=youlian
```

---

## 📁 项目结构

```plaintext
check-flink/
├── .github/
│   └── workflows/
│       └── check_links.yml       # ⭐ 核心：双 Job 配置（状态 + 截图）
├── main.py                        # 状态检测（保留历史 siteshot 字段）
├── screenshot.py                  # ⭐ 截图 + 上传 + 兜底
├── screenshot_runner.py           # ⭐ 截图入口（独立 job 调用）
├── inject.css                     # 截图时注入的 CSS
├── requirements.txt               # 依赖（含 selenium）
├── .env.example                   # 环境变量示例
├── link.example.csv               # CSV 数据源示例
├── static/
│   ├── index.html                 # 原作者自带的结果展示页
│   ├── result.json                # 自动生成：状态 + 截图统一数据
│   └── pic-doc/                   # README 配图
└── README.md                      # 本文件
```

### 关键文件说明

| 文件 | 作用 | 何时被调用 |
|------|------|------------|
| `main.py` | 读友链 → 测延迟 → 写 result.json | check_links job（每天 2 次） |
| `screenshot.py` | 单条截图 + 上传 + 兜底 | 被 screenshot_runner 调用 |
| `screenshot_runner.py` | 遍历 result.json → 调用 screenshot → 写回 | take_screenshots job（6 天 1 次） |
| `inject.css` | 隐藏截图时的滚动条 | screenshot.py 内部注入到目标页面 |
| `result.json` | 最终输出，被博客前端 fetch | 每次 job 结束后更新 |

---

## 🚀 部署教程

> ⏱️ 全程约 30 分钟
> 
> 💰 零成本：GitHub Actions 免费额度 + Vercel Hobby 计划 + cfbed 图床

### Step 1 · Fork 仓库

1. 访问 [github.com/willow-god/check-flink](https://github.com/willow-god/check-flink)
2. 点击右上角 **Fork** 按钮
3. 仓库名可自定义（如 `check-flink`、`friend-monitor`）
4. ⚠️ **不要勾选** "Copy the main branch only"，因为我们要用 `page` 分支托管

或者用命令行：

```bash
git clone https://github.com/willow-god/check-flink.git
cd check-flink
git remote set-url origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### Step 2 · 配置 GitHub Secrets

进入 **Settings → Secrets and variables → Actions → New repository secret**

#### 必填项

| Secret 名称 | 值 | 说明 |
|------------|----|------|
| `SOURCE_URL` | `https://你的博客.com/friends.json` | 友链数据源 URL（详见 Step 4） |
| `AUTHOR_URL` | `fqzlr.com` | 你的博客域名（用于反链检测） |
| `IMG_UPLOAD_URL` | `https://tu.fqzlr.com/upload` | 图床上传端点（cfbed.sanyue.de 兼容） |
| `IMG_AUTH_CODE` | （从图床后台获取） | 上传认证码 |

#### 可选项

| Secret 名称 | 值 | 说明 |
|------------|----|------|
| `PROXY_URL` | `https://nginx.example.com/` | CloudFlare Worker 代理（提升准确率） |
| `IMG_UPLOAD_FOLDER` | `youlian` | 上传目录，默认 `youlian` |

#### 添加截图（参考）

```
Settings → Secrets and variables → Actions
  ↓
New repository secret
  ↓
Name: IMG_UPLOAD_URL
Secret: https://tu.fqzlr.com/upload
  ↓
Add secret
```

### Step 3 · 开启 Actions 写权限

> ⚠️ **必做**，否则 workflow 推送到 `page` 分支会失败

1. 进入仓库 **Settings → Actions → General**
2. 滚到最下面 **Workflow permissions**
3. 选择 **Read and write permissions**
4. ✅ 勾选 **Allow GitHub Actions to create and approve pull requests**
5. 点击 **Save**

### Step 4 · 准备友链数据源

> 这是博客侧的工作，但必须在 check-flink 配置 `SOURCE_URL` 之前完成

#### 方案 A：JSON 端点（**推荐**）

在博客侧新增一个 Astro 端点（如 `src/pages/friends.json.ts`）：

```ts
import { friendsConfig } from "@/config/friendsConfig";
import type { APIRoute } from "astro";

export const GET: APIRoute = async () => {
  const enabled = friendsConfig.filter(f => f.enabled !== false);
  return new Response(
    JSON.stringify({
      link_list: enabled.map(f => ({
        name: f.title,
        link: f.siteurl,
        avatar: f.imgurl,
        descr: f.desc,
        siteshot: "",  // 留空，由 check-flink 填充
        linkpage: f.linkpage || "",  // 可选：友链页面 URL，用于反链检测
      })),
      length: enabled.length,
    }),
    {
      headers: { "Content-Type": "application/json" },
    }
  );
};
```

部署后访问 `https://你的博客.com/friends.json`，应返回上述结构。

#### 方案 B：CSV 文件（简单）

在仓库根目录创建 `link.csv`：

```csv
清羽飞扬,https://blog.liushen.fun/,https://blog.liushen.fun/link/
ChrisKim,https://www.zouht.com/,https://www.zouht.com/link-exchange
Akilar,https://akilar.top/,https://akilar.top/link/
张洪Heo,https://blog.zhheo.com/,https://blog.zhheo.com/link/
```

格式：`name,link,linkpage`

然后设置 Secret：
```
SOURCE_URL = ./link.csv
```

### Step 5 · 准备图床（截图上传）

> 如果你已有 cfbed 兼容图床，跳过此步

#### 方案 A：用 [cfbed](https://cfbed.sanyue.de/) 自建

参考 [cfbed 文档](https://cfbed.sanyue.de/api/upload.html) 部署 CloudFlare Workers 图床。

部署后获取：
- `IMG_UPLOAD_URL`：`https://你的域名.com/upload`
- `IMG_AUTH_CODE`：Workers 配置里的 `AUTH_CODE`

#### 方案 B：用现成服务

- [imgbb.com](https://imgbb.com/)（免费但无 authCode）
- [SM.MS](https://sm.ms/)（国内访问快）
- 自建兰空图床 / Lsky Pro

> ⚠️ **API 必须兼容 cfbed 规范**（POST + `authCode` + `uploadFolder`）

#### API 调用规范（参考）

```http
POST /upload?authCode=xxx&uploadFolder=youlian&uploadNameType=origin
Content-Type: multipart/form-data

file=<binary PNG>
```

成功响应：

```json
[
  {
    "publicUrl": "/youlian/blog.example.com.png",
    "src": "/youlian/blog.example.com.png",
    "name": "blog.example.com.png",
    "size": 123456
  }
]
```

上传后可通过 `https://你的域名.com/youlian/blog.example.com.png` 访问。

### Step 6 · 首次触发 Workflow

1. 进入仓库 **Actions** 标签
2. 左侧选择 **Check Links and Generate JSON**
3. 右侧点击 **Run workflow**
4. 选择任务类型：
   - `status_only`（默认）：仅状态检测
   - `screenshots_only`：仅截图（使用最近一次检测数据）
   - `both`：状态检测 + 截图
5. `target_link` 留空（首次需全量跑）
6. 点击 **Run workflow**（绿色按钮）
7. 等待约 1-3 分钟（状态检测）/ 5-10 分钟（含截图）

> 💡 首次部署建议选 `both`，后续日常用 `status_only` 即可

### Step 7 · 部署到 Vercel

> 让 `result.json` 走 CDN 加速，国内访问友好

1. 访问 [vercel.com/new](https://vercel.com/new)
2. **Import** 你的 check-flink 仓库
3. 配置：
   - **Project Name**：任意
   - **Framework Preset**：选 `Other`
   - **Root Directory**：`.`（默认）
   - **Build Command**：留空
   - **Output Directory**：`static`
4. 点击 **Deploy**
5. 部署完成后会得到一个 URL，如 `https://check-flink-xxx.vercel.app`

#### 验证部署

```bash
curl https://check-flink-xxx.vercel.app/result.json | head -50
```

应看到类似：

```json
{
    "timestamp": "2026-07-25 20:30:00",
    "accessible_count": 43,
    "inaccessible_count": 4,
    "total_count": 47,
    "has_author_link_count": 12,
    "author_url": "fqzlr.com",
    "link_status": [
        {
            "name": "番茄主理人",
            "link": "https://fqzlr.com/",
            "latency": 0.25,
            "fail_count": 0,
            "has_author_link": true,
            "linkpage": "",
            "siteshot": "https://tu.fqzlr.com/youlian/fqzlr.com.png"
        }
    ]
}
```

### Step 8 · 博客侧接入

在博客 `Layout.astro` 中 fetch 这个 result.json：

```js
const CHECK_FLINK_RESULT_URL = "https://check-flink-xxx.vercel.app/result.json";

async function applyFriendStatus() {
  const res = await fetch(CHECK_FLINK_RESULT_URL);
  const data = await res.json();
  
  document.querySelectorAll(".friend-card-link").forEach(card => {
    const link = (card.getAttribute("href") || "").replace(/\/$/, "");
    const info = data.link_status.find(x => x.link.replace(/\/$/, "") === link);
    if (!info) return;
    
    // 1. 状态徽章
    const statusEl = card.querySelector(".friend-status");
    if (statusEl) {
      statusEl.setAttribute("data-status", 
        info.latency < 1 ? "success" :
        info.latency < 2 ? "slow" :
        info.latency < 999 ? "warn" : "timeout"
      );
      statusEl.textContent = info.latency < 0 ? "超时" : 
        (Math.round(info.latency * 1000) + " MS");
      statusEl.style.display = "inline-flex";
    }
    
    // 2. 截图背景
    if (info.siteshot) {
      const screenshot = card.querySelector(".friend-card-screenshot");
      if (screenshot) {
        screenshot.style.setProperty("--siteshot-url", `url("${info.siteshot}")`);
        card.setAttribute("data-siteshot", info.siteshot);
      }
    }
  });
}

document.addEventListener("astro:page-load", applyFriendStatus);
```

> 完整的前端组件（友链卡片 + 状态徽章 + 截图背景）参考 [blog-rogue-craft skill](https://blog.liushen.fun/posts/ai/ai-blog-ai-zero-editing/) 实现。

---

## 📸 截图工作机制

### 数据流

```
博客 friendsConfig.ts (47 友链)
   ↓ Astro 构建
https://你的博客.com/friends.json
   ↓ check-flink check_links job 拉取
   ↓ main.py 测延迟（requests，~1 分钟）
   ↓
result.json (含 status + 保留历史 siteshot)
   ↓ take_screenshots job 拉取
   ↓ screenshot_runner.py 遍历可达友链
   ↓ 对每个友链：screenshot.py
   ↓   ├── Selenium + Chrome 截图
   ↓   ├── 上传到 IMG_UPLOAD_URL
   ↓   └── 失败 → thum.io 兜底
   ↓
result.json (含 status + 新 siteshot)
   ↓ git push --force HEAD:page
   ↓ Vercel 自动重新部署
https://check-flink-xxx.vercel.app/result.json
   ↓ 博客 Layout.astro fetch
FriendCard 背景图渲染
```

### 截图参数

| 参数 | 值 | 来源 |
|------|-----|------|
| 视窗大小 | 1280 × 800 | `screenshot.py:WINDOW_WIDTH/HEIGHT` |
| 页面等待 | 3 秒 | `screenshot.py:PAGE_LOAD_WAIT` |
| 浏览器 | Chrome stable | `browser-actions/setup-chrome@v2` |
| 字体 | 文泉驿微米黑 / 字体 | `fonts-wqy-microhei fonts-wqy-zenhei` |
| 滚动条 | 隐藏 | `inject.css` |
| 懒加载图 | 强制 eager | `inject.css` |

### 兜底链（三级降级）

```text
Level 1：Selenium 本地截图
  ├─ 成功 → 上传图床
  │   ├─ 成功 → https://tu.xxx.com/youlian/host.png
  │   └─ 失败（401 等）↓ Level 2
  └─ 失败（chromedriver 异常等）↓ Level 2

Level 2：thum.io 在线截图
  └─ https://image.thum.io/get/width/1280/crop/800/png/{url}
     （永远可用，免费但有 ~1 秒延迟）

Level 3：（本项目未实现，但可扩展）
  └─ 404.png 本地占位
```

---

## 🎯 触发方式详解

### 自动触发（定时）

| cron 表达式 | 执行内容 | 说明 |
|-------------|----------|------|
| `0 1 * * *` | 仅状态检测 | 每天 01:00 |
| `0 13 * * *` | 仅状态检测 | 每天 13:00 |
| `30 1 1,7,13,19,25 * *` | 状态检测 + 截图 | 每 6 天（1/7/13/19/25 号 01:30） |

> 截图 job 通过 `github.event.schedule` 判断是否为截图 cron，避免每天跑重型 Selenium

### 手动触发（workflow_dispatch）

| 参数 | 类型 | 说明 |
|------|------|------|
| `task` | choice | `status_only`（默认）/ `screenshots_only` / `both` |
| `target_link` | string | 只处理指定友链（名称或 URL 关键词），留空 = 全部 |

**组合示例**：

| 场景 | task | target_link |
|------|------|-------------|
| 日常巡检 | `status_only` | 留空 |
| 新增友链后只检测它 | `status_only` | `番茄` |
| 新增友链后检测+截图 | `both` | `blog.example.com` |
| 重新截全部友链 | `screenshots_only` | 留空 |
| 全量检测+截图 | `both` | 留空 |

### Job 触发条件

```yaml
check_links:
  # 除「手动选仅截图」外，其余情况都执行状态检测
  if: ${{ github.event_name != 'workflow_dispatch' || inputs.task != 'screenshots_only' }}

take_screenshots:
  # 只有「6天定时」或「手动选了截图/全部」才执行
  if: ${{ (github.event_name == 'schedule' && github.event.schedule == '30 1 1,7,13,19,25 * *')
       || (github.event_name == 'workflow_dispatch' && (inputs.task == 'screenshots_only' || inputs.task == 'both')) }}
```

### TARGET_LINK 增量处理

当 `target_link` 非空时：

1. **状态检测**（main.py）：只请求匹配的友链，其余从上次 `result.json` 保留，按数据源顺序合并
2. **截图**（screenshot_runner.py）：只对匹配且可达的友链截图，其余保留已有截图 URL
3. **total_count 不变**：不会因只跑一个友链就丢失其他友链数据

> 💡 匹配规则：友链名称或 URL 包含 target_link 字符串（子串匹配）

## 🎯 触发方式详解

两个核心 Job（`check_links` 状态检测、`take_screenshots` 主页截图）**已解耦**，可按不同频率独立运行，也支持手动按需触发。

### 一、自动触发（定时 schedule）

| 时间 | 状态检测 | 主页截图 | 范围 |
|------|:---:|:---:|------|
| 每天 01:00 | ✅ | ❌ | 全部友链 |
| 每天 13:00 | ✅ | ❌ | 全部友链 |
| 每 6 天（1/7/13/19/25 号 01:30） | ✅ | ✅ | 全部友链 |

### 二、手动触发（workflow_dispatch）

在 **Actions → Run workflow** 面板有两个输入框：

**`task`（决定跑什么）：**

| 选项 | 状态检测 | 主页截图 |
|------|:---:|:---:|
| `status_only`（默认） | ✅ | ❌ |
| `screenshots_only` | ❌ | ✅ |
| `both` | ✅ | ✅ |

**`target_link`（决定跑哪些）：**

- 留空 → 处理全部友链
- 填关键词（友链名称或 URL 子串）→ 只处理匹配的那一个，**其余友链保留历史状态**（增量更新，不丢数据）

### 三、Job 触发条件（workflow 内部逻辑）

```yaml
check_links:
  # 除「手动选择仅截图」外，其余触发（定时 / 手动）都执行
  if: ${{ github.event_name != 'workflow_dispatch' || inputs.task != 'screenshots_only' }}

take_screenshots:
  # 仅在【每 6 天定时】或【手动选择 screenshots_only / both】时执行
  if: ${{ (github.event_name == 'schedule' && github.event.schedule == '30 1 1,7,13,19,25 * *') || (github.event_name == 'workflow_dispatch' && (inputs.task == 'screenshots_only' || inputs.task == 'both')) }}
```

### 四、增量处理（TARGET_LINK）

`main.py` 和 `screenshot_runner.py` 都读取 `TARGET_LINK` 环境变量：

- **main.py**：只检测匹配的友链；未检测的友链从上一次 `result.json` 保留历史状态，按数据源顺序合并回结果，`total_count` 不减少。
- **screenshot_runner.py**：只截图匹配的可达友链，其余友链的 `siteshot` 字段保持不变。

> 💡 典型场景：新增/修改某个友链后，手动触发 `task=both` + `target_link=友链名`，几十秒即可完成单个友链的检测 + 截图，无需全量跑。

---

## ⚙️ 高级配置

### 1. 调整截图频率

编辑 `.github/workflows/check_links.yml`：

```yaml
on:
  schedule:
    # 截图 cron：默认 1/7/13/19/25 号 01:30（即每 6 天）
    - cron: '30 1 1,7,13,19,25 * *'
    # 改为每天：- cron: '30 1 * * *'
    # 改为每周一：- cron: '30 1 * * 1'
```

> ⚠️ 改频繁会快速消耗 GitHub Actions 配额（每月 2000 分钟）

### 2. 调整并发数

```yaml
# .github/workflows/check_links.yml
take_screenshots:
  env:
    SCREENSHOT_WORKERS: '2'  # 改为 1-4（Chrome 进程吃内存）
```

### 3. 关闭截图功能

如果只想用状态检测、不需要截图，编辑 workflow 文件：

```yaml
take_screenshots:
  if: false  # ← 加这一行禁用
  # ...
```

### 4. 自定义文件名规则

编辑 `screenshot.py:_safe_filename()`：

```python
def _safe_filename(host: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9.\-]", "_", host)
    return f"{safe}.png"  # 默认 blog.example.com.png
    # return f"{safe}-{int(time.time())}.png"  # 加时间戳
    # return f"{hashlib.md5(host.encode()).hexdigest()}.png"  # 哈希
```

### 5. 自定义上传目录

```
Settings → Secrets → New secret
Name: IMG_UPLOAD_FOLDER
Secret: myfriends   # 截图会存到 tu.xxx.com/myfriends/
```

### 6. 添加反链检测

反链检测需要友链数据中有 `linkpage` 字段：

```json
{
  "name": "清羽飞扬",
  "link": "https://blog.liushen.fun/",
  "linkpage": "https://blog.liushen.fun/link/"
}
```

并在 Secrets 中设置 `AUTHOR_URL=fqzlr.com`（你的博客域名）。

> 🔍 检测逻辑：抓取友链页面（`linkpage`，留空回退首页），用正则提取所有真实 `<a href>` 链接并精确比对主机名（兼容 `www`、协议相对、带路径、大小写）；**仅纯文本出现域名不计为反链**，避免误报。

---

## 🐛 常见问题

### Q1: Workflow 跑完但 `page` 分支没更新？

**A**: 检查三处：

1. **Actions 权限**：Settings → Actions → General → Workflow permissions → **Read and write permissions**（Step 3）
2. **GITHUB_TOKEN 是否有效**：在 Secrets 页面应该自动有，无需手动加
3. **手动查看日志**：Actions → 选 run → 看 "Commit and push" 步骤

### Q2: 截图全部走 thum.io 兜底，上传图床全部 401？

**A**: 大概率是 `IMG_AUTH_CODE` 错误或认证方式不匹配：

1. 登录 cfbed 控制台，确认 Token 拥有 **`upload`** 权限
2. **新版 cfbed 已废弃 `authCode` query 参数，只认 `Authorization: Bearer` 头**（本项目已适配）
3. 重新设置 Secret，值填完整的 token 字符串
4. 本地快速测试：
   ```bash
   curl -X POST "https://tu.xxx.com/upload?uploadFolder=youlian&uploadNameType=origin" \
     -H "Authorization: Bearer 你的TOKEN" \
     -F "file=@test.png"
   ```
   应返回 `200 OK` + JSON 数组。

### Q3: 截图显示 "tuple index out of range" 错误？

**A**: 这是 `webdriver-manager` 在下载 chromedriver 时的网络错误：

- GitHub Action 环境会重试
- 或者在 `requirements.txt` 锁定 `webdriver-manager` 版本：

```txt
webdriver-manager==4.0.2
```

### Q4: 国内访问 Vercel 太慢？

**A**: 三个方案：

1. **CDN 缓存**：博客侧给 `result.json` 加 SWR 缓存（5-10 分钟）
2. **迁移到 Zeabur**（国内访问更快）：同样导入 GitHub 仓库，Output 选 `static`
3. **自建反代**：用 CloudFlare Worker 反向代理 Vercel

### Q5: 友链数据源 404 / CORS 错误？

**A**: 检查：

1. 博客是否部署成功：`curl https://你的博客.com/friends.json`
2. 博客端点是否包含 CORS 头（默认应该没问题）
3. `SOURCE_URL` 拼写是否正确

### Q6: Selenium 截图截到了登录墙 / 404 页？

**A**: 在 inject.css 中已经处理了部分情况：

```css
.cookie-banner, .ad-overlay, .notification-popup { display: none !important; }
```

如果还不够，可以：

- 增加等待时间：`PAGE_LOAD_WAIT = 5`
- 在 `screenshot.py` 中检测页面 title 跳过异常页面

### Q7: 怎么只检测/截图某个友链？

**A**: 无需改代码！手动触发时填写 `target_link` 参数即可：

1. Actions → Run workflow
2. `task` 选择 `both`（或按需选 status_only / screenshots_only）
3. `target_link` 填写友链名称或 URL 关键词，如 `番茄` 或 `blog.example.com`
4. 运行

**增量合并机制**：只有匹配的友链会被重新检测/截图，其余友链保留上次结果，`total_count` 不会减少。

### Q8: 图床出现 `xxx.com(5).png` 这种带后缀的旧文件，怎么清理？

**A**: 项目已自动调用 cfbed 删除 API 在每次上传前清理同名旧图：

- **删除端点**：`GET {base_url}/api/manage/delete/{folder}/{filename}`
- **认证**：`Authorization: Bearer {IMG_AUTH_CODE}`（与上传共用一个 Token）
- **容错设计**：删除失败时仅记录警告，**不影响上传主流程**

> ⚠️ **已知限制**：如果 cfbed 后端 R2 配置异常，删除接口会返回 `HTTP 400 {"success":false,"error":"Delete file failed"}`。此时图床会出现 `host.png`、`host(5).png`、`host(10).png` 等历史文件，但 `result.json` 中始终保存最新 URL，不影响前端展示。
>
> 解决方法：检查 cfbed 后台的 R2 CORS Policy（需允许 `DELETE`），或在 cfbed 后台「系统设置 → CloudFlare API Token」中配置 Global API Key 让 cfbed 自行清理缓存。

---

## 📜 二次开发

### 本地运行

```bash
# 克隆
git clone https://github.com/<你的用户名>/check-flink.git
cd check-flink

# 安装依赖
pip install -r requirements.txt

# 复制环境变量
cp .env.example .env
# 编辑 .env 填入你的配置

# 安装 Chrome（Ubuntu/Debian）
sudo apt-get install -y google-chrome-stable

# 运行状态检测
python main.py

# 运行截图（先运行 main.py 生成 result.json）
python screenshot_runner.py
```

### Windows 快速提交脚本

仓库根目录的 `上传.bat` 提供了交互式提交：

```cmd
上传.bat
```

按提示输入 commit message 即可。

### 调试截图

```bash
# 单条友链测试
python screenshot.py https://blog.liushen.fun/ blog.liushen.fun
# 输出：{"url": "https://tu.xxx.com/youlian/blog.liushen.fun.png"}
```

---

## 📄 License

继承原项目 License：[MIT](https://github.com/willow-god/check-flink/blob/main/LICENSE)

---

## 🙏 致谢

- [willow-god/check-flink](https://github.com/willow-god/check-flink) — 状态检测核心
- [thun888/Python-WebSite-Screenshot](https://github.com/thun888/Python-WebSite-Screenshot) — 截图核心
- [cfbed.sanyue.de](https://cfbed.sanyue.de/) — 图床 API 规范
- [image.thum.io](https://image.thum.io/) — 截图兜底服务

---

<div align="center">

**[⬆ 回到顶部](#-友情链接自动检查--主页截图)**

Made with ❤️ for the Blog Community

</div>
