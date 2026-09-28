"""聊天文件按需拉取工具。

面向聊天中直发的文件消息（QQ 的"发送文件"），与既有两个媒体工具互补：
- download_group_file：群文件面板上传（FileCapture 捕获 group_upload）
- media_lookup：聊天媒体消息（MediaManager 落盘的 hash 回查）
- fetch_chat_file（本工具）：聊天文件消息，按文件名从最近消息上下文
  找到 file_id，再通过 FileCapture 的 OneBot WS 通道 get_file 下载到
  data/chat_files/ 并返回本地路径。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger

from src.core.components.base import BaseTool
from src.core.components.types import ChatType

logger = get_logger("notice_injector")

# 聊天文件默认保存目录（工作目录相对路径，与 download_group_file 一致）
_CHAT_FILE_DIR = Path("data") / "chat_files"

# 视频扩展名：返回提示中指路 analyze_video
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts"}


def _fmt_size(num: object) -> str:
    """字节数转可读大小；无效输入返回空串。"""
    try:
        size = int(num)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{size}B"


class FetchChatFileTool(BaseTool):
    """按文件名下载聊天中收到的文件。"""

    tool_description = "下载聊天中收到的文件消息到本地，返回保存路径"
    tool_name = "fetch_chat_file"

    name: str = "fetch_chat_file"
    description: str = (
        "下载聊天中对方直接发送的文件（占位符形如 [文件:xxx.mp4(1.7MB)]）到本地，返回路径。"
        "适用：文件消息。群文件面板上传的文件请用 download_group_file；"
        "聊天中收到的视频/语音消息（占位符带媒体哈希）请用 media_lookup。"
        "视频类文件可将返回路径作为 url 传给 analyze_video 分析内容。"
        "参数：file_name（必填，占位符里的文件名）。群号自动从会话上下文获取。"
    )

    async def go_activate(self) -> bool:
        """激活判定（前向兼容保留）。

        注意：当前框架只对 Action/Agent 调用 ``go_activate``（见
        ``core/managers/action_manager.py`` 与 ``agent_manager.py``），
        Tool 的筛选走 ``ToolManager.filter_tools`` 的静态过滤，不会调用本方法。
        插件启停的有效门控在 ``NoticeInjectorPlugin.get_components()``：
        插件禁用时该 Tool 直接不注册。
        """
        plugin_obj = getattr(self, "plugin", None)
        config_obj = getattr(plugin_obj, "config", None)
        plugin_section = getattr(config_obj, "plugin", None)
        if plugin_section is None or not getattr(plugin_section, "enabled", True):
            return False
        file_capture = getattr(plugin_obj, "file_capture", None)
        return file_capture is not None and bool(getattr(file_capture, "_running", False))

    async def execute(
        self,
        file_name: Annotated[str, "要下载的文件名，来自聊天文件占位符 [文件:xxx] 中的名字"],
    ) -> tuple[bool, str]:
        """执行聊天文件下载。

        Returns:
            (是否成功, 结果描述文本)
        """
        file_name = str(file_name or "").strip()
        if not file_name:
            return False, "file_name 不能为空（占位符 [文件:xxx] 里的文件名）"

        plugin_obj = getattr(self, "plugin", None)
        file_capture = getattr(plugin_obj, "file_capture", None)
        if not file_capture:
            return False, "文件捕获服务未启动，无法通过 OneBot API 获取文件"

        # ── 1. 从最近消息上下文解析 file_id ──
        file_id = await self._resolve_file_id(file_name)
        if not file_id:
            return (
                False,
                f"当前会话最近的文件消息中未找到 '{file_name}'。"
                "file_id 只在收到文件消息时缓存，过旧的文件无法获取；"
                "请确认文件名与 [文件:xxx] 占位符一致。",
            )

        # ── 2. OneBot get_file → 本地路径或 URL ──
        detail = await file_capture.send_api("get_file", {"file_id": file_id}, timeout=30.0)
        if not detail or not (
            detail.get("status") == "ok" or detail.get("retcode") == 0
        ):
            return False, f"get_file 调用失败（file_id={file_id}），文件可能已过期"

        data = detail.get("data") or {}
        local_path = str(data.get("file") or data.get("file_path") or "").strip()
        url = str(data.get("url") or "").strip()
        file_size = data.get("file_size") or data.get("size") or 0

        # ── 3. 落盘到 data/chat_files/ ──
        save_path = await self._save_file(local_path, url, file_name)
        if not save_path:
            return (
                False,
                "文件下载失败：NapCat 未返回可用的本地路径或下载链接，"
                "文件可能已被 QQ 服务器清理",
            )

        size_text = _fmt_size(file_size) or "未知大小"
        logger.info(f"聊天文件下载成功: {file_name} -> {save_path} ({size_text})")

        lines = [f"文件已下载到: {save_path.resolve()}（{file_name}，{size_text}）"]
        if save_path.suffix.lower() in _VIDEO_EXTS:
            lines.append("这是视频文件，可将上方路径作为 url 传给 analyze_video 分析内容。")
        return True, "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _resolve_file_id(self, file_name: str) -> str | None:
        """从当前会话最近的消息历史中按文件名查找 file_id。

        file 类 media 项的 data 是元信息（name/size/id），入库时不剔除，
        因此可从流历史消息的 content["media"] 中按名匹配取回 file_id。
        """
        from src.core.managers.stream_manager import get_stream_manager

        stream_id = self.get_current_stream_id()
        if not stream_id:
            return None

        try:
            messages = await get_stream_manager().get_stream_messages(
                stream_id, limit=50
            )
        except Exception as e:
            logger.debug(f"拉取会话消息失败: {e}")
            return None

        # 从新到旧找第一个名字匹配的 file 项
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if not isinstance(content, dict):
                continue
            media_list = content.get("media")
            if not isinstance(media_list, list):
                continue
            for item in media_list:
                if not isinstance(item, dict) or item.get("type") != "file":
                    continue
                data = item.get("data")
                if not isinstance(data, dict):
                    continue
                name = str(data.get("name") or "").strip()
                if name == file_name:
                    fid = data.get("id") or data.get("file_id")
                    if fid:
                        return str(fid)
        return None

    async def _save_file(
        self, local_path: str, url: str, file_name: str
    ) -> Path | None:
        """把文件落盘到 data/chat_files/。本地路径优先复制，其次 URL 下载。"""
        import asyncio
        import shutil
        import aiohttp

        target = self._reserve_target(file_name)
        if target is None:
            return None

        # 1) NapCat 与本机同盘：直接复制本地文件
        if local_path:
            src = Path(local_path)
            if src.is_file():

                def _copy() -> None:
                    shutil.copyfile(src, target)

                try:
                    await asyncio.to_thread(_copy)
                    return target
                except OSError as e:
                    logger.warning(f"复制本地文件失败 ({src}): {e}")

        # 2) URL 下载
        if url.startswith(("http://", "https://")):
            try:
                timeout = aiohttp.ClientTimeout(total=300)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.warning(f"下载失败，HTTP {resp.status}")
                            target.unlink(missing_ok=True)
                            return None
                        with open(target, "wb") as f:
                            async for chunk in resp.content.iter_chunked(8192):
                                f.write(chunk)
                return target
            except Exception as e:
                logger.warning(f"URL 下载失败 ({url[:80]}): {e}")
                target.unlink(missing_ok=True)
                return None

        # 两条落盘路径都不可用：释放占位文件，避免留下 0 字节垃圾
        target.unlink(missing_ok=True)
        return None

    def _reserve_target(self, file_name: str) -> Path | None:
        """原子占位一个不会覆盖既有文件的保存路径。

        用 ``O_EXCL`` 独占创建把「取名」与「占位」合并为一次原子操作：
        同名文件已存在、或并发调用争抢同一路径时，都会自动退避到下一个
        候选名（毫秒时间戳 + 递增序号），因此同秒/同毫秒的重复下载不会
        互相覆盖。占位失败时返回 None。
        """
        base = _CHAT_FILE_DIR / Path(file_name).name
        try:
            _CHAT_FILE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"创建落盘目录失败 ({_CHAT_FILE_DIR}): {e}")
            return None

        stem, suffix = base.stem, base.suffix
        stamp = int(time.time() * 1000)
        candidates = [base]
        candidates.extend(
            _CHAT_FILE_DIR / f"{stem}_{stamp}_{index}{suffix}"
            for index in range(1, 100)
        )
        for candidate in candidates:
            try:
                with open(candidate, "xb"):
                    return candidate
            except FileExistsError:
                continue
            except OSError as e:
                logger.warning(f"分配落盘路径失败 ({candidate}): {e}")
                return None
        logger.warning(f"同名文件过多，无法为 {file_name} 分配落盘路径")
        return None
