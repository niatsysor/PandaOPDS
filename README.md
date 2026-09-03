# PandaOPDS

**OPDS-PSE 串流服务器**，作为 E-Hentai 的中转代理：输出 **OPDS 1.2（Atom）** 与 **OPDS 2.0（JSON）** 双版本目录 + PSE 串流链接，适用于各种支持 OPDS-PSE 流式传输的阅读器。

> 本仓库仅包含中转代理逻辑，不托管任何内容。

## 部署

镜像由 GitHub Actions 自动构建发布至 `ghcr.io/niatsysor/pandaopds`（多架构 linux/amd64 + linux/arm64，树莓派/NAS 直接使用）。免构建启动：

### 方式一：一行命令（无需克隆仓库）

登录 e-hentai.org，从浏览器 cookie 取 `ipb_member_id` / `ipb_pass_hash` 填入：

```bash
docker run -d --name pandaopds --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e IPB_MEMBER_ID=<your_id> \
  -e IPB_PASS_HASH=<your_hash> \
  -e CACHE_DIR=/data/cache \
  -e ARCHIVE_DIR=/data/archives \
  -v pandaopds-cache:/data/cache \
  -v pandaopds-archives:/data/archives \
  ghcr.io/niatsysor/pandaopds:latest
```

可选参数：`-e EH_SITE=exhentai`、`-e PUBLIC_BASE_URL=https://opds.example.com`（反代下输出绝对 URL）、`-e AUTH_USERNAME=xxx -e AUTH_PASSWORD=xxx`（两者都设置才启用 Basic Auth）、`-v ./config:/config`（自定义 OPDS 2.0 首页布局）。

### 方式二：Docker Compose（开箱即用）

```bash
git clone https://github.com/<your-name>/PandaOPDS.git
cd PandaOPDS
cp .env.example .env     # 可选：按需填写 IPB_MEMBER_ID / IPB_PASS_HASH（不复制也能直接启动，全默认）
# 高级可选配置见 .env.example 注释 —— compose 经 env_file 把 .env **全部变量**批量透传进容器，
# 无需在 docker-compose.yml 逐项映射；`.env` 文件缺失或为空也能正常启动（公开内容 + 默认配置），
# 容器内 CACHE_DIR/ARCHIVE_DIR 固定指向持久卷，限流预设由镜像内置
# 自定义 env 路径：
#   PANDAPDS_ENV_FILE=/path/mine.env docker compose --env-file=/path/mine.env up -d
# （两个参数须指向同一文件，保证 environment 插值与 env_file 同源）

docker compose up -d     # 直接拉取预构建镜像，不构建
# 升级：docker compose pull && docker compose up -d
# 固定版本：发布 tag（如 v0.2.0）存在后，把 image 改为 ghcr.io/niatsysor/pandaopds:v0.2.0
```

### 方式三：本地构建（开发者 / ghcr 拉取受限时）

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### 可选配置

- **OPDS 2.0 首页布局**：编辑 `config/home.toml`（参照 `config/home.toml.example`），声明分组与区块；不配置时使用内置默认布局。
- **分类筛选（facets）**：`FACETS` 环境变量，格式 `名称:排除掩码`，逗号分隔（如 `FACETS=纯本子:1021,漫画:1019`）。
- **不提供 IPB cookie**：服务照常运行，公开内容（Latest / Popular / Toplist / Search）可用，仅 Watched / Favorites 导航项不输出。

### Cloudflare Workers 版

如果你只需要无状态的读代理路径（OPDS、图片代理、详情页与流式阅读），可以直接使用 `workers/` 下的新子项目：

- 不包含归档、周期扫描、磁盘缓存和长期后台任务。
- 适合部署到 Cloudflare Workers，使用边缘缓存替代本地状态。
- 入口说明见 [`workers/README.md`](workers/README.md)。

## WebUI

内置一个轻量管理界面，用于**查看**当前配置与运行状态（当前阶段只读，编辑能力后续迭代）：

| 路由                                        | 说明                                                                                   |
| ------------------------------------------- | -------------------------------------------------------------------------------------- |
| `GET /`                                   | 单页界面：仪表盘（状态/熔断器/请求计数/缓存）+ 环境变量配置 + 首页布局（挂载于根目录） |
| `GET /api/status`                         | JSON：服务状态、熔断器、节流计数、缓存统计、首页来源                                   |
| `GET /api/config`                         | JSON：全量生效配置（分组），凭据类字段服务端脱敏                                       |
| `GET /api/home`                           | JSON：home.toml 布局（groups/sections、来源标记、解析错误）                            |
| `GET /api/archive`                        | JSON：归档列表 + 统计                                                                  |
| `GET /api/archive/{gid}/{token}/quote`    | 归档报价（标题/画质/GP 价格，不扣 GP）                                                 |
| `POST /api/archive/{gid}/{token}/start`   | 购买并开始归档任务（消耗 GP；已购直接重下）                                            |
| `GET/DELETE /api/archive/{gid}/{token}`   | 单条状态 / 删除本地归档                                                                |
| `POST /api/archive/{gid}/{token}/refresh` | 重新下载（不扣 GP）                                                                    |

