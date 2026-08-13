# PandaOPDS 客户端元数据手册

面向**自研阅读器**开发者。描述 PandaOPDS 输出的 OPDS 1.2（Atom）与 OPDS 2.0（JSON）全部文档结构、字段、取值与渲染规则。通用客户端（对标 Panels）消费标准层即可；自研客户端额外消费 `extensions` 私货层。

---

## 1. 概览

| 项 | 值 |
|---|---|
| 版本路径 | `/opds/v1.2`（Atom）、`/opds/v2.0`（JSON） |
| 媒体类型 | v1.2 导航 `application/atom+xml;profile=opds-catalog;kind=navigation`；采集 `…;kind=acquisition` |
| | v2.0 导航 `application/opds+json;profile=navigation`；采集 `application/opds+json;profile=acquisition` |
| href | 默认**相对路径**；设置 `PUBLIC_BASE_URL` 时输出绝对 URL |
| 页码 | PSE stream 默认 **1-based**（第 1 页 = `page/1`）；`PSE_PAGE_BASE=0` 可切 0-based（带外约定，不在链路中传输） |
| 缓存 | feed 均 `Cache-Control: public, max-age=300` |

**登录态**：`IPB_MEMBER_ID`/`IPB_PASS_HASH` 可选。未提供时 Watched/Favorites 导航项**不输出**；提供时输出（不做探测验证，cookie 失效时对应 feed 返回 503）。

---

## 2. 主页文档（`GET /opds/v2.0`）——设计主页的核心

文档包含 `navigation[]`（纯导航链接）和 `groups[]`（含内联 publication 预览的分组区块，OPDS 2.0 §2.5）。真实输出：

```json
{
  "metadata": { "title": "PandaOPDS", "identifier": "urn:ehentai:root", "modified": "2026-08-11T15:31:30Z" },
  "links": [
    { "href": "/opds/v2.0", "rel": "self", "type": "application/opds+json;profile=navigation", "title": "PandaOPDS" },
    { "href": "/opds/v2.0", "rel": "start", "type": "application/opds+json;profile=navigation", "title": "PandaOPDS" },
    { "href": "/opds/v2.0/gallery?query={searchTerms}", "rel": "search", "type": "application/opds+json;profile=acquisition", "title": "Search" }
  ],
  "navigation": [
    {
      "metadata": {
        "title": "Watched",
        "identifier": "urn:ehentai:subsection:watched",
        "modified": "2026-08-11T15:31:30Z",
        "description": "Watched galleries"
      },
      "links": [
        { "href": "/opds/v2.0/gallery?query=watched", "rel": "subsection",
          "type": "application/opds+json;profile=acquisition", "title": "Watched" }
      ]
    }
  ],
  "groups": [
    {
      "metadata": {
        "title": "Latest",
        "identifier": "urn:ehentai:group:latest",
        "modified": "2026-08-11T15:31:30Z"
      },
      "links": [
        { "rel": "self", "href": "/opds/v2.0/gallery",
          "type": "application/opds+json;profile=acquisition", "title": "Latest" }
      ],
      "publications": [ "…前 N 条 publication（见 §3）…" ]
    }
  ]
}
```

### 2.1 分区逻辑

| 组件 | 内容 | 说明 |
|---|---|---|
| `groups[]` | 内联 publication 预览的分组区块 | `config/home.toml`（`[[group]]`）控制；环境变量 `HOME_CONFIG` 可指定路径 |
| `navigation[]` | 纯导航链接（不含 extensions） | `config/home.toml`（`[[navigation]]`）控制；Watched/Favorites 无 IPB cookie 时不输出 |
| `links[].rel="search"` | 搜索模板 | 顶层 link，客户端替换 `{searchTerms}` 即得搜索结果 |

### 2.2 groups[] 元素结构（OPDS 2.0 标准，§2.5）

