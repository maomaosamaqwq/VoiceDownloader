"""
VoiceDownloader - AstrBot 语音下载插件

解决 AstrBot 与 QQ 客户端不在同一台机器上时，语音消息的本地文件路径无效的问题。

工作原理：
- 在插件初始化时 patch Record 类的 convert_to_file_path 方法
- 补丁版本会检查 url 字段，自动下载远程语音到本地临时目录
- 下载后更新 path 和 file 字段，使后续流程（STT、Agent 等）都能正常访问
- 实现定时清理功能

Author: maomaosamaqwq
Version: 1.0.1
"""

import os
import time
import asyncio
import uuid
import shutil
import platform

import aiohttp
import aiofiles

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.message.components import Record


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
    return shutil.which("ffmpeg") is not None


# ============================================================
# 语音下载器（工具类，供补丁方法调用）
# ============================================================

class VoiceDownloaderCore:
    """核心语音下载逻辑"""

    def __init__(self, temp_dir: str, ffmpeg_available: bool, convert_amr_to_wav: bool):
        self.temp_dir = temp_dir
        self.ffmpeg_avail = ffmpeg_available
        self.convert_amr = convert_amr_to_wav

    async def ensure_voice_file(self, comp: Record) -> bool:
        """确保 Record 组件指向有效的本地文件。返回 True 表示有修改。"""
        file_val = getattr(comp, "file", None) or ""
        url_val = getattr(comp, "url", None) or ""
        path_val = getattr(comp, "path", None) or ""

        # 已有有效路径 → 跳过
        if path_val and os.path.exists(path_val):
            return False
        if file_val and os.path.exists(file_val):
            return False

        # 确定下载 URL
        download_url = url_val or (file_val if file_val.startswith("http") else "")

        # file:// 协议 → 尝试直接使用
        if file_val.startswith("file:///"):
            local_path = file_val[8:]
            if os.path.exists(local_path):
                comp.path = local_path
                comp.file = local_path
                return True

        if not download_url:
            return False

        # 下载
        ext = self._guess_extension(download_url, file_val)
        ts = int(time.time() * 1000)
        safe_filename = f"voice_{uuid.uuid4().hex[:8]}_{ts}{ext}"
        save_path = os.path.join(self.temp_dir, safe_filename)

        logger.info(f"VoiceDownloader: 下载语音 -> {save_path}")

        try:
            await self._download_file(download_url, save_path)
        except Exception as e:
            logger.warning(f"VoiceDownloader: 下载失败: {e}")
            return False

        # amr → wav 转码
        if self.convert_amr and self.ffmpeg_avail:
            converted = await self._convert_amr_to_wav(save_path)
            if converted:
                save_path = converted

        comp.path = save_path
        comp.file = save_path
        logger.info(f"VoiceDownloader: 语音就绪: {save_path}")
        return True

    async def _download_file(self, url: str, save_path: str) -> None:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                async with aiofiles.open(save_path, "wb") as f:
                    await f.write(await resp.read())

    async def _convert_amr_to_wav(self, file_path: str) -> str | None:
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
            logger.warning(f"ffmpeg 转码失败: {stderr.decode(errors='ignore')[:200]}")
            return None
        try:
            os.remove(file_path)
        except OSError:
            pass
        return wav_path

    @staticmethod
    def _guess_extension(url: str, file_name: str) -> str:
        path_part = url.split("?")[0] if "?" in url else url
        ext = os.path.splitext(path_part)[1].lower()
        if ext in (".amr", ".silk", ".wav", ".mp3", ".ogg", ".pcm"):
            return ext
        if file_name:
            ext = os.path.splitext(file_name)[1].lower()
            if ext:
                return ext
        return ".silk"


# ============================================================
# 补丁：替换 Record.convert_to_file_path 方法
# ============================================================

_original_convert = Record.convert_to_file_path
_downloader_instance: VoiceDownloaderCore | None = None


async def _patched_convert_to_file_path(self: Record) -> str:
    """替换 Record.convert_to_file_path，增加 url 下载支持。"""
    # 1. 尝试原始逻辑
    file_val = self.file
    if file_val and file_val.startswith("file:///"):
        local = file_val[8:]
        if os.path.exists(local):
            return os.path.abspath(local)
    if file_val and file_val.startswith("http"):
        path = await download_image_by_url_compat(file_val)
        return os.path.abspath(path)
    if file_val and file_val.startswith("base64://"):
        import base64
        bs64_data = file_val.removeprefix("base64://")
        image_bytes = base64.b64decode(bs64_data)
        file_path = os.path.join(
            _get_temp_dir(), f"recordseg_{uuid.uuid4()}.amr"
        )
        with open(file_path, "wb") as f:
            f.write(image_bytes)
        return os.path.abspath(file_path)
    if file_val and os.path.exists(file_val):
        return os.path.abspath(file_val)

    # 2. 原始逻辑失败 → 尝试 url 下载
    url_val = getattr(self, "url", None) or ""
    if url_val:
        core = _get_downloader()
        await core.ensure_voice_file(self)
        # 如果下载成功，self.file 已被更新
        if self.file and os.path.exists(self.file):
            return os.path.abspath(self.file)

    # 3. 全部失败
    raise Exception(f"not a valid file: {self.file}")


