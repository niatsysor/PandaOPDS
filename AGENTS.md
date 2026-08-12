# AGENTS.md — PandaOPDS 项目指南

## 项目概述

PandaOPDS 是一个 **OPDS-PSE 串流服务器**，作为 E-Hentai.org 的中转代理：

- 从 E-Hentai.org 抓取数据（图库列表、图库元数据、图片），输出为 **OPDS 1.2（Atom）与 OPDS 2.0（JSON）双版本目录 + OPDS-PSE 串流链接**。严格版本路径 `/opds/v1.2` / `/opds/v2.0`，旧 `/opds` 前缀已废弃。
- 目标客户端：**自研阅读器**（对标 Panels 的 OPDS-PSE 消费方式），非 Mihon/Tachiyomi 插件生态（它们无 OPDS 源）。
- 技术栈：**Python + FastAPI + uvicorn**（单进程异步）。
- 部署：Docker 单机，nginx/caddy 反代，需要 `PUBLIC_BASE_URL` 支持。

**现状**：项目处于调研完成、待实施阶段。`example/JHenTai` 是参考项目（只读，禁止修改），本仓库尚未有实现代码。

## 项目结构

```
PandaOPDS/
├── AGENTS.md          # 本文件：项目约定
├── docs/
│   └── plans/
│       └── PLAN-2026-08-11-archived.md  # 初期实施计划（已完成归档）
├── deploy/            # nginx/Caddy 反代示例
├── Dockerfile / docker-compose.yml
├── app/               # 服务端代码
├── tests/             # 测试
└── example/
    └── JHenTai/       # 参考项目（Flutter E-Hentai 客户端），只读，独立 git 仓库
```

## 参考代码位置（JHenTai，实施时对照）

| 用途 | 文件 |
|------|------|
| 官方 API 调用（gdata） | `example/JHenTai/lib/src/network/eh_request.dart`（`requestGalleryMetadata(s)`） |
| HTML 解析（全部选择器） | `example/JHenTai/lib/src/utils/eh_spider_parser.dart`（1529 行） |
| 缩略图解析（新旧结构） | 同上：`_parseGalleryDetailsForNewThumbnails` / `_parseGalleryDetailsForOldSmallThumbnails` / `_parseGalleryDetailsForOldLargeThumbnails` |
| 图片页解析（#img/509/nl/f_shash） | 同上：`imagePage2GalleryImage` |
| 列表页解析（四种视图） | 同上：`_parseThumbnailGallery` / `_parseCompactGallery` / `_parseExtendedGallery` / `_parseMinimalGallery` |
| cookie / 缓存 / 异常检测 | `lib/src/network/eh_cookie_manager.dart`、`eh_cache_manager.dart`、`eh_request.dart` 的 `_emitEHExceptionIfFailed` |

## 关键技术知识（E-Hentai）

### 端点

| 端点 | 用途 |
|------|------|
| `POST https://api.e-hentai.org/api.php` | 官方 JSON API，`gdata` 方法批量取元数据（**首选**） |
| `GET https://e-hentai.org/?f_search=...&next={lastGid}` | 列表页（搜索/最新/热门），`next` 参数分页 |
| `GET https://e-hentai.org/g/{gid}/{token}/?p={n}` | 图库详情页（缩略图每页 20 个，`?p` 翻页） |
| `GET https://e-hentai.org/s/{imageToken}/{gid}-{pageNo}` | 单图片页（pageNo **1-based**），解析 `#img` src 得真实图片 URL |
| `https://e-hentai.org/popular` | 热门；`/favorites.php` 收藏；`/toplist.php` 排行榜（`?tl=` 周期：15=昨天/13=近一月/12=近一年/11=全部，`?p=` 翻页；对齐 JHenTai `RanklistType` day/month/year/allTime） |

exhentai.org 对应域名：`exhentai.org`（页面）、`exhentai.org/api.php`（API）。

### 鉴权 Cookie（环境变量注入）

```python
cookies = {
    "nw": "1",            # 必须：绕过 Offensive For Everyone 警告
    "datatags": "1",      # 建议：启用新缩略图结构（含 data-orghash）
    "ipb_member_id": ..., # 环境变量 IPB_MEMBER_ID
    "ipb_pass_hash": ..., # 环境变量 IPB_PASS_HASH
}
```
`igneous` **不是用户需要提供的长效凭据**（`IGNEOUS` 仅作可选种子，`mystery` 忽略）：

