"""Entry point for `sipsmith` CLI command and direct uvicorn launch."""

from __future__ import annotations

from pathlib import Path


def main() -> None:
    import uvicorn

    from sipsmith.config import get_settings

    settings = get_settings()

    ssl_keyfile = settings.server.tls_key
    ssl_certfile = settings.server.tls_cert

    # Fall back to plain HTTP if TLS files not present (dev mode)
    use_ssl = Path(ssl_certfile).exists() and Path(ssl_keyfile).exists()

    uvicorn.run(
        "sipsmith.app:create_app",
        factory=True,
        host=settings.server.host,
        port=settings.server.port,
        ssl_keyfile=ssl_keyfile if use_ssl else None,
        ssl_certfile=ssl_certfile if use_ssl else None,
        log_config=None,  # we configure logging ourselves
        access_log=True,
    )


if __name__ == "__main__":
    main()
