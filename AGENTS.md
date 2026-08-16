# AGENTS.md — PandaOPDS 项目指南

## 项目概述

PandaOPDS 是一个 **OPDS-PSE 串流服务器**，作为 E-Hentai.org 的中转代理：

- 从 E-Hentai.org 抓取数据（图库列表、图库元数据、图片），输出为 **OPDS 1.2（Atom）与 OPDS 2.0（JSON）双版本目录 + OPDS-PSE 串流链接**。严格版本路径 `/opds/v1.2` / `/opds/v2.0`，旧 `/opds` 前缀已废弃。
- 目标客户端：**Kasane、Panels 等支持 OPDS-PSE 流式阅读的阅读器**（消费方式对标 Panels），非 Mihon/Tachiyomi 插件生态（它们无 OPDS 源）。
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

### gdata 调用时机（传统爬虫模式，主链路零 ehapi）

浏览阶段（列表/首页/toplist feed）**禁止调用 gdata**：条目完全由列表页 HTML 解析数据渲染（`GalleryListItem`：标题/分类/封面/页数/评分/发布时间/语言/全量标签）。**详情文档同样零 gdata**：v1.2 `/chapters`、v2.0 `/gallery/{gid}/{token}` 直接用详情页 HTML 渲染——详情页本身携带 gdata 等价字段（`#gn`/`#gj`/`#gdd`/`#gdn`/`#grt2`/`#gd5`，对齐 JHenTai `detailPage2GalleryAndDetailAndApikey`），且该页面已由 `/stream` 主链路抓取并缓存 1h。gdata（`get_metadata`/`get_metadatas`）保留在 service 层作兜底与测试用，主链路不再触发：**API 用量为 0**。

缩略图 `/image/{gid}/{token}/thumb` 同样不依赖 gdata：优先命中列表解析时写入的 cover 内存缓存，冷未命中回退详情页第 0 页第一个缩略图（1 次 HTML 请求服务 20 个 `/stream`）。

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
| 列表页解析结果（search/popular/watched/favorites/toplist） | 内存 | 10min | 首页/展示区块高频命中，避免重复抓列表页（`LIST_CACHE_TTL_SECONDS`）；解析时顺带写入 cover 缓存 |
| cover URL（列表页封面） | 内存 | 1h | 缩略图代理零 ehapi 的依赖（`cover:{gid}:{token}`，TTL 同页面 URL 映射） |
| 图库元数据（gdata 结果） | 内存 | 10min | 主链路不再触发（详情走详情页 HTML）；保留 service 层作兜底/测试（`METADATA_TTL_SECONDS`），~1-2KB/条 |
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
| `GET /opds/v1.2` | 根导航 feed（**硬编码，不读 home.toml**）：Latest 置顶，其后 Watched / Favorites / Popular / Toplist（单入口，默认 `period=yesterday`）按序排列，尾部固定 Search；Watched/Favorites 按 cookie 存在性过滤；纯标准导航，无扩展标记、无采集条目 |
| `GET /opds/v1.2/search.xml` | OpenSearchDescription 文档 |
| `GET /opds/v1.2/gallery?query=&next=` | 图库采集 feed（`rel="next"` 分页复用 `next` + `lastGid`；`query` 支持浏览维度：空=主页、`watched`、`favorites`、`popular`） |
| `GET /opds/v1.2/toplist?period=&page=` | Toplist 采集 feed（`period` ∈ yesterday/month/year/alltime；`rel="next"` 用 `page` 分页；纯标准 Atom，**不涉及 extensions**；内嵌 **OPDS 1.2 period facets**（`rel="http://opds-spec.org/facet"` + `opds:facetGroup="period"`，当前周期标 `opds:activeFacet="true"`），供客户端在榜单内切换周期） |
| `GET /opds/v1.2/gallery/{gid}/{token}/chapters` | 图库详情 feed（单章节条目 + PSE stream link） |
| `GET /stream/{gid}/{token}/page/{n}` | 图片代理流（默认 1-based，`PSE_PAGE_BASE=0` 时 0-based，返回 image/jpeg；v1.2/v2.0 共用） |
| `GET /image/{gid}/{token}/thumb` | 缩略图代理（共用） |

