# 文件收发工具（file_transfer）

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：让机器人能在 QQ 会话里**收发文件**、**自动保存收到的文件**并**查看内容**，支持权限控制与自动清理。

## 功能

- **发送本地文件**：把机器人本机的文件发到当前私聊 / 群聊
- **自动接收**：静默监听消息里的文件段，自动下载保存到插件数据目录
- **查看内容**：查看最近收到的文件，或任意本地文本文件（自动识别文本 / 二进制，超长自动截断）
- **文件列表 / 删除**：列出最近收到的文件，按文件名删除
- **权限控制**：可限制仅私聊收发，或用群号 / QQ 白名单
- **自动清理**：按设定间隔，自动删除接收目录里超期的文件

## 指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/发文件 <路径>` | `sendfile` | 把本地文件发送到当前会话 |
| `/查看文件 [序号\|路径]` | `catfile` | 查看最近收到的文件（按序号）或任意本地文本文件（按路径） |
| `/文件列表` | `filelist` | 列出最近收到的文件 |
| `/删除文件 <文件名>` | `delfile` | 删除接收目录里已收到的文件 |

## 配置项

| 配置 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `callback_api_base` | string | `http://0.0.0.0:6185` | URL 模式发送文件用。填 NapCat 可访问的地址（含 `/api/file/{token}` 路由）。留空则改用路径映射模式 |
| `napcat_path_mapping` | string | 空 | 路径映射规则，格式 `AstrBot路径:NapCat路径`，如 `/test:/napcat/test`。仅当两者共享同一目录时使用 |
| `auto_save` | bool | `true` | 是否自动保存收到的文件 |
| `max_view_chars` | int | `1500` | 查看文件时最多显示的字符数 |
| `private_only` | bool | `false` | 开启后只有私聊可以收发文件，群聊一律拒绝 |
| `whitelist_enable` | bool | `false` | 开启后仅收发白名单内的群和用户 |
| `whitelist_groups` | list | 空 | 群号白名单，每行一个 |
| `whitelist_users` | list | 空 | QQ 号白名单，每行一个 |
| `auto_clean` | bool | `false` | 是否自动清理接收目录里超期的文件 |
| `clean_interval_hours` | int | `1` | 文件保存超过该时长（小时）后被自动删除 |

## 安装

1. 将本插件目录放入 AstrBot 的 `data/plugins/` 下
2. 在 AstrBot 管理面板启用插件
3. 按需在插件配置里填写 `callback_api_base` 或 `napcat_path_mapping`

## 说明

- 发送文件支持两种模式：**URL 模式**（NapCat 自行下载，需配置 `callback_api_base`）与**路径映射模式**（直接传本地路径，需配置 `napcat_path_mapping`）
- 接收到的文件保存在插件数据目录的 `received/` 下，记录持久化在 `received_records.json`

## 作者

- 名字：shuran
- 邮箱：2556246092@qq.com
