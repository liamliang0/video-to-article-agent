# Video-to-Article Research Agent

An AI-assisted local workflow that converts video links into readable transcripts or structured text and delivers the result through Telegram.

[中文说明](README.zh-CN.md)

## Why I built it

Long videos are slow to scan, search, and revisit. I wanted a reading-first workflow for research and learning, so I defined a pipeline that turns a video URL into text that can be searched, highlighted, and reviewed quickly.

## My role and AI assistance

I defined the user need, workflow, fallback rules, output format, and iteration priorities. I used OpenAI Codex to help implement and troubleshoot the local Python agent.

This is an AI-assisted workflow project. It demonstrates requirements definition, process design, troubleshooting, and validation; it is not presented as a production SaaS product or as a claim that every supported platform works permanently without maintenance.

## Workflow

```text
Video URL
   ↓
Platform recognition
   ↓
Existing subtitles, when available
   ↓ otherwise
Local Faster-Whisper transcription
   ↓
Optional translation / structured analysis
   ↓
TXT delivery through Telegram
```

The current recognizer is designed for common platforms including Bilibili, YouTube, Douyin/TikTok, X, Instagram, Facebook, and Vimeo. Actual availability depends on the platform, region, authentication requirements, and upstream changes.

## Key design decisions

- Prefer existing subtitles before running speech recognition.
- Fall back to local Faster-Whisper when no usable subtitle track exists.
- Keep transcript delivery separate from optional analysis.
- Use local Ollama models by default for translation and analysis.
- Restrict Telegram access by user ID when deployed for personal use.
- Keep credentials and browser cookies outside version control.

## What I learned

Real-world automation failures were often operational rather than conceptual. During local use, I investigated network interruptions, duplicate Telegram polling processes, browser-cookie access, and Windows startup-path issues. Those constraints shaped the current configuration and documentation.

## Current limitations

- Platform extractors can break when websites change.
- Login, paid, regional, or member-only videos may require the user's own browser cookies.
- Hardcoded subtitles require audio transcription and take longer.
- Local transcription and model inference depend on the user's hardware.
- The project has been used locally, but it is not presented as a continuously hosted production service.

## Local setup

Requires Python 3.11+, FFmpeg, and optionally Ollama.

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

Configure `TELEGRAM_BOT_TOKEN` in `.env`. Never commit the real `.env` file.

To test the local model without Telegram:

```powershell
.\.venv\Scripts\python.exe local_smoke_test.py
```

## Configuration

The repository includes `.env.example` with safe placeholders. The main options cover:

- Telegram credentials and allowed user IDs
- subtitle language priority
- local or OpenAI-compatible model providers
- local Faster-Whisper settings
- optional browser-cookie sources
- concurrent job limits

## Responsible use

Only process content you are authorized to access and use. Follow the terms of the video platform, Telegram, model providers, and applicable copyright rules.