**OPDS 2.0（JSON，`app/opds2/`）**

| 路由 | 说明 |
|------|------|
| `GET /opds/v2.0` | 根导航文档：`[[group]]` 声明命名组，`[[section]]` 引用组 ID 挂载 publication/navigation 条目；无 group 的 section 独立成组或进入根 navigation；Watched/Favorites 无 IPB cookie 时自动过滤 |
| `GET /opds/v2.0/search.xml` | OpenSearchDescription（兼容保留，客户端无需依赖；template 指向 v2.0 gallery） |
| `GET /opds/v2.0/gallery?query=&next=` | 采集文档（`application/opds+json;profile=acquisition`）：publications 内嵌完整元数据 + `rel="next"` 分页；`query` 支持浏览维度（空=主页、`watched`、`favorites`、`popular`） |
| `GET /opds/v2.0/toplist?period=&page=` | Toplist 采集文档（同上，`page` 分页；内嵌 **OPDS 2.0 period facets**：`facets[0].metadata.title="Period"`，4 条 link 对应 4 周期，当前周期 link 带 `"active": true`） |
| `GET /opds/v2.0/gallery/{gid}/{token}` | 单 publication 采集文档（完整元数据入口，对应 v1.2 章节 feed；`detail` 模式下为列表 acquisition 落点，`direct` 模式下列表不暴露、Kasane 由 identifier 拼 URL；其 acquisition 恒指向图片流、不指向自身） |
| `GET /opds/v2.0/gallery/{gid}/{token}/publication` | 单 publication 文档（**顶层 RWPM publication 对象**，非采集文档）：`context`/`metadata`/`links`/`images`/`readingOrder`；每个 publication 的 `rel="self"` 指向此端点，Stump 等客户端跟随 `self` 打开详情并通过内嵌 `readingOrder`（逐页 `/stream/.../page/{n}`）流式阅读 |

浏览维度 `watched`/`favorites` 复用列表解析器（`parse_list_page`）：`EHService.watched_galleries` → `/watched`，`EHService.favorites_galleries` → `/favorites.php`。

**WebUI（`app/webui/`，挂载于根目录，无 `/webui` 前缀）**

| 路由 | 说明 |
|------|------|
| `GET /` | 单页管理界面（page.html，内联 CSS/JS 无外链）：仪表盘（状态/熔断器/请求计数/缓存）+ 环境变量配置 + 首页布局；仅读 `app.state`，零出站请求，配置异常时页面照常可访问 |
| `GET /api/status` | JSON：服务状态、熔断器、节流计数、缓存统计、首页来源 |
| `GET /api/config` | JSON：全量生效配置（分组），凭据类字段服务端脱敏 |
| `GET /api/home` | JSON：home.toml 布局（groups/sections、来源标记、解析错误） |

根路径 `/` 与 `/api/*` 命名空间由 WebUI 独占（勿在其上挂新路由）；`/health` 为独立探活端点（`app/main.py`），互不冲突。

### 首页排版（server-driven，**v2.0 专属**）

**v1.2 不读 `home.toml`**：根导航硬编码（Latest / Watched / Favorites / Popular / Toplist / Search），Toplist 周期在 feed 内以标准 facets 切换（见路由表）。

**约束：凡涉及 `extensions` 的机制一律排除 v1.2**——v1.2 保持纯标准导航，不输出任何扩展标记，不在根 feed 混入采集条目（Latest 不展开、全部目录化）。

- **`groups[]`（OPDS 2.0 标准，§2.5）**：每个 group 包含 `metadata.title`、`links`（`rel="self"`）。可含 `publications[]`（`kind="publication"`）和/或 `navigation[]`（`kind="navigation"`，Komga 风格）。同一 `group` 的 section 合并进一个槽位，publication 与 navigation 可混排。**任何兼容 OPDS 2.0 的客户端均可原生渲染**——无需私货标记。
- **`navigation[]`**：来自无 `group` 的 `kind="navigation"` 条目。
- **配置**：`config/home.toml`（**仅 v2.0 消费**）。`[[group]]` 声明组，`[[section]]` 引用 `group` 字段挂载条目。不设文件时使用内置默认布局（publication 预览默认 20 条）。

