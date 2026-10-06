from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from telegram import InputFile, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from pipeline import PipelineError, analyze_video, extract_video_url, process_video


# 每个 Bot 使用自己目录下的 .env，避免电脑里其他 Bot 的同名环境变量串号。
load_dotenv(override=True)
LOG_FILE = Path(__file__).resolve().with_name("bot.log")
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=2_000_000,
            backupCount=2,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)
# Telegram 库底层会把包含 Bot Token 的请求 URL 记到 INFO 日志中；禁止输出。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
YES_WORDS = {"是", "要", "好的", "好", "可以", "分析", "yes", "y"}
NO_WORDS = {"否", "不要", "不用", "不", "no", "n"}


async def post_init(app: Application) -> None:
    me = await app.bot.get_me()
    logger.info("已连接到 Telegram 机器人：@%s (ID: %s)", me.username, me.id)


async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("处理 Telegram 消息时发生错误", exc_info=context.error)


def _allowed_ids() -> set[int]:
    raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
    return {int(item.strip()) for item in raw.split(",") if item.strip().isdigit()}


def _authorized(update: Update) -> bool:
    allowed = _allowed_ids()
    return not allowed or bool(update.effective_user and update.effective_user.id in allowed)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("收到 /start，用户 ID：%s", update.effective_user.id if update.effective_user else "未知")
    if not update.message:
        return
    if not _authorized(update):
        await update.message.reply_text("这个 Bot 是私用的，你当前没有访问权限。发送 /id 可查看用户 ID。")
        return
    await update.message.reply_text(
        "把视频链接发给我，我会返回简体中文的完整原文。\n\n"
        "支持 B站、YouTube、抖音/TikTok、X、Instagram、Facebook、Vimeo 等常见平台。"
        "优先读取平台字幕；没有字幕时才使用本地 Whisper 识别音频。\n\n"
        "原文发送后，我会询问是否需要千问分析。"
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text(f"你的 Telegram 用户 ID：{update.effective_user.id}")


def _safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip(" ._")
    return (name[:80] or "视频原文") + ".txt"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    logger.info("收到文本消息，用户 ID：%s", update.effective_user.id if update.effective_user else "未知")
    if not _authorized(update):
        await update.message.reply_text("你没有访问权限。发送 /id 可查看用户 ID。")
        return

    answer = update.message.text.strip().lower()
    pending = context.user_data.get("pending_analysis")
    if pending and answer in YES_WORDS:
        context.user_data.pop("pending_analysis", None)
        analysis_message = await update.message.reply_text("千问正在阅读原文…")

        async def analysis_status(text: str) -> None:
            try:
                await analysis_message.edit_text(text)
                await update.effective_chat.send_action(ChatAction.TYPING)
            except Exception:
                logger.debug("更新分析进度失败", exc_info=True)

        try:
            async with JOB_SEMAPHORE:
                analysis = await analyze_video(
                    pending["title"], pending["transcript"], status=analysis_status
                )
            await analysis_message.edit_text("分析完成，完整内容见下方 TXT 文件。")
            analysis_text = (
                f"视频标题：{pending['title']}\n\n"
                f"{analysis}"
            )
            document = io.BytesIO(analysis_text.encode("utf-8-sig"))
            document.name = _safe_filename(f"{pending['title']}_千问分析")
            await update.message.reply_document(
                document=InputFile(document, filename=document.name),
                caption=f"千问分析 · {pending['title']}",
            )
        except PipelineError as exc:
            logger.warning("分析失败: %s", exc)
            await analysis_message.edit_text(f"分析失败：{exc}")
        except Exception as exc:
            logger.exception("分析时发生未处理异常")
            await analysis_message.edit_text(f"分析失败：{str(exc)[:1000]}")
        return
    if pending and answer in NO_WORDS:
        context.user_data.pop("pending_analysis", None)
        await update.message.reply_text("好的，本次处理结束。")
        return

    url = extract_video_url(update.message.text)
    if not url:
        if pending:
            await update.message.reply_text("请回复“是”开始分析，或回复“否”结束。")
            return
        await update.message.reply_text(
            "请发送受支持的视频链接，例如 B站、YouTube、抖音/TikTok、X、Instagram、Facebook 或 Vimeo。"
        )
        return

    status_message = await update.message.reply_text("任务已收到，正在排队…")

    async def status(text: str) -> None:
        try:
            await status_message.edit_text(text)
            await update.effective_chat.send_action(ChatAction.TYPING)
        except Exception:
            logger.debug("更新进度消息失败", exc_info=True)

    try:
        async with JOB_SEMAPHORE:
            result = await process_video(url, status=status)
        await status_message.edit_text(
            f"处理完成：{result.title}\n文本来源：{result.transcript_source}\n完整原文见下方 TXT 文件。"
        )

        plain_text = (
            f"视频标题：{result.title}\n"
            f"视频来源：{result.source_url}\n"
            f"文本来源：{result.transcript_source}\n\n"
            f"{result.article}"
        )
        document = io.BytesIO(plain_text.encode("utf-8-sig"))
        document.name = _safe_filename(result.title)
        await update.message.reply_document(
            document=InputFile(document, filename=document.name),
            caption=f"完整原文 · {result.title}",
        )
        context.user_data["pending_analysis"] = {
            "title": result.title,
            "transcript": result.article,
        }
        await update.message.reply_text("原文已经发给你了。要不要让千问分析？请回复“是”或“否”。")
    except PipelineError as exc:
        logger.warning("任务失败: %s", exc)
        await status_message.edit_text(f"处理失败：{exc}")
    except Exception as exc:
        logger.exception("未处理异常")
        message = str(exc)
        if "401" in message or "api_key" in message.lower():
            message = "OpenAI API Key 无效或未配置。"
        await status_message.edit_text(f"处理失败：{message[:1000]}")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("缺少 TELEGRAM_BOT_TOKEN，请复制 .env.example 为 .env 后填写。")
    if os.getenv("MODEL_PROVIDER", "ollama").lower() == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("缺少 OPENAI_API_KEY，请在 .env 中填写。")
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(log_error)
    logger.info("Bot 已启动")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
