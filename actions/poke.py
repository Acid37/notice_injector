"""发送戳一戳动作"""

from __future__ import annotations

import asyncio
import random

from src.core.components.base import BaseAction
from src.core.components.types import ChatType
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("notice_injector")

_DEFAULT_ADAPTER_SIGN = "napcat_adapter:adapter:napcat_adapter"


# ============================================================================
# 共享工具函数
# ============================================================================

def _normalize_numeric_id(value: object) -> str | None:
    """将输入归一化为仅数字ID字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or not text.isdigit():
        return None
    return text


def _resolve_effective_user_id(user_id: object, target_user_id: str | None) -> str | None:
    """解析并校验最终目标用户ID。"""
    if target_user_id:
        return _normalize_numeric_id(target_user_id)
    return _normalize_numeric_id(user_id)


def _is_positive_numeric_id(value: str | None) -> bool:
    """判断字符串 ID 是否为正整数。"""
    if not value:
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _is_plugin_enabled(plugin: object) -> bool:
    """检查插件总开关是否开启。"""
    config_obj = getattr(plugin, "config", None)
    plugin_section = getattr(config_obj, "plugin", None)
    if plugin_section is None:
        return False
    return bool(getattr(plugin_section, "enabled", True))


async def _resolve_group_id_from_stream(chat_stream: object) -> str | None:
    """从流上下文或流记录中解析群ID（数字）。"""
    # 1) 优先从当前消息 extra 中获取（零 DB 查询）
    context = getattr(chat_stream, "context", None)
    if context:
        current_message = getattr(context, "current_message", None)
        if current_message:
            extra = getattr(current_message, "extra", {})
            group_id = _normalize_numeric_id(extra.get("group_id"))
            if group_id:
                return group_id

    # 2) 回退：通过 StreamManager.get_stream_info 查询（带 alru_cache，同 stream_id 只查一次 DB）
    stream_id = getattr(chat_stream, "stream_id", "")
    if not stream_id:
        return None

    try:
        from src.core.managers.stream_manager import get_stream_manager

        stream_info = await get_stream_manager().get_stream_info(stream_id)
        if stream_info:
            return _normalize_numeric_id(stream_info.get("group_id"))
        return None
    except Exception as e:
        logger.debug(f"通过 stream_info 回查 group_id 失败: {e}")
        return None


# ============================================================================
# 群聊单用户连续戳
# ============================================================================

class SendGroupPokeAction(BaseAction):
    """在群聊中戳一戳指定用户（支持连续戳）"""

    action_name = "send_group_poke"
    action_description = (
        "在群聊中向指定用户发送戳一戳动作（仅群聊环境可用）。"
        "支持通过 poke_count 指定连续戳一戳次数；"
        "支持通过 target_user_id 显式指定目标（可不等于当前回复对象）；"
        "群号会从当前会话上下文自动解析，无需传入。"
        "请结合上下文与提示词决定次数。"
        "插件默认最大次数为3，硬上限为10，超出会自动按上限截断。"
    )
    chat_type = ChatType.GROUP

    async def go_activate(self) -> bool:
        """仅在群聊且能解析到群号时激活。"""
        if not _is_plugin_enabled(getattr(self, "plugin", None)):
            return False

        chat_stream = getattr(self, "chat_stream", None)
        if str(getattr(chat_stream, "chat_type", "")) != ChatType.GROUP.value:
            return False

        group_id = await _resolve_group_id_from_stream(chat_stream)
        return _is_positive_numeric_id(group_id)

    async def execute(
        self,
        user_id: str,
        poke_count: int = 1,
        target_user_id: str | None = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行群聊戳一戳动作

        Args:
            user_id: 要戳的用户ID
            poke_count: 连续戳一戳次数（默认1，最大10）
            target_user_id: 可选，显式目标用户ID
            **kwargs: 上下文参数
        """
        try:
            from src.core.managers.adapter_manager import get_adapter_manager

            adapter_manager = get_adapter_manager()
            chat_stream = getattr(self, "chat_stream", None)

            # 读取配置
            plugin_obj = getattr(self, "plugin", None)
            config_obj = getattr(plugin_obj, "config", None)
            plugin_config = getattr(config_obj, "plugin", None)

            # 计算有效上限（默认3，硬上限10）
            configured_max = 3
            if plugin_config is not None:
                try:
                    configured_max = int(getattr(plugin_config, "max_poke_count", 3) or 3)
                except (TypeError, ValueError):
                    configured_max = 3
            effective_max = min(max(configured_max, 1), 10)

            # 截断到合法范围
            try:
                requested_count = int(poke_count)
            except (TypeError, ValueError):
                requested_count = 1
            actual_count = min(max(requested_count, 1), effective_max)

            # 读取间隔和校验配置
            interval_min_ms = 100
            interval_max_ms = 200
            validate_target_before_poke = False
            validate_target_in_group = True
            adapter_sign = _DEFAULT_ADAPTER_SIGN

            if plugin_config is not None:
                try:
                    interval_min_ms = int(getattr(plugin_config, "poke_interval_min_ms", 100) or 0)
                    interval_max_ms = int(getattr(plugin_config, "poke_interval_max_ms", 200) or 0)
                except (TypeError, ValueError):
                    pass
                validate_target_before_poke = bool(
                    getattr(plugin_config, "validate_target_before_poke", False)
                )
                validate_target_in_group = bool(
                    getattr(plugin_config, "validate_target_in_group", True)
                )

            interval_min_ms = max(0, interval_min_ms)
            interval_max_ms = max(0, interval_max_ms)
            if interval_min_ms > interval_max_ms:
                interval_min_ms, interval_max_ms = interval_max_ms, interval_min_ms

            # 确定目标用户
            effective_user_id = _resolve_effective_user_id(user_id, target_user_id)
            if not effective_user_id:
                return False, "目标用户ID无效，操作取消"

            # 从上下文解析群ID（不信任 LLM 传入的值）
            effective_group_id = await _resolve_group_id_from_stream(chat_stream) if chat_stream else None

            # 群聊 Action 必须有 group_id
            if not effective_group_id:
                logger.error(f"群聊戳一戳缺失 group_id: stream_id={getattr(chat_stream, 'stream_id', None)}, "
                             f"current_message={bool(getattr(getattr(chat_stream, 'context', None), 'current_message', None))}")
                return False, "无法获取群号，该会话可能缺少群信息，请尝试重新触发对话后再戳"
            if not _is_positive_numeric_id(effective_group_id):
                return False, "群号无效，操作取消"

            # 可选目标校验
            if validate_target_before_poke and validate_target_in_group:
                verify_result = await adapter_manager.send_adapter_command(
                    adapter_sign=adapter_sign,
                    command_name="get_group_member_info",
                    command_data={
                        "group_id": effective_group_id,
                        "user_id": effective_user_id,
                        "no_cache": True,
                    },
                    timeout=10.0,
                )
                if verify_result.get("status") != "ok":
                    error_msg = verify_result.get("message", "未知错误")
                    logger.warning(f"目标校验失败: user_id={effective_user_id}, error={error_msg}")
                    return False, f"目标校验失败，操作取消: {error_msg}"

            # 发送戳一戳
            for i in range(actual_count):
                result = await adapter_manager.send_adapter_command(
                    adapter_sign=adapter_sign,
                    command_name="group_poke",
                    command_data={
                        "group_id": effective_group_id,
                        "user_id": effective_user_id
                    },
                    timeout=10.0
                )
                logger.debug(f"群戳一戳 NapCat 原始响应: 第{i + 1}/{actual_count}次, result={result}")
                if result.get("status") != "ok":
                    error_msg = result.get("message", "未知错误")
                    logger.error(f"发送戳一戳失败: 第{i + 1}/{actual_count}次, 错误: {error_msg}")
                    return False, f"发送戳一戳失败（第{i + 1}/{actual_count}次）: {error_msg}，请检查 NapCat 是否正常运行且 Packet 模式可用"

                if i < actual_count - 1:
                    interval_ms = random.randint(interval_min_ms, interval_max_ms)
                    await asyncio.sleep(interval_ms / 1000.0)

            logger.info(f"已在群 {effective_group_id} 中连续戳了用户 {effective_user_id} {actual_count} 次")
            return True, f"已在群 {effective_group_id} 中连续戳了用户 {effective_user_id} {actual_count} 次"

        except Exception as e:
            logger.error(f"发送群聊戳一戳时发生异常: {e}", exc_info=True)
            return False, f"发送戳一戳时发生异常: {str(e)}"