**OPDS 2.0 搜索（JSON，最终形态）**：导航/采集文档顶层 `rel="search"` link 的 `href` 直接含 `{searchTerms}` 模板（`/opds/v2.0/gallery?query={searchTerms}`，type `application/opds+json;profile=acquisition`）——客户端替换占位符即得搜索结果文档，无需先请求 OpenSearch XML。search 链接同样标 `templated: true`（模板链接统一标记，见「获取模式」）。v1.2 保持 OpenSearch XML（`search.xml`）不变；`/opds/v2.0/search.xml` 仅作兼容保留。

### 章节条目 XML 模板

```xml
<entry>
  <id>urn:ehentai:gallery:{gid}:{token}</id>
  <title>Chapter 1: {title}</title>
  <updated>{iso8601}</updated>
  <author><name>{artist}</name></author>
  <category term="{genre}" label="{genre}" scheme="http://e-hentai.org"/>
  <!-- summary 当前不输出（预留）：列表/章节条目 summary 恒为空，与 v2.0 description 一致 -->
  <link rel="http://opds-spec.org/image/thumbnail" href="/image/{gid}/{token}/thumb" type="image/jpeg"/>
  <link rel="http://vaemendis.net/opds-pse/stream"
        href="/stream/{gid}/{token}/page/{pageNumber}"
        type="image/jpeg" pse:count="{filecount}"/>
</entry>
```

### OPDS 2.0 publication JSON 模板（`app/opds2/feed.py`）

OPDS 2.0 无官方串流扩展，PSE stream 以自定义 rel + `properties.numberOfItems`（OPDS 2.0 标准属性）表达；页码基数不再传输（默认 1-based，与 LANraragi/Kasane 一致，`PSE_PAGE_BASE=0` 部署由 Kasane 带外约定同步）。封面/缩略图按 OPDS 2.0 §2.3 放入顶层 `images` 集合——thumbnail link rel 是 OPDS 1.x 的 links 做法，v2.0 **不输出**（v1.2 Atom 仍用 link rel）。`images` 恒有（缩略图代理零 ehapi，不依赖 gdata）。

**获取模式（`OPDS_ACQ_DETAIL`，布尔，默认 `false`）**：列表/首页 publication 的 acquisition 指向由部署者按客户端能力配置（与 `PSE_PAGE_BASE` 同类带外约定）：

- `false`（默认，兼容至上，即 direct）：acquisition **直接指向图片流**（`/stream/{gid}/{token}/page/{pageNumber}`，`type="image/jpeg"`，`properties.numberOfItems`）——客户端点击即读，**零二次请求**；不输出指向详情文档的 acquisition（详情文档仍可访问，Kasane 由 identifier 拼 URL）。
- `true`（即 detail）：acquisition 指向详情文档（`/opds/v2.0/gallery/{gid}/{token}`，`type="application/opds+json;profile=acquisition"`）——客户端二次请求详情后再读（Panels 风格）。
- 旧字符串形式 `OPDS_ACQ_MODE=detail|direct` 在 `OPDS_ACQ_DETAIL` 未设置时仍被兼容解析。
- **详情文档自身恒输出直接 image-stream acquisition（两种模式一致），绝不指向自身**（无自循环）。未知页数（无 `page_count`）时 direct 模式不输出 acquisition/stream link；detail 模式保留指向详情文档的 acquisition（无 `numberOfItems`）。
- **模板链接一律标 `templated: true`**（RWPM link 语义）：href 含 `{...}` 的链接（stream/acquisition 的 `{pageNumber}`、search 的 `{searchTerms}`）自动标记，规范客户端替换占位符、**永不按字面请求**（`app/opds2/feed.py` `_link()` 自动检测，无需逐处维护）；self/alternate/next/facets 等具体 URL 不带此标记。v1.2（Atom）无 templated 属性——PSE rel 语义自身定义 href 为模板。
- **语义分工（规范收敛，Kasane 契约）**：`rel="self"` 恒指向单 publication 文档（`/opds/v2.0/gallery/{gid}/{token}/publication`）= **重新获取该 publication 文档的入口**（含 reviews/完整 tags 的详情补全走这里）；`rel="acquisition"` 只承担**内容获取**（直接读流/下载，不承担详情入口）。列表内嵌数据（stream/页数/基础元数据）足够阅读 → 客户端**零请求基线**；进详情 = 明确信号 → 经 self 拉一次完整 manifest 回填（**每次进入拉一次**，无持久化门控；服务端详情页 HTML 缓存 1h，成本可忽略；同一详情视图会话内去重）。旧格式（acquisition → 详情采集文档，type `opds+json`）作为 fallback 保留（无 self 或 self 不可解析时）。

