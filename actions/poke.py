"""发送戳一戳动作"""

from __future__ import annotations

import asyncio
import random
import re

from src.core.components.base import BaseAction
from src.core.components.types import ChatType
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("notice_injector")

_DEFAULT_ADAPTER_SIGN = "onebot_adapter:adapter:onebot_adapter"


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


def _normalize_nickname(value: object) -> str | None:
    """将输入归一化为可查询的目标（去 @ 与首尾空白）。"""
    if value is None:
        return None
    text = str(value).strip().lstrip("@").strip()
    return text or None


async def _resolve_user_id_from_db(platform: str, nickname: str) -> str | None:
    """通过本地用户库按昵称/群名片解析用户 ID。"""
    try:
        from src.app.plugin_system.api.person_api import resolve_user_id

        return await resolve_user_id(platform, nickname)
    except Exception as e:
        logger.debug(f"本地库昵称解析失败: nickname={nickname}, error={e}")
        return None


async def _fetch_group_member_list(
    adapter_manager: object,
    adapter_sign: str,
    group_id: str,
) -> list[dict] | None:
    """拉取群成员列表；失败时返回 None。"""
    try:
        result = await adapter_manager.send_adapter_command(
            adapter_sign=adapter_sign,
            command_name="get_group_member_list",
            command_data={"group_id": group_id},
            timeout=10.0,
        )
        if not isinstance(result, dict) or result.get("status") != "ok":
            error = result.get("message", "未知错误") if isinstance(result, dict) else result
            logger.warning(f"获取群成员列表失败: group_id={group_id}, error={error}")
            return None
        data = result.get("data")
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [m for m in data["data"] if isinstance(m, dict)]
        return None
    except Exception as e:
        logger.debug(f"获取群成员列表异常: {e}")
        return None


def _match_member_by_name(members: list[dict], nickname: str) -> str | None:
    """在成员列表中按昵称/群名片匹配，唯一命中才返回 user_id。"""
    normalized = nickname.lower()
    exact: list[str] = []
    for m in members:
        name = str(m.get("nickname") or "").strip().lower()
        card = str(m.get("card") or "").strip().lower()
        if name == normalized or card == normalized:
            uid = str(m.get("user_id") or "").strip()
            if uid and uid not in exact:
                exact.append(uid)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    partial: list[str] = []
    for m in members:
        name = str(m.get("nickname") or "").strip().lower()
        card = str(m.get("card") or "").strip().lower()
        if normalized in name or normalized in card:
            uid = str(m.get("user_id") or "").strip()
            if uid and uid not in partial:
                partial.append(uid)
    return partial[0] if len(partial) == 1 else None


async def _resolve_target_from_context(chat_stream: object, platform: str) -> str | None:
    """从当前消息解析目标用户 ID：优先消息中的 @ 对象，私聊回退到发送者。"""
    context = getattr(chat_stream, "context", None)
    current_message = getattr(context, "current_message", None)
    if not current_message:
        return None

    text = str(
        getattr(current_message, "processed_plain_text", "")
        or getattr(current_message, "content", "")
        or ""
    )
    at_ids = re.findall(r"@<[^>]+?:(\d+)>", text)
    if at_ids:
        bot_id = ""
        try:
            from src.app.plugin_system.api import adapter_api

            bot_info = await adapter_api.get_bot_info_by_platform(platform) or {}
            bot_id = str(bot_info.get("bot_id") or "")
        except Exception:
            bot_id = ""
        for at_id in at_ids:
            if at_id != bot_id:
                return at_id

    chat_type = str(getattr(chat_stream, "chat_type", ""))
    if chat_type == ChatType.PRIVATE.value:
        sender_id = str(getattr(current_message, "sender_id", "") or "").strip()
        if sender_id:
            return sender_id
    return None


