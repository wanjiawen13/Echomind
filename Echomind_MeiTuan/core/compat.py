import asyncio
import functools


async def async_to_thread(func, *args, **kwargs):
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(func, *args, **kwargs)
    loop = asyncio.get_event_loop()
    bound = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, bound)


def create_task(coro):
    if hasattr(asyncio, "create_task"):
        return asyncio.create_task(coro)
    return asyncio.ensure_future(coro)
