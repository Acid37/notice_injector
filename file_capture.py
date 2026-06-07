"""NapCat 文件捕获模块。

通过直接连接 NapCat SSE 服务器的 WebSocket 端口，
独立捕获 group_upload 原始事件，提取 file_id / busid 等元数据。
API 请求同样通过 WebSocket 发送（OneBot v11 双向通信）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("notice_injector")

# 默认存储上限
_MAX_FILES_PER_GROUP = 100


class FileCapture:
    """NapCat 文件捕获器。

    连接 NapCat SSE 服务器的 WebSocket 端口，监听 group_upload 事件，
    将 file_id / busid 等元数据保存在内存中供 download_group_file action 使用。
    API 请求（如 get_group_file_url）通过同一 WebSocket 连接发送。
    """

    def __init__(self) -> None:
        self._ws_url: str = ""
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._running: bool = False
        # {group_id_str: {file_name: {file_id, busid, name, size, url, timestamp}}}
        self._uploads: dict[str, dict[str, dict]] = {}
        # WebSocket API 请求响应关联
        self._api_pending: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _process_event(self, event: dict) -> None:
        """处理一条 OneBot 事件，提取 group_upload 信息。"""
        if event.get("post_type") != "notice":
            return
        if event.get("notice_type") != "group_upload":
            return

        group_id = str(event.get("group_id", ""))
        file_info = event.get("file")
        if not group_id or not file_info:
            return

        file_id = file_info.get("id", "")
        busid = file_info.get("busid", 0)
        name = file_info.get("name", "")
        size = file_info.get("size", 0)
        url = file_info.get("url", "")

        if not file_id or not name:
            logger.debug(f"[FileCapture] group_upload 缺少 file_id 或 name，跳过")
            return

        if group_id not in self._uploads:
            self._uploads[group_id] = {}

        self._uploads[group_id][name] = {
            "file_id": file_id,
            "busid": busid,
            "name": name,
            "size": size,
            "url": url,
            "timestamp": time.time(),
        }

        # 按上限裁剪最旧的条目
        group_files = self._uploads[group_id]
        if len(group_files) > _MAX_FILES_PER_GROUP:
            oldest_key = min(group_files, key=lambda k: group_files[k]["timestamp"])
            del group_files[oldest_key]

        logger.info(
            f"[FileCapture] 捕获群 {group_id} 文件上传: {name} "
            f"(file_id={file_id}, busid={busid}, size={size})"
        )

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def lookup(self, group_id: str, file_name: str) -> dict | None:
        """根据群号和文件名查找文件元数据。

        同名文件返回最新上传的那一个。
        """
        group_files = self._uploads.get(str(group_id))
        if not group_files:
            return None
        return group_files.get(file_name)

    def list_recent(self, group_id: str, limit: int = 10) -> list[dict]:
        """列出指定群最近上传的文件（按时间倒序）。"""
        group_files = self._uploads.get(str(group_id))
        if not group_files:
            return []
        sorted_files = sorted(
            group_files.values(), key=lambda f: f["timestamp"], reverse=True
        )
        return sorted_files[:limit]

    # ------------------------------------------------------------------
    # NapCat WebSocket API
    # ------------------------------------------------------------------

    async def send_api(self, action: str, params: dict, timeout: float = 15.0) -> dict | None:
        """通过 WebSocket 发送 OneBot v11 API 请求并等待响应。

        Args:
            action: API 名称，如 'get_group_file_url'
            params: API 参数
            timeout: 超时秒数

        Returns:
            NapCat 响应 dict，失败返回 None
        """
        if not self._ws:
            logger.warning("[FileCapture] WebSocket 未连接，无法发送 API 请求")
            return None

        echo = str(uuid.uuid4())
        request_payload = {"action": action, "params": params, "echo": echo}

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._api_pending[echo] = future

        try:
            await self._ws.send(json.dumps(request_payload))
            logger.debug(f"[FileCapture] 已发送 API 请求: {action} (echo={echo})")
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[FileCapture] API 请求超时: {action}")
            return None
        except Exception as e:
            logger.error(f"[FileCapture] API 请求异常: {action} -> {e}", exc_info=True)
            return None
        finally:
            self._api_pending.pop(echo, None)

    async def get_file_url(
        self, group_id: str, file_id: str, busid: int
    ) -> str | None:
        """通过 NapCat WebSocket API 获取文件临时下载链接。"""
        params = {"group_id": int(group_id), "file_id": file_id, "busid": busid}
        result = await self.send_api("get_group_file_url", params)
        if not result:
            return None

        # NapCat 响应：status=ok 或 retcode=0 表示成功
        if result.get("status") == "ok" or result.get("retcode") == 0:
            data = result.get("data") or {}
            file_url = data.get("url") or result.get("url")
            if file_url:
                return file_url

        logger.warning(f"[FileCapture] get_group_file_url 返回异常: {result}")
        return None

    async def list_group_files(self, group_id: str) -> list[dict]:
        """通过 NapCat API 列出群根目录文件（fallback 用）。

        Returns:
            文件元数据列表，每项包含 file_id / busid / name / size。
            失败返回空列表。
        """
        params = {"group_id": int(group_id)}
        result = await self.send_api("get_group_root_files", params)
        if not result:
            return []

        if result.get("status") != "ok" and result.get("retcode") != 0:
            logger.warning(f"[FileCapture] get_group_root_files 返回异常: {result}")
            return []

        data = result.get("data") or {}
        files = data.get("files") or []

        out: list[dict] = []
        for f in files:
            file_id = f.get("file_id", "")
            busid = f.get("busid", 0)
            name = f.get("file_name", "")
            if file_id and name:
                out.append({
                    "file_id": file_id,
                    "busid": busid,
                    "name": name,
                    "size": f.get("file_size", f.get("size", 0)),
                })
        logger.debug(f"[FileCapture] 列出群 {group_id} 根目录文件: {len(out)} 个")
        return out

    async def download_file(self, file_url: str, save_path: Path) -> bool:
        """通过 HTTP 下载文件到指定路径。"""
        try:
            timeout = aiohttp.ClientTimeout(total=300)  # 大文件给 5 分钟
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(file_url) as resp:
                    if resp.status != 200:
                        logger.error(
                            f"[FileCapture] 下载失败，HTTP {resp.status}"
                        )
                        return False
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(save_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"[FileCapture] 下载文件异常: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, ws_url: str) -> None:
        """启动捕获器，在后台建立 WebSocket 连接。"""
        self._ws_url = ws_url
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"[FileCapture] 已启动，目标: {ws_url}")

    async def stop(self) -> None:
        """停止捕获器，关闭连接。"""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("[FileCapture] 已停止")

    async def _run_loop(self) -> None:
        """主循环：连接 → 监听 → 断线重连。"""
        import websockets
        from websockets.exceptions import (
            ConnectionClosed,
            InvalidStatusCode,
        )

        while self._running:
            try:
                logger.info(f"[FileCapture] 正在连接 {self._ws_url} ...")
                async with websockets.connect(
                    self._ws_url, max_size=10 * 1024 * 1024
                ) as ws:
                    self._ws = ws
                    logger.info("[FileCapture] WebSocket 连接成功")
                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            self._handle_incoming(msg)
                        except (json.JSONDecodeError, Exception) as e:
                            logger.debug(f"[FileCapture] 消息解析失败: {e}")
            except (ConnectionClosed, InvalidStatusCode) as e:
                if self._running:
                    logger.warning(
                        f"[FileCapture] WebSocket 连接关闭 ({e})，5 秒后重连"
                    )
            except OSError as e:
                if self._running:
                    logger.warning(
                        f"[FileCapture] 网络错误 ({e})，10 秒后重连"
                    )
                    await asyncio.sleep(10)
                    continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.error(
                        f"[FileCapture] 未知异常 ({e})，10 秒后重连",
                        exc_info=True,
                    )
                    await asyncio.sleep(10)
                    continue

            # 重连前清理所有待处理的 API 请求
            for echo, fut in list(self._api_pending.items()):
                if not fut.done():
                    fut.set_exception(
                        ConnectionError("WebSocket 连接已断开")
                    )
            self._api_pending.clear()

            if self._running:
                await asyncio.sleep(5)

        self._ws = None

    def _handle_incoming(self, msg: dict) -> None:
        """处理一条 WebSocket 消息：区分 API 响应和事件推送。"""
        echo = msg.get("echo")
        if echo and echo in self._api_pending:
            # 这是一条 API 响应
            future = self._api_pending.pop(echo)
            if not future.done():
                future.set_result(msg)
                logger.debug(f"[FileCapture] API 响应已匹配: echo={echo}")
            return

        # 普通事件推送
        post_type = msg.get("post_type", "")
        logger.debug(
            f"[FileCapture] 收到事件: post_type={post_type}, "
            f"notice_type={msg.get('notice_type', '')}"
        )
        self._process_event(msg)