- 用户提供成对的 `ipb_member_id` + `ipb_pass_hash`（e-hentai 登录态）**为可选项**：未提供时服务照常运行，公开内容（Latest/Popular/Toplist/Search）可用，仅 **Watched/Favorites 导航项不输出**（判定为配置派生 `bool(ipb_member_id and ipb_pass_hash)`，零上游探测）；cookie 失效时访问对应 feed 返回 503 + WARNING 日志（被动降级，不自动隐藏）。
- 首次上游请求前 `EHClient.establish_session()` 先带 IPB 会话访问 e-hentai 鉴权/保活；
  `EH_SITE=exhentai` 时再访问 exhentai.org 一次，其响应 Set-Cookie 下发的 session 级
  `igneous` 由 httpx cookie jar 自动维护（进程生命周期内随会话/IP 变化，不持久化）。
- 图片请求只需 cookie，无需 referer（可附带 Referer 头以防站点策略变化）。

### 元数据：gdata API（首选）

```
POST api.e-hentai.org/api.php
Content-Type: application/json
{"method": "gdata", "gidlist": [[gid, token], ...], "namespace": 1}
```
- 返回 `gmetadata` 数组：`gid, token, title, title_jpn, category, thumb, rating, tags, filecount, filesize, posted, uploader, torrentcount, expunged`。
- **每请求最多 25 个 gid**。`filecount` 即 OPDS 的 `pse:count`。
- 列表页拿到 gid/token 后应批量回填元数据，避免逐条 HTML 解析。

### 页面 URL 获取（无 API 替代，必须抓详情页）

详情页 `?p={n}` 每页 20 个缩略图，解析 `#gdt`（2024-10-15 起有两种结构，**都要支持**）：

- **新结构**（`datatags=1` 下）：`#gdt` 带 class，子元素 `<a href>` + `<div style="...url(缩略图)...">`，div 有 `data-orghash`（40 位原图哈希）。href 可能是 MPV 格式 `/mpv/{gid}/{token}/`，此时用 `data-orghash` 前 10 位构造：`/s/{hash10}/{gid}-{pageNo}`。
- **旧结构**：`#gdt > .gdtm`（小图）/ `.gdtl`（大图），从 `a[href]` 直接取 `/s/` URL。
- 页码信息：`.gtb > .gpc` 文本 "Showing X - Y of Z images"；`.ptt` 分页控件。
- 翻页次数 = `ceil(filecount / 20)`，每个详情页 HTML 缓存 1 小时（1 个 HTML 请求可服务 20 个 `/stream` 请求）。

### 图片 URL 解析（/s/ 页）

- `#img` 的 `src` = 图片 URL（`style` 含宽高）。
- **509 占位图检测**：src 为 `https://ehgt.org/g/509.gif`（EH）或 `https://exhentai.org/img/509.gif`（EX）→ 已超图片限额，返回 429。
- `#loadfail` 的 `onclick="return nl('...')"` = reloadKey：图片加载失败时用 `?nl={reloadKey}` 重试（mihon 插件同款机制）。
- `#i6 div a` 的 `f_shash` = 原图哈希；`#i6 a[id]` 附近有原图链接（可选，MVP 不做原图）。

### 限流与异常检测（服务器生死线，必须实现）

| 响应特征 | 含义 | 处理 |
|---|---|---|
| body 以 `Your IP address` / `This IP address` 开头 | **IP 被禁** | 全局熔断 + 告警 |
| body 以 `You have exceeded your image` 开头 | 超出图片限额 | 停止图片请求、降速 |
| body 含 `Page load has been aborted due to a fatal error` | EH 服务器错误 | 退避重试 |
| body 为空 | 需要登录（sadPanda） | 提示 cookie 失效 |
| 404 + e-hentai host | 图库已删除 | 404 返回客户端 |
| 403 | Cloudflare | 退避 |
| 图片 src = 509.gif | 图片限额 | 返回 429 |

PandaOPDS 是**服务器**（多客户端、单 IP 集中请求），比 JHenTai 客户端更易触发封禁 → **全局节流 + 缓存命中率是第一优先级**。

### 缓存策略（与 JHenTai 参数一致）

