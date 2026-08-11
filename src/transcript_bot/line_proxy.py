"""A narrow public ingress for LINE webhooks.

Run this separately from the trusted-LAN Web workspace, then point a temporary
Tunnel at this port.  It forwards only the signed webhook payload to the local
workspace; every other route is absent from the public surface.
"""
from __future__ import annotations

import httpx
from flask import Flask, Response, request

from transcript_bot.config import settings


def create_line_proxy_app(target_url: str | None = None) -> Flask:
    """Create a small relay which exposes only LINE's webhook path."""
    target = target_url or f"http://127.0.0.1:{settings.web_port}/line/webhook"
    app = Flask(__name__)

    @app.post("/line/webhook")
    def relay_line_webhook():
        headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
        signature = request.headers.get("X-Line-Signature")
        if signature:
            headers["X-Line-Signature"] = signature
        try:
            upstream = httpx.post(target, content=request.get_data(cache=False), headers=headers, timeout=20.0)
        except httpx.HTTPError:
            return "LINE webhook 暫時無法轉送。", 503
        return Response(upstream.content, status=upstream.status_code, content_type=upstream.headers.get("Content-Type"))

    return app


def main() -> None:
    create_line_proxy_app().run(host="127.0.0.1", port=settings.line_proxy_port, debug=False, use_reloader=False)


if __name__ == "__main__":  # pragma: no cover - command-line entrypoint
    main()
