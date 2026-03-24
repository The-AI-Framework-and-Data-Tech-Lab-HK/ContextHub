"""Auth middleware: API key validation only. No SQL, no tenant binding."""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if not api_key or api_key != request.app.state.settings.api_key:
            return JSONResponse(status_code=401, content={"detail": "Invalid API key"})

        return await call_next(request)