**RWPM/Stump 兼容（所有 publication 恒有）**：

- 顶层 `context` = `https://readium.org/webpub-manifest/context.jsonld`（RWPM 标记）。
- `links` 含 `rel="self"` → `/opds/v2.0/gallery/{gid}/{token}/publication`（`type="application/opds+json"`）——**Stump 等客户端跟随 `self` 打开详情**（它们的解析器要求 selfURL 返回**顶层 publication 对象**，因此 self 指向单 publication 端点而非采集文档）。
- `metadata.author`（RWPM 单数）与 `authors` 并存（Stump/Readium 只认 `author`）。
- `detail_document=True` 时（`/gallery/{gid}/{token}` 与 `/gallery/{gid}/{token}/publication` 返回的 publication）额外内嵌 `readingOrder`：逐页图片 URL（`/stream/{gid}/{token}/page/{n}`，n 从 `PSE_PAGE_BASE` 起，共页数条）——Stump 的 Stream 阅读器据此逐页拉图，零额外查询。

**字段分层约定（本项目核心）**：

- **标准层**：只输出 OPDS/RWPM 标准字段（`title`/`identifier`/`authors`/`language`/`subject`/`numberOfPages`/`modified`/`published`；`description` 预留、当前不输出），通用客户端（对标 Panels）直接消费。`subject` 为拍平标签字符串数组（RWPM/Komga 风格，`ns:key`，不含分类）：详情文档含完整 taglist（经 status 过滤后的全部标签）；列表 feed 是子集（额外剔除 `language`/`artist`——language 已有独立字段，author 由客户端从文件名解析）。
- **语言码（BCP 47）**：`metadata.language` 输出 RFC 5646（BCP 47）语言码（`chinese`→`zh`、`chinese (simplified)`→`zh-Hans`、`chinese (traditional)`→`zh-Hant`…），由 `app/eh/languages.py` 映射表统一映射（列表/详情/gdata 三路共用）；未知语言与标记伪标签（`translated`/`rewrite`/`raw`）不输出——原始标签文本仍在详情文档 `subject` 中。搜索语法不受影响：`query=language:chinese` 仍用 EH 原生标签名。
- **标签 status（社区可信度，全局过滤策略）**：EH 标签带 `gt`(confidence)/`gtl`(skepticism)/`gtw`(incorrect) class（列表页与详情页 `#taglist` 同构）。低于 `TAG_STATUS_FILTER` 等级（`balanced` 默认：confidence+skepticism；`strict`：仅 confidence；`off`：全部）的标签从 **subject 与 mytags 一并剔除**——拒绝模棱两可的标签进入目录。status **不传递给客户端**（服务端消费后即丢弃），客户端无法感知被过滤标签的存在。
- **私货层 `metadata.extensions`**：**所有** EH 专属/非标准字段收敛于此单一字段，Kasane 只读它：`rating`、`uploader`、`titleJpn`、`sizeBytes`、`expunged`、`category`、`mytags`、`reviews`。`category` 刻意不进 `subject`（避免与标签混淆）；未来如需对通用客户端暴露分类，走 OPDS 2.0 `facets`（按分类筛选）或 `navigation`（分类浏览入口），勿再塞回 `subject`。
- **`extensions.mytags`（列表专属字段，详情不输出）**：仅含**带高亮 style 的标签**（经 status 过滤后），条目 = `namespace`/`key` + `style`（`color`/`borderColor`/`background`，来自列表页 inline style，`!important` 已剥离），**无 status**。语义 = "值得高亮展示的标签"，客户端用它查询高亮样式。详情文档不含 mytags（详情页 `#taglist` 本无高亮 style）——客户端展开详情时**合并**（subject 以详情完整版替换、mytags 保留列表条目继承高亮），勿整体替换重建。
- **浏览 vs 详情（字段分级）**：浏览 feed（列表/首页/toplist）零 ehapi，`extensions` 只含列表页可得字段子集（`category`、`rating`、`mytags`）；`titleJpn`/`sizeBytes`/`expunged`/`uploader` 仅详情文档输出（gdata）。Kasane 必须按字段缺失容忍，完整元数据以详情文档为准（subject 亦以详情完整版为准）。
- 标签高亮数据来源：列表 feed 的 `mytags` 来自列表页解析的高亮标签（布局固定 extended）；全量标签进 `subject`（列表精简 / 详情完整），二者皆经 `TAG_STATUS_FILTER` 统一过滤，保持子集关系。
- **`extensions.reviews`（详情专属，v2.0 仅输出；v1.2 纯标准 Atom 不含）**：评论区（详情页 `#cdiv > .c1`，解析器随详情页 HTML 一并提取，零额外上游请求；随 1h 详情页缓存同步过期）。条目 = `id`/`username`/`userId`（可选，无则省略）/`time`/`lastEditTime`（可选）/`content`（**原始 HTML**，含 `.c6` 容器与 id 属性，客户端自行 sanitize 后渲染）。交互状态（fromMe/votedUp/votedDown）与评分详情**不输出**。`COMMENTS_ENABLED=0` 关闭输出（解析仍进行，仅控制序列化）。**链接重写**：`content` 中 E-Hentai 图库链接（`(e-hentai|exhentai).org/(g|mpv)/{gid}/{token}/`，含 `?p=`/锚点）自动重写为 OPDS 2.0 详情链接 `href()`（相对路径 / `PUBLIC_BASE_URL` 绝对）→ `/opds/v2.0/gallery/{gid}/{token}`，锚文本保留原 URL，非图库链接（uploader/forums/外链）原样——app 内点击评论引用图库即可跳转。