| 层 | 介质 | TTL | 说明 |
|---|---|---|---|
| 列表页解析结果（search/popular/watched/favorites/toplist） | 内存 | 10min | 首页/展示区块高频命中，避免重复抓列表页（`LIST_CACHE_TTL_SECONDS`） |
| 图库元数据（gdata 结果） | 内存 | 1h | ~1-2KB/条 |
| 页面 URL 映射（/s/ 列表） | 内存 | 1h | 避免重复翻页 |
| 图片字节 | 磁盘 LRU | 7 天 | 默认 4GB（环境变量 `CACHE_DIR`/`CACHE_MAX_GB` 可调），可关 |

## OPDS-PSE 规范要点（服务端必须严格遵循，v1.2 与 v2.0 共用串流语义）

规范原文：`http://vaemendis.net/opds-pse/`（2014-12-01，v1.0）。参考实现：Tachidesk/Suwayomi（`server/src/main/kotlin/suwayomi/tachidesk/opds/`，格式对齐对象）。

- 命名空间：`http://vaemendis.net/opds-pse/ns`（前缀惯例 `pse`）。
- Stream link MUST 属性：
  - `rel="http://vaemendis.net/opds-pse/stream"`
  - `type` ∈ {`image/jpeg`, `image/gif`, `image/png`}（用 `image/jpeg`）
  - `pse:count` = 页数（来自 `filecount`）
  - `href` 必须含 `{pageNumber}` 占位符（客户端替换），可选 `{maxWidth}`
- **页码**：默认 **1-based**（第 1 页 = `page/1`），与 LANraragi（参考 PSE 服务器）和 Kasane 客户端对齐；规范原文为 0-based，设置 `PSE_PAGE_BASE=0` 可切回 0-based。`/s/` URL 的 pageNo 本就是 1-based，代理层直接透传。
- 双页视为单页，服务器不切图。
- Feed 媒体类型：`application/atom+xml;profile=opds-catalog;kind=navigation` / `kind=acquisition`。
- OpenSearch：`application/opensearchdescription+xml`，`template` 含 `{searchTerms}`。

### 路由设计（严格版本路径，无旧路径兼容）

**OPDS 1.2（Atom，`app/opds/`）**

| 路由 | 说明 |
|------|------|
| `GET /opds/v1.2` | 根导航 feed（Watched / Favorites / Popular / Toplist×4 / Search；Home 已移除，Latest 直接走 `/opds/v1.2/gallery`；Watched/Favorites 按 cookie 存在性过滤） |
| `GET /opds/v1.2/search.xml` | OpenSearchDescription 文档 |
| `GET /opds/v1.2/gallery?query=&next=` | 图库采集 feed（`rel="next"` 分页复用 `next` + `lastGid`；`query` 支持浏览维度：空=主页、`watched`、`favorites`、`popular`） |
| `GET /opds/v1.2/toplist?period=&page=` | Toplist 采集 feed（`period` ∈ yesterday/month/year/alltime；`rel="next"` 用 `page` 分页；纯标准 Atom，**不涉及 extensions**） |
| `GET /opds/v1.2/gallery/{gid}/{token}/chapters` | 图库详情 feed（单章节条目 + PSE stream link） |
| `GET /stream/{gid}/{token}/page/{n}` | 图片代理流（默认 1-based，`PSE_PAGE_BASE=0` 时 0-based，返回 image/jpeg；v1.2/v2.0 共用） |
| `GET /image/{gid}/{token}/thumb` | 缩略图代理（共用） |

**OPDS 2.0（JSON，`app/opds2/`）**

| 路由 | 说明 |
|------|------|
| `GET /opds/v2.0` | 根导航文档（`application/opds+json;profile=navigation`）：`groups[]` = 各区块内联 publication 预览（OPDS 2.0 §2.5，由 `config/home.toml` 的 `[[group]]` 控制，书写顺序 = 输出顺序）；`navigation[]` = `[[navigation]]` 纯导航链接（无 extensions）；Watched/Favorites 无 IPB cookie 时自动过滤 |
| `GET /opds/v2.0/search.xml` | OpenSearchDescription（兼容保留，客户端无需依赖；template 指向 v2.0 gallery） |
| `GET /opds/v2.0/gallery?query=&next=` | 采集文档（`application/opds+json;profile=acquisition`）：publications 内嵌完整元数据 + `rel="next"` 分页；`query` 支持浏览维度（空=主页、`watched`、`favorites`、`popular`） |
| `GET /opds/v2.0/toplist?period=&page=` | Toplist 采集文档（同上，`page` 分页） |
| `GET /opds/v2.0/gallery/{gid}/{token}` | 单 publication 文档（acquisition 落点 / 完整元数据入口，对应 v1.2 章节 feed） |

