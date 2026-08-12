# 初期实施计划（PandaOPDS）

> 目标：实现 OPDS-PSE 串流服务器，代理 E-Hentai.org，服务自研阅读器客户端（对标 Panels）。
> 技术栈：Python + FastAPI + uvicorn + httpx + lxml。
> 详细背景见 `AGENTS.md`。本计划按阶段推进，每阶段有明确验收标准。

## 阶段 0 — 项目骨架与配置

**任务**
- [x] 初始化 Python 项目：`pyproject.toml`（依赖：fastapi, uvicorn[standard], httpx, lxml, cssselect, python-dotenv, pytest, pytest-asyncio）
- [x] 目录结构：`app/`（main.py, config.py, eh/（数据层）, opds/（feed 层）, stream/（代理层）, cache/, throttle/）
- [x] `config.py`：从环境变量读取 `IPB_MEMBER_ID`、`IPB_PASS_HASH`、`DATATAGS=1`、`NW=1`、`PUBLIC_BASE_URL`（可选）、`CACHE_DIR`、`CACHE_MAX_GB`（默认 4）、`EH_SITE`（e-hentai/exhentai，默认 e-hentai）
- [x] 统一出站 HTTP 客户端（httpx.AsyncClient）：默认超时 6s、Cookie 注入、失败重试 3 次（仅网络错误）
- [x] `app/main.py`：FastAPI 启动，`/health` 端点

**验收**：`uvicorn app.main:app` 可启动，`/health` 返回 200；错误配置（缺 cookie）给出明确提示。

---

## 阶段 1 — E-Hentai 数据层（核心闭环）

**任务**
- [x] **gdata API 客户端**：`POST api.e-hentai.org/api.php`，`{"method":"gdata","gidlist":[[gid,token]],"namespace":1}`，批量上限 25 gid；解析 `gmetadata`（title, title_jpn, category, thumb, rating, tags, filecount, filesize, posted, uploader, torrentcount, expunged）
- [x] **列表页解析**：`GET /?f_search={query}&next={lastGid}`，解析条目（gid/token/title/缩略图/分类），支持 `next` 分页取 `lastGid`
- [x] **详情页解析**：`GET /g/{gid}/{token}/?p={n}`，解析 `#gdt` 缩略图区 → 页面 URL 列表（**新旧两种结构都要支持**，见 AGENTS.md）；`.gtb > .gpc` 页码信息
- [x] **图片页解析**：`GET /s/{imageToken}/{gid}-{pageNo}`，解析 `#img` src；检测 509 占位图；解析 `#loadfail` 的 `nl` reloadKey
- [x] 异常检测：banned / exceedLimit / fatal error / 空 body / 404 / 403 / Gallery not found（按 AGENTS.md 表格映射为内部异常类型）

**验收**：Python 脚本（pytest）用真实 cookie 跑通：列表 → gdata 元数据 → 详情翻页拿全部页面 URL → 单页 `/s/` 解析出真实图片 URL → 下载图片字节成功。509 模拟测试（或文档说明）。

---

## 阶段 2 — 缓存与限流

**任务**
- [x] 内存缓存（asyncio + TTL）：元数据 1h、页面 URL 列表 1h（键：gid/token；详情页解析结果缓存 1h）
- [x] 磁盘图片缓存：LRU + TTL 7 天，`CACHE_DIR`/`CACHE_MAX_GB` 控制，可开关；异步写入，避免阻塞
- [x] 限流器：全局 `asyncio.Semaphore`（默认并发 2）+ 出站请求间隔（HTML 默认 1.5s，可配置）；banned/exceedLimit 触发全局熔断（拒绝新任务，返回 503）
- [x] 509 → 映射 HTTP 429 给客户端；图库已删除 → 404；cookie 失效 → 503 + 明确错误信息

**验收**：同一图库连续请求 `/stream/page/{n}` 全部命中缓存时**零出站请求**；压力测试（并发 20 请求）不触发 E-Hentai 封禁（观察 banned/exceedLimit 日志为空）。

---

## 阶段 3 — OPDS Feed 层

**任务**
- [x] Atom XML 生成（lxml）：feed 头（`application/atom+xml;profile=opds-catalog;kind=navigation`）、entry、link 序列化
- [x] `GET /opds`：根导航 feed（Latest / Popular / Search 三个 subsection 入口 + OpenSearch link）
- [x] `GET /opds/search.xml`：OpenSearchDescription（template 指向 `/opds/gallery?query={searchTerms}`）
- [x] `GET /opds/gallery?query=&next=`：图库采集 feed；条目含 title/author/category/缩略图/subsection 链接（→ 章节 feed）；分页 `rel="next"`（用 `next` + lastGid）
- [x] `GET /opds/gallery/{gid}/{token}/chapters`：章节 feed——gdata 元数据（无 API 结果时 fallback 详情页）→ 单章节条目 + PSE stream link（`pse:count=filecount`，href 含 `{pageNumber}`）+ 缩略图 link
- [x] 绝对/相对 URL 策略：默认相对路径；设 `PUBLIC_BASE_URL` 时输出绝对 URL

