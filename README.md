# PandaOPDS

**OPDS-PSE 串流服务器**，作为 E-Hentai / ExHentai 的中转代理：输出 **OPDS 1.2（Atom）** 与 **OPDS 2.0（JSON）** 双版本目录 + PSE 串流链接，任何支持 OPDS-PSE 的漫画阅读器（Panels、Kasane、LANraragi 客户端等）都能直接当作目录源使用。

- 浏览 / 搜索 / 热门 / 排行榜 / Watched / Favorites 一站式目录
- 图片与缩略图代理串流，内置磁盘 LRU 缓存（默认 4GB / 7 天），一次抓取服务多个客户端
- 内置全局节流与熔断（IP 封禁 / 图片限额自动检测降级），服务器长期稳定运行
- 单进程异步，Python 3.11+ / FastAPI / httpx

> 本仓库仅包含中转代理逻辑，不托管任何内容。

## 部署

### Docker（推荐）

```bash
git clone https://github.com/<your-name>/PandaOPDS.git
cd PandaOPDS

# 1. 登录 e-hentai.org，从浏览器 cookie 中取 ipb_member_id 与 ipb_pass_hash
cp .env.example .env
# 编辑 .env，填入 IPB_MEMBER_ID / IPB_PASS_HASH

# 2. 启动
docker compose up -d --build
```

服务监听 `127.0.0.1:8000`（仅本机），用反代对外暴露（示例见 `deploy/`）：

- `deploy/nginx.conf.example` — nginx
- `deploy/Caddyfile.example` — Caddy

反代需透传 Host；设置 `PUBLIC_BASE_URL=https://opds.example.com` 后 feed 输出绝对 URL，否则为相对路径。

```bash
# 3. 验证
curl -H "Accept: application/opds+json" http://localhost:8000/opds/v2.0
curl http://localhost:8000/health
```

客户端目录源填入 `https://opds.example.com/opds/v2.0`（OPDS 2.0）或 `/opds/v1.2`（OPDS 1.2）。

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export IPB_MEMBER_ID=... IPB_PASS_HASH=...
uvicorn app.main:app --port 8000
```

### 使用 exhentai

只需设置 `EH_SITE=exhentai`。服务器会用 IPB 会话先在 e-hentai 鉴权，再访问 exhentai，会话级 `igneous` 由 cookie jar 自动维护（`IGNEOUS` 无需提供）。

### 可选配置

- **OPDS 2.0 首页布局**：编辑 `config/home.toml`（参照 `config/home.toml.example`），声明分组与区块；不配置时使用内置默认布局。
- **分类筛选（facets）**：`FACETS` 环境变量，格式 `名称:排除掩码`，逗号分隔（如 `FACETS=纯本子:1021,漫画:1019`）。
- **不提供 IPB cookie**：服务照常运行，公开内容（Latest / Popular / Toplist / Search）可用，仅 Watched / Favorites 导航项不输出。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `IPB_MEMBER_ID` | 空 | **必需**，登录 cookie（不填则 Watched/Favorites 不可用） |
| `IPB_PASS_HASH` | 空 | **必需**，登录 cookie |
| `EH_SITE` | `e-hentai` | `e-hentai` \| `exhentai` |
| `IGNEOUS` | 空 | 可选会话种子；exhentai 会话建立时自动下发，无需用户提供 |
| `NW` | `1` | 绕过 Offensive For Everyone 警告 |
| `DATATAGS` | `1` | 启用新缩略图结构 |
| `PUBLIC_BASE_URL` | 空 | 设置后 feed 输出绝对 URL，如 `https://opds.example.com` |
| `CACHE_DIR` | `./cache` | 图片磁盘缓存目录 |
| `CACHE_MAX_GB` | `4` | 磁盘缓存上限（GB） |
| `IMAGE_CACHE_ENABLED` | `true` | 设为 `false` 关闭磁盘缓存 |
| `HTML_INTERVAL_SECONDS` | `0.3` | HTML 出站请求最小间隔（秒），防封关键；docker-compose 预设 `1.5` |
| `MAX_CONCURRENCY` | `5` | 出站并发上限；docker-compose 预设 `2` |
| `TIMEOUT_SECONDS` | `6` | 出站请求超时（秒） |
| `RETRIES` | `3` | 网络错误重试次数 |
| `BANNED_COOLDOWN_SECONDS` | `1800` | IP 封禁熔断冷却（秒） |
| `EXCEED_COOLDOWN_SECONDS` | `300` | 图片限额熔断冷却（秒） |
| `PSE_PAGE_BASE` | `1` | PSE 页码基数：`1`（LANraragi/Kasane 兼容，默认）或 `0`（OPDS-PSE 规范原文） |
| `TAG_STATUS_FILTER` | `balanced` | 标签可信度过滤：`balanced`（默认，confidence+skepticism）、`strict`（仅 confidence）、`off`（全部保留） |
| `FACETS` | 内置 10 分类 | OPDS 2.0 分类筛选，格式 `名称:掩码,名称:掩码` |
| `EH_PROFILE` | `PandaOPDS` | E-Hentai uconfig 独立 profile 名；设空串关闭 |
| `HOME_CONFIG` | `./config/home.toml` | OPDS 2.0 首页布局配置文件路径 |
| `LOG_LEVEL` | `INFO` | `INFO` \| `DEBUG`（DEBUG 输出每次出站请求，用于排障） |

完整路由与客户端接入细节见 [AGENTS.md](AGENTS.md)。

## 注意

- 本服务是服务器（多客户端、单 IP 集中请求），比个人客户端更易触发 E-Hentai 封禁。请保持节流参数默认值、善用缓存。
- 图片限额触发返回 429；IP 被封 / 超限触发全局熔断并返回 503，冷却后自动恢复。
- 仅限个人使用，请遵守 E-Hentai 服务条款。
