"""Runs the on-call agent's bash and file tools on the Beelink (docs/architecture.md, "On-call
agent"). It holds only the environment key: anything the agent runs can read what this process
can, and its output goes to Anthropic."""

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.environments import EnvironmentWorker


async def main() -> None:
    key = Path("/run/secrets/anthropic_environment_key").read_text(encoding="utf-8").strip()
    lock = json.loads(Path(os.environ["CLAUDE_LOCK"]).read_text(encoding="utf-8"))
    async with AsyncAnthropic(auth_token=key) as client:
        worker = EnvironmentWorker(
            client,
            environment_id=lock["resources"]["./agent/environment.yaml"]["id"],
            environment_key=key,
            workdir="/workspace",
            # No memory stores are attached, and /mnt is read-only here.
            memory_sync_interval=None,
        )
        task = asyncio.create_task(worker.run())
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    asyncio.run(main())
