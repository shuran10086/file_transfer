# 文件收发工具（file_transfer）

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：让机器人能在 QQ 会话里**收发文件**、**自动保存收到的文件**并**查看内容**。工作区直接复用 AstrBot 会话工作区——私聊按 QQ 号独立、群聊按群号共享，AI 完成任务时写进工作区的脚本和产物也可以正常使用。

> 当前版本：**1.5.2**（支持 AstrBot >= 4.0.0（4.x），已在 4.28.1 上实测；版本要求已按 AstrBot 源码接口核对校正）。

## 功能

- **私聊工作区按 QQ 号隔离**：`data/workspaces/<平台>_FriendMessage_<QQ号>/`，同一用户的所有私聊文件共用一个工作区。
- **群聊共享工作区**：`data/workspaces/<平台>_GroupMessage_<群号>/`，同群成员共同使用；只要在对应群且有权限，成员都可以查看 / 删除 / 发送该工作区里的文件。
- **工作区即 AI 任务目录**：工作区就是 AstrBot 的会话工作区（AI 任务 cwd），收到的文件可直接被任务使用；AI 为完成任务编写的脚本、产物也存放在这里。
- **自动接收**：监听消息里的文件段，自动下载保存；保存成功的提示只在私聊发送，群聊不刷屏；被过滤/超限/下载失败等异常提示在当前会话发送（含群聊）。
- **安全接收**：文件名自动清洗（去路径、去非法字符、去尾部空格/点、限长、避开 Windows 保留名），后缀黑名单过滤，单文件大小与上传配额限制；下载经过临时文件 + 原子落盘，失败不留残文件；同一 URL 10 分钟内只保存一次，引用消息中的文件默认不保存；不会覆盖 `EXTRA_PROMPT.md` 等受保护文件。
- **发送本地文件**：`/发文件` 支持按工作区序号发送，或按路径发送（URL 模式：AstrBot 文件服务 + NapCat 下载；路径映射模式：NapCat 与 AstrBot 共享目录）。
- **发送权限**：
  - **AstrBot 管理员**：可发送任意可读文件（包括 AstrBot 数据目录、系统文件等），请只授予可信人员；
  - **普通用户**：只能发送当前工作区或管理员配置目录（`allowed_send_dirs`）中的文件。
- **配额只统计上传文件**：数量 / 容量上限只统计**经插件接收的文件**，AI 任务脚本、程序产物、用户手动放入的文件不计入；群聊配额 = 群内所有成员上传文件的总和。
- **定时清理**：默认开启，定时清理所有 QQ 用户 / 群聊工作区中的超期文件（**包含 AI 编写的任务脚本和产物**），保留时长默认 1 小时；跳过 `EXTRA_PROMPT.md`、隐藏文件、目录与符号链接。
- **其他**：仅私聊收发、群号白名单、QQ 白名单、限长查看文件、管理员跨 QQ 查看工作区列表。

## 指令

| 指令 | 别名 | 权限 | 说明 |
| --- | --- | --- | --- |
| `/发文件 <序号\|路径>` | `sendfile` | 普通用户：当前工作区/配置目录；管理员：任意可读文件 | 按工作区序号或本地路径把文件发送到当前会话 |
| `/查看文件 [序号\|文件名]` | `catfile` | 通用 | 查看当前工作区最近/指定文件内容 |
| `/文件列表` | `filelist` | 通用 | 列出当前工作区文件（群聊为群共享工作区） |
| `/文件列表 <QQ号>` | `filelist` | 管理员 | 查看指定 QQ 号私聊工作区的文件清单（只读，不创建） |
| `/删除文件 <序号\|文件名>` | `delfile` | 通用 | 按序号或文件名删除当前工作区里的文件（受保护文件除外） |

> `/查看文件 <序号>`、`/发文件 <序号>`、`/删除文件 <序号>` 与 `/文件列表` 使用同一排序（按修改时间升序，序号 1 为最旧），列表只显示最近 15 个文件。工作区里的 AI 任务脚本、产物同样会出现在列表与查看结果中。

