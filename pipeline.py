from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx


StatusCallback = Callable[[str], Awaitable[None]]
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
TIMESTAMP_RE = re.compile(
    r"^\s*(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}"
)
TAG_RE = re.compile(r"<[^>]+>")
SUPPORTED_HOST_SUFFIXES = (
    "bilibili.com",
    "b23.tv",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "douyin.com",
    "iesdouyin.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
)


class PipelineError(RuntimeError):
    pass


@dataclass(slots=True)
class ArticleResult:
    title: str
    article: str
    source_url: str
    transcript_source: str


def _to_plain_simplified(text: str) -> str:
    try:
        from opencc import OpenCC

        text = OpenCC("t2s").convert(text)
    except ImportError as exc:
        raise PipelineError("缺少简繁转换组件，请重新安装 requirements.txt。") from exc
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_video_url(text: str) -> str | None:
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,，。!?！？;；)]}〉》")
        host = (urlparse(url).hostname or "").lower()
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in SUPPORTED_HOST_SUFFIXES):
            return url
    return None


# 保留旧名称，避免已有脚本或测试失效。
extract_bilibili_url = extract_video_url


def parse_vtt(text: str) -> str:
    lines: list[str] = []
    previous = ""
    for raw in text.replace("\ufeff", "").splitlines():
        line = raw.strip()
        if (
            not line
            or line == "WEBVTT"
            or line.isdigit()
            or line.startswith(("NOTE", "Kind:", "Language:"))
            or TIMESTAMP_RE.match(line)
        ):
            continue
        line = html.unescape(TAG_RE.sub("", line)).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return "\n".join(lines)


async def _run(*args: str, cwd: Path | None = None, timeout: int = 1800) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise PipelineError("处理超时，请稍后重试。")
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[-1500:].strip()
        raise PipelineError(f"外部工具执行失败：{detail or args[0]}")
    return stdout.decode("utf-8", errors="replace")


def _yt_dlp_base(
    cookies_file: str | None, *, include_browser_cookies: bool = False
) -> list[str]:
    args = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if cookies_file:
        cookie_path = Path(cookies_file).expanduser()
        if not cookie_path.is_file():
            raise PipelineError("BILIBILI_COOKIES_FILE 指向的文件不存在。")
        args += ["--cookies", str(cookie_path)]
    elif include_browser_cookies:
        browser = (
            os.getenv("VIDEO_COOKIES_FROM_BROWSER", "").strip()
            or os.getenv("BILIBILI_COOKIES_FROM_BROWSER", "").strip()
        )
        if browser:
            args += ["--cookies-from-browser", browser]
    return args


async def _metadata(url: str, cookies_file: str | None) -> dict:
    output = await _run(*_yt_dlp_base(cookies_file), "--dump-single-json", "--skip-download", url)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise PipelineError("无法读取视频信息。") from exc


async def _download_subtitles(url: str, folder: Path, cookies_file: str | None) -> str | None:
    # 不下载 YouTube 的几十种自动翻译字幕，只取最有用的中英文轨道。
    languages = os.getenv(
        "SUBTITLE_LANGUAGES", "zh-Hans,zh-CN,ai-zh,zh.*,en-US,en-GB,en.*"
    ).strip()
    # 先尝试人工/上传者字幕，再尝试平台自动字幕。B站的“AI字幕”属于后者。
    for subtitle_flag, prefix in (("--write-subs", "manual"), ("--write-auto-subs", "auto")):
        args = _yt_dlp_base(cookies_file, include_browser_cookies=True) + [
            "--skip-download",
            subtitle_flag,
            "--sub-langs",
            languages,
            "--sub-format",
            "srt/vtt/best",
            "-o",
            str(folder / f"{prefix}.%(ext)s"),
            url,
        ]
        try:
            await _run(*args)
        except PipelineError:
            continue
        candidates = sorted(
            (p for p in folder.glob(f"{prefix}*.*") if p.suffix.lower() in {".srt", ".vtt"}),
            key=lambda p: (
                "zh-hans" not in p.name.lower() and "zh-cn" not in p.name.lower(),
                "zh" not in p.name.lower(),
                "en" not in p.name.lower(),
                len(p.name),
            ),
        )
        for path in candidates:
            transcript = parse_vtt(path.read_text(encoding="utf-8", errors="replace"))
            if len(transcript) >= 80:
                return transcript
    return None


