#!/usr/bin/env bash
# Start the local Langfuse (development and evaluation only). The first run writes
# dev/langfuse/.env with random secrets and the project's API keys; it is not committed.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  rand() { openssl rand -hex "$1"; }
  umask 077
  cat > .env <<EOF
POSTGRES_PASSWORD=$(rand 16)
CLICKHOUSE_PASSWORD=$(rand 16)
MINIO_ROOT_PASSWORD=$(rand 16)
REDIS_AUTH=$(rand 16)
SALT=$(rand 16)
NEXTAUTH_SECRET=$(rand 32)
ENCRYPTION_KEY=$(rand 32)
LANGFUSE_PUBLIC_KEY=pk-lf-$(rand 16)
LANGFUSE_SECRET_KEY=sk-lf-$(rand 16)
LANGFUSE_INIT_USER_PASSWORD=$(rand 12)
EOF
  echo "wrote dev/langfuse/.env"
fi

docker compose up -d --wait
echo "Langfuse: http://localhost:3000 (login dev@chatforge.local, password LANGFUSE_INIT_USER_PASSWORD in dev/langfuse/.env)"
echo "Backend:  LANGFUSE_ENABLED=true, LANGFUSE_HOST=http://localhost:3000 and the two keys from dev/langfuse/.env"