## 工作区说明

| 场景 | 工作区目录 | 说明 |
| --- | --- | --- |
| 私聊 | `data/workspaces/<平台>_FriendMessage_<QQ号>/` | 按 QQ 号独立，只有本人使用 |
| QQ 群聊 | `data/workspaces/<平台>_GroupMessage_<群号>/` | 群成员共享，可互相查看 / 删除 / 发送 |
| 其他平台 | 不做适配 | 以 AstrBot 自身会话工作区为准，行为不作保证 |

- 工作区目录由 `unified_msg_origin` 确定性生成，不依赖目录扫描，匹配稳定。
- 收到的文件写入工作区根目录，与 AI 任务文件同目录；重名时自动追加 `_1`、`_2` 后缀，不覆盖已有文件。
- 插件在 `data/plugin_data/file_transfer/received_index.json` 维护“上传文件”索引，用于数量 / 容量统计；索引随文件删除、清理自动同步。
- 1.3.3 版本的文件本来就在会话工作区中，升级后直接可见；1.4.0（未发布中间版）写入插件数据目录的文件不会自动迁移，如需保留请手动移入对应工作区。

## 配置项

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `callback_api_base` | string | `http://127.0.0.1:6185` | URL 模式的 NapCat 访问地址（AstrBot 文件服务地址）。留空则只用路径映射模式 |
| `napcat_path_mapping` | string | 空 | 路径映射，格式 `AstrBot路径:NapCat路径`（如 `/test:/napcat/test`）；Windows 可写 `D:/astrbot:/napcat`；也可用 `=>` 分隔 |
| `auto_save` | bool | `true` | 是否自动保存收到的文件到当前会话工作区 |
| `auto_save_quoted` | bool | `false` | 是否保存引用消息中的文件（默认关闭，避免重复下载） |
| `max_view_chars` | int | `1500` | 查看文件时最多显示的字符数 |
| `private_only` | bool | `false` | 开启后仅私聊可以收发，群聊一律拒绝 |
| `whitelist_enable` | bool | `false` | 开启后仅白名单内的群 / 用户可用 |
| `whitelist_groups` | list | 空 | 群号白名单 |
| `whitelist_users` | list | 空 | QQ 号白名单 |
| `suffix_filter_enable` | bool | `true` | 是否开启接收文件后缀过滤 |
| `blocked_suffixes` | list | `.exe/.js/.ps1` 等 | 拦截后缀黑名单（命中不接收） |
| `max_file_count` | int | `10` | 每个工作区**上传文件**数量上限（AI 脚本不计），群聊按群成员上传总和；`0`=不限制 |
| `max_storage_mb` | int | `10` | 每个工作区**上传文件**总大小上限（MB），`0`=不限制 |
| `max_single_file_mb` | int | `50` | 单个接收文件大小上限（MB），`0`=不限制 |
| `max_send_mb` | int | `100` | `/发文件` 发送文件大小上限（MB），`0`=不限制 |
| `allowed_send_dirs` | list | 空 | 允许**普通用户**发送的额外目录（管理员不受限） |
| `workspace_auto_clean` | bool | `true` | 是否定时清理所有 QQ 用户 / 群聊工作区的超期文件（含任务脚本） |
| `workspace_clean_interval_hours` | int | `1` | 工作区文件保留时长（小时） |

## 权限与清理规则

**发送判定**

1. 发送者是 AstrBot 管理员（`admins_id` / `role=admin`）→ 允许发送任意可读文件；
2. 否则，文件位于当前工作区内 → 允许；
3. 否则，文件位于 `allowed_send_dirs` 内 → 允许；
4. 否则 → 拒绝。

**接收/查看/删除/列表**

