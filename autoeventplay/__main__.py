"""Entry point: ``python -m autoeventplay``."""

from __future__ import annotations

import uvicorn

from .config import AppConfig
from .container import Services
from .infrastructure.logs import setup_logging
from .web.app import create_app


def main() -> None:
    config = AppConfig.from_env()
    config.ensure_dirs()
    setup_logging(config.log_dir, config.log_level)
    services = Services(config)
    app = create_app(services)
    uvicorn.run(app, host=config.host, port=config.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