| 字段 | 说明 |
|---|---|
| `metadata.title` | 区块标题（如 `Latest`、`Popular`、`Toplist: Yesterday`） |
| `metadata.identifier` | `urn:ehentai:group:{key}` |
| `metadata.modified` | ISO8601（UTC） |
| `links[0]` | `rel="self"`，`href` = 该区块的完整采集文档 |
| `publications[]` | 内联预览条目（数量由 TOML `publications` 字段控制），字段见 §3 |

每个 group 是 OPDS 2.0 标准结构——**任何兼容客户端均可原生渲染为分栏网格**，无需私货标记。

### 2.3 navigation[] 元素结构

| 字段 | 说明 |
|---|---|
| `metadata.title` | 导航入口标题 |
| `metadata.identifier` | `urn:ehentai:subsection:{title 小写}` |
| `metadata.description` | 一句话描述 |
| `links[0]` | `rel="subsection"`，`href` = 完整采集文档 |

> **不再有 `extensions.layout` 私货**：showcase 机制已被 groups 取代。`navigation[]` 中所有条目均为纯导航链接，客户端按标准 `subsection` 语义处理即可。

### 2.4 所有已知区块清单

| title | type / query | href | 出现条件 |
|---|---|---|---|
| Latest | `preset` / `latest` | `/opds/v2.0/gallery` | 恒有 |
| Watched | `preset` / `watched` | `/opds/v2.0/gallery?query=watched` | 有 IPB cookie |
| Favorites | `preset` / `favorites` | `/opds/v2.0/gallery?query=favorites` | 有 IPB cookie |
| Popular | `preset` / `popular` | `/opds/v2.0/gallery?query=popular` | 恒有 |
| Toplist: Yesterday | `preset` / `toplist:yesterday` | `/opds/v2.0/toplist?period=yesterday` | 恒有 |
| Toplist: Past Month | `preset` / `toplist:month` | `/opds/v2.0/toplist?period=month` | 恒有 |
| Toplist: Past Year | `preset` / `toplist:year` | `/opds/v2.0/toplist?period=year` | 恒有 |
| Toplist: All Time | `preset` / `toplist:alltime` | `/opds/v2.0/toplist?period=alltime` | 恒有 |
| 自定义搜索 | `search` / 任意表达式 | `/opds/v2.0/gallery?query=…` | 恒有 |

**服务端调控**：`config/home.toml`（`[[group]]` / `[[navigation]]`），环境变量 `HOME_CONFIG` 可指定路径。书写顺序 = 输出顺序；`publications` 字段控制预览条数。

---

## 3. publication（条目/Item）元数据

任意采集文档（首页 Latest、gallery feed、toplist feed、详情文档）中的单个条目。字段分两层：**标准层**（通用客户端直接消费）与**私货层 `metadata.extensions`**（EH 专属，自研客户端只读）。

### 3.1 标准层

| 字段 | 类型 | 说明 | 条件 |
|---|---|---|---|
| `title` | string | 干净标题（已剥离 `[...]`/`(...)` 标记；作者见 `authors` 字段） | 恒有 |
| `identifier` | string | `urn:ehentai:gallery:{gid}:{token}` | 恒有 |
| `modified` | string | 上传时间 ISO8601（UTC） | 恒有 |
| `authors` | [ {`name`} ] | 作者（从标题 `[Author]` 括号解析，见 §3.6）；上传者本人见详情文档 `extensions.uploader` | 非空时 |
| `language` | [string] | 语言（gdata language 标签，默认 `Japanese`） | 非空时 |
| `published` | string | = `modified`（上传时间） | 恒有 |
| `description` | string | **当前不输出**（预留字段；客户端如需描述，可自行拼接 `language`/`numberOfPages`/`authors`/`extensions.rating`/`extensions.sizeBytes`） | — |
| `subject` | [string] | 拍平标签 `ns:key` 数组（Komga 风格，**不含分类**，去重保序） | 有标签时 |
| `numberOfPages` | int | 页数（= `filecount`） | >0 时 |

### 3.2 私货层 `extensions`（单一桶，全部 EH 专属字段）

