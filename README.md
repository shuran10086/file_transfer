# 文件收发工具（file_transfer）

让 AstrBot 在 QQ 会话里**收发文件**、**自动保存收到的文件**、**查看内容**，并提供可操作的 WebUI 管理面板。工作区复用 AstrBot 会话工作区：私聊独立、群聊共享；AI 任务脚本与产物也可正常使用。

支持 **NapCat / aiocqhttp** 与 **QQ 官方机器人（qq_official / qq_official_webhook）**。

> 当前版本：**1.6.0**　|　AstrBot：**>= 4.0.0**（WebUI 页面需支持插件可视化视图）　|　分类：工具

## 开发者信息

- 作者：**shuran**
- 联系邮箱：**2556246092@qq.com**
- 适配平台：aiocqhttp（NapCat）、qq_official、qq_official_webhook
- 插件市场：AstrBot Cloud（分类：工具）
- 随插件 Skill：`skills/file-transfer/SKILL.md`

## 功能特性

- **WebUI 管理面板**：概览、文件管理、审计日志、配额与限速、运行与配置 5 个子页面；支持上传/重命名/删除/清理文件、清空审计、在线修改全部配置。WebUI 上传使用分片 JSON，避开 multipart/反向代理 body 限制导致的 502。
- **SQLite 索引**：上传索引从 JSON 升级为 SQLite；旧索引自动迁移并备份。
- **哈希去重**：接收后计算 SHA256，同一工作区已有相同内容时跳过保存。
- **操作审计**：接收/发送/上传/重命名/删除/限速/清理/查看/下载/配置修改事件可筛选、分页、按天保留。
- **传输可靠性**：并发上限、下载重试、传输超时、磁盘保留空间检查。
- **查看增强**：UTF-8 / UTF-16 / GB18030 识别、分页查看、图片预览、文件元信息。
- **配额与限速**：上传数量/存储配额、发送/接收频率限制、WebUI 使用率展示。
- **双平台发送**：NapCat 用 URL/路径映射；QQ 官方机器人用官方富媒体上传。
- **安全接收**：文件名清洗、后缀黑名单、大小/配额限制、临时文件原子落盘。
- **定时清理**：默认开启，清理 QQ 工作区超期文件（包括 AI 脚本与产物）。

## 快速开始

1. 将插件目录放入 AstrBot 的 `data/plugins/`，在 WebUI 启用并重载。
2. 打开插件详情页 →「页面」，进入 **文件概览 / 文件管理 / 审计日志 / 配额与限速 / 运行与配置**。
3. 按平台配置：
   - **NapCat**：填「发送设置 → AstrBot 文件服务地址」（NapCat 能访问的 dashboard 地址）；共享目录时可用路径映射。
   - **qq_official**：无需 `callback_api_base` / 路径映射，直接使用官方富媒体上传。
4. 私聊发送文件测试自动保存；用 `/文件列表` 查看。
5. 按需配置白名单、审计保留天数与发送/接收频率。

## WebUI 页面

页面顶部提供「概览 / 文件管理 / 审计日志 / 配额与限速 / 运行与配置」标签栏，主页面可直接切换全部功能；侧边栏也可单独打开任一页面。

- **文件概览**：工作区、文件总数、上传配额、24h 审计事件、最近事件、工作区排行。
- **文件管理**：按工作区浏览、搜索、查看大小/来源/SHA256、文本预览、下载，以及上传、重命名、删除、清理过期文件、清空工作区。
- **审计日志**：按事件/级别/工作区/关键词筛选，分页查看操作记录；支持清空审计。
- **配额与限速**：工作区上传数量/存储使用率、频率限制、传输并发/重试/超时。
- **运行与配置**：版本、平台、工作区与 SQLite 路径；可编辑全部可见配置并保存/恢复默认。

> WebUI 为**管理面板**：文件、审计、配置均可操作；仅 AstrBot 管理员可访问 Dashboard，请妥善保管管理入口。发送到 QQ 会话仍使用 `/发文件` 指令。

## 使用说明

### 工作区

- 私聊：`<AstrBot数据目录>/workspaces/<平台>_FriendMessage_<用户ID>/`
- 群聊：`<AstrBot数据目录>/workspaces/<平台>_GroupMessage_<群ID>/`，群成员共享。
- 收到的文件与 AI 任务文件同目录；重名自动追加 `_1`/`_2`，不覆盖已有文件。
- NapCat 的 ID 为 QQ 号/群号；qq_official 为 openid / group_openid。

