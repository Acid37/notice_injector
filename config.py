"""Notice Injector 插件配置"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class NoticeInjectorConfig(BaseConfig):
    """Notice Injector 插件配置类"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Notice Injector 配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置"""

        enabled: bool = Field(default=True, description="插件总开关。false 时本插件所有能力均停用。")
        enable_poke: bool = Field(default=True, description="是否处理戳一戳相关通知（notice_type=poke）。")
        enable_ban: bool = Field(default=True, description="是否处理群禁言/解除禁言通知。")
        enable_group_upload: bool = Field(default=True, description="是否处理群文件上传通知。")
        enable_debug: bool = Field(default=False, description="是否输出调试日志；建议仅在排障时开启。")
        ignore_self_notice: bool = Field(default=True, description="是否忽略机器人自身触发的通知，避免自循环。")
        trigger_chat: bool = Field(default=False, description="是否将通知注入对话流触发聊天。false 可减少 token 与上下文污染。")
        # 单戳连击限制
        max_poke_count: int = Field(default=3, description="单次动作最大连戳次数。建议 1~3；运行时会被强制限制在 [1, 10]。")
        poke_interval_min_ms: int = Field(default=100, description="连戳最小间隔（毫秒）。与 max 组成随机区间，降低风控概率。")
        poke_interval_max_ms: int = Field(default=200, description="连戳最大间隔（毫秒）。若小于 min，运行时会自动交换两者。")
        validate_target_before_poke: bool = Field(default=False, description="发送戳一戳前是否先做目标存在性校验（程序内 API，不消耗 LLM token）。")
        validate_target_in_group: bool = Field(default=True, description="群聊场景是否执行目标校验（建议开启，避免无效成员 ID）。")
        validate_target_in_private: bool = Field(default=False, description="私聊场景是否执行目标校验。默认 false；通常不推荐设为 true（会增加一次额外 API 调用）。")
        # AOE 戳一戳配置
        aoe_poke_max_targets: int = Field(default=5, description="AOE 戳一戳（send_poke_multiple）的最大目标人数上限。运行时会被限制在 [1, 20]。")
        validate_target_before_aoe_poke: bool = Field(default=True, description="AOE 戳一戳前是否校验目标用户存在。")
        # 群文件下载（FileCapture）
        enable_file_capture: bool = Field(default=True, description="是否启用群文件捕获服务。启用后会自动连接 NapCat SSE 服务器的 WebSocket 端口，捕获 group_upload 事件中的 file_id/busid，供 download_group_file 动作使用。")
        napcat_ws_url: str = Field(default="ws://127.0.0.1:9999", description="NapCat SSE 服务器的 WebSocket 地址。用于捕获群文件上传事件及发送 API 请求（如获取文件下载链接）。需与 NapCat 配置中 httpSseServers 的端口一致。")

    plugin: PluginSection = Field(default_factory=PluginSection)