浏览维度 `watched`/`favorites` 复用列表解析器（`parse_list_page`）：`EHService.watched_galleries` → `/watched`，`EHService.favorites_galleries` → `/favorites.php`。

### 首页排版（server-driven，v2.0 专属）

**约束：凡涉及 `extensions` 的机制一律排除 v1.2**——v1.2 保持纯标准导航，不输出任何扩展标记，也不在根 feed 混入采集条目。

- **`groups[]`（OPDS 2.0 标准，§2.5）**：根文档 `groups[]` 中每个 group 包含 `metadata.title`、`links`（`rel="self"` 指向完整列表）、`publications[]`（内联预览条目，数量由 TOML `publications` 字段控制）。**任何兼容 OPDS 2.0 的客户端均可原生渲染为分栏网格**——无需私货标记。
- **`navigation[]`**：`navigation[]` 来自 `[[navigation]]` 条目，以及无 IPB cookie 时省略的 Watched/Favorites。不再输出 `extensions.layout` 私货。
- **配置**：`config/home.toml`（`[[group]]` / `[[navigation]]`），环境变量 `HOME_CONFIG` 可指定文件路径。不设文件时使用内置默认布局。

**OPDS 2.0 搜索（JSON，最终形态）**：导航/采集文档顶层 `rel="search"` link 的 `href` 直接含 `{searchTerms}` 模板（`/opds/v2.0/gallery?query={searchTerms}`，type `application/opds+json;profile=acquisition`）——客户端替换占位符即得搜索结果文档，无需先请求 OpenSearch XML。v1.2 保持 OpenSearch XML（`search.xml`）不变；`/opds/v2.0/search.xml` 仅作兼容保留。

### 章节条目 XML 模板

```xml
<entry>
  <id>urn:ehentai:gallery:{gid}:{token}</id>
  <title>Chapter 1: {title}</title>
  <updated>{iso8601}</updated>
  <author><name>{artist}</name></author>
  <category term="{genre}" label="{genre}" scheme="http://e-hentai.org"/>
  <summary type="text">{语言/页数/上传者/评分/大小}</summary>
  <link rel="http://opds-spec.org/image/thumbnail" href="/image/{gid}/{token}/thumb" type="image/jpeg"/>
  <link rel="http://vaemendis.net/opds-pse/stream"
        href="/stream/{gid}/{token}/page/{pageNumber}"
        type="image/jpeg" pse:count="{filecount}"/>
</entry>
```

### OPDS 2.0 publication JSON 模板（`app/opds2/feed.py`）

OPDS 2.0 无官方串流扩展，PSE stream 以自定义 rel + `properties.numberOfItems`（OPDS 2.0 标准属性）表达；页码基数不再传输（默认 1-based，与 LANraragi/Kasane 一致，`PSE_PAGE_BASE=0` 部署由自研客户端带外约定同步）。

**字段分层约定（本项目核心）**：

- **标准层**：只输出 OPDS/RWPM 标准字段（`title`/`identifier`/`authors`/`language`/`subjects`/`numberOfPages`/`description`/`modified`/`published`），通用客户端（对标 Panels）直接消费。`subjects` 为拍平标签字符串数组（Komga 风格，含全部 `ns:key` 标签，不含分类）。
- **私货层 `metadata.extensions`**：**所有** EH 专属/非标准字段收敛于此单一字段，自研客户端只读它：`rating`、`titleJpn`、`sizeBytes`、`expunged`、`category`、`tags`（完整标签：`namespace`/`key` + 仅非常规时输出的 `status`（`skepticism`/`incorrect`）+ 仅高亮标签输出的 `style`（`color`/`borderColor`/`background`，来自上游 HTML inline style，`!important` 已剥离））。`category` 刻意不进 `subjects`（避免与标签混淆）；未来如需对通用客户端暴露分类，走 OPDS 2.0 `facets`（按分类筛选）或 `navigation`（分类浏览入口），勿再塞回 `subjects`。
- 标签高亮数据来源：列表 feed 用列表页解析的高亮标签子集（布局相关：compact/extended 全量、thumbnail 仅高亮、minimal 无）覆盖 gdata 标签；详情文档用详情页 `#taglist`（完整 + 状态 + 样式，详情页缓存 1h）。