- **归档（Archiver）**：WebUI 的「归档」视图通过上述 API 管理 GP 购买的持久归档——输入图库 URL 查询报价 → 选画质确认 → 后台下载统一保存为 zip（cbz）母本于 `ARCHIVE_DIR`，`/stream` 对该图库直接读归档页（长效缓存，不受磁盘 LRU/7 天 TTL 约束）。需登录态与星会员；未解锁档消耗 GP，请确认后操作。
- 前端为单 HTML（内联 CSS/JS，无构建链、无 CDN 依赖），消费上述 JSON API；未来功能（离线项目管理、自动化工作流）扩展 API 层即可，页面契约不变。
- **安全**：`IPB_PASS_HASH` / `IGNEOUS` 永不回传明文（页面与 API 均只显示占位符）。`IPB_MEMBER_ID` 为登录标识，会完整展示。
- WebUI 不触达 E-Hentai，仅读取内存状态；服务配置异常时页面照常可访问并显示错误详情。
- **可选 Basic Auth**：设置 `AUTH_USERNAME` + `AUTH_PASSWORD` 后，除 `/health`（及 `AUTH_EXEMPT_PATHS` 指定路径、`AUTH_EXEMPT_PREFIXES` 路径前缀）外全部路由需 Basic 凭据，WebUI 同样受保护；未配置时保持默认公开（docker-compose 绑定 loopback / 反代控制访问）。Web 阅读器渲染评论封面等 `<img>` 原生加载场景建议 `AUTH_EXEMPT_PATHS=/image/fetch`（见下）。

## 环境变量