```json
{
  "metadata": {
    "title": "{title}",
    "identifier": "urn:ehentai:gallery:{gid}:{token}",
    "modified": "{iso8601}",
    "authors": [{"name": "{作者：标题括号解析，非 uploader}"}],
    "language": ["{language}"],
    "subject": ["{ns}:{key}", "..."],
    "numberOfPages": {filecount},
    "description": "（预留，当前不输出）",
    "published": "{iso8601}",
    "extensions": {
      "rating": {rating},
      "uploader": "{uploader}",
      "titleJpn": "{title_jpn}",
      "sizeBytes": {filesize},
      "expunged": {expunged},
      "category": "{category}",
      "mytags": [{"namespace": "{ns}", "key": "{key}",
                   "style": {"color": "#f1f1f1", "borderColor": "#048751",
                             "background": "radial-gradient(#048751,#24A771)"}}]
    }
  },
  "images": [
    {"href": "/image/{gid}/{token}/thumb", "type": "image/jpeg"}
  ],
  "links": [
    {"rel": "http://opds-spec.org/acquisition", "href": "/stream/{gid}/{token}/page/{pageNumber}",
     "type": "image/jpeg", "templated": true,
     "properties": {"numberOfItems": {filecount}}},
    {"rel": "http://vaemendis.net/opds-pse/stream",
     "href": "/stream/{gid}/{token}/page/{pageNumber}",
     "type": "image/jpeg", "templated": true,
     "properties": {"numberOfItems": {filecount}}},
    {"rel": "self", "href": "/opds/v2.0/gallery/{gid}/{token}/publication",
     "type": "application/opds+json", "title": "{title}"},
    {"rel": "alternate", "href": "https://{e-hentai|exhentai}.org/g/{gid}/{token}/",
     "type": "text/html", "title": "{site_host}"}
  ]
}
```

