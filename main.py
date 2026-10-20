"""文件收发工具

功能：
1. /发文件 <路径>  把本地文件发送到当前会话（URL 模式 / 路径映射模式）
2. 自动接收        静默监听消息中的文件段，自动下载保存
3. /查看文件 [序号|路径]  查看最近收到的文件或任意本地文本文件
4. /文件列表       列出最近收到的文件
5. /删除文件 <文件名>  删除 received 目录里已收到的文件
6. 权限控制        仅私聊收发 / 群号与 QQ 白名单
7. 自动清理        按设定间隔自动删除 received 目录里超期的文件
8. 后缀过滤        默认拦截可执行 / 脚本文件，可自定义后缀黑名单
9. 容量限制        限制接收文件数量与总存储大小，超限自动跳过
"""

import asyncio
import json
import os
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.io import download_file

MAX_RECORDS = 50
MAX_VIEW_CHARS_DEFAULT = 1500
TEXT_SNIFF_BYTES = 4096

@register(
    "file_transfer",
    "shuran",
    "文件收发工具：把本地文件发到 QQ 会话，自动保存收到的文件并查看内容",
    "1.3.2",
)
class FileTransferPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir("file_transfer")
        self.received_dir = self.data_dir / "received"
        self.received_dir.mkdir(parents=True, exist_ok=True)
        self.records_file = self.data_dir / "received_records.json"
        self.received_files: list[dict] = self._load_records()
        self._last_clean_ts = 0.0
        self._clean_task = None
        try:
            self._clean_task = asyncio.create_task(self._cleanup_loop())
        except RuntimeError:
            pass
        logger.info(f"[file_transfer] 已加载，接收目录：{self.received_dir}")

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
    def max_view_chars(self) -> int:
        try:
            return int(self.config.get("max_view_chars", MAX_VIEW_CHARS_DEFAULT))
        except (TypeError, ValueError):
            return MAX_VIEW_CHARS_DEFAULT

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
    def auto_clean(self) -> bool:
        return bool(self.config.get("auto_clean", False))

    @property
    def clean_interval_hours(self) -> float:
        try:
            val = float(self.config.get("clean_interval_hours", 1))
        except (TypeError, ValueError):
            val = 1.0
        return val if val > 0 else 1.0

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

    @property
    def max_file_count(self) -> int:
        try:
            val = int(self.config.get("max_file_count", 10))
        except (TypeError, ValueError):
            val = 10
        return val if val > 0 else 10

    @property
    def max_storage_mb(self) -> int:
        try:
            val = int(self.config.get("max_storage_mb", 10))
        except (TypeError, ValueError):
            val = 10
        return val if val > 0 else 10

    @property
    def max_storage_bytes(self) -> int:
        return self.max_storage_mb * 1024 * 1024

    @property
    def send_restrict_paths(self) -> bool:
        return bool(self.config.get("send_restrict_paths", False))

    @property
    def allowed_send_dirs(self) -> list:
        dirs = []
        for x in (self.config.get("allowed_send_dirs") or []):
            s = str(x).strip()
            if s:
                dirs.append(s)
        # 接收目录始终允许发送
        dirs.append(str(self.received_dir))
        return dirs

    # ---------------- 权限控制 ----------------

    def _check_access(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查当前会话是否允许收发文件。

        返回 (是否允许, 拒绝原因)。允许时拒绝原因为空字符串。
        """
        group_id = event.get_group_id()
        is_private = not group_id

        # 仅私聊模式：群聊一律拒绝
        if self.private_only and not is_private:
            return False, "已开启「仅私聊收发文件」，群聊里不能用哦"

        # 白名单模式：仅白名单内的群 / 用户可用
        if self.whitelist_enable:
            if is_private:
                sender = str(event.get_sender_id())
                if sender not in self.whitelist_users:
                    return False, "你不在文件收发白名单里，暂时不能用这个功能"
            else:
                if str(group_id) not in self.whitelist_groups:
                    return False, "本群不在文件收发白名单里，暂时不能用这个功能"

        return True, ""

    # ---------------- 记录持久化 ----------------

    def _load_records(self) -> list[dict]:
        try:
            if self.records_file.exists():
                data = json.loads(self.records_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[-MAX_RECORDS:]
        except Exception as e:
            logger.warning(f"[file_transfer] 读取接收记录失败：{e}")
        return []

    def _save_records(self):
        try:
            self.records_file.write_text(
                json.dumps(self.received_files[-MAX_RECORDS:], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[file_transfer] 写入接收记录失败：{e}")

    # ---------------- 自动清理 ----------------

    async def _cleanup_loop(self):
        """后台定时清理：每隔一段时间删除 received 目录里超期的文件。"""
        while True:
            try:
                if self.auto_clean:
                    interval = self.clean_interval_hours * 3600
                    now = time.time()
                    if now - self._last_clean_ts >= interval:
                        self._cleanup_once(interval)
                        self._last_clean_ts = now
            except Exception as e:
                logger.warning(f"[file_transfer] 自动清理出错：{e}")
            await asyncio.sleep(60)

    def _cleanup_once(self, max_age_seconds: float):
        """删除 received 目录中修改时间超过 max_age_seconds 的文件，并同步记录。"""
        now = time.time()
        removed = []
        try:
            entries = list(self.received_dir.iterdir())
        except Exception as e:
            logger.warning(f"[file_transfer] 遍历接收目录失败：{e}")
            return
        for p in entries:
            if not p.is_file():
                continue
            try:
                age = now - p.stat().st_mtime
            except Exception:
                continue
            if age >= max_age_seconds:
                try:
                    p.unlink()
                    removed.append(p.name)
                except Exception as e:
                    logger.warning(f"[file_transfer] 清理文件失败 {p}: {e}")
        if not removed:
            return
        removed_set = set(removed)
        kept = []
        for r in self.received_files:
            name = r.get("name") or ""
            path = r.get("path") or ""
            if name in removed_set:
                continue
            if path and not Path(path).exists():
                continue
            kept.append(r)
        self.received_files = kept
        self._save_records()
        logger.info(f"[file_transfer] 自动清理删除 {len(removed)} 个文件：{removed}")

    def _dir_total_size(self) -> int:
        """统计 received 目录当前占用的总字节数。"""
        total = 0
        try:
            for p in self.received_dir.iterdir():
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[file_transfer] 统计目录大小失败：{e}")
        return total

    def _is_sensitive_path(self, path: Path) -> bool:
        """判断路径是否落在系统 / 用户敏感目录下，防止读取或外发密钥、配置等。"""
        try:
            resolved = Path(path).resolve()
        except Exception:
            resolved = path
        home = Path.home()
        candidates = [
            "/etc", "/proc", "/sys", "/boot", "/dev",
            "/var/log", "/root/.ssh",
            str(home / ".ssh"), str(home / ".aws"),
            str(home / ".gnupg"), str(home / ".config"),
        ]
        for s in candidates:
            try:
                sp = Path(s).resolve()
            except Exception:
                sp = Path(s)
            if resolved == sp or str(resolved).startswith(str(sp) + os.sep):
                return True
        return False

    @staticmethod
    def _is_within(path: Path, base: Path) -> bool:
        """判断 path 是否位于 base 目录内（含自身）。"""
        try:
            rp = Path(path).resolve()
            rb = Path(base).resolve()
        except Exception:
            return False
        return rp == rb or str(rp).startswith(str(rb) + os.sep)

    async def terminate(self):
        if self._clean_task:
            self._clean_task.cancel()
            try:
                await self._clean_task
            except BaseException:
                pass
            self._clean_task = None

    # ---------------- 接收文件 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """静默监听：检测到文件段就自动保存并提示。"""
        if not self.auto_save:
            return
        # 权限检查：不通过则静默跳过，避免刷屏打扰
        allowed, _ = self._check_access(event)
        if not allowed:
            return
        try:
            segments = []
            chain = event.message_obj.message or []
            for seg in chain:
                if isinstance(seg, File):
                    segments.append(seg)
                elif isinstance(seg, Reply) and getattr(seg, "chain", None):
                    for sub in seg.chain:
                        if isinstance(sub, File):
                            segments.append(sub)
            for seg in segments:
                await self._save_received(seg, event)
        except Exception as e:
            logger.warning(f"[file_transfer] 接收文件处理失败：{e}")

    async def _save_received(self, seg: File, event: AstrMessageEvent):
        url = getattr(seg, "url", "") or ""
        name = getattr(seg, "name", "") or ""
        if not url:
            logger.info("[file_transfer] 文件段没有可下载的 URL，跳过")
            return

        is_private = not event.get_group_id()

        async def _notify(text: str):
            # 仅私聊提醒，群聊静默跳过，避免刷屏
            if not is_private:
                return
            try:
                await event.send(event.chain_result([Plain(text)]))
            except Exception:
                pass

        # 1) 后缀过滤：命中黑名单直接拦截，不下载
        if self.suffix_filter_enable:
            suffix = os.path.splitext(name)[1].lower()
            if suffix and suffix in self.blocked_suffixes:
                logger.info(f"[file_transfer] 后缀被过滤：{name}")
                await _notify(f"已过滤文件：{name}（{suffix} 类型不允许接收）")
                return

        # 2) 数量上限：达到上限则跳过
        if len(self.received_files) >= self.max_file_count:
            logger.info("[file_transfer] 文件数量已达上限，跳过")
            await _notify(f"文件池已满（上限 {self.max_file_count} 个），已跳过：{name}")
            return

        # 3) 存储上限：下载前预检当前占用
        if self._dir_total_size() >= self.max_storage_bytes:
            logger.info("[file_transfer] 存储已达上限，跳过")
            await _notify(f"存储已满（上限 {self.max_storage_mb}MB），已跳过：{name}")
            return

        safe_name = os.path.basename(name) or f"file_{int(time.time())}"
        target = self.received_dir / safe_name
        if target.exists():
            stem, suffix = os.path.splitext(safe_name)
            target = self.received_dir / f"{stem}_{int(time.time())}{suffix}"
        try:
            await download_file(url, str(target))
        except Exception as e:
            logger.warning(f"[file_transfer] 下载文件失败 {url}: {e}")
            return
        if not target.exists():
            return

        # 3b) 存储上限：下载后复核，超限则删除刚下载的文件
        if self._dir_total_size() > self.max_storage_bytes:
            try:
                target.unlink()
            except Exception:
                pass
            logger.info("[file_transfer] 下载后超出存储上限，已删除")
            await _notify(f"存储已满（上限 {self.max_storage_mb}MB），已跳过：{name}")
            return
        try:
            sender = event.get_sender_name()
        except Exception:
            sender = ""
        record = {
            "name": target.name,
            "path": str(target),
            "size": target.stat().st_size,
            "time": time.strftime("%m-%d %H:%M"),
            "from": sender,
        }
        self.received_files.append(record)
        self.received_files = self.received_files[-MAX_RECORDS:]
        self._save_records()
        logger.info(f"[file_transfer] 已保存文件：{target}")
        try:
            await event.send(
                event.chain_result(
                    [Plain(f"已收到文件：{target.name}（{self._fmt_size(record['size'])}），/查看文件 可以看内容")]
                )
            )
        except Exception as e:
            logger.warning(f"[file_transfer] 发送接收提示失败：{e}")

    # ---------------- 发送文件 ----------------

    @filter.command("发文件", alias={"sendfile"})
    async def on_send_file(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return
        parts = event.message_str.strip().split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
        raw = raw.strip().strip('"').strip("'")
        if not raw:
            yield event.chain_result([Plain("用法：/发文件 <文件路径>，例如 /发文件 /test/1.txt")])
            return

        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            yield event.chain_result([Plain(f"找不到这个文件：{path}")])
            return
        if not path.is_file():
            yield event.chain_result([Plain(f"这不是一个文件：{path}")])
            return

        # 敏感目录防护（始终生效）
        if self._is_sensitive_path(path):
            yield event.chain_result([Plain("这个路径不允许发送哦")])
            return
        # 目录白名单（可选开启）：仅允许发送指定目录内的文件
        if self.send_restrict_paths:
            if not any(self._is_within(path, Path(d)) for d in self.allowed_send_dirs):
                yield event.chain_result([Plain("该文件不在允许发送的目录范围内")])
                return

        name = path.name
        size = self._fmt_size(path.stat().st_size)

        # 模式一：URL 模式（NapCat 自行下载）
        if self.callback_api_base:
            try:
                from astrbot.core import file_token_service

                token = await file_token_service.register_file(str(path), timeout=600)
                url = f"{self.callback_api_base}/api/file/{token}"
                await event.send(event.chain_result([File(name=name, url=url)]))
                yield event.chain_result([Plain(f"文件已发送：{name}（{size}）")])
                return
            except Exception as e:
                logger.warning(f"[file_transfer] URL 模式失败，回退路径模式：{e}")

        # 模式二：路径映射模式（直接给 NapCat 本地路径）
        file_path = str(path)
        if self.path_mapping and ":" in self.path_mapping:
            frm, to = self.path_mapping.split(":", 1)
            frm = frm.rstrip("/")
            to = to.rstrip("/")
            if file_path.startswith(frm):
                file_path = to + file_path[len(frm):]
        try:
            await self._send_file_segment(event, name, file_path)
            yield event.chain_result([Plain(f"文件已发送：{name}（{size}）")])
        except Exception as e:
            logger.error(f"[file_transfer] 路径模式发送失败：{e}")
            yield event.chain_result([Plain(f"发送失败了：{e}")])

    async def _send_file_segment(self, event: AstrMessageEvent, name: str, file_value: str):
        """用原始消息段发送文件，绕过 File 组件的本地存在性检查。"""
        segment = {"type": "file", "data": {"name": name, "file": file_value}}
        if event.get_group_id():
            await event.bot.call_action(
                "send_group_msg",
                group_id=int(event.get_group_id()),
                message=[segment],
            )
        else:
            await event.bot.call_action(
                "send_private_msg",
                user_id=int(event.get_sender_id()),
                message=[segment],
            )

    # ---------------- 查看文件 ----------------

    @filter.command("查看文件", alias={"catfile"})
    async def on_view_file(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return
        parts = event.message_str.strip().split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
        raw = raw.strip().strip('"').strip("'")

        if not raw:
            if not self.received_files:
                yield event.chain_result([Plain("还没有收到过文件呢，先发我一个试试？")])
                return
            rec = self.received_files[-1]
            yield event.chain_result([Plain(self._render_file(Path(rec["path"])))])
            return

        if raw.isdigit():
            idx = int(raw)
            if not self.received_files:
                yield event.chain_result([Plain("还没有收到过文件呢")])
                return
            if idx < 1 or idx > len(self.received_files):
                yield event.chain_result([Plain(f"序号超出范围，当前共 {len(self.received_files)} 个文件")])
                return
            rec = self.received_files[idx - 1]
            yield event.chain_result([Plain(self._render_file(Path(rec["path"])))])
            return

        # 非数字输入：在接收目录里按文件名查找，命中则显示并提示对应序号
        target_name = os.path.basename(raw)
        hit_idx = None
        for i, rec in enumerate(self.received_files):
            if rec.get("name") == target_name:
                hit_idx = i
                break
        if hit_idx is not None:
            rec = self.received_files[hit_idx]
            content = self._render_file(Path(rec["path"]))
            tip = f"（第 {hit_idx + 1} 个，可用 /查看文件 {hit_idx + 1} 查看）"
            yield event.chain_result([Plain(tip + chr(10) + content)])
            return
        yield event.chain_result(
            [Plain(f"接收目录里没有「{target_name}」，用 /文件列表 看序号，再 /查看文件 <序号> 查看")]
        )

    @filter.command("文件列表", alias={"filelist"})
    async def on_file_list(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return
        if not self.received_files:
            yield event.chain_result([Plain("还没有收到过文件呢")])
            return
        lines = ["最近收到的文件："]
        start = max(0, len(self.received_files) - 10)
        for i in range(start, len(self.received_files)):
            rec = self.received_files[i]
            lines.append(f"{i + 1}. {rec['name']}（{self._fmt_size(rec['size'])}，{rec['time']}）")
        lines.append("用 /查看文件 <序号> 查看内容")
        yield event.chain_result([Plain("\n".join(lines))])

    # ---------------- 删除文件 ----------------

    @filter.command("删除文件", alias={"delfile"})
    async def on_delete_file(self, event: AstrMessageEvent):
        """删除 received 目录里已收到的文件。"""
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.chain_result([Plain(reason)])
            return

        parts = event.message_str.strip().split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
        raw = raw.strip().strip('"').strip("'")
        if not raw:
            yield event.chain_result([Plain("用法：/删除文件 <文件名>，例如 /删除文件 1.txt")])
            return

        # 安全：只允许删除 received 目录内的文件，防止路径穿越
        safe_name = os.path.basename(raw)
        if not safe_name or safe_name in (".", ".."):
            yield event.chain_result([Plain("文件名不太对哦")])
            return

        target = self.received_dir / safe_name
        if not target.exists() or not target.is_file():
            yield event.chain_result([Plain(f"没找到这个文件：{safe_name}")])
            return

        try:
            target.unlink()
        except Exception as e:
            logger.error(f"[file_transfer] 删除文件失败 {target}: {e}")
            yield event.chain_result([Plain(f"删除失败了：{e}")])
            return

        # 同步清理接收记录
        self.received_files = [
            r for r in self.received_files if r.get("name") != safe_name
        ]
        self._save_records()
        logger.info(f"[file_transfer] 已删除文件：{target}")
        yield event.chain_result([Plain(f"已删除：{safe_name}")])

    # ---------------- 工具方法 ----------------

    def _render_file(self, path: Path) -> str:
        try:
            size = path.stat().st_size
        except OSError:
            return "文件读取失败"
        is_text = False
        head = b""
        try:
            with open(path, "rb") as f:
                head = f.read(TEXT_SNIFF_BYTES)
            if b"\x00" not in head:
                head.decode("utf-8")
                is_text = True
        except (UnicodeDecodeError, OSError):
            is_text = False
        if not is_text:
            return f"【{path.name}】\n类型：二进制文件\n大小：{self._fmt_size(size)}\n路径：{path}"
        try:
            full = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"读取失败：{e}"
        limit = self.max_view_chars
        if len(full) > limit:
            full = full[:limit] + f"\n……（内容过长已截断，全文共 {len(full)} 字符）"
        return f"【{path.name}】\n大小：{self._fmt_size(size)}\n{full}"

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        return f"{size / 1024 / 1024:.1f}MB"