| 字段 | 类型 | 说明 | 条件 |
|---|---|---|---|
| `rating` | float | 评分（0–5，保留原精度如 4.5） | ≠0 时 |
| `titleJpn` | string | 日文标题 | 非空时 |
| `sizeBytes` | int | 文件总字节 | ≠0 时 |
| `expunged` | bool | 已删除标记 | 仅 `true` 时输出 |
| `category` | string | 分类（Doujinshi/Manga/Artist CG/Game CG/Image Set/Non-H/Western/Misc…） | 恒有 |
| `uploader` | string | 上传者（详情页 `#gdn`） | 仅详情文档，非空时 |
| `mytags` | [Tag] | 仅**带高亮 style 的标签**（经 `TAG_STATUS_FILTER` 过滤后），条目 = `namespace`/`key` + `style`，**无 status**；**列表 feed 专属**（详情文档不输出，客户端展开详情时合并继承） | 有高亮标签时 |

### 3.3 mytags 条目（列表 feed 专属）

```json
{
  "namespace": "female",
  "key": "netorare",
  "style": {
    "color": "#f1f1f1",
    "borderColor": "#048751",
    "background": "radial-gradient(#048751,#24A771)"
  }
}
```

| 字段 | 说明 | 条件 |
|---|---|---|
| `namespace` | 命名空间（`female`/`male`/`parody`/`language`/`artist`/`group`/`character`/`temp`…） | 恒有 |
| `key` | 标签名（下划线已还原为空格） | 恒有 |
| `style` | 高亮标签样式（投票高的 featured 标签），取自上游 inline style，`!important` 已剥离 | 仅高亮标签 |

- **无 `status` 字段**：标签可信度（`gt`/`gtl`/`gtw`）由服务端 `TAG_STATUS_FILTER` 全局消费后即丢弃，不传输给客户端；客户端无法感知被过滤标签的存在。
- **仅列表 feed**：首页/列表的 `mytags` 来自列表页解析的高亮标签（仅含带 inline style 的 featured 标签，经 status 过滤）。**详情文档不输出 `mytags`**（详情页 `#taglist` 无高亮 style）——客户端展开详情时以详情 `subject` 完整版替换、`mytags` 保留列表条目继承高亮，勿整体替换重建。
- **全量标签**：进 `subject`（列表精简 / 详情完整，二者同经 `TAG_STATUS_FILTER`，保持子集关系）；详情文档的完整 `subject` 即为全量标签。

### 3.4 链接（`links[]`）

| rel | href | type | 附加 |
|---|---|---|---|
| `http://opds-spec.org/acquisition` | `/opds/v2.0/gallery/{gid}/{token}` | `application/opds+json;profile=acquisition` | `properties.numberOfItems` = 页数（>0 时） |
| `http://vaemendis.net/opds-pse/stream` | `/stream/{gid}/{token}/page/{pageNumber}` | `image/jpeg` | `properties.numberOfItems` = 页数；`{pageNumber}` 占位符由客户端替换；页数>0 时 |
| `alternate` | 上游 E-Hentai 图库页 `https://{e-hentai\|exhentai}.org/g/{gid}/{token}/` | `text/html` | **恒有**；**分享表单取此 link**（客户端无需感知 `EH_SITE`）；绝对 URL，不受 `PUBLIC_BASE_URL` 影响 |

> **封面不在 `links` 中**：thumbnail link rel（`http://opds-spec.org/image/thumbnail`）是 OPDS 1.x 的做法，v2.0 按规范 §2.3 放入 `images[]` 集合（见 §3.5）。v1.2（Atom）仍用 link rel。

### 3.5 封面（`images[]` 集合）

OPDS 2.0 将视觉表现（封面/缩略图）放在顶层 `images` 集合。**恒有**（缩略图代理零 ehapi，不依赖 gdata）：

```json
"images": [
  { "href": "/image/{gid}/{token}/thumb", "type": "image/jpeg" }
]
```

| 字段 | 说明 |
|---|---|
| `href` | 缩略图代理（302 到上游或磁盘缓存字节） |
| `type` | `image/jpeg` |

当前仅输出一个尺寸；响应式多尺寸（`width`/`height` 变体）预留，未来有尺寸数据时再加。

