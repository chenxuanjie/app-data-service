"""Bounded loopback HTTP server helpers; never contact external services."""

import asyncio
import socket
from contextlib import asynccontextmanager, suppress

import uvicorn


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.005)


@asynccontextmanager
async def serving(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            log_level="critical",
            access_log=False,
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await until(lambda: server.started or task.done())
        if task.done():
            task.result()
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            listener.close()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
