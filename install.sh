#!/usr/bin/env bash
# install.sh — AnsiAuth VM installer
#
# Instala Docker, Node.js, clona el repo, genera secretos, levanta el stack
# con Docker Compose y registra servicios systemd. Pensado para VMs Ubuntu
# 22.04+/24.04 y RHEL/Fedora recientes.
#
# Uso:
#   curl -sSL https://raw.githubusercontent.com/Marconia40/ansiauth/main/install.sh | sudo bash
#
# Variables de entorno reconocidas (todas opcionales):
#   INSTALL_DIR   — dónde clonar el repo         (default: /opt/ansiauth)
#   REPO_URL      — URL HTTPS del repo           (default: github.com/Marconia40/ansiauth.git)
#   BRANCH        — rama a checkoutear           (default: main)
#   NODE_MAJOR    — mayor de Node.js a instalar  (default: 20)
#   RUN_USER      — usuario que correrá node     (default: ansiauth)
#   SKIP_UFW      — 1 para no tocar el firewall  (default: 0)

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/ansiauth}"
REPO_URL="${REPO_URL:-https://github.com/Marconia40/ansiauth.git}"
BRANCH="${BRANCH:-main}"
NODE_MAJOR="${NODE_MAJOR:-20}"
RUN_USER="${RUN_USER:-ansiauth}"
SKIP_UFW="${SKIP_UFW:-0}"

CREDS_FILE="/root/ansiauth-credentials.txt"
COMPOSE_FILE="$INSTALL_DIR/docker-compose.prod.yml"
ENV_FILE="$INSTALL_DIR/.env"

# ─── Helpers ──────────────────────────────────────────────────────────────────
c_blue=$'\033[1;34m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'
c_green=$'\033[1;32m'; c_reset=$'\033[0m'
log()  { printf '%s[install]%s %s\n' "$c_blue"   "$c_reset" "$*"; }
warn() { printf '%s[warn]%s %s\n'    "$c_yellow" "$c_reset" "$*" >&2; }
err()  { printf '%s[err]%s %s\n'     "$c_red"    "$c_reset" "$*" >&2; exit 1; }
ok()   { printf '%s[ok]%s %s\n'      "$c_green"  "$c_reset" "$*"; }

on_error() {
    local exit_code=$?
    err "Instalación abortada (exit $exit_code). Revisá los últimos mensajes."
    err "Podés re-correr el script; es idempotente y retomará donde falló."
    exit "$exit_code"
}
trap on_error ERR

[[ $EUID -eq 0 ]] || err "Ejecutar como root: sudo bash install.sh"

# ─── Detectar SO ──────────────────────────────────────────────────────────────
if   command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
else err "Solo se soporta apt (Debian/Ubuntu) y dnf (RHEL/Fedora)."
fi
log "Gestor de paquetes: $PKG"