### 3.6 完整 publication 示例

```json
{
  "metadata": {
    "title": "Nejire",
    "identifier": "urn:ehentai:gallery:4113236:73634e0e9a",
    "modified": "2025-08-11T08:13:20Z",
    "authors": [{ "name": "leopoldo" }],
    "language": ["chinese"],
    "published": "2025-08-11T08:13:20Z",
    "subject": ["language:chinese", "female:netorare", "parody:zenless zone zero"],
    "numberOfPages": 42,
    "extensions": {
      "rating": 4.5,
      "category": "Manga",
      "mytags": [
        { "namespace": "female", "key": "netorare",
          "style": { "color": "#f1f1f1", "borderColor": "#048751",
                     "background": "radial-gradient(#048751,#24A771)" } }
      ]
    }
  },
  "links": [
    { "rel": "http://opds-spec.org/acquisition", "href": "/opds/v2.0/gallery/4113236/73634e0e9a",
      "type": "application/opds+json;profile=acquisition", "title": "Nejire",
      "properties": { "numberOfItems": 42 } },
    { "rel": "http://vaemendis.net/opds-pse/stream", "href": "/stream/4113236/73634e0e9a/page/{pageNumber}",
      "type": "image/jpeg", "properties": { "numberOfItems": 42 } },
    { "rel": "alternate", "href": "https://e-hentai.org/g/4113236/73634e0e9a/",
      "type": "text/html", "title": "e-hentai.org" }
  ],
  "images": [
    { "href": "/image/4113236/73634e0e9a/thumb", "type": "image/jpeg" }
  ]
}
```

---

## 4. 各文档形态

### 4.1 采集文档（gallery feed / toplist feed）

| 端点 | `metadata.identifier` | 分页 |
|---|---|---|
| `/opds/v2.0/gallery?query=watched` | `urn:ehentai:gallery-list:watched` | `rel="next"` → `?next={lastGid}&query=…` |
| `/opds/v2.0/gallery`（Latest） | `urn:ehentai:gallery-list:latest` | `rel="next"` → `?next={lastGid}` |
| `/opds/v2.0/toplist?period=month` | `urn:ehentai:toplist:month` | `rel="next"` → `?period=month&page={n}`（**`page` 分页**，与 lastGid 不同轨） |

- 采集文档恒带 `self` / `start` / `search` 链接；`search` 为 JSON 模板（§5）。
- `query` 取值：空=Latest、`watched`、`favorites`、`popular`；其他任意值 = 搜索词（`f_search`）。

### 4.2 详情文档（`/opds/v2.0/gallery/{gid}/{token}`）

- `publications` 仅 1 条；**不输出 `description`**；完整标签在 `subject`（详情 `#taglist` 全量，经 `TAG_STATUS_FILTER` 过滤）；`extensions` 含 `rating`/`uploader`/`titleJpn`/`sizeBytes`/`expunged`/`category`（**无 `mytags`**，§3.3）。
- 图库不存在 → 404。

### 4.3 搜索

- **v2.0**：顶层 `rel="search"` link 的 `href` 直接含模板 `/opds/v2.0/gallery?query={searchTerms}`——客户端替换 `{searchTerms}` 即得搜索结果文档，无需先请求 OpenSearch。
- **v1.2**：`rel="search"` 指向 `/opds/v1.2/search.xml`（OpenSearchDescription），模板 `?query={searchTerms}`。

---

## 5. 链接语义（rel 表）

| rel | 用途 |
|---|---|
| `self` / `start` | 本文档 / 根导航 |
| `search` | 搜索（v2.0 JSON 模板；v1.2 OpenSearch 文档） |
| `next` | 下一页（gallery 用 lastGid；toplist 用 page） |
| `subsection` | 导航项 → 采集文档 |
| `http://opds-spec.org/acquisition` | 获取详情 |
| `http://opds-spec.org/image/thumbnail` | 封面（**仅 v1.2 Atom**；v2.0 走 `images[]` 集合，§3.5） |
| `http://vaemendis.net/opds-pse/stream` | PSE 串流（`{pageNumber}` 占位符） |
| `alternate` | 上游 E-Hentai 原始网页（恒有，分享/跳浏览器用） |

