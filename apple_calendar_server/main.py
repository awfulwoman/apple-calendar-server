from __future__ import annotations
import logging
import uvicorn
from apple_calendar_server import http, store
from apple_calendar_server.config import Config

logger = logging.getLogger("apple_calendar_server")


def create_app(config: Config):
    if not config.bearer_tokens:
        logger.warning("Auth DISABLED — set CALENDAR_SERVER_BEARER_TOKENS to require a bearer token")
    store.init(config)
    http.init(config)
    return http.create_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = Config()
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
