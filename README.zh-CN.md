# 通用视频完整原文 Telegram Bot

把视频链接发给 Telegram Bot，它会：

1. 自动识别 B站、YouTube、抖音/TikTok、X、Instagram、Facebook、Vimeo 等常见平台；
2. 优先抓取平台已有字幕，并优先选择简体中文、其他中文或英文；
3. 没有独立字幕轨时，使用本地 Faster-Whisper 识别音频；
4. 将识别结果统一转换为简体中文；
5. 在 Telegram 只返回一个完整 TXT 原文文件，不生成分析、不重复发送。

原文发送完成后，Bot 会询问是否需要分析。回复“是”后，本地千问只输出
“视频核心观点”和“千问的看法”两部分，并以单独的 TXT 文档发送；
回复“否”则结束本次处理。

如果取得的是英文、日文或韩文字幕，Bot 会先调用本地千问逐段完整翻译成
简体中文，不总结、不删减，再发送原文 TXT。中文视频不会经过这一步。

## 本地运行

需要 Python 3.11+。`yt-dlp` 会随 Python 依赖安装。当前默认输出完整原文，
不需要 Ollama；只有把 `OUTPUT_MODE` 改成 `article` 时才需要本地模型。

先启动 Ollama，并确保已经有模型：

```powershell
ollama list
ollama pull qwen3.5:9b
```

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Windows 没有 `cp` 时执行：

```powershell
Copy-Item .env.example .env
```

可以先不配置 Telegram，单独测试本地模型：

```powershell
.\.venv\Scripts\python.exe local_smoke_test.py
```

看到“本地模型运行成功”后，再填写 Telegram Token 并启动 `bot.py`。

## Windows 后台启动

完成首次安装和配置后，双击 `start_bot_hidden.vbs` 即可让 Bot 在后台运行，
不需要保留 PowerShell 窗口。运行日志保存在同目录的 `bot.log`。

需要登录的平台在 Firefox 中登录一次即可；处理视频时可以关闭浏览器，避免 Cookie 数据库被占用。

然后编辑 `.env`，至少填写：

- `TELEGRAM_BOT_TOKEN`：在 Telegram 里向 `@BotFather` 发送 `/newbot` 获取。
- `MODEL_PROVIDER=ollama`：使用本地模型。
- `OLLAMA_MODEL=qwen3.5:9b`：本地模型名称。

建议首次先不给 `ALLOWED_TELEGRAM_USER_IDS` 填值，启动后私聊 Bot 发送 `/id`，拿到自己的数字 ID；随后把它写入 `.env` 并重启，以免别人消耗你的额度。

## Docker 运行

```bash
docker build -t bili-article-bot .
docker run -d --name bili-article-bot --restart unless-stopped --env-file .env bili-article-bot
```

如果配置了 Cookie 文件，需额外挂载：

```bash
docker run -d --name bili-article-bot --restart unless-stopped \
  --env-file .env \
  -v /绝对路径/cookies.txt:/run/secrets/bilibili-cookies.txt:ro \
  -e BILIBILI_COOKIES_FILE=/run/secrets/bilibili-cookies.txt \
  bili-article-bot
```

## 使用与注意事项

- 私聊 Bot，直接粘贴受支持平台的视频链接。
- 默认只处理链接指向的单个视频，不会批量抓取收藏夹或播放列表。
- 画面硬字幕无法直接下载；程序会自动下载音频并使用本地 Whisper 转写，耗时更长。
- 付费、会员、地区限制或登录后可见的视频可能需要你自己的 Cookie。
- 请只处理你有权访问和使用的内容，并遵守视频平台、Telegram 与模型服务的条款。

## 常见问题

**提示 Cookie / 需要登录**：先在 Firefox 登录对应平台，并设置 `VIDEO_COOKIES_FROM_BROWSER=firefox`；也可导出 Netscape 格式 `cookies.txt` 后填写 `BILIBILI_COOKIES_FILE`。

**提示找不到 ffmpeg**：安装 ffmpeg，并确保终端执行 `ffmpeg -version` 有输出。

**提示无法连接 Ollama**：打开 Ollama 应用，或在终端执行 `ollama serve`。

**想换本地模型**：先执行 `ollama pull 模型名`，再修改 `OLLAMA_MODEL`。
