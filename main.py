"""文件收发工具 v1.5.2

功能：
1. /发文件 <路径>           把本地文件发送到当前会话（URL 模式 / 路径映射模式）
2. 自动接收                 静默监听消息中的文件段，自动下载保存到当前会话工作区
3. /查看文件 [序号|文件名]  查看工作区内的文件内容（限长读取）
4. /文件列表 [QQ号]         查询当前工作区文件；管理员可指定 QQ 号查询
5. /删除文件 <文件名>       删除工作区里的文件
6. 权限控制                 仅私聊收发 / 群号与 QQ 白名单；普通用户仅能发送工作区与配置目录内的文件
7. 定时清理                 默认开启，清理所有 QQ 用户/群聊工作区中的超期文件（含任务脚本）
8. 后缀过滤                 默认拦截可执行 / 脚本文件，可自定义后缀黑名单
9. 容量限制                 只统计经插件接收的文件：数量 / 总存储 / 单文件 / 发送大小，0 表示不限制
10. 工作区                 直接使用 AstrBot 会话工作区：私聊按 QQ 号、群聊按群号，群成员共享群工作区

说明：
- AstrBot 管理员可以发送任意可读文件（含 AstrBot 数据目录、系统文件等），请只授予可信人员；
- 普通用户只能发送本人工作区或管理员配置目录内的文件；
- 工作区中的 AI 任务脚本等文件不计入数量/容量上限，只会被定时清理；
- 接收文件名经过清洗，且不会覆盖 EXTRA_PROMPT.md 等受保护文件；
- 接收成功提示只在私聊发送；被过滤/超限/下载失败等提示在群聊也会发送。
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import time
import unicodedata
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

PLUGIN_VERSION = "1.5.2"
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
        self.data_dir = self._resolve_data_dir()
        self.workspaces_dir = self._resolve_workspaces_dir()
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.received_index_path = self.data_dir / "received_index.json"

        self._received_index: dict[str, dict[str, dict]] = self._load_index()
        self._clean_task = None
        self._last_clean_ts = 0.0
        self._recent_urls: dict[str, float] = {}

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

    # ---------------- 配置读取 ----------------

    @property
    def callback_api_base(self) -> str:
        return str(self.config.get("callback_api_base", "") or "").strip().rstrip("/")

    @property
    def path_mapping(self) -> str:
        return str(self.config.get("napcat_path_mapping", "") or "").strip()

    @property
    def auto_save(self) -> bool:
        return bool(self.config.get("auto_save", True))

    @property
    def auto_save_quoted(self) -> bool:
        return bool(self.config.get("auto_save_quoted", False))

    @property
    def max_view_chars(self) -> int:
        try:
            val = int(self.config.get("max_view_chars", DEFAULT_MAX_VIEW_CHARS))
        except (TypeError, ValueError):
            return DEFAULT_MAX_VIEW_CHARS
        return val if val > 0 else DEFAULT_MAX_VIEW_CHARS

    @property
    def private_only(self) -> bool:
        return bool(self.config.get("private_only", False))

    @property
    def whitelist_enable(self) -> bool:
        return bool(self.config.get("whitelist_enable", False))

    @property
    def whitelist_groups(self) -> set:
        return {
            str(x).strip()
            for x in (self.config.get("whitelist_groups") or [])
            if str(x).strip()
        }

    @property
    def whitelist_users(self) -> set:
        return {
            str(x).strip()
            for x in (self.config.get("whitelist_users") or [])
            if str(x).strip()
        }

    @property
    def suffix_filter_enable(self) -> bool:
        return bool(self.config.get("suffix_filter_enable", True))

    @property
    def blocked_suffixes(self) -> set:
        result = set()
        for x in (self.config.get("blocked_suffixes") or []):
            s = str(x).strip().lower()
            if not s:
                continue
            if not s.startswith("."):
                s = "." + s
            result.add(s)
        return result

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
        return self._positive_int(self.config.get("max_file_count", 10), 10, True)

    @property
    def max_storage_mb(self) -> int:
        """每个工作区内上传文件的总大小上限（MB），0 表示不限制。"""
        return self._positive_int(self.config.get("max_storage_mb", 10), 10, True)

    @property
    def max_storage_bytes(self) -> int:
        return self.max_storage_mb * 1024 * 1024

    @property
    def max_single_file_mb(self) -> int:
        """单个接收文件大小上限（MB），0 表示不限制。"""
        return self._positive_int(self.config.get("max_single_file_mb", 50), 50, True)

    @property
    def max_single_file_bytes(self) -> int:
        return self.max_single_file_mb * 1024 * 1024

    @property
    def max_send_mb(self) -> int:
        """发送文件大小上限（MB），0 表示不限制。"""
        return self._positive_int(self.config.get("max_send_mb", 100), 100, True)

    @property
    def max_send_bytes(self) -> int:
        return self.max_send_mb * 1024 * 1024

    @property
    def allowed_send_dirs(self) -> list[str]:
        dirs = []
        for x in (self.config.get("allowed_send_dirs") or []):
            s = str(x).strip()
            if s:
                dirs.append(s)
        return dirs

    @property
    def workspace_auto_clean(self) -> bool:
        return bool(self.config.get("workspace_auto_clean", True))

    @property
    def workspace_clean_interval_hours(self) -> float:
        try:
            val = float(self.config.get("workspace_clean_interval_hours", 1))
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

        if self.whitelist_enable:
            if is_private:
                sender = str(event.get_sender_id())
                if sender not in self.whitelist_users:
                    return False, "你不在文件收发白名单里，暂时不能用这个功能"
            else:
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

    # ---------------- 接收索引（只统计上传文件） ----------------

    def _load_index(self) -> dict:
        try:
            if self.received_index_path.is_file():
                data = json.loads(self.received_index_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        str(ws): dict(entries)
                        for ws, entries in data.items()
                        if isinstance(entries, dict)
                    }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 读取接收索引失败：{e}")
        return {}

    def _save_index(self):
        try:
            self.received_index_path.parent.mkdir(parents=True, exist_ok=True)
            self.received_index_path.write_text(
                json.dumps(self._received_index, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 保存接收索引失败：{e}")

    @staticmethod
    def _workspace_key(ws_dir: Path) -> str:
        return ws_dir.name

    def _received_entries(self, ws_dir: Path) -> dict[str, int]:
        """返回该工作区中仍存在的“上传文件” {文件名: 大小}，顺带清理失效条目。"""
        key = self._workspace_key(ws_dir)
        entries = self._received_index.get(key)
        if not entries:
            return {}
        result = {}
        changed = False
        for name, info in list(entries.items()):
            path = ws_dir / str(name)
            try:
                if path.is_file() and not path.is_symlink():
                    size = path.stat().st_size
                    result[name] = size
                    if not isinstance(info, dict) or info.get("size") != size:
                        changed = True
                else:
                    entries.pop(name, None)
                    changed = True
            except OSError:
                entries.pop(name, None)
                changed = True
        if not entries:
            self._received_index.pop(key, None)
        if changed:
            self._save_index()
        return result

    def _received_storage_bytes(self, ws_dir: Path) -> int:
        return sum(self._received_entries(ws_dir).values())

    def _index_add(self, ws_dir: Path, filename: str, size: int):
        key = self._workspace_key(ws_dir)
        self._received_index.setdefault(key, {})[filename] = {
            "size": int(size),
            "ts": time.time(),
        }
        self._save_index()

    def _index_remove(self, ws_dir: Path, filename: str):
        key = self._workspace_key(ws_dir)
        entries = self._received_index.get(key)
        if entries and filename in entries:
            entries.pop(filename, None)
            if not entries:
                self._received_index.pop(key, None)
            self._save_index()

    def _prune_index(self):
        changed = False
        for key in list(self._received_index.keys()):
            ws_dir = self.workspaces_dir / key
            entries = self._received_index.get(key) or {}
            for name in list(entries.keys()):
                try:
                    if not (ws_dir / str(name)).is_file():
                        entries.pop(name, None)
                        changed = True
                except OSError:
                    entries.pop(name, None)
                    changed = True
            if not entries:
                self._received_index.pop(key, None)
                changed = True
        if changed:
            self._save_index()

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
                        await asyncio.to_thread(self._cleanup_workspaces, interval)
                        self._prune_index()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[file_transfer] 自动清理出错：{e}")
            await asyncio.sleep(CLEAN_CHECK_INTERVAL)

    def _cleanup_workspaces(self, max_age_seconds: float):
        """删除 QQ 用户/群聊工作区中修改时间超过 max_age_seconds 的文件。"""
        removed = 0
        now = time.time()
        try:
            entities = list(self.workspaces_dir.iterdir())
        except OSError as e:
            logger.warning(f"[file_transfer] 遍历工作区根目录失败：{e}")
            return

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

    async def terminate(self):
        if self._clean_task:
            self._clean_task.cancel()
            try:
                await self._clean_task
            except BaseException:  # noqa: BLE001
                pass
            self._clean_task = None

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
        if not url:
            logger.info("[file_transfer] 文件段没有可下载的 URL，跳过")
            return
        if self._is_duplicate_url(url):
            logger.info("[file_transfer] 重复的文件段 URL，跳过")
            return

        name = self._sanitize_filename(
            raw_name, fallback=f"file_{int(time.time())}"
        )
        name = self._protect_received_name(name)
        if self.suffix_filter_enable and self._is_blocked_suffix(name):
            logger.info(f"[file_transfer] 后缀被过滤：{name}")
            await self._notify(
                event, f"已过滤文件：{name}（该类型不允许接收）", always=True
            )
            return

        try:
            ws_dir = self.get_event_workspace_dir(event)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[file_transfer] 获取工作区失败：{e}")
            return

        received = self._received_entries(ws_dir)
        max_count = self.max_file_count
        if max_count and len(received) >= max_count:
            logger.info(f"[file_transfer] 工作区 {ws_dir.name} 上传文件数量已达上限")
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
            size = await self._download_limited(url, tmp_path, max_bytes)
        except FileTooLargeError:
            self._unlink_quiet(tmp_path)
            logger.info(f"[file_transfer] 文件超过大小限制，跳过：{name}")
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
            await self._notify(event, f"下载文件失败：{name}", always=True)
            return

        try:
            if target.exists():
                target = self._unique_target(ws_dir, name)
            os.replace(tmp_path, target)
        except OSError as e:
            self._unlink_quiet(tmp_path)
            logger.warning(f"[file_transfer] 保存文件失败 {target}：{e}")
            await self._notify(event, f"保存文件失败：{name}", always=True)
            return

        self._index_add(ws_dir, target.name, size)
        self._mark_url(url)
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

    async def _download_limited(self, url: str, tmp_path: Path, max_bytes: int) -> int:
        """流式下载文件到临时路径，超过 max_bytes 时中止并抛 FileTooLargeError。"""
        try:
            import aiohttp
        except ImportError:
            from astrbot.core.utils.io import download_file

            await download_file(url, str(tmp_path))
            size = tmp_path.stat().st_size
            if max_bytes and size > max_bytes:
                raise FileTooLargeError("文件超过大小限制")
            return size

        timeout = aiohttp.ClientTimeout(total=1800, connect=30, sock_read=120)
        written = 0
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                declared = resp.headers.get("Content-Length", "")
                if declared.isdigit() and max_bytes and int(declared) > max_bytes:
                    raise FileTooLargeError("文件超过大小限制")
                with open(tmp_path, "wb") as file_obj:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise FileTooLargeError("文件超过大小限制")
                        file_obj.write(chunk)
        return written

    # ---------------- 发送文件 ----------------

    @filter.command("发文件", alias={"sendfile"})
    async def on_send_file(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
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
            yield event.chain_result(
                [Plain(f"文件太大（上限 {self.max_send_mb}MB），已拒绝发送")]
            )
            return

        send_ok, send_reason = self._send_decision(event, path)
        if not send_ok:
            yield event.chain_result([Plain(send_reason)])
            return

        display_name = self._sanitize_filename(path.name, fallback="file")
        size_text = self._fmt_size(file_size)

        # 模式一：URL 模式（AstrBot 文件服务 + NapCat 下载）
        if self.callback_api_base:
            try:
                from astrbot.core import file_token_service

                token = await file_token_service.register_file(str(path), timeout=600)
                url = f"{self.callback_api_base}/api/file/{token}"
                await event.send(
                    event.chain_result([File(name=display_name, url=url)])
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

        # 模式二：路径映射模式（NapCat 与 AstrBot 共享目录）
        file_value = self._map_path(str(path))
        try:
            await self._send_file_segment(event, display_name, file_value)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[file_transfer] 路径模式发送失败：{e}")
            yield event.chain_result(
                [
                    Plain(
                        "发送失败：请检查 callback_api_base / napcat_path_mapping "
                        "配置或查看 AstrBot 日志"
                    )
                ]
            )
            return
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

        raw = self._parse_arg(event)
        try:
            ws_dir = self.get_event_workspace_dir(event)
        except Exception:  # noqa: BLE001
            yield event.chain_result([Plain("获取工作区失败，请稍后重试")])
            return
        ws_files = self._sorted_workspace_files(ws_dir)

        if not raw:
            if not ws_files:
                yield event.chain_result([Plain("工作区里还没有文件呢，先发我一个试试？")])
                return
            yield event.chain_result([Plain(self._render_file(ws_files[-1]))])
            return

        if raw.isdigit():
            index = int(raw)
            if not ws_files:
                yield event.chain_result([Plain("工作区里还没有文件呢")])
                return
            if index < 1 or index > len(ws_files):
                yield event.chain_result(
                    [Plain(f"序号超出范围，当前工作区共 {len(ws_files)} 个文件")]
                )
                return
            yield event.chain_result([Plain(self._render_file(ws_files[index - 1]))])
            return

        target_name = raw.replace("\\", "/").split("/")[-1]
        for index, candidate in enumerate(ws_files):
            if candidate.name == target_name:
                content = self._render_file(candidate)
                tip = f"（第 {index + 1} 个，可用 /查看文件 {index + 1} 查看）"
                yield event.chain_result([Plain(tip + chr(10) + content)])
                return

        yield event.chain_result(
            [
                Plain(
                    f"当前工作区里没有「{target_name}」，"
                    "可用 /文件列表 查看，再 /查看文件 <序号>"
                )
            ]
        )

    def _render_file(self, path: Path) -> str:
        """渲染文件内容：二进制只显示元信息，文本限长读取。"""
        try:
            size = path.stat().st_size
        except OSError:
            return "文件读取失败"
        try:
            with open(path, "rb") as file_obj:
                head = file_obj.read(TEXT_SNIFF_BYTES)
        except OSError:
            return "读取失败"

        is_text = b"\x00" not in head
        if is_text:
            try:
                head.decode("utf-8")
            except UnicodeDecodeError:
                is_text = False

        if not is_text:
            return f"【{path.name}】\n类型：二进制文件\n大小：{self._fmt_size(size)}"

        limit = self.max_view_chars
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as file_obj:
                text = file_obj.read(limit + 1)
        except OSError:
            return "读取失败"
        if len(text) > limit:
            text = text[:limit] + "\n……（内容过长已截断）"
        return f"【{path.name}】\n大小：{self._fmt_size(size)}\n{text}"

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
            if not clean_arg.isdigit():
                yield event.chain_result(
                    [Plain("参数格式不正确，管理员用法：/文件列表 <QQ号>")]
                )
                return
            if not self.is_astrbot_admin(event):
                yield event.chain_result(
                    [Plain("只有管理员才能查询指定 QQ 号的工作区文件哦")]
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
            yield event.chain_result([Plain("该文件受保护，不能删除")])
            return

        try:
            if not target.exists() or not target.is_file():
                yield event.chain_result([Plain(f"没找到这个文件：{target.name}")])
                return
            target.unlink()
        except OSError as e:
            logger.error(f"[file_transfer] 删除文件失败 {target}: {e}")
            yield event.chain_result([Plain("删除失败了，请查看 AstrBot 日志")])
            return

        self._index_remove(ws_dir, target.name)
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
