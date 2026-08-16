# PandaOPDS

**OPDS-PSE 串流服务器**，作为 E-Hentai 的中转代理：输出 **OPDS 1.2（Atom）** 与 **OPDS 2.0（JSON）** 双版本目录 + PSE 串流链接，适用于各种支持 OPDS-PSE 流式传输的阅读器。

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

### 可选配置

- **OPDS 2.0 首页布局**：编辑 `config/home.toml`（参照 `config/home.toml.example`），声明分组与区块；不配置时使用内置默认布局。
- **分类筛选（facets）**：`FACETS` 环境变量，格式 `名称:排除掩码`，逗号分隔（如 `FACETS=纯本子:1021,漫画:1019`）。
- **不提供 IPB cookie**：服务照常运行，公开内容（Latest / Popular / Toplist / Search）可用，仅 Watched / Favorites 导航项不输出。

## WebUI

内置一个轻量管理界面，用于**查看**当前配置与运行状态（当前阶段只读，编辑能力后续迭代）：

| 路由 | 说明 |
|------|------|
| `GET /` | 单页界面：仪表盘（状态/熔断器/请求计数/缓存）+ 环境变量配置 + 首页布局（挂载于根目录） |
| `GET /api/status` | JSON：服务状态、熔断器、节流计数、缓存统计、首页来源 |
| `GET /api/config` | JSON：全量生效配置（分组），凭据类字段服务端脱敏 |
| `GET /api/home` | JSON：home.toml 布局（groups/sections、来源标记、解析错误） |

- 前端为单 HTML（内联 CSS/JS，无构建链、无 CDN 依赖），消费上述 JSON API；未来功能（离线项目管理、自动化工作流）扩展 API 层即可，页面契约不变。
- **安全**：`IPB_PASS_HASH` / `IGNEOUS` 永不回传明文（页面与 API 均只显示占位符）。`IPB_MEMBER_ID` 为登录标识，会完整展示。
- WebUI 不触达 E-Hentai，仅读取内存状态；服务配置异常时页面照常可访问并显示错误详情。
- **可选 Basic Auth**：设置 `AUTH_USERNAME` + `AUTH_PASSWORD` 后，除 `/health`（及 `AUTH_EXEMPT_PATHS` 指定路径）外全部路由需 Basic 凭据，WebUI 同样受保护；未配置时保持默认公开（docker-compose 绑定 loopback / 反代控制访问）。

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
| `AUTH_USERNAME` | 空 | 可选 Basic Auth 用户名；与 `AUTH_PASSWORD` **同时设置**才启用 |
| `AUTH_PASSWORD` | 空 | 可选 Basic Auth 密码（明文，脱敏显示；仅建议 HTTPS 反代下启用） |
| `AUTH_EXEMPT_PATHS` | 空 | 逗号分隔的精确路径，认证下仍公开；`/health` 恒豁免 |
| `LOG_LEVEL` | `INFO` | `INFO` \| `DEBUG`（DEBUG 输出每次出站请求，用于排障） |

完整路由与客户端接入细节见 [AGENTS.md](AGENTS.md)。

## 注意

- 本服务是服务器（多客户端、单 IP 集中请求），比个人客户端更易触发 E-Hentai 封禁。请保持节流参数默认值、善用缓存。
- 图片限额触发返回 429；IP 被封 / 超限触发全局熔断并返回 503，冷却后自动恢复。
- **启用 Basic Auth 时必须位于 HTTPS 反代之后**：Basic 凭据仅 base64 编码（非加密），明文传输即泄露。
- 仅限个人使用，请遵守 E-Hentai 服务条款。

## 许可证与致谢

本项目采用 **Apache License 2.0**（见 `LICENSE`）。

E-Hentai 抓取与解析实现（`app/eh/`）参考了 [JHenTai](https://github.com/jiangtian616/JHenTai)（Apache License 2.0，Copyright JHenTai contributors）：HTML 选择器、页面 URL 约定、会话/cookie 处理与上游异常检测机制均以 JHenTai 为参照，代码为本项目的 Python 独立重写。依据 Apache 2.0 §4，本仓库保留其归属声明（见 `THIRD_PARTY_LICENSES`）。