# ============================================================================
# 私聊单用户连续戳
# ============================================================================

class SendPrivatePokeAction(BaseAction):
    """在私聊中戳一戳指定用户（支持连续戳）"""

    action_name = "send_private_poke"
    action_description = (
        "在私聊/好友环境中向指定用户发送戳一戳动作（仅私聊环境可用）。"
        "支持通过 poke_count 指定连续戳一戳次数；"
        "支持通过 target_user_id 显式指定目标（可不等于当前回复对象）；"
        "请结合上下文与提示词决定次数。"
        "插件默认最大次数为3，硬上限为10，超出会自动按上限截断。"
    )
    chat_type = ChatType.PRIVATE

    async def go_activate(self) -> bool:
        """仅在私聊中激活。"""
        if not _is_plugin_enabled(getattr(self, "plugin", None)):
            return False

        chat_stream = getattr(self, "chat_stream", None)
        return str(getattr(chat_stream, "chat_type", "")) == ChatType.PRIVATE.value

    async def execute(
        self,
        user_id: str,
        poke_count: int = 1,
        target_user_id: str | None = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行私聊戳一戳动作

        Args:
            user_id: 要戳的用户ID
            poke_count: 连续戳一戳次数（默认1，最大10）
            target_user_id: 可选，显式目标用户ID
            **kwargs: 上下文参数
        """
        try:
            from src.core.managers.adapter_manager import get_adapter_manager

            adapter_manager = get_adapter_manager()

            # 读取配置
            plugin_obj = getattr(self, "plugin", None)
            config_obj = getattr(plugin_obj, "config", None)
            plugin_config = getattr(config_obj, "plugin", None)

            # 计算有效上限
            configured_max = 3
            if plugin_config is not None:
                try:
                    configured_max = int(getattr(plugin_config, "max_poke_count", 3) or 3)
                except (TypeError, ValueError):
                    configured_max = 3
            effective_max = min(max(configured_max, 1), 10)

            # 截断到合法范围
            try:
                requested_count = int(poke_count)
            except (TypeError, ValueError):
                requested_count = 1
            actual_count = min(max(requested_count, 1), effective_max)

            # 读取间隔和校验配置
            interval_min_ms = 100
            interval_max_ms = 200
            validate_target_before_poke = False
            validate_target_in_private = False
            adapter_sign = _DEFAULT_ADAPTER_SIGN

            if plugin_config is not None:
                try:
                    interval_min_ms = int(getattr(plugin_config, "poke_interval_min_ms", 100) or 0)
                    interval_max_ms = int(getattr(plugin_config, "poke_interval_max_ms", 200) or 0)
                except (TypeError, ValueError):
                    pass
                validate_target_before_poke = bool(
                    getattr(plugin_config, "validate_target_before_poke", False)
                )
                validate_target_in_private = bool(
                    getattr(plugin_config, "validate_target_in_private", False)
                )

            interval_min_ms = max(0, interval_min_ms)
            interval_max_ms = max(0, interval_max_ms)
            if interval_min_ms > interval_max_ms:
                interval_min_ms, interval_max_ms = interval_max_ms, interval_min_ms

            # 确定目标用户
            effective_user_id = _resolve_effective_user_id(user_id, target_user_id)
            if not effective_user_id:
                return False, "目标用户ID无效，操作取消"
            if not _is_positive_numeric_id(effective_user_id):
                return False, "目标用户ID无效，操作取消"

            # 可选目标校验
            if validate_target_before_poke and validate_target_in_private:
                verify_result = await adapter_manager.send_adapter_command(
                    adapter_sign=adapter_sign,
                    command_name="get_stranger_info",
                    command_data={"user_id": effective_user_id},
                    timeout=10.0,
                )
                if verify_result.get("status") != "ok":
                    error_msg = verify_result.get("message", "未知错误")
                    logger.warning(f"目标校验失败: user_id={effective_user_id}, error={error_msg}")
                    return False, f"目标校验失败，操作取消: {error_msg}"

            # 发送戳一戳
            for i in range(actual_count):
                result = await adapter_manager.send_adapter_command(
                    adapter_sign=adapter_sign,
                    command_name="friend_poke",
                    command_data={"user_id": effective_user_id},
                    timeout=10.0
                )
                logger.debug(f"私戳一戳 NapCat 原始响应: 第{i + 1}/{actual_count}次, result={result}")
                if result.get("status") != "ok":
                    error_msg = result.get("message", "未知错误")
                    logger.error(f"发送戳一戳失败: 第{i + 1}/{actual_count}次, 错误: {error_msg}")
                    return False, f"发送戳一戳失败（第{i + 1}/{actual_count}次）: {error_msg}，请检查 NapCat 是否正常运行且 Packet 模式可用"

                if i < actual_count - 1:
                    interval_ms = random.randint(interval_min_ms, interval_max_ms)
                    await asyncio.sleep(interval_ms / 1000.0)

            logger.info(f"已连续戳了用户 {effective_user_id} {actual_count} 次")
            return True, f"已连续戳了用户 {effective_user_id} {actual_count} 次"

        except Exception as e:
            logger.error(f"发送私聊戳一戳时发生异常: {e}", exc_info=True)
            return False, f"发送戳一戳时发生异常: {str(e)}"


# ============================================================================
# 群聊 AOE 戳多个用户
# ============================================================================

class SendGroupPokeMultipleAction(BaseAction):
    """在群聊中 AOE 戳多个用户"""

    action_name = "send_group_poke_multiple"
    action_description = (
        "在群聊中戳多个参与互动的用户（仅群聊环境可用）。"
        "与 send_group_poke 为互斥关系，请根据场景选择："
        "- send_group_poke：单用户连戳多次"
        "- send_group_poke_multiple：多用户各戳一次"
        "参数说明："
        "- user_ids: 目标用户ID列表（必填）。建议从上下文最近有互动的用户中选择。"
        "- max_targets: 最大目标人数上限，默认5，最大10。"
        "- validate_targets: 是否校验目标用户存在，默认true。"
        "群号会从当前会话上下文自动解析，无需传入。"
        "注意：每人只戳一次，不支持连戳。"
    )
    chat_type = ChatType.GROUP

    async def go_activate(self) -> bool:
        """仅在群聊且能解析到群号时激活。"""
        if not _is_plugin_enabled(getattr(self, "plugin", None)):
            return False

        chat_stream = getattr(self, "chat_stream", None)
        if str(getattr(chat_stream, "chat_type", "")) != ChatType.GROUP.value:
            return False

        group_id = await _resolve_group_id_from_stream(chat_stream)
        return _is_positive_numeric_id(group_id)

    async def execute(
        self,
        user_ids: list[str],
        max_targets: int | None = None,
        validate_targets: bool | None = None,
    ) -> tuple[bool, str]:
        """执行 AOE 戳一戳动作

        Args:
            user_ids: 目标用户ID列表
            max_targets: 最大目标人数上限（默认从配置读取）
            validate_targets: 是否校验目标用户存在（默认从配置读取）
        """
        try:
            # 从配置读取默认值
            plugin_obj = getattr(self, "plugin", None)
            config_obj = getattr(plugin_obj, "config", None)
            plugin_config = getattr(config_obj, "plugin", None)

            # max_targets 默认值
            if max_targets is None:
                config_max = 5
                if plugin_config is not None:
                    try:
                        config_max = int(getattr(plugin_config, "aoe_poke_max_targets", 5) or 5)
                    except (TypeError, ValueError):
                        config_max = 5
                max_targets = min(max(config_max, 1), 10)  # 硬上限 10

            # validate_targets 默认值
            if validate_targets is None:
                if plugin_config is not None:
                    validate_targets = bool(getattr(plugin_config, "validate_target_before_aoe_poke", True))
                else:
                    validate_targets = True

            # 参数预处理
            if not user_ids:
                return False, "目标用户列表为空"

            # 限制人数
            effective_max = min(max(max_targets, 1), 10)
            if len(user_ids) > effective_max:
                logger.warning(f"AOE戳一戳目标人数 {len(user_ids)} 超过上限 {effective_max}，已截断")
                user_ids = user_ids[:effective_max]

            # 从上下文解析群ID（不信任 LLM 传入的值）
            chat_stream = getattr(self, "chat_stream", None)
            normalized_group_id = await _resolve_group_id_from_stream(chat_stream) if chat_stream else None

            if not normalized_group_id:
                logger.error(f"AOE戳一戳缺失 group_id: stream_id={getattr(chat_stream, 'stream_id', None)}, "
                             f"current_message={bool(getattr(getattr(chat_stream, 'context', None), 'current_message', None))}")
                return False, "无法获取群号，该会话可能缺少群信息，请尝试重新触发对话后再戳"
            if not _is_positive_numeric_id(normalized_group_id):
                return False, "群号无效，操作取消"

            # 读取动作参数配置
            adapter_sign = _DEFAULT_ADAPTER_SIGN
            interval_min_ms = 100
            interval_max_ms = 200
            if plugin_config is not None:
                try:
                    interval_min_ms = int(getattr(plugin_config, "poke_interval_min_ms", 100) or 0)
                    interval_max_ms = int(getattr(plugin_config, "poke_interval_max_ms", 200) or 0)
                except (TypeError, ValueError):
                    pass

            interval_min_ms = max(0, interval_min_ms)
            interval_max_ms = max(0, interval_max_ms)
            if interval_min_ms > interval_max_ms:
                interval_min_ms, interval_max_ms = interval_max_ms, interval_min_ms

            from src.core.managers.adapter_manager import get_adapter_manager
            adapter_manager = get_adapter_manager()

            # 校验目标用户（可选）
            valid_user_ids: list[str] = []
            invalid_users: list[tuple[str, str]] = []

            if validate_targets:
                for uid in user_ids:
                    normalized_uid = _normalize_numeric_id(uid)
                    if not normalized_uid:
                        invalid_users.append((uid, "无效ID格式"))
                        continue

                    result = await adapter_manager.send_adapter_command(
                        adapter_sign=adapter_sign,
                        command_name="get_group_member_info",
                        command_data={
                            "group_id": normalized_group_id,
                            "user_id": normalized_uid,
                            "no_cache": True,
                        },
                        timeout=10.0,
                    )

                    if result.get("status") == "ok":
                        valid_user_ids.append(normalized_uid)
                    else:
                        error = result.get("message", "未知错误")
                        invalid_users.append((uid, error))

                if not valid_user_ids:
                    error_detail = "; ".join([f"{uid}({err})" for uid, err in invalid_users])
                    return False, f"所有目标用户校验失败: {error_detail}"
            else:
                valid_user_ids = [
                    uid for uid in user_ids
                    if _normalize_numeric_id(uid)
                ]
                if not valid_user_ids:
                    return False, "目标用户列表中无有效ID"

            # 执行 AOE 戳一戳
            success_users: list[str] = []
            failed_users: list[tuple[str, str]] = []

            for i, uid in enumerate(valid_user_ids):
                result = await adapter_manager.send_adapter_command(
                    adapter_sign=adapter_sign,
                    command_name="group_poke",
                    command_data={
                        "group_id": normalized_group_id,
                        "user_id": uid,
                    },
                    timeout=10.0,
                )

                logger.debug(f"AOE戳一戳 NapCat 原始响应: uid={uid}, result={result}")
                if result.get("status") == "ok":
                    success_users.append(uid)
                else:
                    error = result.get("message", "未知错误")
                    failed_users.append((uid, error))

                # 间隔延迟，降低风控
                if i < len(valid_user_ids) - 1:
                    interval_ms = random.randint(interval_min_ms, interval_max_ms)
                    await asyncio.sleep(interval_ms / 1000.0)

            # 汇总结果
            if success_users:
                success_msg = f"成功戳了 {len(success_users)} 人: {', '.join(success_users)}"
                if failed_users:
                    fail_msg = f"，失败 {len(failed_users)} 人: {', '.join([f'{u}({e})' for u, e in failed_users])}"
                    logger.info(f"AOE戳一戳结果: {success_msg}{fail_msg}")
                    return True, success_msg + fail_msg
                else:
                    if invalid_users:
                        success_msg += f"（另有 {len(invalid_users)} 人因校验失败跳过）"
                    logger.info(f"AOE戳一戳完成: {success_msg}")
                    return True, success_msg
            else:
                return False, f"AOE戳一戳全部失败: {', '.join([f'{u}({e})' for u, e in failed_users])}"

        except Exception as e:
            logger.error(f"AOE戳一戳时发生异常: {e}", exc_info=True)
            return False, f"AOE戳一戳时发生异常: {str(e)}"