（默认 `OPDS_ACQ_DETAIL=false`（direct）；`OPDS_ACQ_DETAIL=true` 时列表/首页的 acquisition 指向 `/opds/v2.0/gallery/{gid}/{token}`（`type="application/opds+json;profile=acquisition"`）；详情文档自身恒为直接 image-stream acquisition，不指向自身。顶层恒有 `context`（RWPM）；`metadata` 含 `author`（单数，与 `authors` 并存）；详情 publication 额外内嵌 `readingOrder`。）

### 反代注意事项

- 默认输出**相对路径** href（OPDS 允许，Tachidesk 即如此）；提供 `PUBLIC_BASE_URL` 环境变量（如 `https://opds.example.com`）时输出绝对 URL。
- **Stump 必须设置 `PUBLIC_BASE_URL`**：Stump 用自身服务器地址解析所有相对链接（`resolveUrl(link.href, sdk.rootURL)`），相对路径会被解析到 Stump 自己而 404；Komga 等输出绝对 URL 的服务器无需此配置。
- nginx/caddy 负责 TLS/限速/访问控制；正确透传 Host。
- **可选 Basic Auth（应用层，`app/auth.py` + `app/main.py` 中间件）**：`AUTH_USERNAME` + `AUTH_PASSWORD` 两者都设置才启用（单侧配置不启用，防锁死）；启用后除 `/health`（恒豁免）与 `AUTH_EXEMPT_PATHS`（逗号分隔精确路径）外全部路由需 Basic 凭据（含 WebUI）。密码为明文 env，常量时间比较（`hmac.compare_digest`，UTF-8 bytes 以支持非 ASCII）；401 返回 JSON + `WWW-Authenticate: Basic realm="PandaOPDS"`。**凭据仅 base64 编码，必须在 HTTPS 反代后启用**；失败尝试日志级别 INFO（仅路径，不记凭据）。

## 架构（FastAPI 分层）

```
客户端（对标 Panels）
   │ OPDS 1.2 (Atom) / 2.0 (JSON) + PSE stream links
   ▼
FastAPI (uvicorn) 单进程
├─ Feed 层：OPDS XML/JSON 生成（v1.2 Atom + v2.0 JSON + OpenSearch）
├─ 代理层：图片/缩略图流式转发 + 磁盘缓存
├─ 数据层：gdata API + 列表/详情/图片页 HTML 解析
├─ 缓存层：内存（元数据/cover/页面URL）+ 磁盘（图片，7d）
└─ 限流层：asyncio.Semaphore + 请求间隔；banned/509/exceedLimit 检测与熔断
```

一次图库完整生命周期：
1. **浏览阶段**（列表/首页/toplist feed）：只抓列表页 HTML → `parse_list_page` 渲染条目（零 ehapi），顺带写入 cover 缓存
2. **详情文档请求**（v1.2 `/chapters` / v2.0 `/gallery/{gid}/{token}`）→ `get_detail_page(gid, token, 0)`（详情页 HTML，缓存 1h，与 `/stream` 共享）→ 解析 `#gn`/`#gj`/`#gdd`/`#gdn`/`#grt2`/`#taglist` 渲染完整条目；**此次抓取同时预暖 page-URL 映射，客户端点"立即阅读"时详情页缓存命中，进入阅读器少一次串行上游往返**（这是客户端"点开详情 → 预取 → 秒开阅读器"的关键路径）
3. `/stream/page/{n}` 请求 → 页面 URL 缓存未命中 → 抓详情页 `?p={(n-1)//20}`（1 请求服务 20 页）→ 取第 n 个 `/s/` URL → 抓 `/s/` 页解析 `#img` src → 抓图片字节 → 磁盘缓存 → 流式返回（n 为 1-based 时）
4. 触发 509 → 429；banned/exceedLimit → 全局熔断

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
curl -s http://localhost:8000/api/status | head -c 200; echo  # WebUI 状态（根目录挂载）
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
