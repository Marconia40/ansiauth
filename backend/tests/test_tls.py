"""Tests for TLS middleware: HSTS header and HTTPS redirect."""
import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.config as config_module
from app.core.tls_middleware import HSTSMiddleware, HTTPSRedirectMiddleware
from app.main import app


def _make_test_app(*middleware_classes):
    """Return a TestClient wrapping a minimal FastAPI app with the given middlewares."""
    _app = FastAPI()
    for cls in middleware_classes:
        _app.add_middleware(cls)

    @_app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(_app, raise_server_exceptions=False)


# ── HSTS ──────────────────────────────────────────────────────────────────────

def test_hsts_header_present_when_ssl_configured():
    """HSTS header must be present on every response when HSTSMiddleware is active."""
    client = _make_test_app(HSTSMiddleware)
    response = client.get("/ping")
    assert "strict-transport-security" in response.headers
    hsts = response.headers["strict-transport-security"]
    assert "max-age=" in hsts
    assert "includeSubDomains" in hsts


def test_hsts_max_age_is_one_year():
    """HSTS max-age must be at least one year (31536000 seconds)."""
    client = _make_test_app(HSTSMiddleware)
    response = client.get("/ping")
    hsts = response.headers.get("strict-transport-security", "")
    max_age = int(hsts.split("max-age=")[1].split(";")[0].strip())
    assert max_age >= 31536000


def test_hsts_absent_without_ssl(client):
    """HSTS header must NOT be present on the main app when SSL is not configured."""
    response = client.get("/health")
    assert "strict-transport-security" not in response.headers


# ── HTTPS redirect ────────────────────────────────────────────────────────────

def test_https_redirect_on_x_forwarded_proto_http():
    """Requests arriving via a proxy with X-Forwarded-Proto: http must be redirected."""
    client = _make_test_app(HTTPSRedirectMiddleware)
    response = client.get(
        "/ping",
        headers={"X-Forwarded-Proto": "http"},
        follow_redirects=False,
    )
    assert response.status_code == 301
    assert response.headers["location"].startswith("https://")


def test_no_redirect_on_x_forwarded_proto_https():
    """Requests with X-Forwarded-Proto: https must pass through without redirect."""
    client = _make_test_app(HTTPSRedirectMiddleware)
    response = client.get(
        "/ping",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert response.status_code == 200


# ── Certificate expiry warning ────────────────────────────────────────────────

def test_cert_expiry_warning_logged(tmp_path, caplog):
    """start.py must log a warning when the certificate expires within 30 days."""
    import ssl as _ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    # Generate a self-signed cert expiring in 5 days.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=5))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    import importlib, sys
    if "start" in sys.modules:
        del sys.modules["start"]

    import logging
    with caplog.at_level(logging.WARNING, logger="__main__"):
        from start import _check_cert_expiry
        _check_cert_expiry(str(cert_path))

    assert any("expires in" in r.message.lower() or "day" in r.message.lower() for r in caplog.records)
