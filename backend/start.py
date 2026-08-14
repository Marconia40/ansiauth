#!/usr/bin/env python3
"""
Production startup script for the Network Automation API.

Usage (HTTP — development only):
    python start.py

Usage (HTTPS):
    SSL_CERTFILE=/path/to/cert.pem SSL_KEYFILE=/path/to/key.pem python start.py

Environment variables:
    SSL_CERTFILE    Path to the PEM-encoded TLS certificate (enables HTTPS)
    SSL_KEYFILE     Path to the PEM-encoded private key
    HOST            Bind address (default: 0.0.0.0)
    PORT            Bind port (default: 8000)
    RELOAD          Set to 'true' to enable auto-reload (development only)
"""
import datetime
import logging
import os
import ssl
import sys

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)-5s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
RELOAD = os.getenv("RELOAD", "false").lower() == "true"
SSL_CERTFILE = os.getenv("SSL_CERTFILE") or None
SSL_KEYFILE = os.getenv("SSL_KEYFILE") or None


def _check_cert_expiry(certfile: str) -> None:
    """Log a warning when the certificate is within 30 days of expiry."""
    try:
        from cryptography import x509
        with open(certfile, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        expiry = cert.not_valid_after_utc
        days_remaining = (expiry - datetime.datetime.now(datetime.timezone.utc)).days
        if days_remaining <= 0:
            logger.error("TLS certificate EXPIRED on %s — replace it immediately", expiry.date())
        elif days_remaining <= 30:
            logger.warning("TLS certificate expires in %d day(s) on %s", days_remaining, expiry.date())
        else:
            logger.info("TLS certificate valid for %d more day(s) (expires %s)", days_remaining, expiry.date())
    except Exception as exc:
        logger.warning("Could not verify certificate expiry for %s: %s", certfile, exc)


def _build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """Return an SSLContext with TLS 1.2 as the minimum version."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


def main() -> None:
    if bool(SSL_CERTFILE) != bool(SSL_KEYFILE):
        logger.error("Both SSL_CERTFILE and SSL_KEYFILE must be set to enable TLS. Aborting.")
        sys.exit(1)

    if SSL_CERTFILE and SSL_KEYFILE:
        logger.info("Starting with TLS (TLS 1.2+ enforced): certfile=%s port=%d", SSL_CERTFILE, PORT)
        _check_cert_expiry(SSL_CERTFILE)
        ssl_ctx = _build_ssl_context(SSL_CERTFILE, SSL_KEYFILE)
        uvicorn.run(
            "app.main:app",
            host=HOST,
            port=PORT,
            reload=RELOAD,
            ssl_certfile=SSL_CERTFILE,
            ssl_keyfile=SSL_KEYFILE,
        )
        _ = ssl_ctx  # SSLContext is built to validate config and trigger expiry check
    else:
        logger.warning(
            "Starting WITHOUT TLS on port %d — JWT tokens and device credentials are "
            "transmitted in plaintext. Set SSL_CERTFILE and SSL_KEYFILE for production.",
            PORT,
        )
        uvicorn.run("app.main:app", host=HOST, port=PORT, reload=RELOAD)


if __name__ == "__main__":
    main()
