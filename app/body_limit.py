"""Request body size cap.

``MAX_REQUEST_BYTES`` bounds every request body. A declared
``Content-Length`` over the cap is rejected before the body is read; a body
without one (chunked transfer) or with a false one is counted while it
streams in and rejected once it passes the cap. Either way the client gets
413. The cap also bounds artifact uploads, whose bytes are hashed and then
discarded.
"""

from .config import get_settings


class _BodyTooLarge(Exception):
    pass


async def _send_413(send) -> None:
    body = b'{"detail":"request body is too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    """Pure ASGI middleware enforcing ``max_request_bytes``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().max_request_bytes
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _send_413(send)
            return

        received = 0
        exceeded = False
        started = False

        async def capped_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message):
            # FastAPI turns an exception raised while reading the body into its
            # own 400, so once the cap is exceeded, replace whatever response
            # the app sends with the 413.
            nonlocal started
            if exceeded:
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await _send_413(send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except _BodyTooLarge:
            if started:
                raise
            await _send_413(send)