### 发送文件

- **NapCat**：URL 模式（`callback_api_base` + 一次性 token URL）优先；失败回退路径映射（`napcat_path_mapping`，格式 `A:B` 或 `A=>B`）。
- **qq_official**：`File(name, file=本地路径)` 交给官方适配器上传；支持群聊与 C2C 单聊，**频道消息不支持文件**；平台硬限制 200MB。
- 权限：管理员可发送任意可读文件；普通用户仅当前工作区或 `allowed_send_dirs`。
- 频率：默认每用户/会话每分钟 10 次，可在「配额与限速」调整。

### 接收文件

- 直接发送的文件自动保存；引用消息文件默认不保存（`auto_save_quoted`）。
- 接收前检查后缀黑名单、单文件大小、上传配额、磁盘空间与接收频率。
- 下载失败自动重试（默认 2 次）；临时文件 + 原子改名，失败不留残文件。
- 默认哈希去重：同工作区已有相同 SHA256 时提示重复并跳过。
- 保存成功只在私聊提示；异常提示群聊也会发送。

### 查看文件

- `/查看文件`：最新文件第 1 页。
- `/查看文件 <序号|文件名> [页码]`：分页查看，页码最多 500。
- 自动识别 UTF-8 / UTF-16 / GB18030；图片在大小限制内直接预览；二进制显示元信息与 SHA256。
- WebUI「文件管理」支持文本预览、下载、上传、重命名、删除、清理过期文件与清空工作区。

### 审计 / 配额 / 清理

- 审计默认开启、保留 30 天；记录接收、发送、删除、限速、清理、查看、WebUI 下载等事件。
- 上传配额统计经插件接收或 WebUI 上传的文件：`max_file_count`、`max_storage_mb`，0=不限；AI 脚本/产物不计入。
- 发送/接收频率：`send_rate_per_minute`、`receive_rate_per_minute`，0=不限。
- 定时清理默认开启（保留 1 小时），清理 QQ 用户/群工作区超期普通文件（含 AI 脚本/产物）；跳过 `EXTRA_PROMPT.md`、隐藏文件、目录、符号链接。

## 平台与兼容性