---

## 6. 端点 href 模板

| 用途 | href |
|---|---|
| 图片流 | `/stream/{gid}/{token}/page/{pageNumber}` → `image/jpeg`；越界/509 → 429/404 |
| 封面 | `/image/{gid}/{token}/thumb` → `image/jpeg` |
| 详情（v2.0） | `/opds/v2.0/gallery/{gid}/{token}` |
| 章节（v1.2） | `/opds/v1.2/gallery/{gid}/{token}/chapters` |
| Toplist | `/opds/{v1.2,v2.0}/toplist?period=yesterday\|month\|year\|alltime&page={n}` |

---

## 7. v1.2（Atom）对照——仅标准，无私货

**约束：v1.2 不输出任何 `extensions` 标记，也不在根 feed 混入采集条目。** 自研客户端如需 v1.2 兼容，只消费标准字段。

### 7.1 根导航 entry

```xml
<entry>
  <id>urn:ehentai:subsection:popular</id>
  <title>Popular</title>
  <updated>2026-08-11T15:31:30Z</updated>
  <summary>Popular this week</summary>
  <link rel="subsection" href="/opds/v1.2/gallery?query=popular"
        type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>
</entry>
```

### 7.2 图库 entry（列表/章节）

| 元素 | 说明 |
|---|---|
| `id` | `urn:ehentai:gallery:{gid}:{token}` |
| `title` | 列表 = 标题；章节 = `Chapter 1: {title}` |
| `updated` / `author/name` | 上传时间 / 上传者 |
| `category` | `term`/`label` = 分类，`scheme="http://e-hentai.org"` |
| `summary` | **当前不输出**（预留；v1.2 列表/章节条目 `summary` 恒为空，与 v2.0 `description` 一致） |
| link `http://opds-spec.org/image/thumbnail` | 封面 |
| link `http://opds-spec.org/acquisition` | `/opds/v1.2/gallery/{gid}/{token}/chapters` |
| link `http://vaemendis.net/opds-pse/stream` | `/stream/{gid}/{token}/page/{pageNumber}`，`type="image/jpeg"`，**`pse:count` 属性** = 页数（命名空间 `http://vaemendis.net/opds-pse/ns`） |
| link `alternate` | 上游 E-Hentai 图库页（`type="text/html"`），分享/跳浏览器用 |

---

## 8. 客户端渲染规则速查（自研阅读器）

1. 请求 `/opds/v2.0` 作为主页文档。
2. `groups[]` → 每个 group 直接渲染为一个网格区块：标题 = `metadata.title`，内容 = `publications[]`。点击区块条目 → 走 `acquisition` 或直接 `stream`；点击区块标题 → 完整列表（`links[0].href`）。通用客户端同样原生支持 groups，无需任何私货解析。
3. `navigation[]` → 渲染为普通导航列表（可点击进入完整列表）。
4. 搜索：用顶层 `search` link 的 JSON 模板替换 `{searchTerms}`。
5. 分页：`rel="next"`（gallery 传 `next`，toplist 传 `page`）。
6. 详情：`/opds/v2.0/gallery/{gid}/{token}` 的 `subject` 为完整标签（经 status 过滤）；列表 `mytags` 无 status、仅高亮样式——展开详情时用详情 `subject` 替换列表精简版、`mytags` 保留列表条目继承高亮（勿整体替换重建）。
7. 失效兜底：单个 group 上游故障时该 group 不出现在 `groups[]` 中（其他 groups 和 navigation 照常）；首页布局由 `home.toml` 配置驱动，客户端无需感知。
8. **分享**：取 publication / entry 的 `rel="alternate"` link（`type="text/html"`）作为分享 URL——即上游 E-Hentai 页面（e-hentai.org 或 exhentai.org，服务端已按 `EH_SITE` 拼好），客户端无需感知 `EH_SITE`。勿用 acquisition/stream（那些是服务端资源，离开服务端不可达）。
