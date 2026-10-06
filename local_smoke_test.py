from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from pipeline import PipelineError, _response


async def main() -> None:
    load_dotenv(override=True)
    model = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
    print(f"正在测试本地模型 {model}，首次响应可能需要几十秒……")
    try:
        reply = await _response(
            model,
            "你是本地连接测试助手。不要解释。",
            "请只回复：本地模型运行成功",
        )
    except PipelineError as exc:
        raise SystemExit(f"测试失败：{exc}") from exc
    print(f"模型回复：{reply}")


if __name__ == "__main__":
    asyncio.run(main())
