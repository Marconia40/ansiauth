# TLS Setup Guide

This guide covers TLS configuration for the Network Automation API in both development and production environments.

---

## Environment Variables

| Variable | Description | Required for TLS |
|---|---|---|
| `SSL_CERTFILE` | Path to the PEM-encoded TLS certificate | Yes |
| `SSL_KEYFILE` | Path to the PEM-encoded private key | Yes |

Both variables must be set together. Setting only one causes the startup script to abort.

---

## Development — Self-Signed Certificate with mkcert

[mkcert](https://github.com/FiloSottile/mkcert) generates locally-trusted certificates without browser warnings.

### 1. Install mkcert

```bash
# Fedora / RHEL
sudo dnf install mkcert

# Ubuntu / Debian
sudo apt install mkcert

# macOS
brew install mkcert
```

### 2. Install the local CA

```bash
mkcert -install
```

### 3. Generate a certificate for localhost

```bash
cd backend
mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1 ::1
```

### 4. Start the API with TLS

```bash
SSL_CERTFILE=cert.pem SSL_KEYFILE=key.pem python start.py
```

The API is now available at `https://localhost:8000`.

> **Never use self-signed certificates in production.**  
> The `cert.pem` and `key.pem` files must not be committed to version control.

---

## Production — CA-Signed Certificate

### Option A: Let's Encrypt with Certbot

```bash
certbot certonly --standalone -d your-domain.example.com
```

Certbot stores certificates in `/etc/letsencrypt/live/your-domain.example.com/`.

```bash
SSL_CERTFILE=/etc/letsencrypt/live/your-domain.example.com/fullchain.pem \
SSL_KEYFILE=/etc/letsencrypt/live/your-domain.example.com/privkey.pem \
python start.py
```

### Option B: nginx Reverse Proxy (Recommended for Production)

Terminate TLS at nginx and pass traffic to Uvicorn via plain HTTP on localhost.

**nginx config (`/etc/nginx/sites-available/ansiauth`):**

```nginx
server {
    listen 80;
    server_name your-domain.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name your-domain.example.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.example.com/privkey.pem;

    # Enforce TLS 1.2+; disable TLS 1.0 and 1.1
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

With nginx handling TLS, start Uvicorn without SSL env vars:

```bash
python start.py   # or: uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The `HTTPSRedirectMiddleware` inside the app reads `X-Forwarded-Proto: http` (set by nginx for HTTP requests) and issues a 301 redirect to HTTPS.

---

## TLS Version and Cipher Policy

When using `start.py` with `SSL_CERTFILE`/`SSL_KEYFILE`:

- **Minimum TLS version:** TLS 1.2 (enforced via `ssl.SSLContext.minimum_version`)
- **TLS 1.0 and TLS 1.1 are disabled**
- TLS 1.3 is supported when available

When using nginx, set `ssl_protocols TLSv1.2 TLSv1.3;` in the server block (see config above).

---

## HSTS

The `HSTSMiddleware` adds the following header to every response when `SSL_CERTFILE` is configured:

```
Strict-Transport-Security: max-age=31536000; includeSubDomains
```

This instructs browsers to always use HTTPS for this domain for one year.

---

## Certificate Expiry

`start.py` checks certificate expiry at startup:

- **≤ 0 days remaining:** logs `ERROR` — replace immediately
- **≤ 30 days remaining:** logs `WARNING` — renewal required soon
- **> 30 days remaining:** logs `INFO` — no action needed

Set up certificate renewal (e.g., `certbot renew`) via cron or a systemd timer.