async def _resolve_effective_user_id(
    *,
    raw_user_id: object,
    target_user_id: str | None,
    platform: str,
    chat_stream: object,
    adapter_manager: object,
    adapter_sign: str,
    group_id: str | None = None,
) -> tuple[str | None, str]:
    """解析最终目标用户 ID。

    解析链：
    1. 显式数字 ID 直接使用；
    2. 显式昵称：本地用户库 → 群成员列表（仅群聊可用）；
    3. 未提供目标：从当前消息上下文解析（@ 对象 / 私聊发送者）；
    4. 均失败：返回错误说明，不执行戳一戳。

    Returns:
        (user_id, 错误说明)；成功时错误说明为空。
    """
    explicit = target_user_id if target_user_id is not None else raw_user_id
    nickname = _normalize_nickname(explicit)

    if nickname:
        if nickname.isdigit():
            return nickname, ""
        # 昵称：先本地库，再群成员列表
        uid = await _resolve_user_id_from_db(platform, nickname)
        if uid:
            return uid, ""
        if group_id:
            members = await _fetch_group_member_list(adapter_manager, adapter_sign, group_id)
            if members is not None:
                uid = _match_member_by_name(members, nickname)
                if uid:
                    return uid, ""
                return None, f"群内未找到唯一匹配“{nickname}”的用户，请提供 QQ 号或更具体的称呼"
        return None, f"无法解析目标用户“{nickname}”，请提供 QQ 号"

    uid = await _resolve_target_from_context(chat_stream, platform)
    if uid:
        return uid, ""
    return None, "未提供目标用户，且无法从当前消息解析，请明确要戳谁"


# ============================================================================
# 群聊单用户连续戳
# ============================================================================

