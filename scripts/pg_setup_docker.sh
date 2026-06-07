#!/usr/bin/env bash
# PostgreSQL セットアップの統合入口。
# apply: 既存 PostgreSQL に pg_setup.sql を流し込む。
# docker17: PostgreSQL 17 + pgvector を Docker で起動して初期化する。
# どちらのモードも ~/.bluecore/settings.json は更新しない。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SQL_FILE="${SCRIPT_DIR}/pg_setup.sql"
POSTGRES_USER="bluecore"
POSTGRES_DB="bluecore_mem"
CONTAINER_NAME="bluecore-postgres17"
DOCKERFILE_NAME="Dockerfile.postgres17-pgvector"
COMPOSE_FILE_NAME="compose.yaml"
ENV_FILE_NAME=".env"
WORKSPACE_DIR="${PWD}"
SQL_FILE_PATH=""
ORIGIN_USER=""
SERVER_HOST=""
FORCE=0
POSTGRES_PASSWORD=""

usage() {
  cat <<'EOF'
Usage:
  bash pg_setup.sh apply [--postgres-url URL | URL] [--sql-file PATH]
  MEM_PG_URL=URL bash pg_setup.sh apply [--sql-file PATH]
  bash pg_setup.sh docker17 [--workspace PATH] [--sql-file PATH] [--origin-user USER] [--server-host HOST] [--force]
  bash pg_setup.sh [postgresql://user:pass@host:5432/db]  # apply の省略形

Modes:
  apply     Load pg_setup.sql into an existing PostgreSQL database.
  docker17  Build and start PostgreSQL 17 + pgvector in Docker with lz4 TOAST compression,
            then load pg_setup.sql and write ${workspace}/.env with a random POSTGRES_PASSWORD.
            Use --force to remove the existing Docker volume and recreate the database.
            Local settings.json is left untouched.

Environment:
  MEM_PG_URL          PostgreSQL URL for apply mode.
EOF
}

log() {
  printf '[pg-setup] %s\n' "$1"
}

fail() {
  printf '[pg-setup] Error: %s\n' "$1" >&2
  exit 1
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Required command not found: $1"
  fi
}

env_value() {
  local key="$1"
  local line=""

  if [[ -f "${ENV_FILE_PATH}" ]]; then
    line="$(grep -E "^${key}=" "${ENV_FILE_PATH}" | tail -n 1 || true)"
  fi

  if [[ -z "${line}" ]]; then
    printf ''
    return 0
  fi

  printf '%s' "${line#*=}"
}

write_template_file() {
  local path="$1"
  local tmp
  tmp="$(mktemp)"

  cat >"${tmp}"

  if [[ -e "${path}" ]]; then
    if cmp -s "${tmp}" "${path}"; then
      rm -f "${tmp}"
      log "Unchanged: ${path}"
      return 0
    fi

    if [[ "${FORCE}" != "1" ]]; then
      rm -f "${tmp}"
      fail "${path} already exists and differs. Re-run with --force to overwrite it."
    fi
  fi

  cp "${tmp}" "${path}"
  chmod 0644 "${path}"
  rm -f "${tmp}"
  log "Wrote: ${path}"
}

write_env_file() {
  local postgres_password="${1}"

  if [[ -f "${ENV_FILE_PATH}" ]]; then
    local existing_user existing_db existing_password
    existing_user="$(env_value POSTGRES_USER)"
    existing_db="$(env_value POSTGRES_DB)"
    existing_password="$(env_value POSTGRES_PASSWORD)"

    if [[ "${existing_user}" != "${POSTGRES_USER}" ]]; then
      fail "${ENV_FILE_PATH} already exists with POSTGRES_USER=${existing_user:-<missing>}. Expected ${POSTGRES_USER}."
    fi

    if [[ "${existing_db}" != "${POSTGRES_DB}" ]]; then
      fail "${ENV_FILE_PATH} already exists with POSTGRES_DB=${existing_db:-<missing>}. Expected ${POSTGRES_DB}."
    fi

    if [[ -z "${existing_password}" ]]; then
      fail "${ENV_FILE_PATH} already exists but POSTGRES_PASSWORD is missing."
    fi

    chmod 0600 "${ENV_FILE_PATH}"
    POSTGRES_PASSWORD="${existing_password}"
    log "Reused: ${ENV_FILE_PATH}"
    return 0
  fi

  cat >"${ENV_FILE_PATH}" <<EOF
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${postgres_password}
POSTGRES_DB=${POSTGRES_DB}
EOF
  chmod 0600 "${ENV_FILE_PATH}"
  POSTGRES_PASSWORD="${postgres_password}"
  log "Wrote: ${ENV_FILE_PATH}"
}

wait_for_postgres() {
  local attempt=1
  local max_attempts=60

  while [[ "${attempt}" -le "${max_attempts}" ]]; do
    if docker exec "${CONTAINER_NAME}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    attempt=$((attempt + 1))
  done

  fail "Timed out waiting for ${CONTAINER_NAME} to become ready."
}

reset_docker_data() {
  log "Force mode: resetting Docker container and volume"

  (
    cd "${WORKSPACE_DIR}"
    docker compose down -v
  )
}

reset_database() {
  log "Force mode: recreating database ${POSTGRES_DB}"

  docker exec -i "${CONTAINER_NAME}" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d postgres <<'SQL'
DROP DATABASE IF EXISTS bluecore_mem WITH (FORCE);
CREATE DATABASE bluecore_mem;
SQL
}

print_sync_settings_example() {
  local origin_user="$1"

  echo "1. Edit the mem.sync section in ~/.bluecore/settings.json:"
  echo '   {'
  echo '     "mem": {'
  echo '       "sync": {'
  echo '         "enabled": true,'
  echo '         "interval_hours": 3,'
  echo '         "postgres_url": "<postgres_url>",'
  printf '         "origin_user": "%s"\n' "${origin_user}"
  echo '       }'
  echo '     }'
  echo '   }'
  echo ""
}

run_apply_mode() {
  local postgres_url="${MEM_PG_URL:-}"
  local sql_file="${DEFAULT_SQL_FILE}"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --postgres-url)
        [[ $# -ge 2 ]] || fail "--postgres-url requires a value"
        postgres_url="$2"
        shift 2
        ;;
      --sql-file)
        [[ $# -ge 2 ]] || fail "--sql-file requires a value"
        sql_file="$2"
        shift 2
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      -*)
        fail "Unknown argument for apply: $1"
        ;;
      *)
        if [[ -z "${postgres_url}" ]]; then
          postgres_url="$1"
          shift
        else
          fail "Unexpected argument for apply: $1"
        fi
        ;;
    esac
  done

  if [[ $# -gt 0 ]]; then
    fail "Unexpected argument for apply: $1"
  fi

  [[ -n "${postgres_url}" ]] || fail "PostgreSQL URL is required. Use --postgres-url, MEM_PG_URL, or pass the URL as a positional argument."
  [[ -f "${sql_file}" ]] || fail "SQL file not found: ${sql_file}"

  require_command psql

  log "Setting up mem PostgreSQL database..."
  log "URL: ${postgres_url%%:*}://***@${postgres_url#*@}"

  psql -v ON_ERROR_STOP=1 "${postgres_url}" -f "${sql_file}"

  printf '\n'
  log "Setup completed successfully!"
  log "Next steps:"
  print_sync_settings_example "<your_username>"
  log "Run: python3 -m bluecore.mem sync"
}

run_docker_mode() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --workspace)
        [[ $# -ge 2 ]] || fail "--workspace requires a value"
        WORKSPACE_DIR="$2"
        shift 2
        ;;
      --sql-file)
        [[ $# -ge 2 ]] || fail "--sql-file requires a value"
        SQL_FILE_PATH="$2"
        shift 2
        ;;
      --origin-user)
        [[ $# -ge 2 ]] || fail "--origin-user requires a value"
        ORIGIN_USER="$2"
        shift 2
        ;;
      --server-host)
        [[ $# -ge 2 ]] || fail "--server-host requires a value"
        SERVER_HOST="$2"
        shift 2
        ;;
      --force)
        FORCE=1
        shift
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      -*)
        fail "Unknown argument for docker17: $1"
        ;;
      *)
        fail "Unexpected argument for docker17: $1"
        ;;
    esac
  done

  if [[ $# -gt 0 ]]; then
    fail "Unexpected argument for docker17: $1"
  fi

  if [[ ! -d "${WORKSPACE_DIR}" ]]; then
    mkdir -p "${WORKSPACE_DIR}" || fail "Failed to create workspace: ${WORKSPACE_DIR}"
  fi

  WORKSPACE_DIR="$(cd "${WORKSPACE_DIR}" && pwd)"

  if [[ -z "${SQL_FILE_PATH}" ]]; then
    SQL_FILE_PATH="${WORKSPACE_DIR}/pg_setup.sql"
  elif [[ "${SQL_FILE_PATH}" != /* ]]; then
    SQL_FILE_PATH="${WORKSPACE_DIR}/${SQL_FILE_PATH}"
  fi

  DOCKERFILE_PATH="${WORKSPACE_DIR}/${DOCKERFILE_NAME}"
  COMPOSE_FILE_PATH="${WORKSPACE_DIR}/${COMPOSE_FILE_NAME}"
  ENV_FILE_PATH="${WORKSPACE_DIR}/${ENV_FILE_NAME}"

  require_command docker
  require_command openssl

  if ! docker compose version >/dev/null 2>&1; then
    fail "Docker Compose plugin is required."
  fi

  if [[ -z "${ORIGIN_USER}" ]]; then
    ORIGIN_USER="$(id -un)"
  fi

  if [[ -z "${SERVER_HOST}" ]]; then
    if ! SERVER_HOST="$(hostname -f)"; then
      fail "hostname -f failed. Re-run with --server-host HOST."
    fi
  fi

  if [[ ! -f "${SQL_FILE_PATH}" ]]; then
    fail "SQL file not found: ${SQL_FILE_PATH}"
  fi

  log "Workspace: ${WORKSPACE_DIR}"
  log "Container name: ${CONTAINER_NAME}"

  if [[ -f "${ENV_FILE_PATH}" ]]; then
    write_env_file ""
  else
    write_env_file "$(openssl rand -hex 16)"
  fi

  write_template_file "${DOCKERFILE_PATH}" <<'EOF'
FROM postgres:17

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential \
    git \
    postgresql-server-dev-17 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN git clone --branch v0.8.2 --depth 1 https://github.com/pgvector/pgvector.git \
  && cd pgvector \
  && make \
  && make install \
  && rm -rf /tmp/pgvector
EOF

  write_template_file "${COMPOSE_FILE_PATH}" <<'EOF'
services:
  postgres:
    build:
      context: .
      dockerfile: Dockerfile.postgres17-pgvector
    container_name: bluecore-postgres17
    command:
      - postgres
      - -c
      - default_toast_compression=lz4
    restart: unless-stopped
    environment:
      POSTGRES_USER: bluecore
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: bluecore_mem
    ports:
      - "5432:5432"
    volumes:
      - bluecore-postgres17-data:/var/lib/postgresql/data

volumes:
  bluecore-postgres17-data:
EOF

  if [[ "${FORCE}" == "1" ]]; then
    reset_docker_data
  fi

  (
    cd "${WORKSPACE_DIR}"
    log "Starting Docker image build and container"
    docker compose up -d --build
  )

  wait_for_postgres

  if [[ "${FORCE}" == "1" ]]; then
    reset_database
  fi

  log "Verifying PostgreSQL version"
  docker exec "${CONTAINER_NAME}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c "SELECT version();"

  log "Loading initial schema"
  docker exec -i "${CONTAINER_NAME}" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < "${SQL_FILE_PATH}"

  printf '\n'
  log "Setup completed"
  log "Connection URL: postgresql://${POSTGRES_USER}:***@${SERVER_HOST}:5432/${POSTGRES_DB}"
  log "Next steps:"
  print_sync_settings_example "${ORIGIN_USER}"
  log "Run: python3 -m bluecore.mem sync"
}

main() {
  case "${1:-}" in
    apply)
      shift
      run_apply_mode "$@"
      ;;
    docker17|docker)
      shift
      run_docker_mode "$@"
      ;;
    -h|--help)
      usage
      ;;
    "")
      if [[ -n "${MEM_PG_URL:-}" ]]; then
        run_apply_mode "$@"
      else
        usage
        exit 1
      fi
      ;;
    --workspace|--origin-user|--server-host|--force)
      run_docker_mode "$@"
      ;;
    *)
      run_apply_mode "$@"
      ;;
  esac
}

main "$@"
