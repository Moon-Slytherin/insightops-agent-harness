"""Real DeepSeek API smoke check; requires a funded or granted DeepSeek key."""

import asyncio

from backend.app.harness.smoke_openai import main


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("deepseek")))
