"""NoticeInjector 插件实现"""

from __future__ import annotations

from typing import Any

# 导入必要的类
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BasePlugin, BaseEventHandler
from src.core.components.loader import register_plugin
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from .config import NoticeInjectorConfig
from .actions.poke import SendGroupPokeAction, SendPrivatePokeAction, SendGroupPokeMultipleAction
from .actions.download import DownloadGroupFileAction
from .file_capture import FileCapture

logger = get_logger("notice_injector")


# ─── Event Handler ──────────────────────────────────────────


class NoticeInjectorEventHandler(BaseEventHandler):
    """Notice 注入事件处理器"""

    name = "notice_injector"
    description = "将 QQ 通知消息（如戳一戳、禁言等）转换为标准文本消息。"

    # 订阅的事件类型
    init_subscribe = [EventType.ON_RECEIVED_OTHER_MESSAGE]
    
    def __init__(self, plugin: "NoticeInjectorPlugin") -> None:
        """初始化处理器
        
        Args:
            plugin: 所属插件实例
        """
        super().__init__(plugin)
        self.plugin = plugin
        self._bot_id: str | None = None

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理其他类型消息，将 notice 转换为标准消息"""
        # 获取配置对象
        config: NoticeInjectorConfig = self.plugin.config  # type: ignore
        if not config:
            # 如果配置未加载，保守起见不进行处理
            return EventDecision.SUCCESS, params
            
        # 1. 检查全局开关
        if not config.plugin.enabled:
            return EventDecision.SUCCESS, params

        raw = params.get("raw", {})
        msg_info = raw.get("message_info", {})
        message_type = msg_info.get("message_type", "")
        
        # 2. 只处理 notice 类型的消息
        if message_type != "notice":
            return EventDecision.SUCCESS, params
            
        extra = msg_info.get("extra", {})
        notice_type = extra.get("notice_type", "")
        text_description = extra.get("text_description", "")

        # 如果没有 text_description，无法处理，直接返回
        if not text_description:
            return EventDecision.SUCCESS, params

        # 3. 按类型检查功能开关（分层控制：先检查总开关 trigger_chat，再检查分开关 enable_*）
        # 第一层：检查是否启用了聊天触发（总开关）
        if not config.plugin.trigger_chat:
            if config.plugin.enable_debug:
                logger.debug(f"Chat Trigger 未启用（总开关关闭），忽略所有通知：{text_description}")
            return EventDecision.SUCCESS, params

        # 不处理贴表情（emoji_like）类型的 notice
        if notice_type == "emoji_like":
            if config.plugin.enable_debug:
                logger.debug(f"忽略贴表情通知: {text_description}")
            return EventDecision.SUCCESS, params

        # 第二层：检查具体通知类型的分开关
        if notice_type == "poke" and not config.plugin.enable_poke:
            if config.plugin.enable_debug:
                logger.debug(f"通过配置忽略戳一戳通知: {text_description}")
            return EventDecision.SUCCESS, params
            
        elif notice_type == "group_ban" and not config.plugin.enable_ban:
            if config.plugin.enable_debug:
                logger.debug(f"通过配置忽略禁言通知: {text_description}")
            return EventDecision.SUCCESS, params
            
        elif notice_type == "group_upload" and not config.plugin.enable_group_upload:
            if config.plugin.enable_debug:
                logger.debug(f"通过配置忽略上传通知: {text_description}")
            return EventDecision.SUCCESS, params

        # 4. 检查是否是机器人自己发送的动作（防止自循环）
        # 优先使用适配器提供的标记，如果没有则回退到简单的逻辑判断
        # 受控于 config.plugin.ignore_self_notice
        if config.plugin.ignore_self_notice:
            is_self = extra.get("self_sent", False)
            if not is_self:
                is_self = await self._is_self_sent_notice(extra, msg_info)
                
            if is_self:
                if config.plugin.enable_debug:
                    logger.debug(f"检测到自己发送的动作，已忽略: {text_description}")
                return EventDecision.SUCCESS, params
            

        # 根据 notice 类型处理并记录日志
        if config.plugin.enable_debug:
            logger.info(f"处理通知消息 [{notice_type}]: {text_description}")
        
        # 关键修改：将 notice 消息转换为 processed 文本
        # 这样 _handle_other 方法会创建 Message 并触发 ON_MESSAGE_RECEIVED
        
        # 设置 processed 字段，让 _handle_other 创建标准消息
        params["processed"] = text_description
        
        # 同时保留原始信息到 extra，供后续使用
        # 且标记为 trigger_chat=True，供下游 StreamManager 参考（目前核心层尚未实现瞬态消息支持，未来可扩展 ephemeral）
        current_extra = msg_info.get("extra", {})
        current_extra.update({
            "original_notice_type": notice_type,
            "trigger_chat": True,
            "ephemeral": True  # 建议的核心层协议：标记为瞬态消息，不持久化到数据库
        })
        msg_info["extra"] = current_extra
        
        return EventDecision.SUCCESS, params

    async def _is_self_sent_notice(self, extra: dict, msg_info: dict) -> bool:
        """检查是否是机器人自己发送的通知动作
        
        Args:
            extra: 通知消息的额外信息字典
            msg_info: 消息信息字典
            
        Returns:
            bool: 如果是机器人自己发送的动作返回 True，否则返回 False
        """
        # 尝试获取操作者ID
        # 注意：adapter生成的msg_info中，user_id通常位于from_user字段，而不是extra
        operator_id = extra.get("operator_id") or extra.get("user_id")
        
        if not operator_id:
            # 尝试从 from_user 获取
            from_user = msg_info.get("from_user", {})
            operator_id = from_user.get("user_id")
            
        if not operator_id:
            return False
            
        # 懒加载获取 Bot ID
        if not self._bot_id:
            try:
                from src.app.plugin_system.api import adapter_api
                # 尝试获取 QQ 平台的 bot 信息
                bot_info = await adapter_api.get_bot_info_by_platform("qq")
                if bot_info:
                    self._bot_id = str(bot_info.get("user_id", ""))
            except Exception as e:
                logger.warning(f"获取Bot信息失败: {e}")
        
        # 比对操作者ID与Bot ID
        if self._bot_id and str(operator_id) == self._bot_id:
            return True
            
        return False


# ─── Plugin ────────────────────────────────────────────────


@register_plugin
class NoticeInjectorPlugin(BasePlugin):
    """NoticeInjector 插件主类"""

    plugin_name = "notice_injector"
    configs = [NoticeInjectorConfig]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.file_capture: FileCapture | None = None

    async def on_plugin_loaded(self) -> None:
        """插件加载时的处理：启动 FileCapture 服务。"""
        logger.info("NoticeInjector 插件加载成功")

        config: NoticeInjectorConfig | None = self.config  # type: ignore
        if config and config.plugin.enabled and config.plugin.enable_file_capture:
            self.file_capture = FileCapture()
            try:
                await self.file_capture.start(config.plugin.napcat_ws_url)
                logger.info("FileCapture 服务已启动")
            except Exception as e:
                logger.error(f"FileCapture 服务启动失败: {e}", exc_info=True)
                self.file_capture = None
        else:
            if config and not config.plugin.enable_file_capture:
                logger.info("FileCapture 服务被配置禁用，跳过启动")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时的处理：停止 FileCapture 服务。"""
        if self.file_capture:
            try:
                await self.file_capture.stop()
                logger.info("FileCapture 服务已停止")
            except Exception as e:
                logger.warning(f"FileCapture 服务停止时异常: {e}")
            self.file_capture = None
        logger.info("NoticeInjector 插件卸载成功")

    def get_components(self) -> list[type]:
        """获取插件内所有组件类"""
        return [
            SendGroupPokeAction,
            SendPrivatePokeAction,
            SendGroupPokeMultipleAction,
            DownloadGroupFileAction,
            NoticeInjectorEventHandler,
        ]