async def _transcribe_audio(
    url: str,
    folder: Path,
    client: Any,
    model: str,
    cookies_file: str | None,
) -> str:
    await _run(
        *_yt_dlp_base(cookies_file),
        "-f",
        "bestaudio/best",
        "-o",
        str(folder / "source.%(ext)s"),
        url,
    )
    sources = [p for p in folder.glob("source.*") if p.is_file()]
    if not sources:
        raise PipelineError("音频下载失败。")
    segment_pattern = str(folder / "segment-%03d.mp3")
    await _run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(sources[0]),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        "-f",
        "segment",
        "-segment_time",
        "1200",
        segment_pattern,
    )
    parts: list[str] = []
    for segment in sorted(folder.glob("segment-*.mp3")):
        with segment.open("rb") as audio:
            result = await client.audio.transcriptions.create(
                model=model,
                file=audio,
                response_format="json",
                prompt="这是一个网络视频。请准确识别中文、英文、专有名词和数字。",
            )
        text = getattr(result, "text", "").strip()
        if text:
            parts.append(text)
    if not parts:
        raise PipelineError("语音转写没有得到文本。")
    return "\n".join(parts)


def _faster_whisper_sync(audio_path: Path) -> str:
    try:
        from faster_whisper import WhisperModel

        model_name = os.getenv("WHISPER_MODEL", "small")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        download_root = os.getenv(
            "WHISPER_DOWNLOAD_ROOT",
            str(Path(__file__).resolve().parent / "models" / "faster-whisper"),
        )
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
        )
        segments, _ = model.transcribe(
            str(audio_path),
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
        )
        text = "\n".join(segment.text.strip() for segment in segments if segment.text.strip())
    except Exception as exc:
        raise PipelineError(f"本地语音识别失败：{exc}") from exc
    if not text:
        raise PipelineError("本地语音识别没有得到文本。")
    return text


async def _transcribe_audio_local(
    url: str,
    folder: Path,
    cookies_file: str | None,
) -> str:
    await _run(
        *_yt_dlp_base(cookies_file),
        "-f",
        "bestaudio/best",
        "-o",
        str(folder / "local-source.%(ext)s"),
        url,
    )
    sources = [p for p in folder.glob("local-source.*") if p.is_file()]
    if not sources:
        raise PipelineError("音频下载失败。")
    return await asyncio.to_thread(_faster_whisper_sync, sources[0])


def _chunks(text: str, size: int = 24_000) -> list[str]:
    chunks: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind("。", 0, size) + 1
        if cut < size // 2:
            cut = size
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def _needs_chinese_translation(text: str) -> bool:
    """判断正文是否主要为非中文，避免中文稿件被无谓地重写。"""
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    kana_or_hangul = len(re.findall(r"[\u3040-\u30ff\uac00-\ud7af]", text))
    return kana_or_hangul >= 50 or (latin >= 200 and latin > chinese * 2)


async def _translate_to_chinese(
    model: str,
    title: str,
    transcript: str,
    status: StatusCallback | None = None,
) -> str:
    parts: list[str] = []
    chunks = _chunks(transcript, 18_000)
    for index, chunk in enumerate(chunks, 1):
        if status:
            await status(f"正在用千问翻译为简体中文（{index}/{len(chunks)}）…")
        parts.append(
            await _response(
                model,
                "你是专业字幕翻译。把输入内容完整翻译成自然、准确的简体中文。"
                "不得总结、删减、评论或补充信息；保留人物称呼、数字、专有名词、语气和原有顺序；"
                "修正明显的自动字幕断句，但不要改变含义；只输出译文纯文本，不要标题、说明或 Markdown。",
                f"视频标题：{title}\n\n需要翻译的原文：\n{chunk}",
            )
        )
    return "\n\n".join(parts)


