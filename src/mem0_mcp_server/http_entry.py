"""Production HTTP entry point for container hosts."""

from __future__ import annotations

import os

from .server import create_server


def main() -> None:
    server = create_server()
    # Respect runtime HOST/PORT overrides from the environment.
    server.settings.host = os.getenv("HOST", server.settings.host)
    server.settings.port = int(os.getenv("PORT", server.settings.port))
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
