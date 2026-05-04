"""
VoiceDownloader - AstrBot 语音下载插件

解决 AstrBot 与 QQ 客户端不在同一台机器上时，语音消息的本地文件路径无效的问题。
当检测到 Record 类型消息且本地路径不存在时，自动从远程 URL 下载语音文件到临时目录，
并更新消息 data 中的 path 和 file 字段。

Author: maomaosamaqwq
Version: 1.0.0
"""

import os
import time
import asyncio
import uuid
import shutil
import platform
from pathlib import Path

import aiohttp
import aiofiles

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.message.components import Record


# ============================================================
# 辅助函数
# ============================================================

def _get_temp_dir() -> str:
    """获取语音文件临时存储目录（跨平台）"""
    system = platform.system()
    if system == "Windows":
        temp_dir = os.path.join("C:\\", "Temp", "astrbot_voice")
    else:
        temp_dir = "/tmp/astrbot_voice"
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def _ensure_ffmpeg() -> bool:
    """检查 ffmpeg 是否可用"""
    return shutil.which("ffmpeg") is not None


# ============================================================
# 插件主类
# ============================================================

@register(
    "voice_downloader",
    "maomaosamaqwq",
    "自动下载远程语音消息到本地，解决跨机语音路径无效问题",
    "1.0.0",
    repo="https://github.com/maomaosamaqwq/VoiceDownloader",
)
class VoiceDownloader(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.temp_dir = _get_temp_dir()
        self.ffmpeg_available = _ensure_ffmpeg()
        self._cleanup_task = None

        # —— 配置项 ——
        self.config = {
            "auto_cleanup": True,           # 是否自动清理过期文件
            "cleanup_interval": 3600,        # 清理间隔（秒）
            "max_file_age": 86400,           # 文件最大存活时间（秒）
            "convert_amr_to_wav": True,      # 是否将 amr 转码为 wav
        }

    # ----------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------

    async def initialize(self):
        """插件初始化：启动定时清理任务"""
        logger.info(f"VoiceDownloader: 临时目录 = {self.temp_dir}")
        logger.info(f"VoiceDownloader: ffmpeg {'可用' if self.ffmpeg_available else '不可用'}")

        if self.config["auto_cleanup"]:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            logger.info(
                f"VoiceDownloader: 定时清理任务已启动（间隔 {self.config['cleanup_interval']} 秒）"
            )

    async def terminate(self):
        """插件卸载：停止清理任务并清理临时文件"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            logger.info("VoiceDownloader: 临时文件已清理")

    # ----------------------------------------------------------
    # 核心逻辑：监听 AdapterMessageEvent，处理 Record 组件
    # ----------------------------------------------------------

    @filter.on_decorating_result()
    async def on_voice_message(self, event: AstrMessageEvent):
        """
        在消息发送前（on_decorating_result）处理语音组件。
        检测消息中的 Record 类型，如果本地路径无效则自动下载。
        """
        message_chain = event.get_messages()
        modified = False

        for comp in message_chain:
            if not isinstance(comp, Record):
                continue

            # ------ 提取字段 ------
            path_field = getattr(comp, "path", None) or ""
            url_field = getattr(comp, "url", None) or ""
            file_field = getattr(comp, "file", None) or ""

            # 确定下载用的 URL
            download_url = url_field or (file_field if file_field.startswith("http") else "")

            # 没有远程 URL → 跳过
            if not download_url:
                if path_field and os.path.exists(path_field):
                    logger.debug(f"VoiceDownloader: 语音已存在，跳过: {path_field}")
                else:
                    logger.debug("VoiceDownloader: 语音消息无远程 URL，跳过")
                continue

            # 如果本地路径已存在 → 跳过
            if path_field and os.path.exists(path_field):
                logger.debug(f"VoiceDownloader: 语音文件已存在，跳过: {path_field}")
                continue

            # ------ 执行下载 ------
            ext = self._guess_extension(download_url, file_field)
            ts = int(time.time() * 1000)
            msg_id = getattr(
                getattr(event, "message_obj", None), "message_id", str(uuid.uuid4())[:8]
            )
            safe_filename = f"voice_{msg_id}_{ts}{ext}"
            save_path = os.path.join(self.temp_dir, safe_filename)

            logger.info(f"VoiceDownloader: 开始下载语音: {download_url}")

            try:
                await self._download_file(download_url, save_path)
            except Exception as e:
                logger.warning(f"VoiceDownloader: 语音下载失败: {e}")
                continue

            # ------ 可选：amr -> wav 转码 ------
            if self.config.get("convert_amr_to_wav", True) and self.ffmpeg_available:
                converted = await self._convert_amr_to_wav(save_path)
                if converted:
                    save_path = converted
                    safe_filename = os.path.basename(save_path)

            # ------ 更新消息组件 ------
            comp.path = save_path
            comp.file = save_path
            modified = True

            logger.info(f"VoiceDownloader: 语音下载完成: {save_path}")

        if modified:
            # 可选：通知用户语音已下载
            # yield event.plain_result("🔊 语音已自动下载")
            pass

        return None

    # ----------------------------------------------------------
    # 内部工具方法
    # ----------------------------------------------------------

    async def _download_file(self, url: str, save_path: str) -> None:
        """使用 aiohttp 异步下载文件"""
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                async with aiofiles.open(save_path, "wb") as f:
                    await f.write(await resp.read())

    async def _convert_amr_to_wav(self, file_path: str) -> str | None:
        """使用 ffmpeg 将 amr 转码为 wav"""
        if not file_path.lower().endswith(".amr"):
            return None

        wav_path = file_path.rsplit(".", 1)[0] + ".wav"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", file_path, wav_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                f"VoiceDownloader: ffmpeg 转码失败: {stderr.decode(errors='ignore')}"
            )
            return None

        try:
            os.remove(file_path)
        except OSError:
            pass

        logger.info(f"VoiceDownloader: amr -> wav 转码完成: {wav_path}")
        return wav_path

    def _guess_extension(self, url: str, file_name: str) -> str:
        """根据 URL 或文件名猜测扩展名"""
        # 从 URL 路径部分提取
        path_part = url.split("?")[0] if "?" in url else url
        ext = os.path.splitext(path_part)[1].lower()
        if ext in (".amr", ".silk", ".wav", ".mp3", ".ogg", ".pcm"):
            return ext
        # 从文件名提取
        if file_name:
            ext = os.path.splitext(file_name)[1].lower()
            if ext:
                return ext
        return ".silk"

    async def _periodic_cleanup(self):
        """定时清理过期的临时语音文件"""
        while True:
            await asyncio.sleep(self.config["cleanup_interval"])
            await self._cleanup_expired_files()

    async def _cleanup_expired_files(self):
        """清理超过 max_file_age 的临时文件"""
        if not os.path.exists(self.temp_dir):
            return
        now = time.time()
        count = 0
        for fname in os.listdir(self.temp_dir):
            fpath = os.path.join(self.temp_dir, fname)
            try:
                if os.path.isfile(fpath) and (
                    now - os.path.getmtime(fpath)
                ) > self.config["max_file_age"]:
                    os.remove(fpath)
                    count += 1
            except OSError:
                pass
        if count > 0:
            logger.info(f"VoiceDownloader: 清理了 {count} 个过期语音文件")
