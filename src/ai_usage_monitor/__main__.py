"""Entry point: python -m ai_usage_monitor [--host HOST] [--port PORT]."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from ai_usage_monitor.app import create_app


class _RedactAccessQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn's HTTP protocols pass (client, method, target, version, status).
        # Keep that tuple shape for Uvicorn's AccessFormatter.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, version, status = record.args
            if isinstance(target, str):
                path, separator, _ = target.partition("?")
                if separator:
                    record.args = (client, method, f"{path}?<redacted>", version, status)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ai-usage-monitor",
        description="Local web app monitoring Claude and ChatGPT/OpenAI usage.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument(
        "--port",
        type=int,
        default=61524,
        help="Bind port (default: 61524, chosen to avoid clashing with common local services)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logging.getLogger("uvicorn.access").addFilter(_RedactAccessQuery())

    app = create_app(app_port=args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=None)


if __name__ == "__main__":
    main()