**验收**：curl 各端点返回合法 XML；用 `xmllint` 验证格式；条目字段符合 OPDS-PSE 规范（rel/type/pse:count/href 模板）。

---

## 阶段 4 — PSE 流代理层

**任务**
- [x] `GET /stream/{gid}/{token}/page/{n}`：默认 1-based 页码（`PSE_PAGE_BASE=0` 可切 0-based）→ 换算 pageNo → 查页面 URL 缓存 → 未命中抓详情页 `?p={(n-1)//20}` → `/s/` 页解析 `#img` src → 抓图片字节 → 磁盘缓存 → 流式返回 `image/jpeg`
- [x] 图片加载失败重试：`?nl={reloadKey}` 备份 URL（复用 mihon 插件/JHenTai reloadKey 机制）
- [x] `GET /image/{gid}/{token}/thumb`：缩略图代理（代理转发 + 磁盘缓存，非 302）（详情页解析的缩略图 URL，或 302 到 ehgt.org CDN）
- [x] 超时/取消：客户端断开时取消出站请求；`Cache-Control` 头适当设置

**验收**：`curl -o /tmp/p0.jpg /stream/{gid}/{token}/page/0` 返回有效 JPEG（`file` 命令确认）；连续请求 0..N-1 页全部成功；第二次全量请求全部命中缓存（无新增出站日志）。

---

## 阶段 5 — Docker 与反代

**任务**
- [x] `Dockerfile`（python:3.11-slim）+ `docker-compose.yml`（环境变量注入、`CACHE_DIR` 挂载 volume）— 本机无 docker daemon，未实际 build 验证
- [x] nginx/caddy 反代示例配置（TLS、Host 透传）`deploy/`
- [x] `README.md`：部署步骤、环境变量说明、客户端接入方式（OPDS 根 URL）

**验收**：docker compose up 后经反代域名可访问 `/opds`，`PUBLIC_BASE_URL` 下 feed 输出正确绝对 URL。

---

## 阶段 6 — 客户端联调与压力测试

**任务**
- [x] 与自研客户端（Kasane）联调：目录浏览 → 图库列表 → 逐页串流均验证通过（修复：列表条目补 PSE stream link、页码改 1-based 默认）
- [x] 边界（离线单测）：0 页图库、expunged 元数据、1000+ 页编号、越界 404、509→429 映射
- [x] 压力测试：真实上游并发 20 请求全成功，banned/exceedLimit 日志为空、熔断未触发（并修复冷缓存并发重复抓取：MemoryCache 单飞）
- [x] 日志与监控：出站请求计数（html/api/image）、内存+磁盘缓存命中率、熔断事件日志、/health 暴露

**验收**：客户端完整阅读流程流畅（预取下一页无感）；持续阅读 30 分钟无 banned/exceedLimit；缓存命中率 > 90%（重复浏览场景）。

---

## 里程碑

| 阶段 | 内容 | 依赖 |
|------|------|------|
| 0 | 骨架 + 配置 | 无 |
| 1 | 数据层闭环 | 阶段 0 |
| 2 | 缓存 + 限流 | 阶段 1（可并行） |
| 3 | OPDS Feed | 阶段 1 |
| 4 | PSE 流代理 | 阶段 2 + 3 |
| 5 | Docker + 反代 | 阶段 4 |
| 6 | 联调 + 压测 | 阶段 5 |

**建议推进顺序**：0 → 1（含 2 的限流骨架）→ 3 → 4 → 2 补全 → 5 → 6。

---

## 关键风险与对策

| 风险 | 对策 |
|------|------|
| E-Hentai 封禁 IP / 图片限额 | 全局节流 + 缓存优先 + 熔断 + 异常检测（阶段 2 提前做） |
| 详情页结构再次改版 | 新旧结构兼容解析 + 解析失败时保留原始 HTML 便于调试 |
| 图片 URL 时效性 | 全量代理 + 页面 URL 缓存 1h + 失败重试（nl） |
| 客户端对 PSE 实现细节差异 | 严格按规范 + 对照 Tachidesk 输出；curl 先行验证 |
| 单 IP 集中请求触发限额 | 可扩展多账号轮换（后续迭代，MVP 不做） |
