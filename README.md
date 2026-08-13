# PandaOPDS

**OPDS-PSE 串流服务器**：E-Hentai 的中转代理，输出 **OPDS 1.2（Atom）** 与 **OPDS 2.0（JSON）** 双版本目录 + OPDS-PSE 串流链接，可用于任何支持 OPDS-PSE 的漫画阅读器。

技术栈：Python 3.11+ / FastAPI / uvicorn / httpx / lxml。单进程异步，Docker 单机部署。

## 快速开始

### 1. 配置 Cookie

登录 e-hentai.org 后从浏览器 cookie 中取 `ipb_member_id` 与 `ipb_pass_hash`：

```bash
cp .env.example .env
# 编辑 .env，填入 IPB_MEMBER_ID / IPB_PASS_HASH
```

若使用 exhentai：`EH_SITE=exhentai` 即可——服务器会用 IPB 会话先在 e-hentai 鉴权，再访问 exhentai，响应下发的 session 级 `igneous` 由 cookie jar 自动维护（`IGNEOUS` 仅作可选种子，不需要用户提供）。

### 2. 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export IPB_MEMBER_ID=... IPB_PASS_HASH=...
uvicorn app.main:app --reload --port 8000
```

冒烟测试：

```bash
curl -H "Accept: application/atom+xml" http://localhost:8000/opds/v1.2
curl -H "Accept: application/opds+json" http://localhost:8000/opds/v2.0
curl -H "Accept: application/atom+xml" "http://localhost:8000/opds/v1.2/gallery?query=language:chinese"
curl -o /tmp/p0.jpg "http://localhost:8000/stream/{gid}/{token}/page/0"
```

### 3. Docker 部署

```bash
docker compose up -d --build
```

通过反代暴露（见 `deploy/`）：nginx 示例 `deploy/nginx.conf.example`、Caddy 示例 `deploy/Caddyfile.example`。反代需透传 Host；设置 `PUBLIC_BASE_URL=https://opds.example.com` 时 feed 输出绝对 URL，否则为相对路径。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `IPB_MEMBER_ID` | — | **必需**，登录 cookie |
| `IPB_PASS_HASH` | — | **必需**，登录 cookie |
| `IGNEOUS` | 空 | 可选会话种子；exhentai 会在会话建立时自动下发 igneous，无需用户提供（`mystery` 忽略） |
| `EH_SITE` | `e-hentai` | `e-hentai` / `exhentai` |
| `NW` | `1` | 绕过 Offensive For Everyone 警告 |
| `DATATAGS` | `1` | 启用新缩略图结构 |
| `PUBLIC_BASE_URL` | 空 | 设置后 feed 输出绝对 URL |
| `CACHE_DIR` | `./cache` | 图片磁盘缓存目录 |
| `CACHE_MAX_GB` | `4` | 磁盘缓存上限 |
| `IMAGE_CACHE_ENABLED` | `true` | 设为 `false` 关闭磁盘缓存 |
| `HTML_INTERVAL_SECONDS` | `1.5` | HTML 出站请求最小间隔（防封关键） |
| `MAX_CONCURRENCY` | `2` | 出站并发上限 |
| `PSE_PAGE_BASE` | `1` | PSE 页码基数：`1`（LANraragi/Kasane 兼容，默认）；`0`（OPDS-PSE 规范原文） |
| `TIMEOUT_SECONDS` | `6` | 出站请求超时 |
| `RETRIES` | `3` | 网络错误重试次数（仅网络错误） |

## 路由

**OPDS 1.2（Atom，前缀 `/opds/v1.2`）**

| 路由 | 说明 |
|------|------|
| `GET /opds/v1.2` | 根导航 feed（Home / Watched / Favorites / Popular / Search） |
| `GET /opds/v1.2/search.xml` | OpenSearchDescription |
| `GET /opds/v1.2/gallery?query=&next=` | 图库采集 feed（`rel="next"` 分页；`query` 浏览维度：空=主页、`watched`、`favorites`、`popular`） |
| `GET /opds/v1.2/gallery/{gid}/{token}/chapters` | 章节 feed（单条目 + PSE stream link） |

**OPDS 2.0（JSON，前缀 `/opds/v2.0`）**

