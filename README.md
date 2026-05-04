# VoiceDownloader 🎙️

**AstrBot 语音下载插件** — 解决机器人服务端与 QQ 客户端不在同一台机器上时，语音消息本地路径无效的问题。

## 痛点 🤔

当 AstrBot 与 QQ 客户端（如 go-cqhttp / LLOneBot）部署在不同机器上时，收到的语音消息中 `path` 字段指向的是客户端机器的本地路径，在服务端不可访问。直接使用会导致文件找不到、STT 语音识别失败等问题。

## 解决方案 💡

本插件自动检测语音消息：
1. 检查 `path` 指向的文件是否存在
2. 若不存在 → 从 `url` 异步下载到临时目录
3. 自动更新消息组件中的 `path` 和 `file` 字段
4. 可选：amr → wav 转码（供 STT 使用）
5. 定时清理过期文件，避免磁盘占用

## 安装 📦

### 从插件市场安装
搜索 `VoiceDownloader` 一键安装。

### 手动安装
```bash
cd AstrBot/data/plugins/
git clone https://github.com/maomaosamaqwq/VoiceDownloader.git
```

然后在 WebUI 重载插件即可。

## 配置 ⚙️

编辑 `main.py` 中的 `self.config` 字典：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `auto_cleanup` | `true` | 是否自动清理过期文件 |
| `cleanup_interval` | `3600` | 清理间隔（秒） |
| `max_file_age` | `86400` | 文件最大存活时间（秒，24h） |
| `convert_amr_to_wav` | `true` | 是否将 amr 转码为 wav |

> **提示：** `convert_amr_to_wav` 依赖 `ffmpeg`，如未安装会自动跳过。

## 依赖 📋

- `aiohttp` — 异步 HTTP 下载
- `aiofiles` — 异步文件写入
- `ffmpeg`（可选）— amr → wav 转码

## 工作原理 🔄

```
收到消息 → 遍历消息链 → 发现 Record 组件
  ├─ path 存在 → 跳过（不做处理）
  └─ path 不存在 → 从 url 下载到临时目录
       ├─ 成功 → 更新 path/file 字段
       │  └─ 开启转码 → amr → wav（需 ffmpeg）
       └─ 失败 → 记录警告，跳过
```

下载后的文件命名：`voice_{message_id}_{timestamp}.{ext}`，避免冲突。

## 兼容性 ✅

- **平台：** aiocqhttp（go-cqhttp / LLOneBot 等 QQ 协议）
- **AstrBot 版本：** >= 3.0
- **系统：** Windows / Linux / macOS

## 作者 👤

**maomaosamaqwq**

## 许可 📄

MIT