- 私聊：只作用于本人 QQ 工作区；
- 群聊：作群共享工作区，群内成员只要通过 `private_only` / 白名单校验，就可以查看、删除、发送；
- 受保护文件 `EXTRA_PROMPT.md`：不可接收（会被改名为 `recv_EXTRA_PROMPT.md`）、不可删除、不被清理。

**定时清理**

- 默认开启，每 5 分钟检查一次，达到保留时长（默认 1 小时）即删除；
- 清理范围：`data/workspaces/` 下目录名包含 `_FriendMessage_` 或 `_GroupMessage_` 的工作区（QQ 用户 / 群聊），只处理每个目录第一层的普通文件；
- **会删除 AI 为完成任务编写的脚本、产物等文件**（这正是本插件工作区生命周期管理的目的），请按需调整保留时长；
- 跳过 `EXTRA_PROMPT.md`、以 `.` 开头的隐藏文件、目录与符号链接；清理后自动同步上传索引。

> ⚠️ 清理默认开启且会删除工作区内的任务脚本/产物。若工作区中有需要长期保留的文件，请调大 `workspace_clean_interval_hours` 或关闭 `workspace_auto_clean`。

## 安全说明

- 管理员任意文件权限是明确的信任边界：只把 AstrBot 管理员授予可信人员。
- 普通用户默认只能操作自己的工作区；群聊工作区对群成员共享，请配合 `whitelist_enable` / `private_only` 控制可用群。
- 接收的文件会进入 AI 会话工作区，AI 任务可以直接读取/处理；文件内容对 AI 仍是不可信数据，请勿给不可信群开放文件功能。
- 文件名会被清洗，不会覆盖工作区控制文件；下载失败/超限不会留下残文件。

## 发送文件的两种模式

- **URL 模式（推荐）**：配置 `callback_api_base` 为 NapCat 能访问到的 AstrBot 地址。发送时插件通过 AstrBot 文件服务生成一次性下载链接，NapCat 自行下载。同一容器内可填 `http://127.0.0.1:6185`；NapCat 在宿主机或另一容器时填对应 IP / 域名。
- **路径映射模式**：配置 `napcat_path_mapping`，把 AstrBot 侧路径映射成 NapCat 侧路径（要求两者共享同一目录）。未配置映射时会把文件的本地绝对路径直接交给 NapCat（仅适用于同一宿主机/容器的场景）。

URL 模式发送失败时会自动尝试路径映射模式；两种模式都失败时会给出通用错误提示，详细原因请查看 AstrBot 日志。

## 升级说明（1.3.3 → 1.5.2）

1. **工作区沿用 AstrBot 会话工作区**：私聊 `<平台>_FriendMessage_<QQ>`、群聊 `<平台>_GroupMessage_<群号>`；1.3.3 的文件升级后直接可见。
2. **群聊工作区共享**：同群成员可共同查看 / 删除 / 发送；保存成功不刷屏回执，异常（过滤/超限/失败）会提示。
3. **配额只统计上传文件**：AI 任务脚本、产物不计入数量 / 容量上限；群聊为群内所有成员上传总和。
4. **管理员任意文件权限**：普通用户仍限制在当前工作区 / `allowed_send_dirs`。
5. **清理默认开启**：默认保留 1 小时，会清理工作区内的任务脚本与产物；如不需要请关闭 `workspace_auto_clean` 或调大保留时长。
6. **配置变更**：删除 `auto_clean`、`clean_interval_hours`、`send_restrict_paths`；新增 `auto_save_quoted`、`max_single_file_mb`、`max_send_mb`；`max_file_count` / `max_storage_mb` 语义改为只统计上传文件。
7. **1.5.1**：`/删除文件` 新增按序号删除，与 `/文件列表`、`/查看文件 <序号>` 使用同一排序（修改时间升序，1 为最旧）。
8. **1.5.2**：`/发文件` 新增按序号发送；`/查看文件`、`/发文件`、`/删除文件` 的序号与 `/文件列表` 完全一致。同时按 AstrBot 源码接口核对，`metadata.yaml` 的 `astrbot_version` 由 `>=4.5,<5` 校正为 `>=4.0.0`（所用接口自 v4.0.0 起均存在）；并新增 `category: 工具` 与 `tags`，将 AstrBot Cloud 插件市场分类指定为「工具」。

