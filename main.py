"""文件收发工具 v1.6.0

功能：
1. /发文件 <路径>           把本地文件发送到当前会话（URL 模式 / 路径映射模式）
2. 自动接收                 静默监听消息中的文件段，自动下载保存到当前会话工作区
3. /查看文件 [序号|文件名] [页码]  查看工作区文件（编码识别/分页/图片预览）
4. /文件列表 [QQ号]         查询当前工作区文件；管理员可指定 QQ 号查询
5. /删除文件 <文件名>       删除工作区里的文件
6. 权限控制                 仅私聊收发 / 群号与 QQ 白名单；普通用户仅能发送工作区与配置目录内的文件
7. 定时清理                 默认开启，清理所有 QQ 用户/群聊工作区中的超期文件（含任务脚本）
8. 后缀过滤                 默认拦截可执行 / 脚本文件，可自定义后缀黑名单
9. 容量限制                 只统计经插件接收的文件：数量 / 总存储 / 单文件 / 发送大小，0 表示不限制
10. 工作区                 直接使用 AstrBot 会话工作区：私聊按 QQ 号、群聊按群号，群成员共享群工作区
11. WebUI                 概览 / 文件管理 / 审计日志 / 配额与限速 / 运行与配置 5 个管理页面（文件/审计/配置可操作）
12. SQLite + 审计         上传索引、SHA256 去重、操作审计（接收/发送/删除/限速/清理/查看）
13. 传输可靠性             并发上限、下载重试、传输超时、磁盘保留空间；发送/接收限速

说明：
- AstrBot 管理员可以发送任意可读文件（含 AstrBot 数据目录、系统文件等），请只授予可信人员；
- 普通用户只能发送本人工作区或管理员配置目录内的文件；
- 工作区中的 AI 任务脚本等文件不计入数量/容量上限，只会被定时清理；
- 接收文件名经过清洗，且不会覆盖 EXTRA_PROMPT.md 等受保护文件；
- 接收成功提示只在私聊发送；被过滤/超限/下载失败等提示在群聊也会发送。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

PLUGIN_VERSION = "1.6.0"
DEFAULT_MAX_VIEW_CHARS = 1500
TEXT_SNIFF_BYTES = 4096
URL_DEDUP_TTL = 600
CLEAN_CHECK_INTERVAL = 60
MAX_FILENAME_LEN = 120
INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# 不接收、不可删除、不被清理的工作区控制文件
PROTECTED_WORKSPACE_FILES = {"extra_prompt.md"}
QQ_WORKSPACE_MARKERS = ("_FriendMessage_", "_GroupMessage_")
QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
PLUGIN_NAME = "file_transfer"
DB_FILE_NAME = "file_transfer.db"
MAX_VIEW_PAGE = 500
RATE_WINDOW_SECONDS = 60
VIEW_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
AUDIT_TEXT_LIMIT = 500
QQ_OFFICIAL_FILE_HARD_LIMIT_MB = 200
QQ_OFFICIAL_FILE_RAW_TYPES = {"GroupMessage", "C2CMessage"}
WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class FileTransferDB:
    """SQLite 存储：上传文件索引 + 操作审计。

    连接按操作短生命周期创建，启用 WAL，适合插件这种低频写入场景。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    workspace TEXT NOT NULL,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT,
                    source TEXT NOT NULL DEFAULT 'received',
                    uploader TEXT,
                    mtime REAL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (workspace, name)
                );
                CREATE INDEX IF NOT EXISTS idx_files_workspace ON files(workspace);
                CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    event TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    workspace TEXT,
                    user_id TEXT,
                    group_id TEXT,
                    platform TEXT,
                    filename TEXT,
                    size INTEGER,
                    sha256 TEXT,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit(created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_event ON audit(event);
                CREATE INDEX IF NOT EXISTS idx_audit_workspace ON audit(workspace);
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    # ---------------- 文件索引 ----------------

    def migrate_legacy(self, legacy_path: Path, workspaces_root: Path) -> int:
        """把旧版 received_index.json 导入 SQLite；成功后改名保留备份。"""
        legacy_path = Path(legacy_path)
        if not legacy_path.is_file():
            return 0
        with self._conn() as conn:
            imported = conn.execute(
                "SELECT value FROM meta WHERE key='legacy_index_imported'"
            ).fetchone()
            if imported or conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0:
                return 0
            try:
                data = json.loads(legacy_path.read_text(encoding="utf-8-sig"))
            except Exception:  # noqa: BLE001
                return 0
            if not isinstance(data, dict):
                return 0
            count = 0
            for workspace, entries in data.items():
                if not isinstance(entries, dict):
                    continue
                for name, info in entries.items():
                    path = Path(workspaces_root) / str(workspace) / str(name)
                    try:
                        stat = path.stat()
                        if not path.is_file() or path.is_symlink():
                            continue
                        size = stat.st_size
                        mtime = stat.st_mtime
                    except OSError:
                        continue
                    size = int(info.get("size", size)) if isinstance(info, dict) else size
                    conn.execute(
                        "INSERT OR REPLACE INTO files"
                        "(workspace,name,size,sha256,source,uploader,mtime,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (str(workspace), str(name), size, None, "legacy", None, mtime, time.time()),
                    )
                    count += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('legacy_index_imported',?)",
                (str(count),),
            )
        try:
            backup = legacy_path.with_name(legacy_path.name + ".migrated")
            if backup.exists():
                backup = legacy_path.with_name(
                    f"{legacy_path.name}.migrated.{int(time.time())}"
                )
            legacy_path.replace(backup)
        except OSError as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 旧索引备份失败：{e}")
        return count

    def add_file(
        self,
        workspace: str,
        name: str,
        size: int,
        sha256: str = "",
        source: str = "received",
        uploader: str = "",
        mtime: float | None = None,
    ):
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files"
                "(workspace,name,size,sha256,source,uploader,mtime,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(workspace),
                    str(name),
                    int(size),
                    str(sha256 or ""),
                    str(source or "received"),
                    str(uploader or ""),
                    float(mtime if mtime is not None else now),
                    now,
                ),
            )

    def remove_file(self, workspace: str, name: str):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM files WHERE workspace=? AND name=?",
                (str(workspace), str(name)),
            )

    def find_by_hash(self, workspace: str, sha256: str):
        if not sha256:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE workspace=? AND sha256=? LIMIT 1",
                (str(workspace), str(sha256)),
            ).fetchone()
        return dict(row) if row else None

    def entries(self, workspace: str) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name,size FROM files WHERE workspace=?", (str(workspace),)
            ).fetchall()
        return {str(row["name"]): int(row["size"]) for row in rows}

    def list_files(self, workspace: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM files WHERE workspace=? ORDER BY created_at ASC, name ASC",
                (str(workspace),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_file(self, workspace: str, name: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE workspace=? AND name=?",
                (str(workspace), str(name)),
            ).fetchone()
        return dict(row) if row else None

    def workspace_stats(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT workspace, COUNT(*) AS count, COALESCE(SUM(size),0) AS size,"
                " MAX(created_at) AS last_ts FROM files GROUP BY workspace"
                " ORDER BY size DESC, count DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def totals(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size),0) AS size FROM files"
            ).fetchone()
        return dict(row) if row else {"count": 0, "size": 0}

    def prune_missing(self, workspaces_root: Path) -> int:
        removed = 0
        root = Path(workspaces_root)
        with self._conn() as conn:
            rows = conn.execute("SELECT workspace,name FROM files").fetchall()
            for row in rows:
                path = root / str(row["workspace"]) / str(row["name"])
                try:
                    if not path.is_file() or path.is_symlink():
                        conn.execute(
                            "DELETE FROM files WHERE workspace=? AND name=?",
                            (row["workspace"], row["name"]),
                        )
                        removed += 1
                except OSError:
                    conn.execute(
                        "DELETE FROM files WHERE workspace=? AND name=?",
                        (row["workspace"], row["name"]),
                    )
                    removed += 1
        return removed

    # ---------------- 审计 ----------------

    def audit(
        self,
        event: str,
        *,
        level: str = "info",
        workspace: str = "",
        user_id: str = "",
        group_id: str = "",
        platform: str = "",
        filename: str = "",
        size: int | None = None,
        sha256: str = "",
        detail=None,
    ):
        if isinstance(detail, (dict, list)):
            detail_text = json.dumps(detail, ensure_ascii=False)
        else:
            detail_text = str(detail or "")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit"
                "(created_at,event,level,workspace,user_id,group_id,platform,"
                "filename,size,sha256,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    str(event),
                    str(level),
                    str(workspace or ""),
                    str(user_id or ""),
                    str(group_id or ""),
                    str(platform or ""),
                    str(filename or "")[:512],
                    int(size) if size is not None else None,
                    str(sha256 or ""),
                    detail_text[:AUDIT_TEXT_LIMIT],
                ),
            )

    def query_audit(
        self,
        *,
        event: str = "",
        level: str = "",
        workspace: str = "",
        keyword: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        where = []
        params: list = []
        if event:
            where.append("event=?")
            params.append(event)
        if level:
            where.append("level=?")
            params.append(level)
        if workspace:
            where.append("workspace=?")
            params.append(workspace)
        if keyword:
            where.append(
                "(filename LIKE ? OR detail LIKE ? OR user_id LIKE ?)"
            )
            like = f"%{keyword}%"
            params.extend([like, like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM audit" + clause, tuple(params)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM audit" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": int(total)}

    def audit_summary(self) -> dict:
        now = time.time()
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM audit WHERE created_at>=?",
                (now - 86400,),
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM audit WHERE level IN ('warn','error')"
                " AND created_at>=?",
                (now - 86400,),
            ).fetchone()[0]
        return {"total": int(total), "last_24h": int(today), "warnings_24h": int(errors)}

    def cleanup_audit(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM audit WHERE created_at<?", (cutoff,))
            return int(cursor.rowcount or 0)

    def clear_audit(self, before_ts: float | None = None) -> int:
        with self._conn() as conn:
            if before_ts is None:
                cursor = conn.execute("DELETE FROM audit")
            else:
                cursor = conn.execute(
                    "DELETE FROM audit WHERE created_at<?", (float(before_ts),)
                )
            return int(cursor.rowcount or 0)


class FileTooLargeError(Exception):
    """文件超过允许下载的大小上限。"""


class FileTransferPlugin(Star):
    """文件收发插件。

    工作区直接使用 AstrBot 会话工作区：
    - 私聊：<平台>_FriendMessage_<QQ号>/
    - 群聊：<平台>_GroupMessage_<群号>/（群成员共享）
    AI 完成任务时写入工作区的脚本和产物可以正常使用；
    数量/容量上限只统计经本插件接收的文件。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._migrate_config()
        self.data_dir = self._resolve_data_dir()
        self.workspaces_dir = self._resolve_workspaces_dir()
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / DB_FILE_NAME
        self.legacy_index_path = self.data_dir / "received_index.json"
        self.db = FileTransferDB(self.db_path)
        try:
            imported = self.db.migrate_legacy(self.legacy_index_path, self.workspaces_dir)
            if imported:
                logger.info(f"[file_transfer] 已迁移 {imported} 条旧上传索引到 SQLite")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 迁移旧上传索引失败：{e}")

        self._clean_task = None
        self._last_clean_ts = 0.0
        self._last_audit_clean_ts = 0.0
        self._recent_urls: dict[str, float] = {}
        self._rate_buckets: dict[str, deque[float]] = {}
        self._pending_uploads: dict[str, dict] = {}
        self._transfer_semaphore = asyncio.Semaphore(
            max(1, self.max_concurrent_transfers)
        )
        self._register_web_apis()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._clean_task = loop.create_task(self._cleanup_loop())

        logger.info(
            f"[file_transfer] 已加载 v{PLUGIN_VERSION}，工作区目录：{self.workspaces_dir}"
        )

    # ---------------- 目录解析 ----------------

    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            return Path(StarTools.get_data_dir("file_transfer"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 获取插件数据目录失败，使用回退路径：{e}")
            fallback = Path.cwd() / "data" / "plugin_data" / "file_transfer"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    @staticmethod
    def _resolve_workspaces_dir() -> Path:
        """定位 AstrBot 会话工作区根目录（与 AstrBot 的 AI 任务 cwd 保持一致）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_workspaces_path

            return Path(get_astrbot_workspaces_path())
        except Exception:  # noqa: BLE001
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "workspaces"
        except Exception:  # noqa: BLE001
            pass
        candidates = [
            Path("/AstrBot/data/workspaces"),
            Path.cwd() / "data" / "workspaces",
            Path.cwd().parent / "workspaces",
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
        return Path.cwd() / "data" / "workspaces"

    @staticmethod
    def _astrbot_data_dir() -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()).resolve()
        except Exception:  # noqa: BLE001
            return Path.cwd().resolve() / "data"

    @staticmethod
    def _normalize_workspace_name(umo: str) -> str:
        """把 unified_msg_origin 转成 AstrBot 的工作区目录名。"""
        text = str(umo or "").strip()
        try:
            from astrbot.core.workspace import normalize_umo_for_workspace

            return normalize_umo_for_workspace(text)
        except Exception:  # noqa: BLE001
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
            return name or "unknown"

    @staticmethod
    def _event_umo(event: AstrMessageEvent) -> str:
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        except Exception:  # noqa: BLE001
            umo = ""
        if umo:
            return umo
        group_id = str(event.get_group_id() or "")
        session_id = group_id or str(event.get_sender_id() or "")
        message_type = "GroupMessage" if group_id else "FriendMessage"
        platform = ""
        try:
            platform = str(event.get_platform_id() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        return f"{platform or 'unknown'}:{message_type}:{session_id}"

    def get_event_workspace_dir(self, event: AstrMessageEvent) -> Path:
        """获取当前会话工作区：私聊按 QQ 号，群聊按群号（群成员共享）。"""
        name = self._normalize_workspace_name(self._event_umo(event))
        ws_dir = self.workspaces_dir / name
        ws_dir.mkdir(parents=True, exist_ok=True)
        return ws_dir

    def find_user_workspace_dir(self, qq: str) -> Path | None:
        """按 QQ 号查找私聊工作区（仅用于管理员查询，不创建目录）。"""
        qq = str(qq).strip()
        if not qq:
            return None
        try:
            entities = sorted(self.workspaces_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return None
        suffix = f"_FriendMessage_{qq}"
        for entity in entities:
            try:
                if entity.is_dir() and not entity.is_symlink() and entity.name.endswith(suffix):
                    return entity
            except OSError:
                continue
        return None

    # ---------------- 配置读取与迁移 ----------------

    @staticmethod
    def _config_groups() -> dict[str, tuple[str, ...]]:
        """配置文件的分组结构（与 _conf_schema.json 保持一致）。"""
        return {
            "sending": (
                "callback_api_base",
                "napcat_path_mapping",
                "max_send_mb",
            ),
            "receiving": (
                "auto_save",
                "auto_save_quoted",
                "suffix_filter_enable",
                "blocked_suffixes",
                "max_single_file_mb",
                "max_view_chars",
                "view_image_preview",
                "max_view_image_mb",
            ),
            "limits": (
                "max_file_count",
                "max_storage_mb",
                "send_rate_per_minute",
                "receive_rate_per_minute",
            ),
            "transfer": (
                "max_concurrent_transfers",
                "download_retry",
                "transfer_timeout_seconds",
                "min_free_disk_mb",
            ),
            "storage": (
                "dedupe_enabled",
                "audit_enabled",
                "audit_retention_days",
            ),
            "permissions": (
                "private_only",
                "whitelist_private_enable",
                "whitelist_users",
                "whitelist_group_enable",
                "whitelist_groups",
                "allowed_send_dirs",
            ),
            "workspace": ("workspace_auto_clean", "workspace_clean_interval_hours"),
        }

    _SCHEMA_SPEC_CACHE: dict | None = None
    _SCHEMA_DEFAULTS_CACHE: dict | None = None

    @classmethod
    def _schema_spec(cls) -> dict:
        """读取 _conf_schema.json 的可见分组（字段类型/默认值/说明）。"""
        if cls._SCHEMA_SPEC_CACHE is not None:
            return cls._SCHEMA_SPEC_CACHE
        spec: dict = {}
        try:
            schema_path = Path(__file__).with_name("_conf_schema.json")
            data = json.loads(schema_path.read_text(encoding="utf-8"))
            for group, meta in data.items():
                if not isinstance(meta, dict) or meta.get("type") != "object":
                    continue
                items = meta.get("items") or {}
                if isinstance(items, dict):
                    spec[str(group)] = items
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[file_transfer] 读取配置 schema 失败：{e}")
        cls._SCHEMA_SPEC_CACHE = spec
        return spec

    @classmethod
    def _schema_defaults(cls) -> dict:
        """读取 _conf_schema.json 的默认值，用于判断配置项是否仍为默认值。"""
        if cls._SCHEMA_DEFAULTS_CACHE is not None:
            return cls._SCHEMA_DEFAULTS_CACHE
        defaults: dict = {}
        for group, items in cls._schema_spec().items():
            defaults[str(group)] = {
                str(key): item.get("default") for key, item in items.items()
            }
        cls._SCHEMA_DEFAULTS_CACHE = defaults
        return defaults

    def _migrate_config(self):
        """把旧版扁平配置迁移到分组配置，兼容 1.5.2 及更早版本。

        旧版扁平键保留在 schema 的隐藏项中，避免 AstrBot 升级时直接丢弃用户配置；
        迁移只执行一次，完成后写入 _migrated_v153 标志。
        """
        if not isinstance(self.config, dict):
            self.config = {}
            return
        if bool(self.config.get("_migrated_v153", False)):
            return
        defaults = self._schema_defaults()
        changed = False
        for section, keys in self._config_groups().items():
            section_cfg = self.config.get(section)
            if not isinstance(section_cfg, dict):
                section_cfg = {}
                self.config[section] = section_cfg
                changed = True
            section_defaults = defaults.get(section, {})
            for key in keys:
                if key not in self.config:
                    continue
                flat_value = self.config.get(key)
                default_value = section_defaults.get(key)
                current = section_cfg.get(key, default_value)
                # 仅当分组内仍为默认值（或尚未写入）时，才用旧扁平值覆盖；
                # 避免旧配置残留键覆盖用户后来在分组配置里改过的值。
                if current == default_value and current != flat_value:
                    section_cfg[key] = flat_value
                    changed = True
        permissions = self.config.get("permissions")
        if isinstance(permissions, dict) and "whitelist_enable" in self.config:
            # 旧版单开关同时作用于私聊与群聊；拆分后两个开关都继承旧值
            legacy_enabled = bool(self.config.get("whitelist_enable"))
            for key in ("whitelist_private_enable", "whitelist_group_enable"):
                default_value = defaults.get("permissions", {}).get(key)
                current = permissions.get(key, default_value)
                if current == default_value and current != legacy_enabled:
                    permissions[key] = legacy_enabled
                    changed = True
        self.config["_migrated_v153"] = True
        changed = True
        if changed:
            save_config = getattr(self.config, "save_config", None)
            if callable(save_config):
                try:
                    save_config()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[file_transfer] 保存迁移后的配置失败：{e}")

    def _cfg(self, section: str, key: str, default):
        """按分组读取配置；兼容旧版扁平配置键。"""
        section_cfg = self.config.get(section)
        if isinstance(section_cfg, dict) and key in section_cfg:
            value = section_cfg.get(key, default)
            return default if value is None else value
        if key in self.config:
            value = self.config.get(key, default)
            return default if value is None else value
        return default

    _CONFIG_MINIMUMS = {
        "max_file_count": 0,
        "max_storage_mb": 0,
        "max_single_file_mb": 0,
        "max_send_mb": 0,
        "send_rate_per_minute": 0,
        "receive_rate_per_minute": 0,
        "max_concurrent_transfers": 1,
        "download_retry": 0,
        "transfer_timeout_seconds": 1,
        "min_free_disk_mb": 0,
        "max_view_chars": 1,
        "max_view_image_mb": 0,
        "audit_retention_days": 0,
        "workspace_clean_interval_hours": 1,
    }

    def _coerce_web_config(self, payload: dict) -> tuple[dict, list[str]]:
        """校验并转换 WebUI 提交的可见配置，只接受 schema 中声明的字段。"""
        if not isinstance(payload, dict):
            return {}, ["配置格式错误"]
        spec = self._schema_spec()
        updates: dict = {}
        errors: list[str] = []
        for group, items in spec.items():
            section = payload.get(group)
            if section is None:
                continue
            if not isinstance(section, dict):
                errors.append(f"{group}：配置格式错误")
                continue
            group_updates: dict = {}
            for key, meta in items.items():
                if key not in section:
                    continue
                raw = section.get(key)
                typ = meta.get("type")
                label = f"{group}.{key}"
                if typ == "bool":
                    if not isinstance(raw, bool):
                        errors.append(f"{label}：需要布尔值")
                        continue
                    value = raw
                elif typ == "int":
                    if isinstance(raw, bool):
                        errors.append(f"{label}：需要整数")
                        continue
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        errors.append(f"{label}：需要整数")
                        continue
                    minimum = self._CONFIG_MINIMUMS.get(key, 0)
                    if value < minimum:
                        errors.append(f"{label}：不能小于 {minimum}")
                        continue
                elif typ == "list":
                    if isinstance(raw, str):
                        raw = raw.splitlines()
                    if not isinstance(raw, list):
                        errors.append(f"{label}：需要列表")
                        continue
                    value = [str(item).strip() for item in raw if str(item).strip()]
                else:
                    if not isinstance(raw, str):
                        raw = str(raw or "")
                    value = raw.strip()
                group_updates[key] = value
            updates[group] = group_updates
        return updates, errors

    def _apply_web_config(
        self, updates: dict, *, detail=None, user_id: str = "webui"
    ) -> tuple[bool, str]:
        """把校验后的配置写入 AstrBotConfig，失败时回滚。"""
        backup: dict = {}
        for group, values in updates.items():
            section = self.config.get(group)
            if not isinstance(section, dict):
                section = {}
                self.config[group] = section
            backup[group] = {key: section.get(key) for key in values}
            section.update(values)
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as e:  # noqa: BLE001
                for group, values in backup.items():
                    section = self.config.get(group)
                    if isinstance(section, dict):
                        for key, old in values.items():
                            if old is None:
                                section.pop(key, None)
                            else:
                                section[key] = old
                logger.warning(f"[file_transfer] WebUI 保存配置失败：{e}")
                return False, f"保存失败：{e}"
        self._transfer_semaphore = asyncio.Semaphore(
            max(1, self.max_concurrent_transfers)
        )
        changed_keys = [
            f"{group}.{key}"
            for group, values in updates.items()
            for key in values
        ]
        self._audit(
            "config_save",
            detail={"keys": changed_keys[:80], "extra": detail or ""},
            user_id=user_id,
        )
        return True, ""

    @property
    def callback_api_base(self) -> str:
        return str(self._cfg("sending", "callback_api_base", "") or "").strip().rstrip("/")

    @property
    def path_mapping(self) -> str:
        return str(self._cfg("sending", "napcat_path_mapping", "") or "").strip()

    @property
    def max_send_mb(self) -> int:
        """发送文件大小上限（MB），0 表示不限制。"""
        return self._positive_int(self._cfg("sending", "max_send_mb", 100), 100, True)

    @property
    def max_send_bytes(self) -> int:
        return self.max_send_mb * 1024 * 1024

    @property
    def auto_save(self) -> bool:
        return bool(self._cfg("receiving", "auto_save", True))

    @property
    def auto_save_quoted(self) -> bool:
        return bool(self._cfg("receiving", "auto_save_quoted", False))

    @property
    def suffix_filter_enable(self) -> bool:
        return bool(self._cfg("receiving", "suffix_filter_enable", True))

    @property
    def blocked_suffixes(self) -> set:
        result = set()
        for x in (self._cfg("receiving", "blocked_suffixes", []) or []):
            s = str(x).strip().lower()
            if not s:
                continue
            if not s.startswith("."):
                s = "." + s
            result.add(s)
        return result

    @property
    def max_single_file_mb(self) -> int:
        """单个接收文件大小上限（MB），0 表示不限制。"""
        return self._positive_int(
            self._cfg("receiving", "max_single_file_mb", 50), 50, True
        )

    @property
    def max_single_file_bytes(self) -> int:
        return self.max_single_file_mb * 1024 * 1024

    @property
    def max_view_chars(self) -> int:
        try:
            val = int(self._cfg("receiving", "max_view_chars", DEFAULT_MAX_VIEW_CHARS))
        except (TypeError, ValueError):
            return DEFAULT_MAX_VIEW_CHARS
        return val if val > 0 else DEFAULT_MAX_VIEW_CHARS

    @property
    def view_image_preview(self) -> bool:
        return bool(self._cfg("receiving", "view_image_preview", True))

    @property
    def max_view_image_mb(self) -> int:
        return self._positive_int(
            self._cfg("receiving", "max_view_image_mb", 10), 10, True
        )

    @property
    def max_view_image_bytes(self) -> int:
        return self.max_view_image_mb * 1024 * 1024

    @staticmethod
    def _positive_int(value, default: int, allow_zero: bool) -> int:
        try:
            val = int(value)
        except (TypeError, ValueError):
            return default
        if val > 0:
            return val
        if allow_zero and val == 0:
            return 0
        return default

    @property
    def max_file_count(self) -> int:
        """每个工作区最多保留的上传文件数（不含 AI 脚本/产物），0 表示不限制。"""
        return self._positive_int(
            self._cfg("limits", "max_file_count", 10), 10, True
        )

    @property
    def max_storage_mb(self) -> int:
        """每个工作区内上传文件的总大小上限（MB），0 表示不限制。"""
        return self._positive_int(
            self._cfg("limits", "max_storage_mb", 10), 10, True
        )

    @property
    def max_storage_bytes(self) -> int:
        return self.max_storage_mb * 1024 * 1024

    @property
    def send_rate_per_minute(self) -> int:
        return self._positive_int(
            self._cfg("limits", "send_rate_per_minute", 10), 10, True
        )

    @property
    def receive_rate_per_minute(self) -> int:
        return self._positive_int(
            self._cfg("limits", "receive_rate_per_minute", 20), 20, True
        )

    @property
    def max_concurrent_transfers(self) -> int:
        return self._positive_int(
            self._cfg("transfer", "max_concurrent_transfers", 2), 2, False
        )

    @property
    def download_retry(self) -> int:
        return self._positive_int(self._cfg("transfer", "download_retry", 2), 2, True)

    @property
    def transfer_timeout_seconds(self) -> int:
        return self._positive_int(
            self._cfg("transfer", "transfer_timeout_seconds", 1800), 1800, False
        )

    @property
    def min_free_disk_mb(self) -> int:
        return self._positive_int(
            self._cfg("transfer", "min_free_disk_mb", 200), 200, True
        )

    @property
    def dedupe_enabled(self) -> bool:
        return bool(self._cfg("storage", "dedupe_enabled", True))

    @property
    def audit_enabled(self) -> bool:
        return bool(self._cfg("storage", "audit_enabled", True))

    @property
    def audit_retention_days(self) -> int:
        return self._positive_int(
            self._cfg("storage", "audit_retention_days", 30), 30, True
        )

    @property
    def private_only(self) -> bool:
        return bool(self._cfg("permissions", "private_only", False))

    @property
    def whitelist_private_enable(self) -> bool:
        return bool(self._cfg("permissions", "whitelist_private_enable", False))

    @property
    def whitelist_group_enable(self) -> bool:
        return bool(self._cfg("permissions", "whitelist_group_enable", False))

    @property
    def whitelist_users(self) -> set:
        return {
            str(x).strip()
            for x in (self._cfg("permissions", "whitelist_users", []) or [])
            if str(x).strip()
        }

    @property
    def whitelist_groups(self) -> set:
        return {
            str(x).strip()
            for x in (self._cfg("permissions", "whitelist_groups", []) or [])
            if str(x).strip()
        }

    @property
    def allowed_send_dirs(self) -> list[str]:
        dirs = []
        for x in (self._cfg("permissions", "allowed_send_dirs", []) or []):
            s = str(x).strip()
            if s:
                dirs.append(s)
        return dirs

    @property
    def workspace_auto_clean(self) -> bool:
        return bool(self._cfg("workspace", "workspace_auto_clean", True))

    @property
    def workspace_clean_interval_hours(self) -> float:
        try:
            val = float(self._cfg("workspace", "workspace_clean_interval_hours", 1))
        except (TypeError, ValueError):
            val = 1.0
        return val if val > 0 else 1.0

    # ---------------- 权限控制 ----------------

    def _check_access(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查当前会话是否允许收发文件。"""
        group_id = event.get_group_id()
        is_private = not group_id

        if self.private_only and not is_private:
            return False, "已开启「仅私聊收发文件」，群聊里不能用哦"

        if is_private:
            if self.whitelist_private_enable:
                sender = str(event.get_sender_id())
                if sender not in self.whitelist_users:
                    return False, "你不在私聊文件收发白名单里，暂时不能用这个功能"
        else:
            if self.whitelist_group_enable:
                if str(group_id) not in self.whitelist_groups:
                    return False, "本群不在文件收发白名单里，暂时不能用这个功能"

        return True, ""

    def is_astrbot_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否具有 AstrBot 管理员权限。"""
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return False
        # 兼容旧版本 AstrBot：从 AstrBot 数据目录的 cmd_config.json 读取 admins_id
        try:
            cfg_path = self._astrbot_data_dir() / "cmd_config.json"
            if cfg_path.is_file():
                data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
                admins = {str(x).strip() for x in data.get("admins_id", [])}
                return sender_id in admins
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 读取管理员配置失败：{e}")
        return False

    # ---------------- 平台适配 ----------------

    @staticmethod
    def _platform_name(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "").strip().lower()
        except Exception:  # noqa: BLE001
            return ""

    def _is_qq_official(self, event: AstrMessageEvent) -> bool:
        """是否为 QQ 官方机器人平台（WebSocket 或 Webhook）。"""
        return self._platform_name(event) in QQ_OFFICIAL_PLATFORMS

    @staticmethod
    def _qq_official_raw_type(event: AstrMessageEvent) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return type(raw).__name__ if raw is not None else ""

    def _qq_official_supports_file(self, event: AstrMessageEvent) -> bool:
        """QQ 官方平台目前仅群聊（GroupMessage）与单聊（C2CMessage）支持富媒体文件。"""
        return self._qq_official_raw_type(event) in QQ_OFFICIAL_FILE_RAW_TYPES

    @staticmethod
    def _is_within(path: Path | str, base: Path | str) -> bool:
        """判断 path 是否位于 base 目录内（含自身，处理大小写差异）。"""
        try:
            rp = Path(path).resolve()
            rb = Path(base).resolve()
        except (OSError, RuntimeError):
            return False
        try:
            rp_s = os.path.normcase(str(rp))
            rb_s = os.path.normcase(str(rb))
            return rp_s == rb_s or os.path.commonpath([rp_s, rb_s]) == rb_s
        except ValueError:
            return False

    def _send_decision(self, event: AstrMessageEvent, path: Path) -> tuple[bool, str]:
        """判断是否允许把该文件发送到会话，返回 (是否允许, 拒绝原因)。

        AstrBot 管理员：任意可读文件；
        普通用户：本人工作区或 allowed_send_dirs 内的文件。
        """
        try:
            resolved = Path(path).resolve()
        except (OSError, RuntimeError):
            return False, "文件路径无法解析"

        if self.is_astrbot_admin(event):
            return True, ""

        try:
            own_ws = self.get_event_workspace_dir(event)
        except Exception:  # noqa: BLE001
            own_ws = None
        if own_ws is not None and self._is_within(resolved, own_ws):
            return True, ""

        for allowed_dir in self.allowed_send_dirs:
            try:
                if self._is_within(resolved, Path(allowed_dir)):
                    return True, ""
            except (OSError, ValueError):
                continue

        return (
            False,
            "该文件不在允许发送的目录内：普通用户只能发送当前工作区"
            "或管理员配置目录中的文件",
        )

    # ---------------- 文件名处理 ----------------

    @staticmethod
    def _sanitize_filename(raw: str, fallback: str = "file") -> str:
        """清洗文件名：去路径、去非法字符、去尾部空格/点、限长、避开 Windows 保留名。"""
        name = unicodedata.normalize("NFC", str(raw or ""))
        name = name.replace("\\", "/")
        name = posixpath.basename(name)
        name = "".join(
            ch for ch in name if ch not in INVALID_FILENAME_CHARS and ord(ch) >= 32
        )
        name = name.strip().rstrip(" .")
        if not name or name in {".", ".."}:
            name = fallback
        stem, ext = os.path.splitext(name)
        if len(name) > MAX_FILENAME_LEN:
            ext = ext[:20]
            name = stem[: max(1, MAX_FILENAME_LEN - len(ext))] + ext
        if os.path.splitext(name)[0].upper() in WINDOWS_RESERVED_NAMES:
            name = "_" + name
        return name

    @staticmethod
    def _protect_received_name(name: str) -> str:
        """避免接收文件污染工作区控制文件或变成隐藏文件。"""
        if name.startswith("."):
            name = "recv_" + name.lstrip(".")
        if name.lower() in PROTECTED_WORKSPACE_FILES:
            name = "recv_" + name
        return name

    def _is_blocked_suffix(self, filename: str) -> bool:
        suffix = os.path.splitext(filename)[1].lower()
        return bool(suffix) and suffix in self.blocked_suffixes

    # ---------------- 上传索引 / 审计 / 限速（SQLite） ----------------

    @staticmethod
    def _workspace_key(ws_dir: Path) -> str:
        return ws_dir.name

    def _received_entries(self, ws_dir: Path) -> dict[str, int]:
        """返回该工作区中仍存在的“上传文件” {文件名: 大小}，顺带清理失效索引。"""
        key = self._workspace_key(ws_dir)
        result: dict[str, int] = {}
        try:
            entries = self.db.entries(key)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 读取文件索引失败：{e}")
            return {}
        for name, size in entries.items():
            path = ws_dir / str(name)
            try:
                if path.is_file() and not path.is_symlink():
                    actual = path.stat().st_size
                    result[name] = actual
                    if actual != size:
                        row = self.db.get_file(key, name) or {}
                        self.db.add_file(
                            key,
                            name,
                            actual,
                            sha256=str(row.get("sha256") or ""),
                            source=str(row.get("source") or "received"),
                            uploader=str(row.get("uploader") or ""),
                            mtime=float(row.get("mtime") or time.time()),
                        )
                else:
                    self.db.remove_file(key, name)
            except OSError:
                self.db.remove_file(key, name)
        return result

    def _received_storage_bytes(self, ws_dir: Path) -> int:
        return sum(self._received_entries(ws_dir).values())

    def _index_add(
        self,
        ws_dir: Path,
        filename: str,
        size: int,
        sha256: str = "",
        source: str = "received",
        uploader: str = "",
    ):
        try:
            self.db.add_file(
                self._workspace_key(ws_dir),
                filename,
                size,
                sha256=sha256,
                source=source,
                uploader=uploader,
                mtime=self._safe_mtime(ws_dir / filename),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 写入文件索引失败：{e}")

    def _index_remove(self, ws_dir: Path, filename: str):
        try:
            self.db.remove_file(self._workspace_key(ws_dir), filename)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 移除文件索引失败：{e}")

    def _prune_index(self):
        try:
            removed = self.db.prune_missing(self.workspaces_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 索引清理失败：{e}")
            return
        if removed:
            logger.info(f"[file_transfer] 索引清理：移除 {removed} 条失效记录")

    def _audit(
        self,
        event: str,
        *,
        level: str = "info",
        workspace: str = "",
        user_id: str = "",
        group_id: str = "",
        platform: str = "",
        filename: str = "",
        size: int | None = None,
        sha256: str = "",
        detail=None,
    ):
        if not self.audit_enabled:
            return
        try:
            self.db.audit(
                event,
                level=level,
                workspace=workspace,
                user_id=user_id,
                group_id=group_id,
                platform=platform,
                filename=filename,
                size=size,
                sha256=sha256,
                detail=detail,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 写入审计日志失败：{e}")

    @staticmethod
    def _event_audit_fields(event: AstrMessageEvent) -> dict:
        return {
            "user_id": str(event.get_sender_id() or ""),
            "group_id": str(event.get_group_id() or ""),
            "platform": str(event.get_platform_name() or ""),
        }

    def _event_workspace_name(self, event: AstrMessageEvent) -> str:
        try:
            return self._normalize_workspace_name(self._event_umo(event))
        except Exception:  # noqa: BLE001
            return ""

    def _check_rate(self, key: str, limit: int) -> bool:
        """内存滑动窗口限速；limit<=0 表示不限制。"""
        if limit <= 0:
            return True
        now = time.monotonic()
        bucket = self._rate_buckets.setdefault(key, deque())
        while bucket and now - bucket[0] >= RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        if len(self._rate_buckets) > 2000:
            for old_key in [
                k for k, v in self._rate_buckets.items() if not v or now - v[-1] > 300
            ]:
                self._rate_buckets.pop(old_key, None)
        return True

    # ---------------- 目录统计与列表 ----------------

    @staticmethod
    def _iter_workspace_files(ws_dir: Path) -> list[Path]:
        """列出工作区内的普通文件（跳过隐藏文件、临时文件与符号链接）。"""
        try:
            entries = list(ws_dir.iterdir())
        except OSError:
            return []
        files = []
        for path in entries:
            try:
                if path.name.startswith("."):
                    continue
                if not path.is_file() or path.is_symlink():
                    continue
            except OSError:
                continue
            files.append(path)
        return files

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _sorted_workspace_files(self, ws_dir: Path) -> list[Path]:
        files = self._iter_workspace_files(ws_dir)
        files.sort(key=self._safe_mtime)
        return files

    # ---------------- 自动清理 ----------------

    async def _cleanup_loop(self):
        """后台定时清理：清理所有 QQ 用户/群聊工作区中的超期文件（含任务脚本）。"""
        while True:
            try:
                if self.workspace_auto_clean:
                    interval = self.workspace_clean_interval_hours * 3600
                    now = time.time()
                    if now - self._last_clean_ts >= interval:
                        self._last_clean_ts = now
                        removed = await asyncio.to_thread(
                            self._cleanup_workspaces, interval
                        )
                        self._prune_index()
                        if removed:
                            self._audit(
                                "cleanup",
                                detail={
                                    "files_removed": int(removed or 0),
                                    "retention_hours": self.workspace_clean_interval_hours,
                                },
                            )
                self._cleanup_pending_uploads()
                now = time.time()
                if now - self._last_audit_clean_ts >= 86400:
                    self._last_audit_clean_ts = now
                    audit_removed = self.db.cleanup_audit(
                        self.audit_retention_days
                    )
                    if audit_removed:
                        self._audit(
                            "cleanup",
                            detail={
                                "audit_removed": int(audit_removed),
                                "audit_retention_days": self.audit_retention_days,
                            },
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[file_transfer] 自动清理出错：{e}")
            await asyncio.sleep(CLEAN_CHECK_INTERVAL)

    def _cleanup_workspaces(self, max_age_seconds: float) -> int:
        """删除 QQ 用户/群聊工作区中修改时间超过 max_age_seconds 的文件。"""
        removed = 0
        now = time.time()
        try:
            entities = list(self.workspaces_dir.iterdir())
        except OSError as e:
            logger.warning(f"[file_transfer] 遍历工作区根目录失败：{e}")
            return 0

        for ws_dir in entities:
            try:
                if not ws_dir.is_dir() or ws_dir.is_symlink():
                    continue
            except OSError:
                continue
            if not any(marker in ws_dir.name for marker in QQ_WORKSPACE_MARKERS):
                continue
            try:
                entries = list(ws_dir.iterdir())
            except OSError:
                continue
            for item in entries:
                try:
                    if item.is_symlink() or not item.is_file():
                        continue
                    if item.name.startswith("."):
                        # 仅清理残留的下载临时文件
                        if not item.name.endswith(".part"):
                            continue
                    if item.name.lower() in PROTECTED_WORKSPACE_FILES:
                        continue
                    if now - item.stat().st_mtime >= max_age_seconds:
                        item.unlink()
                        removed += 1
                except OSError as e:
                    logger.warning(f"[file_transfer] 清理工作区文件失败 {item}: {e}")

        if removed:
            logger.info(f"[file_transfer] 工作区自动清理已删除 {removed} 个超期文件")
        return removed

    async def terminate(self):
        if self._clean_task:
            self._clean_task.cancel()
            try:
                await self._clean_task
            except BaseException:  # noqa: BLE001
                pass
            self._clean_task = None

    # ---------------- WebUI 后端 API ----------------

    @staticmethod
    def _web_api():
        from astrbot.api import web

        return web

    def _register_web_apis(self):
        """注册插件可视化页面使用的后端 API；旧版 AstrBot 无此能力时静默跳过。"""
        try:
            from astrbot.api import web  # noqa: F401
        except Exception:  # noqa: BLE001
            return
        context = getattr(self, "context", None)
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            return
        prefix = f"/{PLUGIN_NAME}"
        specs = (
            ("overview", self.web_overview, ["GET"], "File transfer overview"),
            ("workspaces", self.web_workspaces, ["GET"], "List workspaces"),
            ("files", self.web_files, ["GET"], "List workspace files"),
            ("file/preview", self.web_file_preview, ["GET"], "Preview file"),
            ("file/download", self.web_file_download, ["GET"], "Download file"),
            ("files/delete", self.web_file_delete, ["POST"], "Delete file"),
            ("files/rename", self.web_file_rename, ["POST"], "Rename file"),
            ("files/upload/<workspace>", self.web_file_upload, ["POST"], "Upload file"),
            ("files/upload_chunked", self.web_file_upload_chunked, ["POST"], "Chunked upload"),
            ("workspaces/clear", self.web_workspace_clear, ["POST"], "Clear workspace"),
            ("workspaces/clean", self.web_workspace_clean, ["POST"], "Clean workspace"),
            ("audit", self.web_audit, ["GET"], "Audit log"),
            ("audit/clear", self.web_audit_clear, ["POST"], "Clear audit log"),
            ("quotas", self.web_quotas, ["GET"], "Quota and rate limits"),
            ("settings", self.web_settings, ["GET"], "Runtime settings"),
            ("config/save", self.web_config_save, ["POST"], "Save plugin config"),
            ("config/reset", self.web_config_reset, ["POST"], "Reset plugin config"),
        )
        for route, handler, methods, desc in specs:
            try:
                register(f"{prefix}/{route}", handler, methods, desc)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[file_transfer] 注册 Web API 失败 {route}: {e}")

    def _web_workspace_dirs(self) -> list[Path]:
        try:
            entities = sorted(
                self.workspaces_dir.iterdir(), key=lambda p: p.name.lower()
            )
        except OSError:
            return []
        result = []
        for path in entities:
            try:
                if not path.is_dir() or path.is_symlink():
                    continue
            except OSError:
                continue
            if any(marker in path.name for marker in QQ_WORKSPACE_MARKERS):
                result.append(path)
        return result

    def _web_resolve_workspace(self, name: str) -> Path | None:
        name = str(name or "").strip()
        if (
            not name
            or name in {".", ".."}
            or name.startswith(".")
            or "/" in name
            or "\\" in name
        ):
            return None
        path = self.workspaces_dir / name
        if not self._is_within(path, self.workspaces_dir):
            return None
        try:
            if not path.is_dir() or path.is_symlink():
                return None
        except OSError:
            return None
        if not any(marker in path.name for marker in QQ_WORKSPACE_MARKERS):
            return None
        return path

    def _web_resolve_file(self, ws_dir: Path, name: str) -> Path | None:
        name = str(name or "").strip()
        if not name or name in {".", ".."} or name.startswith("."):
            return None
        safe = name.replace("\\", "/").split("/")[-1]
        if not safe or safe != name:
            return None
        path = ws_dir / safe
        if not self._is_within(path, ws_dir):
            return None
        try:
            if not path.is_file() or path.is_symlink():
                return None
        except OSError:
            return None
        return path

    @staticmethod
    def _web_workspace_meta(ws_dir: Path) -> dict:
        name = ws_dir.name
        kind = "group" if "_GroupMessage_" in name else "private"
        platform = ""
        for marker in QQ_WORKSPACE_MARKERS:
            if marker in name:
                platform = name.split(marker, 1)[0]
                break
        return {"name": name, "kind": kind, "platform": platform}

    def _web_workspace_rows(self) -> list[dict]:
        rows = []
        self._prune_index()
        uploads = {
            str(row["workspace"]): row for row in self.db.workspace_stats()
        }
        for ws_dir in self._web_workspace_dirs():
            files = self._iter_workspace_files(ws_dir)
            total_size = 0
            last_mtime = 0.0
            for path in files:
                try:
                    total_size += path.stat().st_size
                    last_mtime = max(last_mtime, path.stat().st_mtime)
                except OSError:
                    continue
            up = uploads.get(ws_dir.name, {})
            meta = self._web_workspace_meta(ws_dir)
            meta.update(
                {
                    "file_count": len(files),
                    "size": total_size,
                    "upload_count": int(up.get("count", 0) or 0),
                    "upload_size": int(up.get("size", 0) or 0),
                    "last_mtime": last_mtime,
                }
            )
            rows.append(meta)
        rows.sort(key=lambda item: item.get("last_mtime") or 0, reverse=True)
        return rows

    async def web_overview(self):
        web = self._web_api()
        try:
            rows = self._web_workspace_rows()
            totals = self.db.totals()
            audit = self.db.audit_summary()
            recent = self.db.query_audit(limit=8)["items"]
            return web.json_response(
                {
                    "version": PLUGIN_VERSION,
                    "plugin": PLUGIN_NAME,
                    "workspaces": len(rows),
                    "files": sum(int(row["file_count"]) for row in rows),
                    "size": sum(int(row["size"]) for row in rows),
                    "uploads": int(totals.get("count", 0) or 0),
                    "upload_size": int(totals.get("size", 0) or 0),
                    "audit": audit,
                    "recent": recent,
                    "limits": {
                        "max_file_count": self.max_file_count,
                        "max_storage_mb": self.max_storage_mb,
                        "max_single_file_mb": self.max_single_file_mb,
                        "max_send_mb": self.max_send_mb,
                        "send_rate_per_minute": self.send_rate_per_minute,
                        "receive_rate_per_minute": self.receive_rate_per_minute,
                    },
                    "transfer": {
                        "max_concurrent_transfers": self.max_concurrent_transfers,
                        "download_retry": self.download_retry,
                        "transfer_timeout_seconds": self.transfer_timeout_seconds,
                        "min_free_disk_mb": self.min_free_disk_mb,
                    },
                    "storage": {
                        "dedupe_enabled": self.dedupe_enabled,
                        "audit_enabled": self.audit_enabled,
                        "audit_retention_days": self.audit_retention_days,
                    },
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI overview 失败：{e}")
            return web.error_response("概览加载失败", status_code=500)

    async def web_workspaces(self):
        web = self._web_api()
        try:
            return web.json_response({"items": self._web_workspace_rows()})
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI workspaces 失败：{e}")
            return web.error_response("工作区加载失败", status_code=500)

    async def web_files(self):
        web = self._web_api()
        request = web.request
        try:
            workspace = str(request.query.get("workspace", "") or "").strip()
            keyword = str(request.query.get("q", "") or "").strip().lower()
            limit = max(1, min(int(request.query.get("limit", 100) or 100), 500))
            offset = max(0, int(request.query.get("offset", 0) or 0))
            ws_dir = self._web_resolve_workspace(workspace)
            if ws_dir is None:
                return web.error_response("工作区不存在", status_code=404)
            db_rows = {str(row["name"]): row for row in self.db.list_files(ws_dir.name)}
            items = []
            for path in self._sorted_workspace_files(ws_dir):
                name = path.name
                if keyword and keyword not in name.lower():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                row = db_rows.get(name, {})
                items.append(
                    {
                        "name": name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "sha256": str(row.get("sha256") or ""),
                        "source": str(row.get("source") or ("ai" if not row else "")),
                        "protected": name.lower() in PROTECTED_WORKSPACE_FILES,
                        "mime": mimetypes.guess_type(name)[0]
                        or "application/octet-stream",
                    }
                )
            items.sort(key=lambda item: item["mtime"], reverse=True)
            total = len(items)
            page_items = items[offset : offset + limit]
            return web.json_response(
                {
                    "workspace": ws_dir.name,
                    "items": page_items,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI files 失败：{e}")
            return web.error_response("文件列表加载失败", status_code=500)

    async def web_file_preview(self):
        web = self._web_api()
        request = web.request
        try:
            workspace = str(request.query.get("workspace", "") or "")
            name = str(request.query.get("name", "") or "")
            page = max(1, min(int(request.query.get("page", 1) or 1), MAX_VIEW_PAGE))
            ws_dir = self._web_resolve_workspace(workspace)
            if ws_dir is None:
                return web.error_response("工作区不存在", status_code=404)
            path = self._web_resolve_file(ws_dir, name)
            if path is None:
                return web.error_response("文件不存在", status_code=404)
            size = self._safe_size(path)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            sha = self._file_sha256(path)
            text_page = self._read_text_page(path, page)
            if text_page is None:
                self._audit(
                    "view", workspace=ws_dir.name, filename=path.name,
                    size=size, detail={"mode": "webui-binary", "page": page},
                    user_id=f"webui:{request.username or ''}",
                )
                return web.json_response(
                    {
                        "kind": "binary",
                        "name": path.name,
                        "size": size,
                        "mtime": self._safe_mtime(path),
                        "mime": mime,
                        "sha256": sha,
                        "message": "二进制文件，不提供文本预览",
                    }
                )
            self._audit(
                "view", workspace=ws_dir.name, filename=path.name,
                size=size, detail={"mode": "webui", "page": page},
                user_id=f"webui:{request.username or ''}",
            )
            return web.json_response(
                {
                    "kind": "text",
                    "name": path.name,
                    "size": size,
                    "mtime": self._safe_mtime(path),
                    "mime": mime,
                    "sha256": sha,
                    **text_page,
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI preview 失败：{e}")
            return web.error_response("预览失败", status_code=500)

    async def web_file_download(self):
        web = self._web_api()
        request = web.request
        try:
            workspace = str(request.query.get("workspace", "") or "")
            name = str(request.query.get("name", "") or "")
            ws_dir = self._web_resolve_workspace(workspace)
            if ws_dir is None:
                return web.error_response("工作区不存在", status_code=404)
            path = self._web_resolve_file(ws_dir, name)
            if path is None:
                return web.error_response("文件不存在", status_code=404)
            self._audit(
                "download", workspace=ws_dir.name, filename=path.name,
                size=self._safe_size(path), detail="WebUI 下载",
                user_id=f"webui:{request.username or ''}",
            )
            return web.file_response(str(path), filename=path.name)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI download 失败：{e}")
            return web.error_response("下载失败", status_code=500)

    async def _web_json_payload(self) -> dict:
        web = self._web_api()
        payload = await web.request.json(default={})
        return payload if isinstance(payload, dict) else {}

    def _web_user_id(self) -> str:
        web = self._web_api()
        return f"webui:{web.request.username or ''}"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _delete_workspace_files(
        self, ws_dir: Path, max_age_seconds: float = 0.0
    ) -> tuple[int, int]:
        """删除工作区普通文件（跳过受保护/隐藏文件），返回 (数量, 字节数)。"""
        removed = 0
        removed_bytes = 0
        now = time.time()
        try:
            entries = list(ws_dir.iterdir())
        except OSError:
            return 0, 0
        for item in entries:
            try:
                if item.is_symlink() or not item.is_file():
                    continue
                if item.name.lower() in PROTECTED_WORKSPACE_FILES:
                    continue
                if item.name.startswith(".") and not item.name.endswith(".part"):
                    continue
                stat = item.stat()
                if max_age_seconds and now - stat.st_mtime < max_age_seconds:
                    continue
                item.unlink()
                removed += 1
                removed_bytes += stat.st_size
            except OSError as e:  # noqa: BLE001
                logger.warning(f"[file_transfer] 删除工作区文件失败 {item}: {e}")
        return removed, removed_bytes

    async def web_file_delete(self):
        web = self._web_api()
        try:
            payload = await self._web_json_payload()
            workspace = str(payload.get("workspace", "") or "")
            name = str(payload.get("name", "") or "")
            ws_dir = self._web_resolve_workspace(workspace)
            if ws_dir is None:
                return web.error_response("工作区不存在", status_code=404)
            path = self._web_resolve_file(ws_dir, name)
            if path is None:
                return web.error_response("文件不存在", status_code=404)
            if path.name.lower() in PROTECTED_WORKSPACE_FILES:
                return web.error_response("该文件受保护，不能删除", status_code=400)
            size = self._safe_size(path)
            path.unlink()
            self._index_remove(ws_dir, path.name)
            self._audit(
                "delete", workspace=ws_dir.name, filename=path.name,
                size=size, detail="WebUI 删除", user_id=self._web_user_id(),
            )
            return web.json_response({"deleted": path.name, "size": size})
        except FileNotFoundError:
            return web.error_response("文件不存在", status_code=404)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI 删除失败：{e}")
            return web.error_response("删除失败", status_code=500)

    async def web_file_rename(self):
        web = self._web_api()
        try:
            payload = await self._web_json_payload()
            workspace = str(payload.get("workspace", "") or "")
            name = str(payload.get("name", "") or "")
            new_name = self._sanitize_filename(
                str(payload.get("new_name", "") or ""), fallback=""
            )
            ws_dir = self._web_resolve_workspace(workspace)
            if ws_dir is None:
                return web.error_response("工作区不存在", status_code=404)
            path = self._web_resolve_file(ws_dir, name)
            if path is None:
                return web.error_response("文件不存在", status_code=404)
            if (
                not new_name
                or new_name.startswith(".")
                or path.name.lower() in PROTECTED_WORKSPACE_FILES
                or new_name.lower() in PROTECTED_WORKSPACE_FILES
            ):
                return web.error_response("新文件名不合法或涉及受保护文件", status_code=400)
            if new_name == path.name:
                return web.json_response({"renamed": path.name})
            target = ws_dir / new_name
            if not self._is_within(target, ws_dir) or target.exists():
                return web.error_response("目标文件已存在或路径不合法", status_code=409)
            stat = path.stat()
            path.rename(target)
            row = self.db.get_file(ws_dir.name, name)
            if row:
                self.db.remove_file(ws_dir.name, name)
                self.db.add_file(
                    ws_dir.name,
                    new_name,
                    int(row.get("size") or stat.st_size),
                    sha256=str(row.get("sha256") or ""),
                    source=str(row.get("source") or "webui"),
                    uploader=str(row.get("uploader") or ""),
                    mtime=stat.st_mtime,
                )
            self._audit(
                "rename", workspace=ws_dir.name, filename=new_name,
                size=stat.st_size, detail=f"由 {name} 重命名",
                user_id=self._web_user_id(),
            )
            return web.json_response({"renamed": new_name})
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI 重命名失败：{e}")
            return web.error_response("重命名失败", status_code=500)

    async def web_file_upload(self, workspace: str):
        web = self._web_api()
        request = web.request
        tmp_path: Path | None = None
        try:
            ws_dir = self._web_resolve_workspace(workspace)
            if ws_dir is None:
                return web.error_response("工作区不存在", status_code=404)
            files = await request.files()
            upload = files.get("file")
            if upload is None:
                return web.error_response("缺少上传文件", status_code=400)
            existing_entries = self._received_entries(ws_dir)
            if self.max_file_count and len(existing_entries) >= self.max_file_count:
                return web.error_response(
                    f"工作区上传数量已达上限（{self.max_file_count} 个）",
                    status_code=400,
                )
            raw_name = str(getattr(upload, "filename", "") or "")
            name = self._protect_received_name(
                self._sanitize_filename(raw_name, fallback="upload.bin")
            )
            caps = [
                cap for cap in (self.max_single_file_bytes, self.max_send_bytes) if cap
            ]
            max_bytes = max(caps) if caps else None
            tmp_path = ws_dir / f".upload-{time.time_ns()}-{os.getpid()}.part"
            size = await upload.save(str(tmp_path), max_bytes=max_bytes)
            if self.max_storage_bytes:
                used = sum(existing_entries.values())
                if used + size > self.max_storage_bytes:
                    self._unlink_quiet(tmp_path)
                    return web.error_response(
                        f"工作区上传存储已达上限（{self.max_storage_mb}MB）",
                        status_code=400,
                    )
            sha256 = await asyncio.to_thread(self._sha256_file, tmp_path)
            if self.dedupe_enabled and sha256:
                existing = self.db.find_by_hash(ws_dir.name, sha256)
                if existing:
                    existing_path = ws_dir / str(existing.get("name") or "")
                    if existing_path.is_file() and not existing_path.is_symlink():
                        self._unlink_quiet(tmp_path)
                        self._audit(
                            "webui_upload_duplicate", workspace=ws_dir.name,
                            filename=name, size=size, sha256=sha256,
                            detail=f"已存在相同文件：{existing_path.name}",
                            user_id=self._web_user_id(),
                        )
                        return web.json_response(
                            {
                                "uploaded": existing_path.name,
                                "duplicate": True,
                                "size": size,
                                "sha256": sha256,
                            }
                        )
                    self.db.remove_file(ws_dir.name, str(existing.get("name") or ""))
            target = self._unique_target(ws_dir, name)
            os.replace(tmp_path, target)
            self._index_add(
                ws_dir, target.name, size, sha256=sha256,
                source="webui", uploader=self._web_user_id(),
            )
            self._audit(
                "webui_upload", workspace=ws_dir.name, filename=target.name,
                size=size, sha256=sha256, detail="WebUI 上传",
                user_id=self._web_user_id(),
            )
            return web.json_response(
                {
                    "uploaded": target.name,
                    "size": size,
                    "sha256": sha256,
                    "duplicate": False,
                }
            )
        except Exception as e:  # noqa: BLE001
            if tmp_path is not None:
                self._unlink_quiet(tmp_path)
            logger.exception(f"[file_transfer] WebUI 上传失败：{e}")
            return web.error_response("上传失败", status_code=500)

    def _cleanup_pending_uploads(self, max_age_seconds: float = 3600.0):
        now = time.time()
        for upload_id, info in list(self._pending_uploads.items()):
            try:
                if now - float(info.get("created", 0)) < max_age_seconds:
                    continue
                self._unlink_quiet(Path(info.get("path") or ""))
            except (OSError, TypeError, ValueError):
                pass
            self._pending_uploads.pop(upload_id, None)

    async def web_file_upload_chunked(self):
        """分片上传：避开 multipart/反向代理 body 限制导致的 502。"""
        web = self._web_api()
        try:
            payload = await self._web_json_payload()
            action = str(payload.get("action", "") or "")
            if action == "init":
                return self._chunk_upload_init(web, payload)
            if action == "chunk":
                return self._chunk_upload_chunk(web, payload)
            if action == "complete":
                return await self._chunk_upload_complete(web, payload)
            if action == "abort":
                return self._chunk_upload_abort(web, payload)
            return web.error_response("未知分片上传动作", status_code=400)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] 分片上传失败：{e}")
            return web.error_response("分片上传失败", status_code=500)

    def _chunk_upload_init(self, web, payload: dict):
        self._cleanup_pending_uploads()
        workspace = str(payload.get("workspace", "") or "")
        ws_dir = self._web_resolve_workspace(workspace)
        if ws_dir is None:
            return web.error_response("工作区不存在", status_code=404)
        raw_name = str(payload.get("name", "") or "")
        name = self._protect_received_name(
            self._sanitize_filename(raw_name, fallback="upload.bin")
        )
        try:
            size = int(payload.get("size", 0) or 0)
        except (TypeError, ValueError):
            return web.error_response("文件大小不合法", status_code=400)
        if size < 0:
            return web.error_response("文件大小不合法", status_code=400)
        caps = [cap for cap in (self.max_single_file_bytes, self.max_send_bytes) if cap]
        if caps and size > max(caps):
            return web.error_response(
                f"文件超过上传大小上限（{max(caps) // 1024 // 1024}MB）",
                status_code=400,
            )
        existing_entries = self._received_entries(ws_dir)
        if self.max_file_count and len(existing_entries) >= self.max_file_count:
            return web.error_response(
                f"工作区上传数量已达上限（{self.max_file_count} 个）",
                status_code=400,
            )
        if self.max_storage_bytes and sum(existing_entries.values()) + size > self.max_storage_bytes:
            return web.error_response(
                f"工作区上传存储已达上限（{self.max_storage_mb}MB）",
                status_code=400,
            )
        upload_id = uuid.uuid4().hex
        tmp_path = ws_dir / f".upload-{upload_id}.part"
        try:
            tmp_path.write_bytes(b"")
        except OSError:
            return web.error_response("创建上传临时文件失败", status_code=500)
        self._pending_uploads[upload_id] = {
            "workspace": ws_dir.name,
            "path": str(tmp_path),
            "name": name,
            "size": size,
            "received": 0,
            "chunks": 0,
            "chunk_size": 256 * 1024,
            "created": time.time(),
            "hasher": hashlib.sha256(),
            "user_id": self._web_user_id(),
        }
        return web.json_response(
            {
                "upload_id": upload_id,
                "chunk_size": 256 * 1024,
                "received": 0,
            }
        )

    def _chunk_upload_chunk(self, web, payload: dict):
        upload_id = str(payload.get("upload_id", "") or "")
        info = self._pending_uploads.get(upload_id)
        if not info:
            return web.error_response("上传会话不存在或已过期", status_code=404)
        try:
            index = int(payload.get("index", -1))
        except (TypeError, ValueError):
            return web.error_response("分片序号不合法", status_code=400)
        if index != int(info.get("chunks", 0)):
            return web.error_response("分片序号不连续，请重新上传", status_code=400)
        raw = payload.get("data", "")
        if not isinstance(raw, str):
            return web.error_response("分片数据不合法", status_code=400)
        try:
            chunk = base64.b64decode(raw, validate=True)
        except Exception:  # noqa: BLE001
            return web.error_response("分片解码失败", status_code=400)
        if len(chunk) > int(info.get("chunk_size", 0)):
            return web.error_response("分片过大", status_code=400)
        received = int(info.get("received", 0))
        total = int(info.get("size", 0))
        if received + len(chunk) > total:
            return web.error_response("分片总大小超出声明值", status_code=400)
        path = Path(str(info.get("path") or ""))
        try:
            with open(path, "ab") as file_obj:
                file_obj.write(chunk)
        except OSError:
            return web.error_response("写入分片失败", status_code=500)
        hasher = info.get("hasher")
        if hasattr(hasher, "update"):
            hasher.update(chunk)
        info["received"] = received + len(chunk)
        info["chunks"] = index + 1
        return web.json_response(
            {
                "received": info["received"],
                "size": total,
                "chunks": info["chunks"],
            }
        )

    async def _chunk_upload_complete(self, web, payload: dict):
        upload_id = str(payload.get("upload_id", "") or "")
        info = self._pending_uploads.get(upload_id)
        if not info:
            return web.error_response("上传会话不存在或已过期", status_code=404)
        if int(info.get("received", 0)) != int(info.get("size", 0)):
            return web.error_response("分片未上传完整", status_code=400)
        ws_dir = self._web_resolve_workspace(str(info.get("workspace") or ""))
        if ws_dir is None:
            self._pending_uploads.pop(upload_id, None)
            return web.error_response("工作区不存在", status_code=404)
        tmp_path = Path(str(info.get("path") or ""))
        name = str(info.get("name") or "upload.bin")
        size = int(info.get("size", 0))
        hasher = info.get("hasher")
        if hasattr(hasher, "hexdigest"):
            sha256 = hasher.hexdigest()
        else:
            try:
                sha256 = await asyncio.to_thread(self._sha256_file, tmp_path)
            except Exception:  # noqa: BLE001
                self._unlink_quiet(tmp_path)
                self._pending_uploads.pop(upload_id, None)
                return web.error_response("校验上传文件失败", status_code=500)
        if self.dedupe_enabled and sha256:
            existing = self.db.find_by_hash(ws_dir.name, sha256)
            if existing:
                existing_path = ws_dir / str(existing.get("name") or "")
                if existing_path.is_file() and not existing_path.is_symlink():
                    self._unlink_quiet(tmp_path)
                    self._pending_uploads.pop(upload_id, None)
                    self._audit(
                        "webui_upload_duplicate", workspace=ws_dir.name,
                        filename=name, size=size, sha256=sha256,
                        detail=f"已存在相同文件：{existing_path.name}",
                        user_id=str(info.get("user_id") or "webui"),
                    )
                    return web.json_response(
                        {
                            "uploaded": existing_path.name,
                            "duplicate": True,
                            "size": size,
                            "sha256": sha256,
                        }
                    )
                self.db.remove_file(ws_dir.name, str(existing.get("name") or ""))
        target = self._unique_target(ws_dir, name)
        try:
            os.replace(tmp_path, target)
        except OSError as e:  # noqa: BLE001
            self._unlink_quiet(tmp_path)
            self._pending_uploads.pop(upload_id, None)
            logger.warning(f"[file_transfer] 分片上传保存失败：{e}")
            return web.error_response("保存上传文件失败", status_code=500)
        self._pending_uploads.pop(upload_id, None)
        self._index_add(
            ws_dir, target.name, size, sha256=sha256,
            source="webui", uploader=str(info.get("user_id") or "webui"),
        )
        self._audit(
            "webui_upload", workspace=ws_dir.name, filename=target.name,
            size=size, sha256=sha256, detail="WebUI 分片上传",
            user_id=str(info.get("user_id") or "webui"),
        )
        return web.json_response(
            {
                "uploaded": target.name,
                "size": size,
                "sha256": sha256,
                "duplicate": False,
            }
        )

    def _chunk_upload_abort(self, web, payload: dict):
        upload_id = str(payload.get("upload_id", "") or "")
        info = self._pending_uploads.pop(upload_id, None)
        if info:
            self._unlink_quiet(Path(str(info.get("path") or "")))
        return web.json_response({"aborted": bool(info)})

    async def web_workspace_clear(self):
        web = self._web_api()
        try:
            payload = await self._web_json_payload()
            workspace = str(payload.get("workspace", "") or "")
            ws_dir = self._web_resolve_workspace(workspace)
            if ws_dir is None:
                return web.error_response("工作区不存在", status_code=404)
            removed, removed_bytes = self._delete_workspace_files(ws_dir)
            self._prune_index()
            self._audit(
                "workspace_clear", workspace=ws_dir.name,
                size=removed_bytes, detail={"files_removed": removed},
                user_id=self._web_user_id(),
            )
            return web.json_response(
                {"removed": removed, "removed_bytes": removed_bytes}
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI 清空工作区失败：{e}")
            return web.error_response("清空工作区失败", status_code=500)

    async def web_workspace_clean(self):
        web = self._web_api()
        try:
            payload = await self._web_json_payload()
            workspace = str(payload.get("workspace", "") or "")
            try:
                hours = float(
                    payload.get("max_age_hours", self.workspace_clean_interval_hours)
                )
            except (TypeError, ValueError):
                hours = self.workspace_clean_interval_hours
            hours = max(0.0, hours)
            removed = 0
            if workspace:
                ws_dir = self._web_resolve_workspace(workspace)
                if ws_dir is None:
                    return web.error_response("工作区不存在", status_code=404)
                removed, _ = self._delete_workspace_files(ws_dir, hours * 3600)
                self._prune_index()
                self._audit(
                    "workspace_clean", workspace=ws_dir.name,
                    detail={"files_removed": removed, "max_age_hours": hours},
                    user_id=self._web_user_id(),
                )
            else:
                removed = await asyncio.to_thread(
                    self._cleanup_workspaces, hours * 3600
                )
                self._prune_index()
                self._audit(
                    "workspace_clean",
                    detail={"files_removed": removed, "max_age_hours": hours},
                    user_id=self._web_user_id(),
                )
            return web.json_response({"removed": removed})
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI 清理工作区失败：{e}")
            return web.error_response("清理工作区失败", status_code=500)

    async def web_audit_clear(self):
        web = self._web_api()
        try:
            payload = await self._web_json_payload()
            try:
                before_days = int(payload.get("before_days", 0) or 0)
            except (TypeError, ValueError):
                before_days = 0
            cutoff = time.time() - before_days * 86400 if before_days > 0 else None
            deleted = self.db.clear_audit(cutoff)
            self._audit(
                "audit_clear",
                detail={"deleted": deleted, "before_days": before_days},
                user_id=self._web_user_id(),
            )
            return web.json_response({"deleted": deleted})
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI 清空审计失败：{e}")
            return web.error_response("清空审计失败", status_code=500)

    async def web_config_save(self):
        web = self._web_api()
        try:
            payload = await self._web_json_payload()
            config_payload = payload.get("config", payload)
            updates, errors = self._coerce_web_config(config_payload)
            if errors:
                return web.error_response("；".join(errors[:8]), status_code=400)
            if not updates:
                return web.error_response("没有可保存的配置", status_code=400)
            ok, message = self._apply_web_config(
                updates, user_id=self._web_user_id()
            )
            if not ok:
                return web.error_response(message, status_code=500)
            return web.json_response({"saved": True})
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI 保存配置失败：{e}")
            return web.error_response("保存配置失败", status_code=500)

    async def web_config_reset(self):
        web = self._web_api()
        try:
            defaults = self._schema_defaults()
            updates = {}
            for group, keys in self._config_groups().items():
                group_defaults = defaults.get(group, {})
                updates[group] = {
                    key: group_defaults[key]
                    for key in keys
                    if key in group_defaults
                }
            ok, message = self._apply_web_config(
                updates, detail="reset", user_id=self._web_user_id()
            )
            if not ok:
                return web.error_response(message, status_code=500)
            return web.json_response({"reset": True})
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI 重置配置失败：{e}")
            return web.error_response("重置配置失败", status_code=500)

    async def web_audit(self):
        web = self._web_api()
        request = web.request
        try:
            result = self.db.query_audit(
                event=str(request.query.get("event", "") or ""),
                level=str(request.query.get("level", "") or ""),
                workspace=str(request.query.get("workspace", "") or ""),
                keyword=str(request.query.get("q", "") or ""),
                limit=int(request.query.get("limit", 50) or 50),
                offset=int(request.query.get("offset", 0) or 0),
            )
            return web.json_response(result)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI audit 失败：{e}")
            return web.error_response("审计日志加载失败", status_code=500)

    async def web_quotas(self):
        web = self._web_api()
        try:
            rows = self._web_workspace_rows()
            for row in rows:
                max_count = self.max_file_count
                max_storage = self.max_storage_mb
                row["count_percent"] = (
                    min(100.0, row["upload_count"] / max_count * 100)
                    if max_count
                    else 0.0
                )
                row["storage_percent"] = (
                    min(100.0, row["upload_size"] / (max_storage * 1024 * 1024) * 100)
                    if max_storage
                    else 0.0
                )
            return web.json_response(
                {
                    "items": rows,
                    "limits": {
                        "max_file_count": self.max_file_count,
                        "max_storage_mb": self.max_storage_mb,
                        "max_single_file_mb": self.max_single_file_mb,
                        "max_send_mb": self.max_send_mb,
                        "send_rate_per_minute": self.send_rate_per_minute,
                        "receive_rate_per_minute": self.receive_rate_per_minute,
                    },
                    "transfer": {
                        "max_concurrent_transfers": self.max_concurrent_transfers,
                        "download_retry": self.download_retry,
                        "transfer_timeout_seconds": self.transfer_timeout_seconds,
                        "min_free_disk_mb": self.min_free_disk_mb,
                    },
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI quotas 失败：{e}")
            return web.error_response("配额数据加载失败", status_code=500)

    async def web_settings(self):
        web = self._web_api()
        try:
            return web.json_response(
                {
                    "version": PLUGIN_VERSION,
                    "plugin": PLUGIN_NAME,
                    "platforms": sorted(QQ_OFFICIAL_PLATFORMS | {"aiocqhttp"}),
                    "workspaces_dir": str(self.workspaces_dir),
                    "db_path": str(self.db_path),
                    "schema": self._schema_spec(),
                    "defaults": self._schema_defaults(),
                    "config": {
                        "sending": {
                            "callback_api_base": self.callback_api_base,
                            "napcat_path_mapping": self.path_mapping,
                            "max_send_mb": self.max_send_mb,
                        },
                        "receiving": {
                            "auto_save": self.auto_save,
                            "auto_save_quoted": self.auto_save_quoted,
                            "suffix_filter_enable": self.suffix_filter_enable,
                            "blocked_suffixes": sorted(self.blocked_suffixes),
                            "max_single_file_mb": self.max_single_file_mb,
                            "max_view_chars": self.max_view_chars,
                            "view_image_preview": self.view_image_preview,
                            "max_view_image_mb": self.max_view_image_mb,
                        },
                        "limits": {
                            "max_file_count": self.max_file_count,
                            "max_storage_mb": self.max_storage_mb,
                            "send_rate_per_minute": self.send_rate_per_minute,
                            "receive_rate_per_minute": self.receive_rate_per_minute,
                        },
                        "transfer": {
                            "max_concurrent_transfers": self.max_concurrent_transfers,
                            "download_retry": self.download_retry,
                            "transfer_timeout_seconds": self.transfer_timeout_seconds,
                            "min_free_disk_mb": self.min_free_disk_mb,
                        },
                        "storage": {
                            "dedupe_enabled": self.dedupe_enabled,
                            "audit_enabled": self.audit_enabled,
                            "audit_retention_days": self.audit_retention_days,
                        },
                        "permissions": {
                            "private_only": self.private_only,
                            "whitelist_private_enable": self.whitelist_private_enable,
                            "whitelist_users": sorted(self.whitelist_users),
                            "whitelist_group_enable": self.whitelist_group_enable,
                            "whitelist_groups": sorted(self.whitelist_groups),
                            "allowed_send_dirs": self.allowed_send_dirs,
                        },
                        "workspace": {
                            "workspace_auto_clean": self.workspace_auto_clean,
                            "workspace_clean_interval_hours": self.workspace_clean_interval_hours,
                        },
                    },
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] WebUI settings 失败：{e}")
            return web.error_response("运行信息加载失败", status_code=500)

    # ---------------- 接收文件 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """监听消息中的文件段，自动保存到当前会话工作区。"""
        if not self.auto_save:
            return
        allowed, _ = self._check_access(event)
        if not allowed:
            return
        try:
            segments = self._extract_file_segments(event)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 解析文件消息失败：{e}")
            return
        for seg in segments:
            try:
                await self._save_received(seg, event)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[file_transfer] 接收文件处理失败：{e}")

    def _extract_file_segments(self, event: AstrMessageEvent) -> list[File]:
        """提取需要保存的文件段；引用消息中的文件默认不处理。"""
        segments: list[File] = []
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        for seg in chain:
            if isinstance(seg, File):
                segments.append(seg)
            elif (
                self.auto_save_quoted
                and isinstance(seg, Reply)
                and getattr(seg, "chain", None)
            ):
                segments.extend(s for s in seg.chain if isinstance(s, File))
        return segments

    def _is_duplicate_url(self, url: str) -> bool:
        expire = self._recent_urls.get(url)
        now = time.time()
        if expire and expire > now:
            return True
        if expire:
            self._recent_urls.pop(url, None)
        return False

    def _mark_url(self, url: str):
        now = time.time()
        if len(self._recent_urls) > 500:
            self._recent_urls = {
                key: expire for key, expire in self._recent_urls.items() if expire > now
            }
        self._recent_urls[url] = now + URL_DEDUP_TTL

    async def _save_received(self, seg: File, event: AstrMessageEvent):
        url = str(getattr(seg, "url", "") or "").strip()
        raw_name = str(getattr(seg, "name", "") or "").strip()
        try:
            ws_dir = self.get_event_workspace_dir(event)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 获取工作区失败：{e}")
            return
        ws_name = ws_dir.name
        audit_fields = self._event_audit_fields(event)
        if not url:
            logger.info("[file_transfer] 文件段没有可下载的 URL，跳过")
            return
        if self._is_duplicate_url(url):
            logger.info("[file_transfer] 重复的文件段 URL，跳过")
            self._audit(
                "receive_duplicate_url", workspace=ws_name, filename=raw_name,
                detail="同一 URL 在去重窗口内重复出现", **audit_fields
            )
            return

        name = self._sanitize_filename(
            raw_name, fallback=f"file_{int(time.time())}"
        )
        name = self._protect_received_name(name)
        if self.suffix_filter_enable and self._is_blocked_suffix(name):
            logger.info(f"[file_transfer] 后缀被过滤：{name}")
            self._audit(
                "receive_filtered", level="warn", workspace=ws_name,
                filename=name, detail="后缀黑名单", **audit_fields
            )
            await self._notify(
                event, f"已过滤文件：{name}（该类型不允许接收）", always=True
            )
            return

        if not self._check_rate(
            f"recv:{ws_name}", self.receive_rate_per_minute
        ):
            logger.info(f"[file_transfer] 工作区 {ws_name} 接收频率超限")
            self._audit(
                "rate_limited", level="warn", workspace=ws_name,
                filename=name, detail={"scope": "receive"}, **audit_fields
            )
            await self._notify(
                event,
                f"接收太频繁啦（每分钟最多 {self.receive_rate_per_minute} 个），"
                f"已跳过：{name}",
                always=True,
            )
            return

        received = self._received_entries(ws_dir)
        max_count = self.max_file_count
        if max_count and len(received) >= max_count:
            logger.info(f"[file_transfer] 工作区 {ws_dir.name} 上传文件数量已达上限")
            self._audit(
                "receive_quota_count", level="warn", workspace=ws_name,
                filename=name, detail=f"上限 {max_count} 个", **audit_fields
            )
            await self._notify(
                event,
                f"工作区上传文件数量已达上限（{max_count} 个），已跳过：{name}",
                always=True,
            )
            return

        max_total = self.max_storage_bytes
        used = sum(received.values())
        if max_total and used >= max_total:
            logger.info(f"[file_transfer] 工作区 {ws_dir.name} 上传文件存储已达上限")
            self._audit(
                "receive_quota_storage", level="warn", workspace=ws_name,
                filename=name, detail=f"上限 {self.max_storage_mb}MB", **audit_fields
            )
            await self._notify(
                event,
                f"工作区上传文件存储已达上限（{self.max_storage_mb}MB），已跳过：{name}",
                always=True,
            )
            return

        caps = []
        if max_total:
            caps.append(max_total - used)
        if self.max_single_file_bytes:
            caps.append(self.max_single_file_bytes)
        max_bytes = min(caps) if caps else 0

        target = self._unique_target(ws_dir, name)
        tmp_path = ws_dir / f".{time.time_ns()}-{os.getpid()}.part"
        try:
            async with self._transfer_semaphore:
                size, sha256 = await self._download_with_retry(
                    url, tmp_path, max_bytes, ws_dir
                )
        except FileTooLargeError:
            self._unlink_quiet(tmp_path)
            logger.info(f"[file_transfer] 文件超过大小限制，跳过：{name}")
            self._audit(
                "receive_rejected", level="warn", workspace=ws_name,
                filename=name, detail="超过大小限制或磁盘空间不足", **audit_fields
            )
            await self._notify(
                event,
                f"文件超过大小限制或工作区剩余空间不足，已跳过：{name}",
                always=True,
            )
            return
        except asyncio.CancelledError:
            self._unlink_quiet(tmp_path)
            raise
        except Exception as e:  # noqa: BLE001
            self._unlink_quiet(tmp_path)
            logger.warning(
                f"[file_transfer] 下载文件失败 {self._safe_url_for_log(url)}："
                f"{type(e).__name__}: {e}"
            )
            self._audit(
                "receive_failed", level="error", workspace=ws_name,
                filename=name, detail=f"{type(e).__name__}: {e}", **audit_fields
            )
            await self._notify(event, f"下载文件失败：{name}", always=True)
            return

        if self.dedupe_enabled and sha256:
            existing = self.db.find_by_hash(ws_name, sha256)
            if existing:
                existing_path = ws_dir / str(existing.get("name") or "")
                try:
                    exists = existing_path.is_file() and not existing_path.is_symlink()
                except OSError:
                    exists = False
                if exists:
                    self._unlink_quiet(tmp_path)
                    self._mark_url(url)
                    self._audit(
                        "receive_duplicate", workspace=ws_name, filename=name,
                        size=size, sha256=sha256,
                        detail=f"已存在相同文件：{existing_path.name}", **audit_fields
                    )
                    logger.info(
                        f"[file_transfer] 哈希去重跳过：{name} -> {existing_path.name}"
                    )
                    await self._notify(
                        event,
                        f"检测到重复文件：{name}，工作区已有相同内容"
                        f"（{existing_path.name}），已跳过保存",
                        always=True,
                    )
                    return
                self.db.remove_file(ws_name, str(existing.get("name") or ""))

        try:
            if target.exists():
                target = self._unique_target(ws_dir, name)
            os.replace(tmp_path, target)
        except OSError as e:
            self._unlink_quiet(tmp_path)
            logger.warning(f"[file_transfer] 保存文件失败 {target}：{e}")
            self._audit(
                "receive_failed", level="error", workspace=ws_name,
                filename=name, detail=f"保存失败：{e}", **audit_fields
            )
            await self._notify(event, f"保存文件失败：{name}", always=True)
            return

        self._index_add(
            ws_dir,
            target.name,
            size,
            sha256=sha256,
            source="received",
            uploader=str(event.get_sender_id() or ""),
        )
        self._mark_url(url)
        self._audit(
            "receive_saved", workspace=ws_name, filename=target.name,
            size=size, sha256=sha256, detail="已保存到工作区", **audit_fields
        )
        logger.info(f"[file_transfer] 已保存文件至工作区：{target}（{size}B）")
        await self._notify(
            event,
            f"已收到文件：{target.name}（{self._fmt_size(size)}），"
            f"已存入工作区",
        )

    async def _notify(self, event: AstrMessageEvent, text: str, always: bool = False):
        """发送接收提示。

        默认只在私聊发送（群聊静默）；always=True 时群聊也发送（用于过滤/超限/失败等需要提示的情况）。
        """
        if event.get_group_id() and not always:
            return
        try:
            await event.send(event.chain_result([Plain(text)]))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[file_transfer] 发送提示失败：{e}")

    @staticmethod
    def _unlink_quiet(path: Path):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _unique_target(ws_dir: Path, name: str) -> Path:
        target = ws_dir / name
        if not target.exists():
            return target
        stem, ext = os.path.splitext(name)
        for index in range(1, 1000):
            candidate = ws_dir / f"{stem}_{index}{ext}"
            if not candidate.exists():
                return candidate
        return ws_dir / f"{stem}_{int(time.time())}{ext}"

    def _has_free_disk(self, directory: Path) -> bool:
        reserve = self.min_free_disk_mb * 1024 * 1024
        if reserve <= 0:
            return True
        try:
            return shutil.disk_usage(directory).free >= reserve
        except OSError:
            return True

    async def _download_with_retry(
        self, url: str, tmp_path: Path, max_bytes: int, ws_dir: Path
    ) -> tuple[int, str]:
        """带重试的下载；返回 (大小, SHA256)。超限/磁盘不足不重试。"""
        attempts = min(max(0, self.download_retry), 10) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._unlink_quiet(tmp_path)
            try:
                if not self._has_free_disk(ws_dir):
                    raise FileTooLargeError("磁盘可用空间不足")
                result = await self._download_limited(url, tmp_path, max_bytes)
                if isinstance(result, int):  # 兼容测试/旧实现
                    return result, ""
                return result
            except FileTooLargeError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt + 1 >= attempts:
                    break
                logger.warning(
                    f"[file_transfer] 下载失败，第 {attempt + 1}/{attempts} 次重试："
                    f"{type(e).__name__}: {e}"
                )
                await asyncio.sleep(min(5.0, 1.2 * (attempt + 1)))
        raise last_error or RuntimeError("下载失败")

    async def _download_limited(
        self, url: str, tmp_path: Path, max_bytes: int
    ) -> tuple[int, str]:
        """流式下载文件到临时路径，计算 SHA256；超限抛 FileTooLargeError。"""
        digest = hashlib.sha256()
        try:
            import aiohttp
        except ImportError:
            from astrbot.core.utils.io import download_file

            await download_file(url, str(tmp_path))
            size = tmp_path.stat().st_size
            if max_bytes and size > max_bytes:
                raise FileTooLargeError("文件超过大小限制")
            with open(tmp_path, "rb") as file_obj:
                for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                    digest.update(chunk)
            return size, digest.hexdigest()

        timeout = aiohttp.ClientTimeout(
            total=self.transfer_timeout_seconds,
            connect=30,
            sock_read=min(120, self.transfer_timeout_seconds),
        )
        written = 0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                declared = resp.headers.get("Content-Length", "")
                if declared.isdigit() and max_bytes and int(declared) > max_bytes:
                    raise FileTooLargeError("文件超过大小限制")
                if declared.isdigit():
                    reserve = self.min_free_disk_mb * 1024 * 1024
                    try:
                        free = shutil.disk_usage(tmp_path.parent).free
                    except OSError:
                        free = 0
                    if free - reserve < int(declared):
                        raise FileTooLargeError("磁盘可用空间不足")
                with open(tmp_path, "wb") as file_obj:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise FileTooLargeError("文件超过大小限制")
                        digest.update(chunk)
                        file_obj.write(chunk)
        return written, digest.hexdigest()

    # ---------------- 发送文件 ----------------

    @filter.command("发文件", alias={"sendfile"})
    async def on_send_file(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return

        ws_name = self._event_workspace_name(event)
        audit_fields = self._event_audit_fields(event)
        sender = str(event.get_sender_id() or "")
        if not self._check_rate(
            f"send:{self._event_umo(event)}:{sender}", self.send_rate_per_minute
        ):
            self._audit(
                "rate_limited", level="warn", workspace=ws_name,
                detail={"scope": "send"}, **audit_fields
            )
            yield event.chain_result(
                [
                    Plain(
                        f"发送太频繁啦（每分钟最多 {self.send_rate_per_minute} 次），"
                        "请稍后再试"
                    )
                ]
            )
            return

        try:
            raw = self._parse_arg(event)
        except Exception:  # noqa: BLE001
            raw = ""
        if not raw:
            yield event.chain_result(
                [
                    Plain(
                        "用法：/发文件 <序号|文件路径>，"
                        "例如 /发文件 1、/发文件 /test/1.txt 或工作区内文件名"
                    )
                ]
            )
            return

        try:
            ws_dir = self.get_event_workspace_dir(event)
        except Exception:  # noqa: BLE001
            yield event.chain_result([Plain("获取工作区失败，请稍后重试")])
            return

        index_hint = ""
        if raw.isdigit():
            # 与 /文件列表、/查看文件、/删除文件 使用同一排序（修改时间升序，序号 1 为最旧）
            index = int(raw)
            ws_files = self._sorted_workspace_files(ws_dir)
            if not ws_files:
                yield event.chain_result([Plain("工作区里还没有文件呢")])
                return
            if index < 1 or index > len(ws_files):
                yield event.chain_result(
                    [Plain(f"序号超出范围，当前工作区共 {len(ws_files)} 个文件")]
                )
                return
            path = ws_files[index - 1]
            index_hint = f"（第 {index} 个）"
        else:
            path = self._resolve_send_path(raw, ws_dir)
            if path is None:
                yield event.chain_result([Plain(f"找不到这个文件：{raw}")])
                return

        try:
            file_size = path.stat().st_size
        except OSError:
            yield event.chain_result([Plain("读取文件信息失败，请稍后重试")])
            return
        if self.max_send_bytes and file_size > self.max_send_bytes:
            self._audit(
                "send_denied", level="warn", workspace=ws_name,
                filename=path.name, size=file_size,
                detail=f"超过发送上限 {self.max_send_mb}MB", **audit_fields
            )
            yield event.chain_result(
                [Plain(f"文件太大（上限 {self.max_send_mb}MB），已拒绝发送")]
            )
            return

        send_ok, send_reason = self._send_decision(event, path)
        if not send_ok:
            self._audit(
                "send_denied", level="warn", workspace=ws_name,
                filename=path.name, size=file_size, detail=send_reason,
                **audit_fields
            )
            yield event.chain_result([Plain(send_reason)])
            return

        display_name = self._sanitize_filename(path.name, fallback="file")
        size_text = self._fmt_size(file_size)

        # QQ 官方机器人：通过官方富媒体上传接口发送本地文件
        if self._is_qq_official(event):
            if not self._qq_official_supports_file(event):
                self._audit(
                    "send_denied", level="warn", workspace=ws_name,
                    filename=path.name, size=file_size,
                    detail="QQ 官方频道消息不支持文件", **audit_fields
                )
                yield event.chain_result(
                    [
                        Plain(
                            "QQ 官方机器人的频道消息暂不支持发送文件，"
                            "请在 QQ 群聊或 C2C 私聊中发送"
                        )
                    ]
                )
                return
            platform_hard_limit = QQ_OFFICIAL_FILE_HARD_LIMIT_MB * 1024 * 1024
            if file_size > platform_hard_limit:
                self._audit(
                    "send_denied", level="warn", workspace=ws_name,
                    filename=path.name, size=file_size,
                    detail=f"超过 QQ 官方硬限制 {QQ_OFFICIAL_FILE_HARD_LIMIT_MB}MB",
                    **audit_fields
                )
                yield event.chain_result(
                    [
                        Plain(
                            f"文件超过 QQ 官方机器人文件硬限制"
                            f"（{QQ_OFFICIAL_FILE_HARD_LIMIT_MB}MB），已拒绝发送"
                        )
                    ]
                )
                return
            started = time.monotonic()
            try:
                async with self._transfer_semaphore:
                    await event.send(
                        event.chain_result([File(name=display_name, file=str(path))])
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[file_transfer] QQ 官方平台发送文件失败：{e}")
                self._audit(
                    "send_failed", level="error", workspace=ws_name,
                    filename=path.name, size=file_size,
                    detail=f"qq_official: {type(e).__name__}: {e}", **audit_fields
                )
                yield event.chain_result(
                    [
                        Plain(
                            "发送失败：请查看 AstrBot 日志，"
                            "并确认 QQ 机器人具备文件消息权限"
                        )
                    ]
                )
                return
            self._audit(
                "send_success", workspace=ws_name, filename=path.name,
                size=file_size, detail={
                    "mode": "qq_official",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }, **audit_fields
            )
            yield event.chain_result(
                [Plain(f"文件已发送：{display_name}（{size_text}）{index_hint}")]
            )
            return

        # 模式一：URL 模式（AstrBot 文件服务 + NapCat 下载）
        if self.callback_api_base:
            started = time.monotonic()
            try:
                from astrbot.core import file_token_service

                async with self._transfer_semaphore:
                    token_timeout = min(
                        3600, max(600, int(file_size / 1024 / 1024) * 5)
                    )
                    token = await file_token_service.register_file(
                        str(path), timeout=token_timeout
                    )
                    url = f"{self.callback_api_base}/api/file/{token}"
                    await event.send(
                        event.chain_result([File(name=display_name, url=url)])
                    )
                self._audit(
                    "send_success", workspace=ws_name, filename=path.name,
                    size=file_size, detail={
                        "mode": "url",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }, **audit_fields
                )
                yield event.chain_result(
                    [Plain(f"文件已发送：{display_name}（{size_text}）{index_hint}")]
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[file_transfer] URL 模式发送失败，尝试路径模式："
                    f"{type(e).__name__}: {e}"
                )
                self._audit(
                    "send_fallback", level="warn", workspace=ws_name,
                    filename=path.name, size=file_size,
                    detail=f"url: {type(e).__name__}: {e}", **audit_fields
                )

        # 模式二：路径映射模式（NapCat 与 AstrBot 共享目录）
        file_value = self._map_path(str(path))
        started = time.monotonic()
        try:
            async with self._transfer_semaphore:
                await self._send_file_segment(event, display_name, file_value)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] 路径模式发送失败：{e}")
            self._audit(
                "send_failed", level="error", workspace=ws_name,
                filename=path.name, size=file_size,
                detail=f"path: {type(e).__name__}: {e}", **audit_fields
            )
            yield event.chain_result(
                [
                    Plain(
                        "发送失败：请检查 callback_api_base / napcat_path_mapping "
                        "配置或查看 AstrBot 日志"
                    )
                ]
            )
            return
        self._audit(
            "send_success", workspace=ws_name, filename=path.name,
            size=file_size, detail={
                "mode": "path",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }, **audit_fields
        )
        yield event.chain_result(
            [Plain(f"文件已发送：{display_name}（{size_text}）{index_hint}")]
        )

    @staticmethod
    def _parse_arg(event: AstrMessageEvent) -> str:
        parts = (event.message_str or "").strip().split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
        return raw.strip('"').strip("'").strip()

    @staticmethod
    def _resolve_send_path(raw: str, ws_dir: Path) -> Path | None:
        try:
            path = Path(raw).expanduser()
        except (OSError, ValueError):
            return None
        if not path.is_absolute():
            candidate = ws_dir / raw
            try:
                path = candidate if candidate.exists() else (Path.cwd() / path)
            except OSError:
                path = Path.cwd() / path
        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            return None
        try:
            if not path.exists() or not path.is_file():
                return None
        except OSError:
            return None
        return path

    def _parse_mapping(self) -> tuple[str, str] | None:
        mapping = self.path_mapping
        if not mapping:
            return None
        if "=>" in mapping:
            frm, to = mapping.split("=>", 1)
        elif ":" in mapping:
            frm, to = mapping.rsplit(":", 1)
        else:
            return None
        frm = frm.strip().rstrip("/\\")
        to = to.strip().rstrip("/\\")
        if not frm or not to:
            return None
        return frm, to

    def _map_path(self, local_path: str) -> str:
        """把 AstrBot 侧路径按配置映射为 NapCat 侧路径；未命中映射时原样返回。"""
        parsed = self._parse_mapping()
        if not parsed:
            return local_path
        frm, to = parsed
        try:
            local = Path(local_path).resolve()
            source_root = Path(frm).resolve()
        except (OSError, RuntimeError):
            return local_path
        if not self._is_within(local, source_root):
            return local_path
        try:
            relative = local.relative_to(source_root)
        except ValueError:
            return local_path
        parts = [str(part) for part in relative.parts]
        if to.startswith("/"):
            return "/".join([to.rstrip("/")] + parts) if parts else to.rstrip("/")
        return str(Path(to).joinpath(*parts)) if parts else to

    async def _send_file_segment(self, event: AstrMessageEvent, name: str, file_value: str):
        """用原始消息段发送文件，绕过 File 组件对本地文件/URL 的转换。"""
        if self._platform_name(event) != "aiocqhttp":
            raise RuntimeError("当前平台不支持路径映射模式，请使用 URL 模式或平台官方上传")
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            raise RuntimeError("当前平台/适配器不支持路径模式发送文件")

        segment = {"type": "file", "data": {"name": name, "file": file_value}}
        group_id = str(event.get_group_id() or "")
        if group_id:
            if not group_id.isdigit():
                raise RuntimeError("群号不是纯数字，无法使用路径模式发送")
            await bot.call_action(
                "send_group_msg", group_id=int(group_id), message=[segment]
            )
        else:
            user_id = str(event.get_sender_id() or "")
            if not user_id.isdigit():
                raise RuntimeError("QQ 号不是纯数字，无法使用路径模式发送")
            await bot.call_action(
                "send_private_msg", user_id=int(user_id), message=[segment]
            )

    # ---------------- 查看文件 ----------------

    @filter.command("查看文件", alias={"catfile"})
    async def on_view_file(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return

        raw, page = self._split_view_arg(self._parse_arg(event))
        try:
            ws_dir = self.get_event_workspace_dir(event)
        except Exception:  # noqa: BLE001
            yield event.chain_result([Plain("获取工作区失败，请稍后重试")])
            return
        ws_files = self._sorted_workspace_files(ws_dir)
        target: Path | None = None
        index_hint = ""

        if not raw:
            if not ws_files:
                yield event.chain_result([Plain("工作区里还没有文件呢，先发我一个试试？")])
                return
            target = ws_files[-1]
        elif raw.isdigit():
            index = int(raw)
            if not ws_files:
                yield event.chain_result([Plain("工作区里还没有文件呢")])
                return
            if index < 1 or index > len(ws_files):
                yield event.chain_result(
                    [Plain(f"序号超出范围，当前工作区共 {len(ws_files)} 个文件")]
                )
                return
            target = ws_files[index - 1]
            index_hint = f"（第 {index} 个）"
        else:
            target_name = raw.replace("\\", "/").split("/")[-1]
            for index, candidate in enumerate(ws_files):
                if candidate.name == target_name:
                    target = candidate
                    index_hint = f"（第 {index + 1} 个）"
                    break
            if target is None:
                yield event.chain_result(
                    [
                        Plain(
                            f"当前工作区里没有「{target_name}」，"
                            "可用 /文件列表 查看，再 /查看文件 <序号>"
                        )
                    ]
                )
                return

        if await self._try_image_preview(event, target):
            self._audit(
                "view", workspace=ws_dir.name, filename=target.name,
                size=self._safe_size(target), detail={
                    "mode": "image", "page": page,
                }, **self._event_audit_fields(event)
            )
            yield event.chain_result(
                [Plain(f"已发送图片预览：{target.name}{index_hint}")]
            )
            return

        content = self._render_file(target, page)
        self._audit(
            "view", workspace=ws_dir.name, filename=target.name,
            size=self._safe_size(target), detail={"mode": "text", "page": page},
            **self._event_audit_fields(event)
        )
        if index_hint:
            content = index_hint + chr(10) + content
        yield event.chain_result([Plain(content)])

    @staticmethod
    def _split_view_arg(raw: str) -> tuple[str, int]:
        """解析 /查看文件 的 [目标] [页码]；只有末尾纯数字才视为页码。"""
        text = str(raw or "").strip()
        if not text:
            return "", 1
        parts = text.rsplit(None, 1)
        if len(parts) == 2 and parts[1].isdigit():
            page = max(1, int(parts[1]))
            return parts[0].strip(), min(page, MAX_VIEW_PAGE)
        return text, 1

    @staticmethod
    def _safe_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _detect_text_encoding(head: bytes) -> str | None:
        if not head:
            return "utf-8"
        if head.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        if head.startswith(b"\xff\xfe\x00\x00") or head.startswith(b"\x00\x00\xfe\xff"):
            return "utf-32"
        if head.startswith(b"\xff\xfe"):
            return "utf-16-le"
        if head.startswith(b"\xfe\xff"):
            return "utf-16-be"
        if b"\x00" in head:
            sample = head[:4096]
            even_nulls = sample[0::2].count(0)
            odd_nulls = sample[1::2].count(0)
            if odd_nulls > even_nulls * 2:
                return "utf-16-le"
            if even_nulls > odd_nulls * 2:
                return "utf-16-be"
            return None
        controls = sum(
            1 for byte in head if byte < 32 and byte not in (9, 10, 12, 13)
        )
        if len(head) >= 64 and controls / len(head) > 0.05:
            return None
        try:
            head.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass
        try:
            head.decode("gb18030")
            return "gb18030"
        except UnicodeDecodeError:
            return None

    def _read_text_page(self, path: Path, page: int = 1) -> dict | None:
        """按页读取文本；自动识别 UTF-8/UTF-16/GB18030，二进制返回 None。"""
        page = max(1, min(int(page or 1), MAX_VIEW_PAGE))
        try:
            size = path.stat().st_size
            with open(path, "rb") as file_obj:
                head = file_obj.read(TEXT_SNIFF_BYTES)
        except OSError:
            return None
        encoding = self._detect_text_encoding(head)
        if not encoding:
            return None
        page_size = self.max_view_chars
        char_budget = page * page_size
        read_bytes = min(
            max(char_budget * 4 + 32, 8192), max(size, 1), 8 * 1024 * 1024
        )
        try:
            with open(path, "rb") as file_obj:
                raw = file_obj.read(read_bytes)
            text = raw.decode(encoding, errors="replace").lstrip("\ufeff")
        except (OSError, LookupError):
            return None
        start = (page - 1) * page_size
        segment = text[start : start + page_size]
        has_more = len(text) > page * page_size or size > len(raw)
        return {
            "text": segment,
            "encoding": encoding,
            "page": page,
            "page_size": page_size,
            "has_more": has_more,
            "truncated": len(segment) >= page_size and has_more,
        }

    def _file_sha256(self, path: Path) -> str:
        try:
            row = self.db.get_file(path.parent.name, path.name)
            if row:
                return str(row.get("sha256") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _render_file(self, path: Path, page: int = 1) -> str:
        """渲染文件：文本支持编码识别和分页，二进制显示元信息。"""
        try:
            size = path.stat().st_size
            mtime = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)
            )
        except OSError:
            return "文件读取失败"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        sha = self._file_sha256(path)
        meta = [
            f"【{path.name}】",
            f"大小：{self._fmt_size(size)}",
            f"类型：{mime}",
            f"修改：{mtime}",
        ]
        if sha:
            meta.append(f"SHA256：{sha[:16]}…")

        text_page = self._read_text_page(path, page)
        if text_page is None:
            meta.append("内容：二进制文件，已显示元信息")
            return chr(10).join(meta)

        meta.append(
            f"编码：{text_page['encoding']}　第 {text_page['page']} 页"
            f"（每页最多 {text_page['page_size']} 字符）"
        )
        body = text_page["text"]
        if not body:
            body = "（本页没有更多内容）"
        lines = meta + ["", body]
        if text_page["has_more"]:
            lines.append("")
            lines.append(
                f"……（内容过长已截断，还有更多；发送 "
                f"/查看文件 {path.name} {text_page['page'] + 1} 继续）"
            )
        return chr(10).join(lines)

    async def _try_image_preview(self, event: AstrMessageEvent, path: Path) -> bool:
        if not self.view_image_preview:
            return False
        if path.suffix.lower() not in VIEW_IMAGE_SUFFIXES:
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if self.max_view_image_bytes and size > self.max_view_image_bytes:
            return False
        try:
            await event.send(event.chain_result([Image.fromFileSystem(str(path))]))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 图片预览发送失败，回退文本：{e}")
            return False

    # ---------------- 文件列表 ----------------

    @filter.command("文件列表", alias={"filelist"})
    async def on_file_list(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return

        arg = self._parse_arg(event)
        group_id = event.get_group_id()
        header_tip = (
            f"本群（{group_id}）共享工作区文件：" if group_id else "你的独立工作区文件："
        )

        if arg:
            clean_arg = arg.strip().strip("@").strip()
            if not WORKSPACE_ID_RE.fullmatch(clean_arg):
                yield event.chain_result(
                    [
                        Plain(
                            "参数格式不正确，管理员用法："
                            "/文件列表 <QQ号/平台用户ID>"
                        )
                    ]
                )
                return
            if not self.is_astrbot_admin(event):
                yield event.chain_result(
                    [Plain("只有管理员才能查询指定用户的工作区文件哦")]
                )
                return
            target_ws_dir = self.find_user_workspace_dir(clean_arg)
            if target_ws_dir is None:
                yield event.chain_result(
                    [Plain(f"用户 [{clean_arg}] 的工作区文件：\n还没有任何文件呢")]
                )
                return
            header_tip = f"用户 [{clean_arg}] 的工作区文件："
        else:
            try:
                target_ws_dir = self.get_event_workspace_dir(event)
            except Exception:  # noqa: BLE001
                yield event.chain_result([Plain("获取工作区失败，请稍后重试")])
                return

        ws_files = self._sorted_workspace_files(target_ws_dir)
        if not ws_files:
            yield event.chain_result([Plain(f"{header_tip}\n还没有任何文件呢")])
            return

        lines = [header_tip]
        start = max(0, len(ws_files) - 15)
        for index in range(start, len(ws_files)):
            path = ws_files[index]
            mtime_str = time.strftime(
                "%m-%d %H:%M", time.localtime(self._safe_mtime(path))
            )
            try:
                size_text = self._fmt_size(path.stat().st_size)
            except OSError:
                size_text = "?"
            lines.append(f"{index + 1}. {path.name}（{size_text}，{mtime_str}）")
        lines.append("可用 /查看文件 <序号> 查看内容")
        yield event.chain_result([Plain("\n".join(lines))])

    # ---------------- 删除文件 ----------------

    @filter.command("删除文件", alias={"delfile"})
    async def on_delete_file(self, event: AstrMessageEvent):
        """删除工作区里已有的文件（支持序号或文件名）。"""
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return

        raw = self._parse_arg(event)
        if not raw:
            yield event.chain_result(
                [
                    Plain(
                        "用法：/删除文件 <序号|文件名>，"
                        "例如 /删除文件 1 或 /删除文件 test.txt"
                    )
                ]
            )
            return

        try:
            ws_dir = self.get_event_workspace_dir(event)
        except Exception:  # noqa: BLE001
            yield event.chain_result([Plain("获取工作区失败，请稍后重试")])
            return
        ws_name = ws_dir.name
        audit_fields = self._event_audit_fields(event)

        index_hint = ""
        if raw.isdigit():
            # 与 /查看文件、/文件列表 使用同一排序（按修改时间升序，序号 1 为最旧）
            index = int(raw)
            ws_files = self._sorted_workspace_files(ws_dir)
            if not ws_files:
                yield event.chain_result([Plain("工作区里还没有文件呢")])
                return
            if index < 1 or index > len(ws_files):
                yield event.chain_result(
                    [Plain(f"序号超出范围，当前工作区共 {len(ws_files)} 个文件")]
                )
                return
            target = ws_files[index - 1]
            index_hint = f"（第 {index} 个）"
        else:
            safe_name = raw.replace("\\", "/").split("/")[-1].strip()
            if not safe_name or safe_name in {".", ".."}:
                yield event.chain_result([Plain("文件名不太对哦")])
                return
            target = ws_dir / safe_name

        if target.name.lower() in PROTECTED_WORKSPACE_FILES:
            self._audit(
                "delete_denied", level="warn", workspace=ws_name,
                filename=target.name, detail="受保护文件", **audit_fields
            )
            yield event.chain_result([Plain("该文件受保护，不能删除")])
            return

        try:
            if not target.exists() or not target.is_file():
                yield event.chain_result([Plain(f"没找到这个文件：{target.name}")])
                return
            size = target.stat().st_size
            target.unlink()
        except OSError as e:
            logger.error(f"[file_transfer] 删除文件失败 {target}: {e}")
            self._audit(
                "delete_failed", level="error", workspace=ws_name,
                filename=target.name, detail=str(e), **audit_fields
            )
            yield event.chain_result([Plain("删除失败了，请查看 AstrBot 日志")])
            return

        self._index_remove(ws_dir, target.name)
        self._audit(
            "delete", workspace=ws_name, filename=target.name,
            size=size, detail="删除工作区文件", **audit_fields
        )
        logger.info(f"[file_transfer] 已删除文件：{target}")
        yield event.chain_result([Plain(f"已删除：{target.name}{index_hint}")])

    # ---------------- 工具方法 ----------------

    @staticmethod
    def _safe_url_for_log(url: str) -> str:
        """日志中脱敏 URL：去掉 query，只保留 scheme、host 与 path。"""
        try:
            from urllib.parse import urlsplit

            parsed = urlsplit(str(url))
            if not parsed.scheme or not parsed.netloc:
                return "<url>"
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except Exception:  # noqa: BLE001
            return "<url>"

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        return f"{size / 1024 / 1024:.1f}MB"