```json
{
  "metadata": {
    "title": "{title}",
    "identifier": "urn:ehentai:gallery:{gid}:{token}",
    "modified": "{iso8601}",
    "authors": [{"name": "{uploader}"}],
    "language": ["{language}"],
    "subjects": ["{ns}:{key}", "..."],
    "numberOfPages": {filecount},
    "description": "{语言/页数/上传者/评分/大小}",
    "published": "{iso8601}",
    "extensions": {
      "rating": {rating},
      "titleJpn": "{title_jpn}",
      "sizeBytes": {filesize},
      "expunged": {expunged},
      "category": "{category}",
      "tags": [{"namespace": "{ns}", "key": "{key}",
                  "status": "{skepticism|incorrect}",
                  "style": {"color": "#f1f1f1", "borderColor": "#048751",
                            "background": "radial-gradient(#048751,#24A771)"}}]
    }
  },
  "links": [
    {"rel": "http://opds-spec.org/image/thumbnail", "href": "/image/{gid}/{token}/thumb", "type": "image/jpeg"},
    {"rel": "http://opds-spec.org/acquisition", "href": "/opds/v2.0/gallery/{gid}/{token}",
     "type": "application/opds+json;profile=acquisition",
     "properties": {"numberOfItems": {filecount}}},
    {"rel": "http://vaemendis.net/opds-pse/stream",
     "href": "/stream/{gid}/{token}/page/{pageNumber}",
     "type": "image/jpeg",
     "properties": {"numberOfItems": {filecount}}}
  ]
}
```

### 反代注意事项

- 默认输出**相对路径** href（OPDS 允许，Tachidesk 即如此）；提供 `PUBLIC_BASE_URL` 环境变量（如 `https://opds.example.com`）时输出绝对 URL。
- nginx/caddy 负责 TLS/限速/访问控制；正确透传 Host。

## 架构（FastAPI 分层）

```
客户端（对标 Panels）
   │ OPDS 1.2 (Atom) / 2.0 (JSON) + PSE stream links
   ▼
FastAPI (uvicorn) 单进程
├─ Feed 层：OPDS XML/JSON 生成（v1.2 Atom + v2.0 JSON + OpenSearch）
├─ 代理层：图片/缩略图流式转发 + 磁盘缓存
├─ 数据层：gdata API + 列表/详情/图片页 HTML 解析
├─ 缓存层：内存（元数据/页面URL，1h）+ 磁盘（图片，7d）
└─ 限流层：asyncio.Semaphore + 请求间隔；banned/509/exceedLimit 检测与熔断
```

一次图库完整生命周期：
1. v1.2 章节 feed / v2.0 详情文档请求 → 缓存未命中 → `gdata` 拿 `filecount`/标题/标签 → 生成条目（v1.2 `pse:count` / v2.0 `numberOfItems` = filecount）
2. `/stream/page/{n}` 请求 → 页面 URL 缓存未命中 → 抓详情页 `?p={(n-1)//20}`（1 请求服务 20 页）→ 取第 n 个 `/s/` URL → 抓 `/s/` 页解析 `#img` src → 抓图片字节 → 磁盘缓存 → 流式返回（n 为 1-based 时）
3. 触发 509 → 429；banned/exceedLimit → 全局熔断

## 开发命令

```bash
# 环境（Python 3.11+）
python -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn httpx lxml python-dotenv

# 运行
export IPB_MEMBER_ID=... IPB_PASS_HASH=...
uvicorn app.main:app --reload --port 8000

# 冒烟测试（无客户端）
curl -H "Accept: application/atom+xml" http://localhost:8000/opds/v1.2
curl -H "Accept: application/opds+json" http://localhost:8000/opds/v2.0
curl -H "Accept: application/atom+xml" "http://localhost:8000/opds/v1.2/gallery?query=language:chinese"
curl -o /tmp/p0.jpg "http://localhost:8000/stream/{gid}/{token}/page/0"
```

## 项目约定

1. **只用 Python**；示例代码（Kotlin/Dart）仅作逻辑参考，不直接搬运语法。
2. `example/JHenTai` **只读**，禁止修改。
3. 所有 E-Hentai 出站请求必须经过**统一的数据层 + 限流层**（禁止绕过）。
4. 限流与缓存为第一优先级；先跑通"列表→元数据→页面URL→图片字节"闭环，再接 OPDS 层。
5. 新会话先读本文件；初期实施计划已全部完成并归档至 `docs/plans/PLAN-2026-08-11-archived.md`，新需求直接按本文件约定推进。
6. 出站请求默认超时 6s（JHenTai 同款）、失败重试 3 次（仅网络错误）。
7. 不要修改 `example/` 下的任何文件。