class SendGroupPokeAction(BaseAction):
    """在群聊中戳一戳指定用户（支持连续戳）"""

    name = "send_group_poke"
    associated_platforms = ["qq"]
    description = (
        "在群聊中向指定用户发送戳一戳动作（仅群聊环境可用）。"
        "支持通过 poke_count 指定连续戳一戳次数；"
        "user_id 或 target_user_id 可填目标用户的 QQ 号，也可填昵称/群名片（例如“凉粉”），系统会自动解析；不填时尝试从消息中的 @ 对象解析。"
        "群号会从当前会话上下文自动解析，无需传入。"
        "请结合上下文与提示词决定次数。"
        "连戳次数受插件配置 max_poke_count 限制（硬上限 10），超出会自动截断。"
    )
    # chat_type 声明为 ALL：核心静态过滤对非 ALL 的 chat_type 会按传入参数粗筛，
    # 而 chatter 调用时未透传实际 chat_type（PR #140 后的行为），导致 GROUP/PRIVATE
    # 动作被静默剔除。真实场景判定由下方 go_activate() 完成（群聊+群号解析）。
    chat_type = ChatType.ALL
    associated_types = ["text"]

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
        user_id: str | None = None,
        poke_count: int = 1,
        target_user_id: str | None = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行群聊戳一戳动作

        Args:
            user_id: 目标用户 QQ 号或昵称（可选，系统会自动解析）
            poke_count: 连续戳一戳次数（默认1，最大10）
            target_user_id: 可选，显式目标用户（QQ号或昵称）
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

            # 从上下文解析群ID（不信任 LLM 传入的值）
            effective_group_id = await _resolve_group_id_from_stream(chat_stream) if chat_stream else None

            # 群聊 Action 必须有 group_id
            if not effective_group_id:
                logger.error(f"群聊戳一戳缺失 group_id: stream_id={getattr(chat_stream, 'stream_id', None)}, "
                             f"current_message={bool(getattr(getattr(chat_stream, 'context', None), 'current_message', None))}")
                return False, "无法获取群号，该会话可能缺少群信息，请尝试重新触发对话后再戳"
            if not _is_positive_numeric_id(effective_group_id):
                return False, "群号无效，操作取消"

            # 解析目标用户（QQ号 / 昵称 / 上下文）
            effective_user_id, resolve_err = await _resolve_effective_user_id(
                raw_user_id=user_id,
                target_user_id=target_user_id,
                platform=str(getattr(chat_stream, "platform", "qq") or "qq"),
                chat_stream=chat_stream,
                adapter_manager=adapter_manager,
                adapter_sign=adapter_sign,
                group_id=effective_group_id,
            )
            if not effective_user_id:
                return False, resolve_err or "目标用户ID无效，操作取消"

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

    name = "send_private_poke"
    associated_platforms = ["qq"]
    description = (
        "在私聊/好友环境中向指定用户发送戳一戳动作（仅私聊环境可用）。"
        "支持通过 poke_count 指定连续戳一戳次数；"
        "user_id 或 target_user_id 可填目标用户的 QQ 号，也可填昵称（系统自动解析）；不填时默认戳当前私聊对象。"
        "请结合上下文与提示词决定次数。"
        "连戳次数受插件配置 max_poke_count 限制（硬上限 10），超出会自动截断。"
    )
    # chat_type 声明为 ALL：见 SendGroupPokeAction 注释，场景判定由 go_activate() 完成。
    chat_type = ChatType.ALL
    associated_types = ["text"]

    async def go_activate(self) -> bool:
        """仅在私聊中激活。"""
        if not _is_plugin_enabled(getattr(self, "plugin", None)):
            return False

        chat_stream = getattr(self, "chat_stream", None)
        return str(getattr(chat_stream, "chat_type", "")) == ChatType.PRIVATE.value

    async def execute(
        self,
        user_id: str | None = None,
        poke_count: int = 1,
        target_user_id: str | None = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行私聊戳一戳动作

        Args:
            user_id: 目标用户 QQ 号或昵称（可选，系统会自动解析）
            poke_count: 连续戳一戳次数（默认1，最大10）
            target_user_id: 可选，显式目标用户（QQ号或昵称）
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

            # 解析目标用户（QQ号 / 昵称 / 私聊发送者）
            effective_user_id, resolve_err = await _resolve_effective_user_id(
                raw_user_id=user_id,
                target_user_id=target_user_id,
                platform=str(getattr(chat_stream, "platform", "qq") or "qq") if chat_stream else "qq",
                chat_stream=chat_stream,
                adapter_manager=adapter_manager,
                adapter_sign=adapter_sign,
                group_id=None,
            )
            if not effective_user_id:
                return False, resolve_err or "目标用户ID无效，操作取消"
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

    name = "send_group_poke_multiple"
    associated_platforms = ["qq"]
    description = (
        "在群聊中戳多个参与互动的用户（仅群聊环境可用）。"
        "与 send_group_poke 为互斥关系，请根据场景选择："
        "- send_group_poke：单用户连戳多次"
        "- send_group_poke_multiple：多用户各戳一次"
        "参数说明："
        "- user_ids: 目标用户ID列表（必填），可填 QQ 号或昵称，系统会自动解析。建议从上下文最近有互动的用户中选择。"
        "- max_targets: 最大目标人数上限，默认5，最大10。"
        "- validate_targets: 是否校验目标用户存在，默认true。"
        "群号会从当前会话上下文自动解析，无需传入。"
        "注意：每人只戳一次，不支持连戳。"
    )
    # chat_type 声明为 ALL：见 SendGroupPokeAction 注释，场景判定由 go_activate() 完成。
    chat_type = ChatType.ALL
    associated_types = ["text"]

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
        user_ids: list[str] | None = None,
        max_targets: int | None = None,
        validate_targets: bool | None = None,
    ) -> tuple[bool, str]:
        """执行 AOE 戳一戳动作

        Args:
            user_ids: 目标用户ID列表（可填 QQ 号或昵称）
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

            # 解析并校验目标用户（一次拉取成员列表 + 本地过滤）
            platform = str(getattr(chat_stream, "platform", "qq") or "qq") if chat_stream else "qq"
            targets = [(_normalize_nickname(uid), uid) for uid in user_ids]
            needs_members = validate_targets or any(
                nickname is not None and not nickname.isdigit() for nickname, _ in targets
            )
            members = (
                await _fetch_group_member_list(adapter_manager, adapter_sign, normalized_group_id)
                if needs_members
                else None
            )
            member_ids = {str(m.get("user_id") or "") for m in members} if members else None

            valid_user_ids: list[str] = []
            invalid_users: list[tuple[str, str]] = []

            for nickname, raw_uid in targets:
                if not nickname:
                    invalid_users.append((raw_uid, "无效目标格式"))
                    continue

                if nickname.isdigit():
                    resolved = nickname
                else:
                    resolved = await _resolve_user_id_from_db(platform, nickname)
                    if not resolved and members:
                        resolved = _match_member_by_name(members, nickname)
                    if not resolved:
                        invalid_users.append((raw_uid, f"无法解析目标“{nickname}”"))
                        continue

                if validate_targets:
                    if member_ids is not None:
                        if resolved not in member_ids:
                            invalid_users.append((raw_uid, "目标不是群成员"))
                            continue
                    else:
                        result = await adapter_manager.send_adapter_command(
                            adapter_sign=adapter_sign,
                            command_name="get_group_member_info",
                            command_data={
                                "group_id": normalized_group_id,
                                "user_id": resolved,
                                "no_cache": True,
                            },
                            timeout=10.0,
                        )
                        if result.get("status") != "ok":
                            error = result.get("message", "未知错误")
                            invalid_users.append((raw_uid, error))
                            continue

                valid_user_ids.append(resolved)

            if not valid_user_ids:
                error_detail = "; ".join([f"{uid}({err})" for uid, err in invalid_users])
                return False, f"所有目标用户校验失败: {error_detail}"

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
