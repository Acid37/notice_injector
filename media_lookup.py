"""媒体回查工具：按 media_id 查询聊天中已落盘的媒体。

与 download_group_file（群文件面板上传，按文件名下载）互补：
本工具面向聊天内收到的媒体消息（视频/语音/图片），本体已由
MediaManager 落盘到 data/media_cache 并记录在数据库，按消息占位符
中的 media_id（sha256 哈希）直接回查本地路径与识别描述。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api import media_api

from src.core.components.base import BaseTool

logger = get_logger("notice_injector")

_TYPE_LABELS = {"image": "图片", "emoji": "表情包", "voice": "语音", "video": "视频"}


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


def _fmt_duration(seconds: object) -> str:
    """秒数转 mm:ss / hh:mm:ss；无效输入返回空串。"""
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class MediaLookupTool(BaseTool):
    """按媒体 ID 查询聊天中已落盘媒体的信息。"""

    tool_description = "按媒体 ID 查询聊天中收到的图片/语音/视频的落盘信息与识别描述"
    tool_name = "media_lookup"

    name: str = "media_lookup"
    description: str = (
        "按媒体 ID（消息占位符中括号里的哈希，如 [视频(a1b2...) 中的 a1b2...]）"
        "查询该媒体的落盘信息：类型、文件名、大小、时长、本地路径与已有的识别描述。"
        "当上下文中出现 [视频(xxx)]/[语音(xxx)]/[图片(xxx)] 占位符而你想了解其内容时使用。"
        "返回的本地路径可直接作为 url 传给 analyze_video（本地视频文件）分析。"
        "注意：群文件面板里上传的文件请改用 download_group_file（按文件名下载）；"
        "本工具只查聊天中直接收到的媒体消息。"
    )

    async def execute(
        self,
        media_id: Annotated[str, "媒体 ID，即消息占位符括号中的哈希值（sha256）"],
    ) -> tuple[bool, str]:
        """查询媒体信息。

        Returns:
            (是否成功, 媒体信息描述文本)
        """
        media_id = str(media_id or "").strip()
        if not media_id:
            return False, "media_id 不能为空（占位符括号中的哈希值）"

        try:
            info = await media_api.get_media_info(media_id)
        except Exception as e:
            logger.warning(f"查询媒体信息失败: {media_id[:8]}..., error={e}")
            return False, f"查询失败: {e}"

        if not info:
            return (
                False,
                f"未找到媒体 {media_id[:16]}...（可能已被清理，或 ID 不属于本机收到的媒体；"
                "群文件面板上传的文件请用 download_group_file）",
            )

        media_type = str(info.get("type") or "")
        label = _TYPE_LABELS.get(media_type, media_type or "未知类型")
        lines: list[str] = [f"媒体信息 [{label}] ID: {media_id}"]

        path = str(info.get("path") or "").strip()
        if path:
            p = Path(path)
            if not p.is_absolute():
                # DB 存的是 data/ 相对路径；解析为绝对路径，
                # 使 analyze_video 拿到即可用，无需模型自己拼。
                p = (Path.cwd() / p).resolve()
            path = str(p)
            lines.append(f"路径: {path}")
            try:
                size = await asyncio.to_thread(lambda: p.stat().st_size if p.is_file() else 0)
            except OSError:
                size = 0
            if size:
                lines.append(f"大小: {_fmt_size(size)}")
            else:
                lines.append("状态: 文件已被清理（描述仍可参考）")
        else:
            lines.append("状态: 无落盘记录")

        duration = _fmt_duration(info.get("duration"))
        if duration:
            lines.append(f"时长: {duration}")

        description = str(info.get("description") or "").strip()
        if description:
            lines.append(f"已有描述: {description}")
        else:
            lines.append("已有描述: 无（该媒体未做过内容识别）")

        lines.append(
            "提示: 本地视频文件可将上方路径作为 url 传给 analyze_video 分析；"
            "语音与图片无内置分析工具，可参考已有描述。"
        )
        return True, "\n".join(lines)