| 路由 | 说明 |
|------|------|
| `GET /opds/v2.0` | 根导航文档（Home / Watched / Favorites / Popular；搜索经顶层 `rel="search"` link） |
| `GET /opds/v2.0/search.xml` | OpenSearchDescription（兼容保留，JSON 搜索客户端无需依赖） |
| `GET /opds/v2.0/gallery?query=&next=` | 采集文档（publications 内嵌元数据 + `rel="next"` 分页；`query` 浏览维度同 v1.2） |
| `GET /opds/v2.0/gallery/{gid}/{token}` | 单 publication 文档（acquisition 落点 / 完整元数据） |

**串流 / 其他（与 OPDS 版本无关）**

| 路由 | 说明 |
|------|------|
| `GET /stream/{gid}/{token}/page/{n}` | 图片代理流（**默认 1-based**，`n` 从 1 起；`PSE_PAGE_BASE=0` 时 0-based） |
| `GET /image/{gid}/{token}/thumb` | 缩略图代理 |
| `GET /health` | 健康检查 + 缓存/限流统计 |

> **破坏性变更（v0.2）**：旧版本化前缀 `/opds` 已废弃并移除，现为严格路径 `/opds/v1.2` / `/opds/v2.0`，旧 URL 返回 404。客户端需重新配置根 URL。

## 客户端接入

自研阅读器把 OPDS 根 URL（如 `https://opds.example.com/opds/v2.0`）加入目录源即可。

**OPDS 2.0（推荐，内嵌元数据，免逐条请求）**：

1. 根文档 → Home（最新）/ Watched / Favorites / Popular 导航条目
2. **搜索**：读根文档顶层 `rel="search"` link，`href` 直接含 `{searchTerms}` 模板（`/opds/v2.0/gallery?query={searchTerms}`）——替换占位符（一次 URL 编码）即得搜索结果文档，无需解析 OpenSearch XML
3. 图库采集文档 → publications 内嵌标准元数据（title/author/language/subjects 标签列表/numberOfPages），无需再请求详情
4. 每个 publication 含 PSE stream link：
   `rel="http://vaemendis.net/opds-pse/stream"`，`href="/stream/{gid}/{token}/page/{pageNumber}"`，`properties.numberOfItems` 为页数；acquisition link 同带该属性
5. 客户端把 `{pageNumber}` 替换为 `1..numberOfItems` 逐页拉流（默认 1-based，与 LANraragi/Kasane 一致；`PSE_PAGE_BASE=0` 部署需客户端带外约定，feed 不再传输页码基数）

**自研客户端增强元数据**：publication 的 `metadata.extensions` 为单一私货字段，承载所有 EH 专属数据（评分 `rating`、日文标题 `titleJpn`、文件大小 `sizeBytes`、删除标记 `expunged`、分类 `category`、完整标签 `tags`：`namespace`/`key` + 可选 `status`（skepticism/incorrect）+ 高亮标签 `style`（color/borderColor/background，来自上游 HTML，`!important` 已剥离））。通用客户端忽略该字段即可。

**OPDS 1.2（兼容）**：

1. 根 feed → Home / Watched / Favorites / Popular / Search 子目录
2. 图库 feed → 条目带 acquisition link 指向章节 feed；PSE stream link 直接内联在条目上（Kasane 风格客户端可直接注册）
3. 章节 feed → 单条目含 PSE stream link：`href="/stream/{gid}/{token}/page/{pageNumber}"`，`pse:count` 为页数

## 开发

```bash
pip install -e ".[dev]"
python -m pytest tests/            # 单测（离线，含真实 HTML fixture 回归）
RUN_EH_INTEGRATION=1 python -m pytest tests/test_integration.py   # 真实闭环集成测试
```

架构与实现约定见 `AGENTS.md`；实施计划见 `PLAN.md`。

## 注意

- PandaOPDS 是服务器（多客户端、单 IP 集中请求），比客户端更易触发 E-Hentai 封禁。请保持节流参数默认值，善用缓存。
- 图片限额触发时返回 429；IP 被封 / 超限会触发全局熔断并返回 503。
- 本仓库仅包含中转代理逻辑，不托管任何内容。