- **aiocqhttp/NapCat**：完整支持；`callback_api_base` 指向 dashboard（默认 6185），跨容器/跨机不能写 `127.0.0.1`。
- **qq_official**：群聊/C2C 支持文件，频道不支持；官方文件类型 4、硬限制 200MB、支持分片上传；需要开放平台文件/富媒体权限；白名单填 openid。
- **版本**：AstrBot `>=4.0.0`，验证基于 4.28.1；WebUI 页面需 AstrBot 插件可视化视图；建议 qq_official 使用 4.27.3+。
- 参考：[AstrBot QQ 官方接入](https://docs.astrbot.app/platform/qqofficial.html)、[QQ 富媒体文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/rich-media.html)。

## 常见问题

- **WebUI 没有页面**：确认 AstrBot 支持插件可视化视图；重载插件并刷新详情页；检查 `pages/<页面>/index.html`。
- **WebUI 上传 502**：v1.6.0 已改为分片上传（每片 256KB）；请重载插件并强制刷新页面。若仍 502，检查反向代理的请求 body 限制、超时与 WebSocket/HTTP 转发配置。
- **审计没有数据**：确认「存储与审计 → 启用操作审计」；升级后只记录新事件。
- **去重误判**：只比较同一工作区 SHA256；可在「存储与审计」关闭。
- **只能看第一页**：用 `/查看文件 <序号|文件名> 2` 继续；页码上限 500。
- **NapCat 发送失败**：确认 `callback_api_base` 指向 dashboard、NapCat 能访问；token 单次有效。
- **路径映射失败**：确认共享目录与映射（Windows 可用 `D:/astrbot:/napcat` 或 `D:/astrbot=>/napcat`）。
- **qq_official 发文件失败**：确认是群聊/单聊而非频道、机器人有文件权限、文件 ≤200MB。
- **文件/脚本消失**：定时清理默认开启；调大保留时长或关闭 `workspace_auto_clean`。
- **旧索引**：升级 1.6.0 自动导入 SQLite，原 `received_index.json` 改名为 `.migrated` 备份。

## 附录 A：指令列表

| 指令 | 别名 | 权限 | 说明 |
| --- | --- | --- | --- |
| `/发文件 <序号\|路径>` | `sendfile` | 普通用户：工作区/配置目录；管理员：任意可读文件 | 按序号或路径发送 |
| `/查看文件 [序号\|文件名] [页码]` | `catfile` | 通用 | 查看内容，支持分页与图片预览 |
| `/文件列表` | `filelist` | 通用 | 当前工作区文件（群聊共享） |
| `/文件列表 <QQ号/用户ID>` | `filelist` | 管理员 | 查询指定用户私聊工作区 |
| `/删除文件 <序号\|文件名>` | `delfile` | 通用 | 删除文件（受保护文件除外） |

> 序号按修改时间升序，`1` 最旧；`/查看文件`、`/发文件`、`/删除文件` 与 `/文件列表` 同一排序。列表只显示最近 15 个，但序号按全部文件计算。

## 附录 B：配置项

**发送设置**：`callback_api_base`（默认 `http://127.0.0.1:6185`，NapCat 用）、`napcat_path_mapping`（空）、`max_send_mb`（100）

**接收与查看**：`auto_save`（true）、`auto_save_quoted`（false）、`suffix_filter_enable`（true）、`blocked_suffixes`（常见可执行/脚本后缀）、`max_single_file_mb`（50）、`max_view_chars`（1500）、`view_image_preview`（true）、`max_view_image_mb`（10）

**配额与限速**：`max_file_count`（10）、`max_storage_mb`（10）、`send_rate_per_minute`（10）、`receive_rate_per_minute`（20）

**传输与可靠性**：`max_concurrent_transfers`（2）、`download_retry`（2）、`transfer_timeout_seconds`（1800）、`min_free_disk_mb`（200）

**存储与审计**：`dedupe_enabled`（true）、`audit_enabled`（true）、`audit_retention_days`（30）

**权限与白名单**：`private_only`（false）、`whitelist_private_enable`（false）、`whitelist_users`（空）、`whitelist_group_enable`（false）、`whitelist_groups`（空）、`allowed_send_dirs`（空）

**工作区清理**：`workspace_auto_clean`（true）、`workspace_clean_interval_hours`（1）

## 附录 C：数据路径

| 内容 | 路径 |
| --- | --- |
| 私聊工作区 | `<AstrBot数据目录>/workspaces/<平台>_FriendMessage_<用户ID>/` |
| 群聊工作区 | `<AstrBot数据目录>/workspaces/<平台>_GroupMessage_<群ID>/` |
| SQLite（索引+审计） | `<AstrBot数据目录>/plugin_data/file_transfer/file_transfer.db` |
| 旧索引备份 | `<AstrBot数据目录>/plugin_data/file_transfer/received_index.json.migrated` |
| 插件配置 | `<AstrBot数据目录>/config/<插件目录名>_config.json` |
| Skill | `<插件目录>/skills/file-transfer/SKILL.md` |

## 更新日志

- **1.6.0**
  - 新增可操作 WebUI（概览/文件/审计/配额/运行与配置 5 页）：文件上传/重命名/删除/清理、清空审计、在线配置保存/重置。
  - JSON 索引升级 SQLite，旧索引自动迁移；SHA256 哈希去重。
  - 操作审计（接收/发送/删除/限速/清理/查看/下载），支持筛选、分页、保留天数。
  - 大文件可靠性：并发上限、下载重试、传输超时、磁盘保留空间。
  - 查看增强：编码识别、分页、图片预览、二进制元信息。
  - 新增发送/接收频率限制与配额使用率展示；配置新增「传输与可靠性」「存储与审计」分组。
- **1.5.3**：适配 QQ 官方机器人；私聊/群聊白名单拆分；配置分组与 README 重构。
- **1.5.2**：`/发文件` 支持序号；版本要求校正为 `>=4.0.0`。
- **1.5.1**：`/删除文件` 支持序号。
- **1.5.0**：工作区恢复 AstrBot 会话工作区；配额只统计上传文件；清理覆盖任务脚本。
- **1.3.3**：初版。