| 变量                                 | 默认                           | 说明                                                                                                                                                                              |
| ------------------------------------ | ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `IPB_MEMBER_ID`                    | 空                             | 登录 cookie（**可选**：不填则仅公开内容，Watched/Favorites 不可用）                                                                                                         |
| `IPB_PASS_HASH`                    | 空                             | 登录 cookie（与 IPB_MEMBER_ID 成对提供）                                                                                                                                          |
| `EH_SITE`                          | `e-hentai`                   | `e-hentai` \| `exhentai`                                                                                                                                                      |
| `IGNEOUS`                          | 空                             | 可选会话种子；exhentai 会话建立时自动下发，无需用户提供                                                                                                                           |
| `NW`                               | `1`                          | 绕过 Offensive For Everyone 警告                                                                                                                                                  |
| `DATATAGS`                         | `1`                          | 启用新缩略图结构                                                                                                                                                                  |
| `PUBLIC_BASE_URL`                  | 空                             | 设置后 feed 输出绝对 URL，如`https://opds.example.com`                                                                                                                          |
| `CACHE_DIR`                        | `./cache`                    | 图片磁盘缓存目录                                                                                                                                                                  |
| `CACHE_MAX_GB`                     | `4`                          | 磁盘缓存上限（GB）                                                                                                                                                                |
| `IMAGE_CACHE_ENABLED`              | `true`                       | 设为`false` 关闭磁盘缓存                                                                                                                                                        |
| `ARCHIVE_DIR`                      | `./archives`                 | 归档持久目录（zip 母本 + meta.json；建议独立卷）                                                                                                                                  |
| `ARCHIVE_QUALITY`                  | `original`                   | 默认画质档（start 未传 quality 时）                                                                                                                                               |
| `ARCHIVE_DOWNLOAD_CONCURRENCY`     | `5`                          | 归档下载/7z 转换并发上限，其余排队                                                                                                                                                |
| `HTML_INTERVAL_SECONDS`            | `0.3`                        | HTML 出站请求最小间隔（秒），防封关键；**compose/Docker 镜像推荐保守值 `1.5`**（单用户自用可安全尝试 `0.3`，在 .env 设置）                                              |
| `MAX_CONCURRENCY`                  | `5`                          | HTML/API 出站并发上限（防封关键路径）；**compose/Docker 镜像推荐保守值 `2`**（单用户自用可安全尝试 `5`）                                                                |
| `IMAGE_MAX_CONCURRENCY`            | `5`                          | 全图（`/stream` 原图）并发上限：509 配额流量，保守                                                                                                                              |
| `THUMB_MAX_CONCURRENCY`            | `25`                         | 封面图/缩略图（ehgt CDN）并发上限：对齐浏览器在源站 25 缩略图并发                                                                                                                 |
| `TIMEOUT_SECONDS`                  | `6`                          | 出站请求超时（秒）                                                                                                                                                                |
| `RETRIES`                          | `3`                          | 网络错误重试次数                                                                                                                                                                  |
| `BANNED_COOLDOWN_SECONDS`          | `1800`                       | IP 封禁熔断冷却（秒）                                                                                                                                                             |
| `EXCEED_COOLDOWN_SECONDS`          | `300`                        | 图片限额熔断冷却（秒）                                                                                                                                                            |
| `PSE_PAGE_BASE`                    | `1`                          | PSE 页码基数：`1`（1-based，默认）或 `0`（OPDS-PSE 规范原文 0-based）                                                                                                         |
| `TAG_STATUS_FILTER`                | `balanced`                   | 标签可信度过滤：`balanced`（默认，confidence+skepticism）、`strict`（仅 confidence）、`off`（全部保留）                                                                     |
| `TAG_TRANSLATION_ENABLED`          | `0`                          | 中文标签翻译（[EhTagTranslation/Database](https://github.com/EhTagTranslation/Database) 词典）：`1` 启用后 OPDS 2.0 subject 输出中文译名、搜索支持中文标签反查；关闭时零行为变化 |
| `TAG_TRANSLATION_URL`              | release latest`db.text.json` | 词典下载地址，可指向自镜像                                                                                                                                                        |
| `TAG_TRANSLATION_INTERVAL_SECONDS` | `86400`                      | 后台刷新间隔（秒）；`0` = 仅启动时拉取一次                                                                                                                                      |
| `TAG_TRANSLATION_STATE`            | `./tag_translation.json`     | 磁盘快照路径（删除后下次刷新重新下载）                                                                                                                                            |
| `FACETS`                           | 内置 10 分类                   | OPDS 2.0 分类筛选，格式`名称:掩码,名称:掩码`                                                                                                                                    |
| `EH_PROFILE`                       | `PandaOPDS`                  | E-Hentai uconfig 独立 profile 名；设空串关闭                                                                                                                                      |
| `HOME_CONFIG`                      | `./config/home.toml`         | OPDS 2.0 首页布局配置文件路径                                                                                                                                                     |
| `AUTH_USERNAME`                    | 空                             | 可选 Basic Auth 用户名；与`AUTH_PASSWORD` **同时设置**才启用                                                                                                              |
| `AUTH_PASSWORD`                    | 空                             | 可选 Basic Auth 密码（明文，脱敏显示；仅建议 HTTPS 反代下启用）                                                                                                                   |
| `AUTH_EXEMPT_PATHS`                | 空                             | 逗号分隔的精确路径，认证下仍公开；`/health` 恒豁免                                                                                                                              |
| `AUTH_EXEMPT_PREFIXES`             | 空                             | 逗号分隔的路径**前缀**，命中即公开；浏览器 `<img>`/`url()` 无法携带认证头，评论封面代理建议 `AUTH_EXEMPT_PATHS=/image/fetch`                                          |
| `IMAGE_PROXY_HOSTS`                | `ehgt.org,s.exhentai.org`    | 评论封面/预览图代理（`/image/fetch`）host 白名单，逗号分隔、强制 https；新封面 host 加此免发版                                                                                  |
| `LOG_LEVEL`                        | `INFO`                       | `INFO` \| `DEBUG`（DEBUG 输出每次出站请求，用于排障）                                                                                                                         |

完整路由与客户端接入细节见 [AGENTS.md](AGENTS.md)。

## 注意

- 本服务是服务器（多客户端、单 IP 集中请求），比个人客户端更易触发 E-Hentai 封禁。请保持节流参数默认值、善用缓存。
- 图片限额触发返回 429；IP 被封 / 超限触发全局熔断并返回 503，冷却后自动恢复。
- **启用 Basic Auth 时必须位于 HTTPS 反代之后**：Basic 凭据仅 base64 编码（非加密），明文传输即泄露。
- 仅限个人使用，请遵守 E-Hentai 服务条款。

## 许可证与致谢

本项目采用 **Apache License 2.0**（见 `LICENSE`）。

E-Hentai 抓取与解析实现（`app/eh/`）参考了 [JHenTai](https://github.com/jiangtian616/JHenTai)（Apache License 2.0，Copyright JHenTai contributors）：HTML 选择器、页面 URL 约定、会话/cookie 处理与上游异常检测机制均以 JHenTai 为参照，代码为本项目的 Python 独立重写。依据 Apache 2.0 §4，本仓库保留其归属声明（见 `THIRD_PARTY_LICENSES`）。

中文标签翻译数据来自 [EhTagTranslation/Database](https://github.com/EhTagTranslation/Database) 项目（**CC BY-NC-SA 3.0 中国大陆** 许可证，© EhTagTranslation 贡献者）：启用 `TAG_TRANSLATION_ENABLED` 后，OPDS 目录中的标签译名与搜索反查均基于该社区词典。词典仅运行时拉取使用，不随本仓库分发。该数据为非商业性许可，本项目亦仅限个人非商业使用；详见 `THIRD_PARTY_LICENSES`。
