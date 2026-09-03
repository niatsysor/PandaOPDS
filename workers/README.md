# PandaOPDS Workers

这是 PandaOPDS 的 Cloudflare Workers 版本，覆盖只读 OPDS / 图片代理路径。

## 范围

这个变体刻意移除了 Python 服务里的后台特性：

- 归档下载 / 转换
- 收藏周期同步
- 基于磁盘的图片缓存
- 长生命周期的进程内任务队列

保留下来的是一个无状态的边缘应用，可以：

- 获取并渲染 OPDS 1.2 feed
- 获取并渲染 OPDS 2.0 feed
- 代理图库页面图片和缩略图 URL
- 通过 `/image/fetch` 代理内联评论图片
- 在配置了 IPB Cookie 时，通过 `/api/favorites` 代理收藏写操作

> [!NOTE]
> 触发 EH 限流 ban ip 时可自行尝试更改本地 ip 以刷新 Cloudflare 分配给 Workers 的 ip

## 本地开发

```bash
cd workers
npm install
npm run dev
```

## 部署

使用 Wrangler：

```bash
cd workers
npm install
npm run deploy
```

把敏感的上游 Cookie 作为 secret 配置，不要写进 `wrangler.toml`：

```bash
wrangler secret put IPB_MEMBER_ID
wrangler secret put IPB_PASS_HASH
```

如果你还需要 `exhentai` 访问，就配置同样的一对 secret，并把 `EH_SITE=exhentai` 作为普通环境变量。

可选变量：

- `EH_SITE` = `e-hentai` 或 `exhentai`
- `NW` = 上游 Cookie 标志，用于绕过警告页；默认 `1`
- `DATATAGS` = 新缩略图结构所需的上游 Cookie 标志；默认 `1`
- `EH_PROFILE` = 可选的独立 uconfig profile 名称；默认 `PandaOPDS`
- `PUBLIC_BASE_URL` = 生成链接时使用的绝对基础 URL
- `HTML_INTERVAL_SECONDS` = HTML/API 上游请求之间的最小间隔；默认 `0.3`
- `AX_CONCURRENCY` = HTML/API 上游并发数；默认 `5`
- `IMAGE_MAX_CONCURRENCY` = 大图上游并发数；默认 `5`
- `THUMB_MAX_CONCURRENCY` = 缩略图 / 评论图并发数；默认 `25`
- `OPDS_ACQ_DETAIL` = 设为 `true` 时，列表 / 根目录 publication 会先获取详情文档；设为 `false` 时保持直接流式获取（未设置时仍兼容旧的 `OPDS_ACQ_MODE=detail|direct`）
- `TAG_STATUS_FILTER` = `balanced`（默认）、`strict` 或 `off`，控制哪些 tag-status class 会输出到 OPDS subject
- `COMMENTS_ENABLED` = `true`（默认）或 `false`，控制是否在 OPDS 2.0 详情文档里输出 `x:reviews`
- `TAG_TRANSLATION_ENABLED` = 设为 `true` 时翻译 OPDS 2.0 subject 标签并重写翻译后的搜索词；默认词典 URL 由 `TAG_TRANSLATION_URL` 提供
- `TAG_TRANSLATION_INTERVAL_SECONDS` = 翻译词典的内存刷新间隔（默认 `86400`；`0` 表示在 isolate 生命周期内保留首次成功加载结果）
- `MYTAGS_TTL_SECONDS` = `/mytags` 样式回填的内存 TTL（默认 `21600`；`0` 表示每次详情请求都刷新）
- `HOME_CONFIG_TOML` = 可选的 OPDS 2.0 根布局 TOML 文本；留空则使用内置默认，也可以直接粘贴 [home.toml.example](home.toml.example) 的内容
- `AUTH_USERNAME` / `AUTH_PASSWORD` = 可选的 Basic Auth 保护
- 启用 Basic Auth 时，建议设置 `AUTH_EXEMPT_PATHS=/image/fetch`，这样浏览器加载的评论图片仍然可以渲染
- `AUTH_EXEMPT_PREFIXES=/image/` 也受支持，如果你想放开更大的公开图片子树
- `IGNEOUS` = 可选的会话种子；正常运行不需要
- `FACETS` = 可选的 `name:mask` 列表，用于 v2.0 分类过滤；默认使用标准的 10 个 EH facet
- `IMAGE_PROXY_HOSTS` = `/image/fetch` 的逗号分隔白名单
- 构建里包含一个名为 `EH_CACHE` 的 KV 绑定；它保存 `mytags` 和 `EhTagTranslation` 的可恢复快照
- 构建里包含一个持久化 Durable Object 绑定 `EH_STATE`；它保存收藏分类和其它仍然受益于串行访问的小状态
- 构建里包含一个 Durable Object 限流绑定 `EH_LIMITER`；它提供 Workers 里的信号量式上游节流

缓存 TTL 变量在运行时读取，不需要重新部署就能调：

- `LIST_CACHE_TTL_SECONDS` = 列表 feed 缓存 TTL，默认 `600`
- `DETAIL_CACHE_TTL_SECONDS` = 详情文档缓存 TTL，默认 `3600`
- `IMAGE_CACHE_TTL_SECONDS` = 图片缓存 TTL，默认 `604800`

## Cloudflare 配置

推荐的首次部署步骤：

1. `cd workers`
2. `npm install`
3. `wrangler kv namespace create EH_CACHE`
4. `wrangler kv namespace create EH_CACHE --preview`
5. 把命令返回的 namespace id 填到 [wrangler.toml](wrangler.toml) 的 `id` 和 `preview_id`
6. `wrangler secret put IPB_MEMBER_ID`
7. `wrangler secret put IPB_PASS_HASH`
8. `wrangler deploy`

本地开发时，把 `.dev.vars.example` 复制为 `.dev.vars`，然后填入同样的键值，这样更接近真实上游会话。

如果你不想手动创建 KV namespace，也可以直接在 Cloudflare dashboard 里拿到 namespace id，然后填回 [wrangler.toml](wrangler.toml) 再部署。

> [!NOTE]
> 每次部署后需手动在 Cloudflare 对应的 worker 项目的面板设置里启用缓存。

## 说明

- 这个项目刻意不包含归档下载、周期收藏同步或磁盘持久化。
- 收藏写代理已经包含，但它仍然是无状态的：没有本地收藏缓存，也没有后台对账。
- 对于不能正确解析相对链接的 OPDS 阅读器，强烈建议设置 `PUBLIC_BASE_URL`。
- Basic Auth 是可选项，只适用于私有部署。
- OPDS 2.0 根布局通过 `HOME_CONFIG_TOML` 配置；[home.toml.example](home.toml.example) 展示了支持的 `group` / `section` 结构。