pkg_install() {
    if [[ "$PKG" == apt ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
    else
        dnf install -y "$@"
    fi
}

# ─── Dependencias base ────────────────────────────────────────────────────────
log "Instalando dependencias base..."
if [[ "$PKG" == apt ]]; then
    apt-get update -qq
fi
pkg_install curl ca-certificates git openssl python3 jq

# ─── Docker + compose plugin ──────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker + compose plugin ya presentes ($(docker --version | awk '{print $3}' | tr -d ','))"
else
    log "Instalando Docker via convenience script..."
    curl -fsSL https://get.docker.com | sh
    if [[ "$PKG" == apt ]]; then
        pkg_install docker-compose-plugin
    else
        pkg_install docker-compose-plugin || dnf install -y docker-compose
    fi
    systemctl enable --now docker
fi

# ─── Rotación de logs de Docker ───────────────────────────────────────────────
log "Configurando rotación json-file (max-size=50m, max-file=5)..."
mkdir -p /etc/docker
DAEMON_JSON=/etc/docker/daemon.json
if [[ -s "$DAEMON_JSON" ]]; then
    # Merge preservando otras opciones que el usuario tenga configuradas.
    tmp=$(mktemp)
    jq '. + {"log-driver": "json-file", "log-opts": ((."log-opts" // {}) + {"max-size":"50m","max-file":"5"})}' \
        "$DAEMON_JSON" > "$tmp"
    if ! cmp -s "$tmp" "$DAEMON_JSON"; then
        mv "$tmp" "$DAEMON_JSON"
        systemctl restart docker
        ok "daemon.json actualizado y docker reiniciado"
    else
        rm -f "$tmp"
        ok "daemon.json ya tenía la rotación configurada"
    fi
else
    cat > "$DAEMON_JSON" <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
JSON
    systemctl restart docker
    ok "daemon.json creado y docker reiniciado"
fi

# ─── Node.js ──────────────────────────────────────────────────────────────────
if command -v node >/dev/null 2>&1 && [[ $(node -v | sed 's/^v//' | cut -d. -f1) -ge "$NODE_MAJOR" ]]; then
    ok "Node.js ya presente ($(node -v))"
else
    log "Instalando Node.js $NODE_MAJOR desde NodeSource..."
    if [[ "$PKG" == apt ]]; then
        curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
        pkg_install nodejs
    else
        curl -fsSL "https://rpm.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
        pkg_install nodejs
    fi
fi

# ─── Usuario del servicio ────────────────────────────────────────────────────
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    log "Creando usuario del sistema '$RUN_USER'..."
    useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER"
fi

# ─── Clonar / actualizar repo ─────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Repo ya en $INSTALL_DIR, actualizando..."
    git -C "$INSTALL_DIR" fetch --tags --prune origin
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
    log "Clonando $REPO_URL (rama $BRANCH) en $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

# ─── Generar secretos y .env ─────────────────────────────────────────────────
gen_fernet() { python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }
gen_hex()    { openssl rand -hex 32; }
gen_pw()     { openssl rand -base64 24 | tr -d '\n=+/' | head -c 24; }

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[[ -z "$HOST_IP" ]] && HOST_IP=$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{print $2; exit}' | cut -d/ -f1)
[[ -z "$HOST_IP" ]] && HOST_IP="127.0.0.1"

if [[ -f "$ENV_FILE" ]]; then
    ok ".env ya existe en $INSTALL_DIR, respetando valores actuales"
    warn "Si querés regenerar secretos, borrá $ENV_FILE antes de re-correr el script."
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
else
    log "Generando secretos y $ENV_FILE..."
    FERNET_KEY=$(gen_fernet)
    JWT_SECRET_KEY=$(gen_hex)
    BOOTSTRAP_ADMIN_PASSWORD=$(gen_pw)
    POSTGRES_PASSWORD=$(gen_pw)

    cat > "$ENV_FILE" <<EOF
# Generado automáticamente por install.sh — $(date -Iseconds)
# NO commitear este archivo. Perms 0600.
#
# Este archivo se pasa a los containers via 'env_file:' en el compose,
# así que cualquier variable declarada acá llega directamente a la app.
# Los valores comentados muestran los defaults hardcodeados en config.py;
# descomentá para tunearlos sin tocar el compose.

# ── Secretos (REQUERIDOS) ──
FERNET_KEY=$FERNET_KEY
JWT_SECRET_KEY=$JWT_SECRET_KEY

# ── Bootstrap del super-admin (solo primer arranque) ──
BOOTSTRAP_ADMIN_USER=admin
BOOTSTRAP_ADMIN_PASSWORD=$BOOTSTRAP_ADMIN_PASSWORD

# ── Postgres ──
POSTGRES_PASSWORD=$POSTGRES_PASSWORD

# ── App ──
EXECUTION_MODE=mock
LOG_FORMAT=json

# ── Cookies ──
# COOKIE_SECURE=false porque el stack corre en HTTP puro; poné 'true' si
# terminás TLS en un reverse proxy.
COOKIE_SECURE=false
# 'lax' porque frontend (:3000) y backend (:8000) están en el mismo host —
# same-site se decide por el registrable domain, no por el puerto, así que
# 'strict' funcionaría, pero 'lax' es más resistente a cambios de topología
# (ej. mover el frontend a otro subdominio).
COOKIE_SAMESITE=lax

CORS_ORIGINS=http://$HOST_IP:3000,http://localhost:3000

# ── Lifetimes de tokens y sesión ──
# Cambiar acá si querés sesiones más largas/cortas sin recompilar la imagen.
# ACCESS_TOKEN_EXPIRE_MINUTES=20
# REFRESH_TOKEN_EXPIRE_MINUTES=240
# REFRESH_TOKEN_IDLE_MINUTES=15         # idle timeout server-side
# SESSION_ABSOLUTE_MAX_HOURS=2          # cap absoluto desde el login
# ELEVATED_TOKEN_EXPIRE_MINUTES=5       # step-up re-auth (destructivo)

# ── Rate limiting (por minuto) ──
# RATE_LIMIT_PER_IP=20
# RATE_LIMIT_PER_USER=200
# RATE_LIMIT_LOGIN=5

# ── Retención y limpieza (ajustar según necesidad de auditoría) ──
AUDIT_RETENTION_DAYS=180
ARTIFACT_RETENTION_DAYS=30
LOGIN_ATTEMPT_RETENTION_DAYS=7
# CLEANUP_INTERVAL_HOURS=6

# ── Observabilidad ──
# METRICS_ENABLED=false   # expone /metrics para Prometheus
EOF
    chmod 600 "$ENV_FILE"
    chown "$RUN_USER:$RUN_USER" "$ENV_FILE"
    ok "Secretos generados"
fi

# ─── docker-compose.prod.yml ─────────────────────────────────────────────────
# El compose se regenera en cada corrida del script — cualquier edición manual
# se pierde. Si tenés customizaciones locales, moverlas a un compose.override.yml
# o al .env.
if [[ -f "$COMPOSE_FILE" ]]; then
    warn "$COMPOSE_FILE existente será sobrescrito (backup en .bak)."
    cp -f "$COMPOSE_FILE" "$COMPOSE_FILE.bak"
fi
log "Escribiendo $COMPOSE_FILE..."
cat > "$COMPOSE_FILE" <<'YAML'
# Compose de producción para AnsiAuth — generado por install.sh.
# Diferencias respecto al docker-compose.yml de dev:
#   - Postgres y Redis bindean a 127.0.0.1 (no expuestos a la red).
#   - Cada service backend/worker/beat carga .env via env_file:, así toda
#     variable declarada en .env llega automáticamente al container.
#   - restart: unless-stopped para sobrevivir reinicios de la VM.
#   - No expone worker/beat como puertos.
#
# 'environment:' se usa sólo para variables que dependen del network interno
# de docker (DATABASE_URL, REDIS_URL con hostnames de container). Compose
# aplica 'environment' encima de 'env_file', así que estos valores ganan
# siempre — cualquier DATABASE_URL en .env es ignorado a propósito.

services:
  redis:
    image: redis:7-alpine
    container_name: ansiauth-redis
    restart: unless-stopped
    ports:
      - "127.0.0.1:6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 10

  db:
    image: postgres:16-alpine
    container_name: ansiauth-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: ansiauth
      POSTGRES_USER: ansiauth
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - ansiauth-postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ansiauth -d ansiauth"]
      interval: 10s
      timeout: 3s
      retries: 10

  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: ansiauth-backend
    restart: unless-stopped
    depends_on:
      db: {condition: service_healthy}
      redis: {condition: service_healthy}
    env_file:
      - .env
    environment:
      DATABASE_URL: postgresql+psycopg://ansiauth:${POSTGRES_PASSWORD}@db:5432/ansiauth
      REDIS_URL: redis://redis:6379/0
    ports:
      - "8000:8000"

  worker:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: ansiauth-worker
    restart: unless-stopped
    command: celery -A app.worker.celery_app worker --loglevel=info --concurrency=4
    depends_on:
      db: {condition: service_healthy}
      redis: {condition: service_healthy}
    env_file:
      - .env
    environment:
      DATABASE_URL: postgresql+psycopg://ansiauth:${POSTGRES_PASSWORD}@db:5432/ansiauth
      REDIS_URL: redis://redis:6379/0

  beat:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: ansiauth-beat
    restart: unless-stopped
    command: celery -A app.worker.celery_app beat --loglevel=info
    depends_on:
      redis: {condition: service_healthy}
    env_file:
      - .env
    environment:
      DATABASE_URL: postgresql+psycopg://ansiauth:${POSTGRES_PASSWORD}@db:5432/ansiauth
      REDIS_URL: redis://redis:6379/0

volumes:
  ansiauth-postgres-data:
YAML
chown "$RUN_USER:$RUN_USER" "$COMPOSE_FILE"

# ─── Frontend: env + build ────────────────────────────────────────────────────
FRONTEND_ENV="$INSTALL_DIR/frontend/.env.local"
if [[ ! -f "$FRONTEND_ENV" ]]; then
    log "Escribiendo $FRONTEND_ENV..."
    cat > "$FRONTEND_ENV" <<EOF
NEXT_PUBLIC_API_URL=http://$HOST_IP:8000
EOF
    chown "$RUN_USER:$RUN_USER" "$FRONTEND_ENV"
fi

log "Instalando dependencias y compilando frontend (esto tarda unos minutos)..."
sudo -u "$RUN_USER" bash -lc "cd '$INSTALL_DIR/frontend' && npm ci --no-audit --no-fund && npm run build"
ok "Frontend compilado"

# ─── systemd: stack backend (docker compose) ─────────────────────────────────
log "Registrando servicio systemd ansiauth-stack.service..."
cat > /etc/systemd/system/ansiauth-stack.service <<EOF
[Unit]
Description=AnsiAuth backend stack (Docker Compose)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/docker compose -f $COMPOSE_FILE --env-file $ENV_FILE up -d
ExecStop=/usr/bin/docker compose -f $COMPOSE_FILE --env-file $ENV_FILE down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

# ─── systemd: frontend Next.js ───────────────────────────────────────────────
log "Registrando servicio systemd ansiauth-frontend.service..."
cat > /etc/systemd/system/ansiauth-frontend.service <<EOF
[Unit]
Description=AnsiAuth frontend (Next.js)
After=network-online.target ansiauth-stack.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$INSTALL_DIR/frontend
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=HOSTNAME=0.0.0.0
ExecStart=/usr/bin/npm run start
Restart=always
RestartSec=5

# stdout/stderr → journald, con rotación gobernada por journald.conf del sistema.
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ansiauth-stack.service ansiauth-frontend.service >/dev/null

# ─── Firewall (UFW en Debian/Ubuntu) ─────────────────────────────────────────
if [[ "$SKIP_UFW" != "1" && "$PKG" == "apt" ]]; then
    log "Configurando UFW (SSH + 3000 frontend + 8000 backend)..."
    pkg_install ufw
    ufw --force reset >/dev/null
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow OpenSSH
    ufw allow 3000/tcp comment 'AnsiAuth frontend'
    ufw allow 8000/tcp comment 'AnsiAuth backend API'
    ufw --force enable
    ok "UFW activo"
elif [[ "$SKIP_UFW" != "1" ]]; then
    warn "Saltando UFW: no está en apt-land. Abrí manualmente 3000/tcp y 8000/tcp."
fi

# ─── Build de imágenes ───────────────────────────────────────────────────────
# Se hace acá y no dentro del ExecStart de systemd para que:
#   - el primer arranque no tarde 5 min extra bloqueando el service manager;
#   - en re-corridas del install.sh (después de git pull) el código nuevo
#     entre a la imagen sin depender de un 'docker compose build' manual.
log "Building/actualizando imágenes docker (esto puede tardar varios minutos)..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --pull

# ─── Arrancar ─────────────────────────────────────────────────────────────────
# 'docker compose up -d' es idempotente y recrea sólo los containers cuya
# imagen cambió — más quirúrgico que un 'systemctl restart' que hace down+up
# completo. systemctl start después sólo marca el service como activo para
# que persista tras reinicios de la VM.
log "Aplicando cambios al stack (up -d)..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --remove-orphans

log "Registrando stack en systemd para arranque automático..."
systemctl start ansiauth-stack.service || true

log "Esperando a que /health responda (hasta 120s)..."
for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
        ok "Backend arriba (después de ${i}x2s)"
        break
    fi
    sleep 2
    if [[ $i -eq 60 ]]; then
        warn "El backend no respondió a /health en 120s."
        warn "Revisá: journalctl -u ansiauth-stack -e  y  docker compose -f $COMPOSE_FILE logs"
    fi
done

log "Arrancando frontend..."
systemctl start ansiauth-frontend.service
sleep 3
if systemctl is-active --quiet ansiauth-frontend.service; then
    ok "Frontend activo"
else
    warn "Frontend no arrancó. Revisá: journalctl -u ansiauth-frontend -e"
fi

# ─── Guardar credenciales ─────────────────────────────────────────────────────
log "Guardando credenciales en $CREDS_FILE (0600)..."
# Recargar el .env por si venía preexistente y no las teníamos en variables.
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
cat > "$CREDS_FILE" <<EOF
AnsiAuth — credenciales generadas $(date -Iseconds)
======================================================
URL frontend:       http://$HOST_IP:3000
URL API:            http://$HOST_IP:8000

Super-admin inicial:
  usuario:          ${BOOTSTRAP_ADMIN_USER:-admin}
  password:         ${BOOTSTRAP_ADMIN_PASSWORD}

Postgres (solo local, 127.0.0.1:5432):
  usuario:          ansiauth
  password:         ${POSTGRES_PASSWORD}

Archivos importantes:
  .env:             $ENV_FILE
  compose prod:     $COMPOSE_FILE
  daemon.json:      /etc/docker/daemon.json
  ansible inv:      $INSTALL_DIR/backend/ansible/inventory/inventory.ini

Modo de ejecución: EXECUTION_MODE=${EXECUTION_MODE:-mock}
  * mock  → no se dispara ningún playbook real (default, seguro para probar).
  * real  → hay que editar el inventory.ini con hosts y credenciales antes
            de setear EXECUTION_MODE=real en $ENV_FILE y reiniciar el stack.

Gestión:
  systemctl status ansiauth-stack ansiauth-frontend
  systemctl restart ansiauth-stack
  journalctl -u ansiauth-stack -f
  journalctl -u ansiauth-frontend -f
  docker compose -f $COMPOSE_FILE logs -f backend

Actualizar a la última versión de $BRANCH:
  sudo bash $INSTALL_DIR/install.sh
  # (esto hace git pull, rebuildea imágenes docker y recrea containers)
EOF
chmod 600 "$CREDS_FILE"

# ─── Resumen ──────────────────────────────────────────────────────────────────
echo
ok "══════════════════════════════════════════════════════════════════"
ok "  Instalación completa."
ok "══════════════════════════════════════════════════════════════════"
echo
echo "  Frontend:       http://$HOST_IP:3000"
echo "  API:            http://$HOST_IP:8000"
echo "  Usuario admin:  ${BOOTSTRAP_ADMIN_USER:-admin}"
echo "  Password admin: ${BOOTSTRAP_ADMIN_PASSWORD}"
echo
echo "  Credenciales guardadas en:  $CREDS_FILE"
echo "  (perms 0600 — solo root puede leerlas)"
echo
warn "El stack corre sobre HTTP. Para exponerlo fuera de la LAN interna,"
warn "poné un reverse proxy (nginx/caddy) con TLS y cambiá COOKIE_SECURE=true."
echo
if [[ "${EXECUTION_MODE:-mock}" != "real" ]]; then
    warn "EXECUTION_MODE=mock: no se dispararán playbooks reales."
    warn "Para automatizar dispositivos, editá:"
    warn "  $INSTALL_DIR/backend/ansible/inventory/inventory.ini"
    warn "y después cambiá EXECUTION_MODE=real en $ENV_FILE y reiniciá:"
    warn "  systemctl restart ansiauth-stack"
    echo
fi
