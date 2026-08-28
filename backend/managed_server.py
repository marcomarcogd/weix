"""由 WeixManager 控制的 Uvicorn 启动入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn


async def watch_stop_file(
    server: uvicorn.Server,
    stop_file: Path,
    interval: float = 0.2,
) -> None:
    """收到本地停止信号后请求 Uvicorn 正常执行 lifespan 退出。"""
    while not server.should_exit:
        if stop_file.exists():
            try:
                stop_file.unlink()
            except OSError:
                pass
            logging.getLogger("managed_server").info("收到管理器停止信号")
            server.should_exit = True
            return
        await asyncio.sleep(interval)


async def serve(host: str, port: int, stop_file: Path) -> None:
    from app.main import app

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    watcher = asyncio.create_task(
        watch_stop_file(server, stop_file),
        name="weix-manager-stop-watcher",
    )
    try:
        await server.serve()
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weix managed backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--stop-file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.stop_file.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(serve(args.host, args.port, args.stop_file.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