## 依赖与兼容性

- AstrBot `>=4.0.0`（4.x）。已按 AstrBot 源码逐项核对：`File(name, file, url)` 与出站 `File.to_dict()`、`Reply.chain`、`StarTools.get_data_dir()`、`filter.command(alias=...)` / `EventMessageType.ALL`、`event.is_admin()/chain_result()/send()/get_group_id()/get_sender_id()/get_platform_id()`、`file_token_service.register_file(path, timeout=...)` 与 `/api/file/<token>` 路由、`_conf_schema.json` + `AstrBotConfig(schema=...)` 默认值合并、插件加载器传入 `config` 等接口在 v4.0.0 即已存在；`event.unified_msg_origin` 在旧版缺失时插件会用平台 ID / 群号 / QQ 号自行拼装兜底。
- 实际运行验证基于 AstrBot 4.28.1；AstrBot 5.x 尚未发布，按官方插件发布文档示例只声明最低版本、不设上限。
- 主要面向 **aiocqhttp / NapCat**（OneBot v11）；其他平台不做适配。
- 兼容旧 AstrBot 时读取管理员名单会回退读取 `data/cmd_config.json` 的 `admins_id`，不写死任何 QQ。

## 插件市场元数据（category / tags）

`metadata.yaml` 中额外声明了插件市场分类与标签：

```yaml
category: 工具
tags:
  - 工具
  - 文件
  - 文件传输
  - 文件管理
  - QQ
```

- `category` / `tags` 供 **AstrBot Cloud 插件市场**做分类与搜索使用；AstrBot 运行时加载器只读取已知字段，这两个字段不会影响本地插件加载。
- 已核对市场接口：发布到 AstrBot Cloud 的插件，其 `metadata.yaml` 的 `category` 字段会被市场收录（部分插件靠审核端手动设置分类）。本插件目标分类为 **工具**。
- 本地 WebUI 插件详情若显示 `[MISSING: features.extension.market.categories.其他]` 之类的文本，是 AstrBot WebUI 缺少对应中文分类的 i18n 标签导致的显示问题（分类值来自云端为中文）；在云端把分类改为「工具」后，市场分类筛选会变更，但该 MISSING 标签是否消失取决于所用 AstrBot WebUI 版本是否提供对应翻译，与本插件代码无关。

## 常见问题

- **`/发文件` 提示“该文件不在允许发送的目录内”**：普通用户只能发送当前工作区或 `allowed_send_dirs` 中的文件；管理员不受限制。
- **想发工作区里的第 N 个文件**：先 `/文件列表` 看序号，再用 `/发文件 N` 发送（与 `/查看文件 N`、`/删除文件 N` 序号一致）。
- **群聊里看不到别人发的文件**：请确认没有开启 `private_only`，且该群通过白名单校验；群聊工作区是共享的。
- **工作区里的脚本怎么没了**：定时清理默认开启，会删除超过保留时长（默认 1 小时）的文件；请调大 `workspace_clean_interval_hours` 或关闭 `workspace_auto_clean`。
- **上传提示“工作区上传文件数量已达上限”**：只统计经插件接收的文件（默认 10 个、10MB），AI 脚本不占配额；可调大 `max_file_count` / `max_storage_mb` 或删除旧上传文件。
- **URL 模式发不出去**：确认 `callback_api_base` 是 NapCat 能访问的地址（容器部署不要用 `127.0.0.1` 指代另一个容器）。
- **路径映射模式发不出去**：确认 NapCat 与 AstrBot 共享了同一目录，且映射正确（Windows 盘符路径可用 `D:/astrbot:/napcat` 或 `D:/astrbot=>/napcat`）。

## 作者

- 名字：shuran
- QQ：2556246092
