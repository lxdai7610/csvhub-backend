from fastapi import HTTPException
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Bound JSON requests as well as streaming chunks, including chunked HTTP bodies."""

    def __init__(self, app, chunk_limit, json_limit=128 * 1024):
        self.app, self.chunk_limit, self.json_limit = app, chunk_limit, json_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.chunk_limit if scope["path"].endswith("/chunks") else self.json_limit
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared:
            try:
                length = int(declared)
                if length < 0:
                    raise ValueError
            except ValueError:
                return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
            if length > limit:
                return await JSONResponse({"detail": "Request body exceeds size limit"}, status_code=413)(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise HTTPException(413, "Request body exceeds size limit")
            return message

        await self.app(scope, bounded_receive, send)
