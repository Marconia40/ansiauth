# Production-leaning image for the AnsiAuth FastAPI backend.
# Slim base + Ansible deps. Schema is owned by Alembic so the entrypoint runs
# `alembic upgrade head` before handing off to uvicorn.

FROM python:3.12-slim AS base

# System deps:
#   - sshpass: required by ansible-runner for password auth to devices
#   - libpq5: psycopg's runtime dependency (psycopg[binary] bundles it, but
#     keeping it explicit means the slim image works even if someone later
#     swaps to psycopg2-binary)
#   - build-essential / libssl-dev / libffi-dev / python3-dev: needed only
#     while installing wheels for cryptography. We don't compile from source
#     when using prebuilt wheels, but install them anyway because
#     ansible-core can pull a source distribution on edge cases.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        sshpass \
        libpq5 \
        build-essential \
        libssl-dev \
        libffi-dev \
        python3-dev \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so the layer caches when only source changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Ansible Galaxy collections used by the playbooks.
RUN ansible-galaxy collection install \
        cisco.ios \
        ansible.netcommon \
        community.network \
 && true

# Project source. Keep the Alembic config + migrations in the image so
# `alembic upgrade head` works without mounting the host repo.
COPY . /app

# uvicorn binds on 0.0.0.0:8000 inside the container; compose maps it out.
EXPOSE 8000

# Entrypoint: run migrations, then start the API. `--reload` is intentional
# OFF in the image — developers can override with `command:` in compose if
# they want a hot-reload dev container.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