def _get_downloader() -> VoiceDownloaderCore:
    global _downloader_instance
    if _downloader_instance is None:
        _downloader_instance = VoiceDownloaderCore(
            temp_dir=_get_temp_dir(),
            ffmpeg_available=_ensure_ffmpeg(),
            convert_amr_to_wav=True,
        )
    return _downloader_instance


# aiohttp 兼容：用 _download_and_save_image 替代 download_image_by_url
async def download_image_by_url_compat(url: str) -> str:
    """兼容 download_image_by_url 的语义：下载文件到临时目录并返回路径"""
    temp_dir = _get_temp_dir()
    ext = _downloader_instance._guess_extension(url, "") if _downloader_instance else ".silk"
    filename = f"voice_{uuid.uuid4().hex[:8]}{ext}"
    save_path = os.path.join(temp_dir, filename)
    core = _get_downloader()
    await core._download_file(url, save_path)
    return save_path


# ============================================================
# 插件主类
# ============================================================

@register(
    "voice_downloader",
    "maomaosamaqwq",
    "自动下载远程语音消息到本地，解决跨机语音路径无效问题",
    "1.0.1",
    repo="https://github.com/maomaosamaqwq/astrbot_plugin_VoiceDownloader",
)
class VoiceDownloader(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.temp_dir = _get_temp_dir()
        self.ffmpeg_available = _ensure_ffmpeg()
        self._cleanup_task = None
        self._patched = False
        self.config = {
            "auto_cleanup": True,
            "cleanup_interval": 3600,
            "max_file_age": 86400,
            "convert_amr_to_wav": True,
        }

    async def initialize(self):
        """插件初始化：打补丁 + 启动清理"""
        # 设置全局下载器
        global _downloader_instance
        _downloader_instance = VoiceDownloaderCore(
            temp_dir=self.temp_dir,
            ffmpeg_available=self.ffmpeg_available,
            convert_amr_to_wav=self.config["convert_amr_to_wav"],
        )

        # 打补丁：替换 Record.convert_to_file_path
        if not self._patched:
            Record.convert_to_file_path = _patched_convert_to_file_path
            self._patched = True
            logger.info("VoiceDownloader: Record.convert_to_file_path 已打补丁 ✓")

        logger.info(f"VoiceDownloader: 临时目录 = {self.temp_dir}")
        logger.info(f"VoiceDownloader: ffmpeg {'可用' if self.ffmpeg_available else '不可用'}")

        if self.config["auto_cleanup"]:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            logger.info(f"VoiceDownloader: 定时清理已启动（间隔 {self.config['cleanup_interval']} 秒）")

    async def terminate(self):
        """插件卸载：恢复原始方法 + 清理"""
        if self._patched:
            Record.convert_to_file_path = _original_convert
            self._patched = False
            logger.info("VoiceDownloader: Record.convert_to_file_path 已恢复")

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ----------------------------------------------------------
    # on_decorating_result：兜底处理（处理未被 patch 捕获的情况）
    # ----------------------------------------------------------

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """兜底：确保所有 Record 组件的路径有效"""
        core = _get_downloader()
        for comp in event.get_messages():
            if isinstance(comp, Record):
                await core.ensure_voice_file(comp)
        return None

    # ----------------------------------------------------------
    # 定时清理
    # ----------------------------------------------------------

    async def _periodic_cleanup(self):
        while True:
            await asyncio.sleep(self.config["cleanup_interval"])
            await self._cleanup_expired_files()

    async def _cleanup_expired_files(self):
        if not os.path.exists(self.temp_dir):
            return
        now = time.time()
        count = 0
        for fname in os.listdir(self.temp_dir):
            fpath = os.path.join(self.temp_dir, fname)
            try:
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > self.config["max_file_age"]:
                    os.remove(fpath)
                    count += 1
            except OSError:
                pass
        if count > 0:
            logger.info(f"VoiceDownloader: 清理了 {count} 个过期文件")