async def _response(model: str, instructions: str, content: str) -> str:
    provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
    if provider == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0)) as client:
                response = await client.post(
                    f"{base_url}/api/chat",
                    json={
                        "model": model,
                        "stream": False,
                        "think": False,
                        "messages": [
                            {"role": "system", "content": instructions},
                            {"role": "user", "content": content},
                        ],
                        "options": {
                            "temperature": 0.2,
                            "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "32768")),
                        },
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.ConnectError as exc:
            raise PipelineError("无法连接 Ollama。请先启动 Ollama 应用。") from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[-1000:]
            raise PipelineError(f"Ollama 调用失败：{detail}") from exc
        result = str(payload.get("message", {}).get("content", "")).strip()
    elif provider == "openai":
        from openai import AsyncOpenAI

        client = AsyncOpenAI()
        response = await client.responses.create(
            model=model,
            instructions=instructions,
            input=content,
            store=False,
        )
        result = response.output_text.strip()
    else:
        raise PipelineError("MODEL_PROVIDER 只能是 ollama 或 openai。")
    if not result:
        raise PipelineError("模型没有返回内容。")
    return result


async def _write_article(model: str, title: str, url: str, transcript: str) -> str:
    notes: list[str] = []
    chunks = _chunks(transcript)
    for index, chunk in enumerate(chunks, 1):
        notes.append(
            await _response(
                model,
                "你是严谨的中文编辑。只依据字幕提取信息，不补充字幕中没有的事实。保留论点、证据、例子、步骤、数字、专有名词和重要限定；删除口头禅与重复。输出层次清晰的详细笔记。",
                f"视频标题：{title}\n这是第 {index}/{len(chunks)} 段字幕：\n\n{chunk}",
            )
        )
    combined = "\n\n--- 分段笔记 ---\n\n".join(notes)
    # 极长视频的分段笔记再压一轮，避免最终请求超过上下文。
    if len(combined) > 90_000:
        compressed: list[str] = []
        for part in _chunks(combined, 30_000):
            compressed.append(
                await _response(
                    model,
                    "合并并压缩以下笔记，去重但不丢失关键事实、论证、例子、数字和行动建议。使用中文 Markdown。",
                    part,
                )
            )
        combined = "\n\n".join(compressed)
    return await _response(
        model,
        "你是一名严谨的中文长文编辑。把笔记改写成一篇可独立阅读的文章。要求：只能使用素材中明确出现的信息，绝对不要补充常识、数字、比例、案例、术语或建议；素材没给出的内容宁可不写；开头给出 3-6 条核心结论；正文使用有信息量的二级标题；结尾仅在素材支持时给出行动清单与局限；不要提及“字幕”“分段笔记”或处理过程；使用简体中文 Markdown。",
        f"标题：{title}\n来源：{url}\n\n素材：\n{combined}",
    )


async def analyze_video(
    title: str,
    transcript: str,
    status: StatusCallback | None = None,
) -> str:
    """用千问分析已取得的原文，只返回核心观点和模型看法。"""
    model = os.getenv("ANALYSIS_MODEL", os.getenv("OLLAMA_MODEL", "qwen3.5:9b"))

    async def report(message: str) -> None:
        if status:
            await status(message)

    chunks = _chunks(transcript)
    if len(chunks) == 1:
        material = transcript
    else:
        notes: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            await report(f"千问正在阅读原文（{index}/{len(chunks)}）…")
            notes.append(
                await _response(
                    model,
                    "只从这段视频原文中提取关键论点、论据、例子、数字和重要限定。"
                    "不要评价，不要添加原文以外的信息，使用简体中文纯文本。",
                    chunk,
                )
            )
        material = "\n\n".join(notes)

    await report("千问正在生成分析…")
    result = await _response(
        model,
        "你是千问。请分析用户提供的视频原文。输出必须且只能包含以下两个部分：\n"
        "视频核心观点\n"
        "准确概括视频最重要的主张、论据和结论。\n\n"
        "千问的看法\n"
        "给出你对这些观点的直接评价，指出合理之处、问题、盲点或值得进一步思考之处。\n\n"
        "必须使用简体中文纯文本；不要使用 Markdown 标记；不要添加前言、结语、免责声明、"
        "视频信息、处理过程或第三个部分；把原文事实和你的看法清楚区分。",
        f"视频标题：{title}\n\n视频原文：\n{material}",
    )
    return _to_plain_simplified(result)


async def process_video(url: str, status: StatusCallback | None = None) -> ArticleResult:
    if not shutil.which("yt-dlp"):
        raise PipelineError("系统未安装 yt-dlp。")
    cookies_file = os.getenv("BILIBILI_COOKIES_FILE") or None
    provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
    summary_model = (
        os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
        if provider == "ollama"
        else os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5-mini")
    )
    transcribe_model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

    async def report(message: str) -> None:
        if status:
            await status(message)

    with tempfile.TemporaryDirectory(prefix="bili-article-") as tmp:
        folder = Path(tmp)
        await report("正在读取视频信息…")
        info = await _metadata(url, cookies_file)
        title = str(info.get("title") or "视频原文")
        await report("正在获取视频字幕…")
        transcript = await _download_subtitles(url, folder, cookies_file)
        source = "视频字幕"
        if not transcript:
            if os.getenv("ENABLE_AUDIO_FALLBACK", "0").lower() not in {"1", "true", "yes", "on"}:
                raise PipelineError("这个视频没有可用字幕，且语音转写已关闭。")
            if not shutil.which("ffmpeg"):
                raise PipelineError("视频没有字幕，音频转写需要安装 ffmpeg。")
            transcription_provider = os.getenv(
                "TRANSCRIPTION_PROVIDER", "faster-whisper"
            ).strip().lower()
            if transcription_provider == "faster-whisper":
                await report("未能取得独立字幕轨，正在使用本地 Whisper 识别音频…")
                transcript = await _transcribe_audio_local(url, folder, cookies_file)
                source = "本地语音转写"
            elif transcription_provider == "openai":
                if not os.getenv("OPENAI_API_KEY"):
                    raise PipelineError("云端语音转写需要 OPENAI_API_KEY。")
                from openai import AsyncOpenAI

                await report("没有找到字幕，正在下载并转写音频（会更久）…")
                transcript = await _transcribe_audio(
                    url, folder, AsyncOpenAI(), transcribe_model, cookies_file
                )
                source = "语音转写"
            else:
                raise PipelineError("TRANSCRIPTION_PROVIDER 只能是 faster-whisper 或 openai。")
        output_mode = os.getenv("OUTPUT_MODE", "transcript").strip().lower()
        if output_mode == "transcript":
            if (
                os.getenv("TRANSLATE_NON_CHINESE", "1").lower() in {"1", "true", "yes", "on"}
                and _needs_chinese_translation(transcript)
            ):
                translation_model = os.getenv(
                    "TRANSLATION_MODEL", os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
                )
                transcript = await _translate_to_chinese(
                    translation_model, title, transcript, status=status
                )
                source += "（已由千问翻译为简体中文）"
            await report("正在整理简体中文原文…")
            article = _to_plain_simplified(transcript)
        elif output_mode == "article":
            await report(f"正在用本地模型 {summary_model} 提炼并撰写文章…" if provider == "ollama" else "正在提炼并撰写文章…")
            article = _to_plain_simplified(
                await _write_article(summary_model, title, url, transcript)
            )
        else:
            raise PipelineError("OUTPUT_MODE 只能是 transcript 或 article。")
        return ArticleResult(title=title, article=article, source_url=url, transcript_source=source)
