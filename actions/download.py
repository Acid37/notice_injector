"""群文件下载动作"""

from __future__ import annotations

from pathlib import Path

from src.core.components.base import BaseAction
from src.core.components.types import ChatType
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("notice_injector")


def _is_plugin_enabled(plugin: object) -> bool:
    """检查插件总开关是否开启。"""
    config_obj = getattr(plugin, "config", None)
    plugin_section = getattr(config_obj, "plugin", None)
    if plugin_section is None:
        return False
    return bool(getattr(plugin_section, "enabled", True))


async def _resolve_group_id_from_stream(chat_stream: object) -> str | None:
    """从流上下文或流记录中解析群ID（数字）。"""
    context = getattr(chat_stream, "context", None)
    if context:
        current_message = getattr(context, "current_message", None)
        if current_message:
            extra = getattr(current_message, "extra", {})
            group_id = str(extra.get("group_id", "")).strip()
            if group_id and group_id.isdigit():
                return group_id

    stream_id = getattr(chat_stream, "stream_id", "")
    if not stream_id:
        return None

    try:
        from src.core.managers.stream_manager import get_stream_manager

        stream_info = await get_stream_manager().get_stream_info(stream_id)
        if stream_info:
            gid = str(stream_info.get("group_id", "")).strip()
            if gid and gid.isdigit():
                return gid
        return None
    except Exception as e:
        logger.debug(f"通过 stream_info 回查 group_id 失败: {e}")
        return None


class DownloadGroupFileAction(BaseAction):
    """下载群聊中上传的文件到本地，并返回文件路径"""

    name = "download_group_file"
    associated_platforms = ["qq"]
    description = (
        "下载群聊中某人上传的文件到本地，返回保存路径。"
        "当有人上传了文件且你需要阅读其内容时使用此动作。"
        "参数：file_name（必填，从上传通知中获取的文件名）。"
        "群号会自动从当前会话上下文获取，无需传入。"
        "文件会保存到 data/group_files/ 目录下。"
        "获取路径后可使用其他工具读取文件内容。"
    )
    # chat_type 声明为 ALL：核心静态过滤对非 ALL 的 chat_type 会按传入参数粗筛，
    # 而 chatter 调用时未透传实际 chat_type（PR #140 后的行为），导致 GROUP 动作
    # 被静默剔除。真实场景判定由下方 go_activate() 完成（群聊+群号+file_capture）。
    chat_type = ChatType.ALL
    associated_types = ["file"]

    async def go_activate(self) -> bool:
        """仅在群聊且插件启用且有 file_capture 时激活。"""
        if not _is_plugin_enabled(getattr(self, "plugin", None)):
            return False

        chat_stream = getattr(self, "chat_stream", None)
        if str(getattr(chat_stream, "chat_type", "")) != ChatType.GROUP.value:
            return False

        group_id = await _resolve_group_id_from_stream(chat_stream)
        if not group_id:
            return False

        # 检查 file_capture 是否可用
        plugin_obj = getattr(self, "plugin", None)
        file_capture = getattr(plugin_obj, "file_capture", None)
        return file_capture is not None and file_capture._running

    async def execute(
        self,
        file_name: str,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行群文件下载

        Args:
            file_name: 要下载的文件名（从群文件上传通知中获取）
            **kwargs: 上下文参数
        """
        try:
            plugin_obj = getattr(self, "plugin", None)
            file_capture = getattr(plugin_obj, "file_capture", None)
            if not file_capture:
                return False, "文件捕获服务未启动"

            chat_stream = getattr(self, "chat_stream", None)
            group_id = await _resolve_group_id_from_stream(chat_stream)
            if not group_id:
                return False, "无法获取群号，该会话可能缺少群信息"

            # 查找文件元数据
            file_info = file_capture.lookup(group_id, file_name)

            # Fallback: 内存中无记录时，通过 API 列群根目录文件并按名匹配
            if not file_info:
                logger.debug(
                    f"[FileCapture] 内存无 '{file_name}' 记录，"
                    f"尝试 API 列群文件 (group={group_id})"
                )
                api_files = await file_capture.list_group_files(group_id)
                for af in api_files:
                    if af["name"] == file_name:
                        file_info = af
                        break

            if not file_info:
                # 如果是闪传，明确告知 LLM 无法下载
                if file_capture.has_recent_flash_transfer(group_id):
                    return (
                        False,
                        "刚才收到的是闪传文件，闪传无法通过群文件 API 下载。"
                        "请让对方改用「群文件」功能上传，或直接把文件内容发在聊天里。",
                    )
                # 提供最近的上传记录帮助 LLM 纠正文件名
                recent = file_capture.list_recent(group_id, limit=5)
                if recent:
                    names = ", ".join(f["name"] for f in recent)
                    return (
                        False,
                        f"未找到名为 '{file_name}' 的文件。"
                        f"该群最近上传的文件有: {names}",
                    )
                # 同时提供 API 列出的文件名
                api_files = await file_capture.list_group_files(group_id)
                if api_files:
                    names = ", ".join(f["name"] for f in api_files[:10])
                    return (
                        False,
                        f"未找到名为 '{file_name}' 的文件。"
                        f"群文件列表中有: {names}",
                    )
                return False, f"未找到名为 '{file_name}' 的文件，且该群没有最近的上传记录"

            file_id = file_info["file_id"]
            busid = file_info["busid"]

            # 获取下载 URL（通过 WebSocket API）
            file_url = await file_capture.get_file_url(
                group_id, file_id, busid
            )
            if not file_url:
                # 如果 API 调用失败，尝试使用事件自带的 url
                file_url = file_info.get("url", "")
            if not file_url:
                return False, "无法获取文件下载链接，文件可能已过期或 API 调用失败"

            # 确定保存路径
            save_dir = Path("data") / "group_files"
            save_path = save_dir / file_name

            # 避免覆盖：如果同名文件存在，加时间戳后缀
            if save_path.exists():
                stem = save_path.stem
                suffix = save_path.suffix
                import time

                timestamp = int(time.time())
                save_path = save_dir / f"{stem}_{timestamp}{suffix}"

            # 下载文件
            success = await file_capture.download_file(file_url, save_path)
            if not success:
                return False, "文件下载失败，请检查网络连接或 NapCat 状态"

            abs_path = str(save_path.resolve())
            file_size = file_info.get("size", 0)
            logger.info(
                f"群文件下载成功: {file_name} -> {abs_path} ({file_size} bytes)"
            )
            return (
                True,
                f"文件已下载到: {abs_path}（{file_name}，{file_size} 字节）",
            )

        except Exception as e:
            logger.error(f"下载群文件时发生异常: {e}", exc_info=True)
            return False, f"下载群文件时发生异常: {str(e)}"
