#!/usr/bin/env bash
# PostgreSQL ネイティブ環境セットアップ
# 既存の PostgreSQL にユーザ登録・DB作成・pg_vector インストール・スキーマ初期化を行う
#
# 使用方法:
#   bash pg_setup_native.sh [OPTIONS]
#   bash pg_setup_native.sh --user myuser --password mypass --db mydb

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- デフォルト値 ----

PG_USER="deepblue"
PG_PASSWORD=""
PG_DB="deepblue_mem"
PG_HOST="localhost"
PG_PORT="5432"
PG_SUPERUSER="postgres"
PG_SUPERUSER_PASSWORD="${PG_SETUP_POSTGRES_PASSWORD:-}"
ORIGIN_USER=""
SQL_FILE="${SCRIPT_DIR}/pg_setup.sql"
INSTALL_PGVECTOR=1
SKIP_SCHEMA=0
FORCE=0
CREDENTIALS_FILE=""

# ---- 実行時状態 ----

APT_UPDATED=0
SUDO_CMD=()
LAST_ERROR_OUTPUT=""
PROGRESS_TOTAL=0
PROGRESS_CURRENT=0
PGVECTOR_WORK_DIR=""  # クリーンアップ対象の一時ディレクトリ

# ---- ログ・エラーハンドリング ----

log() {
  printf '[pg-setup-native] %s\n' "$1"
}

warn() {
  printf '[pg-setup-native] Warning: %s\n' "$1" >&2
}

fail() {
  printf '[pg-setup-native] Error: %s\n' "$1" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  printf '[pg-setup-native] Error: command failed (exit=%s) at line %s: %s\n' \
    "${exit_code}" "${BASH_LINENO[0]:-unknown}" "${BASH_COMMAND:-unknown}" >&2
  exit "${exit_code}"
}

cleanup() {
  if [[ -n "${PGVECTOR_WORK_DIR}" && -d "${PGVECTOR_WORK_DIR}" ]]; then
    rm -rf "${PGVECTOR_WORK_DIR}"
  fi
}

trap on_error ERR
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage:
  bash pg_setup_native.sh [OPTIONS]

Options:
  --user USER                PostgreSQL ユーザ名 (default: deepblue)
  --password PASSWORD        ユーザのパスワード (default: auto-generate)
  --db DB_NAME              データベース名 (default: deepblue_mem)
  --host HOST               PostgreSQL ホスト (default: localhost)
  --port PORT               PostgreSQL ポート (default: 5432)
  --postgres-password PASS  PostgreSQL 管理者ユーザ(postgres)のパスワード
  --origin-user USER        settings.json の origin_user (default: $(id -un))
  --sql-file PATH           SQL スクリプトパス (default: ./pg_setup.sql)
  --credentials-file PATH   認証情報を保存するファイル (default: ~/.deepblue/pg_credentials.json)
  --no-install-pgvector     pg_vector インストールをスキップ
  --skip-schema             スキーマ初期化をスキップ
  --force                   既存ユーザ・DB を削除して再作成
  --help, -h               このヘルプを表示

Environment:
  PG_SETUP_POSTGRES_PASSWORD  PostgreSQL 管理者ユーザ(postgres)のパスワード
  PG_SETUP_DISTRO             ディストリビューション検出を上書き（テスト用）

Requirements:
  - PostgreSQL がインストール済み（バージョン 13 以上推奨）
  - psql がアクセス可能
  - sudo（Linuxでパッケージ導入が必要な場合）
  - ビルド環境：gcc、make、git

Supported Operating Systems:
  - Ubuntu / Debian (apt)
  - RockyLinux / RHEL / CentOS / AlmaLinux / Fedora (yum/dnf)
  - macOS (homebrew)

EOF
}

# ---- ユーティリティ ----

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Required command not found: $1"
  fi
}

generate_password() {
  openssl rand -hex 16
}

# ---- ディストリビューション検出 ----

detect_distro() {
  if [[ -n "${PG_SETUP_DISTRO:-}" ]]; then
    echo "${PG_SETUP_DISTRO}"
    return
  fi

  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    echo "${ID}"
  elif [[ -f /etc/redhat-release ]]; then
    echo "rhel"
  elif [[ "$OSTYPE" == "darwin"* ]]; then
    echo "macos"
  else
    echo "unknown"
  fi
}

get_package_manager() {
  local distro="$1"
  case "${distro}" in
    ubuntu|debian)         echo "apt" ;;
    rhel|rocky|centos|fedora|almalinux)
      if command -v dnf >/dev/null 2>&1; then echo "dnf"; else echo "yum"; fi ;;
    macos)                 echo "brew" ;;
    *)  fail "Unsupported distribution: ${distro}. Please install required packages manually." ;;
  esac
}

# ---- パッケージ管理 ----

ensure_package_manager_available() {
  local pkg_mgr="$1"
  case "${pkg_mgr}" in
    apt)       require_command apt-get ;;
    yum|dnf)   require_command "${pkg_mgr}" ;;
    brew)      require_command brew ;;
    *)         fail "Unsupported package manager: ${pkg_mgr}" ;;
  esac
}

init_sudo_command() {
  if [[ "${EUID}" -eq 0 ]]; then
    SUDO_CMD=()
    return
  fi

  require_command sudo

  if sudo -n true >/dev/null 2>&1; then
    SUDO_CMD=(sudo)
    return
  fi

  if [[ -t 0 ]]; then
    log "sudo authentication is required for package installation"
    if sudo -v; then
      SUDO_CMD=(sudo)
      return
    fi
    fail "Failed to authenticate with sudo."
  fi

  fail "sudo privileges are required, but passwordless sudo is unavailable in non-interactive mode."
}

run_as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    "${SUDO_CMD[@]}" "$@"
  fi
}

is_best_candidate_error() {
  case "$1" in
    *"best update candidate"*|*"best candidate"*|*"最良アップデート候補"*) return 0 ;;
    *) return 1 ;;
  esac
}

install_package() {
  local package="$1"
  local pkg_mgr="$2"
  local first_error=""

  case "${pkg_mgr}" in
    apt)
      if [[ "${APT_UPDATED}" -eq 0 ]]; then
        run_with_spinner "Updating apt package index" run_as_root apt-get update || return 1
        APT_UPDATED=1
      fi
      run_with_spinner "Installing package (${pkg_mgr}): ${package}" run_as_root apt-get install -y "${package}"
      ;;
    dnf)
      if run_with_spinner "Installing package (${pkg_mgr}): ${package}" run_as_root dnf install -y "${package}"; then
        return 0
      fi
      first_error="${LAST_ERROR_OUTPUT}"
      if is_best_candidate_error "${first_error}"; then
        warn "Detected dnf best-candidate issue. Retrying with --nobest."
        run_with_spinner "Retry install with --nobest (${pkg_mgr}): ${package}" \
          run_as_root dnf install -y --nobest "${package}"
        return $?
      fi
      return 1
      ;;
    yum)
      if run_with_spinner "Installing package (${pkg_mgr}): ${package}" run_as_root yum install -y "${package}"; then
        return 0
      fi
      first_error="${LAST_ERROR_OUTPUT}"
      warn "yum install failed. Retrying with --skip-broken."
      if run_with_spinner "Retry install with --skip-broken (${pkg_mgr}): ${package}" \
           run_as_root yum install -y --skip-broken "${package}"; then
        return 0
      fi
      LAST_ERROR_OUTPUT="${first_error}"$'\n'"${LAST_ERROR_OUTPUT}"
      return 1
      ;;
    brew)
      run_with_spinner "Installing package (${pkg_mgr}): ${package}" brew install "${package}"
      ;;
  esac
}

install_package_or_fail() {
  local package="$1"
  local pkg_mgr="$2"
  if ! install_package "${package}" "${pkg_mgr}"; then
    fail "Failed to install package '${package}' via ${pkg_mgr}. ${LAST_ERROR_OUTPUT}"
  fi
}

# ---- 進捗表示 ----

set_progress_total() {
  PROGRESS_TOTAL="$1"
  PROGRESS_CURRENT=0
}

start_progress_step() {
  PROGRESS_CURRENT=$((PROGRESS_CURRENT + 1))
  log "[${PROGRESS_CURRENT}/${PROGRESS_TOTAL}] $1"
}

run_with_spinner() {
  local description="$1"
  shift

  local output_file status pid spinner i
  output_file="$(mktemp)"
  spinner='|/-\'
  i=0

  if [[ -t 1 ]]; then
    # 対話端末: スピナーを表示しながらバックグラウンド実行
    "$@" >"${output_file}" 2>&1 & pid=$!
    while kill -0 "${pid}" 2>/dev/null; do
      printf '\r[pg-setup-native] [%s/%s] %s ... %s' \
        "${PROGRESS_CURRENT}" "${PROGRESS_TOTAL}" "${description}" "${spinner:i++%4:1}"
      sleep 0.1
    done
    wait "${pid}"; status=$?
    printf '\r'
  else
    # 非対話: そのまま実行（出力はキャプチャ）
    "$@" >"${output_file}" 2>&1; status=$?
  fi

  if [[ "${status}" -ne 0 ]]; then
    LAST_ERROR_OUTPUT="$(cat "${output_file}")"
    rm -f "${output_file}"
    [[ -n "${LAST_ERROR_OUTPUT}" ]] && printf '%s\n' "${LAST_ERROR_OUTPUT}" >&2
    return "${status}"
  fi

  rm -f "${output_file}"
  LAST_ERROR_OUTPUT=""
  log "${description} completed"
}

# ---- PostgreSQL 操作 ----

psql_superuser() {
  if [[ -n "${PG_SUPERUSER_PASSWORD}" ]]; then
    PGPASSWORD="${PG_SUPERUSER_PASSWORD}" psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_SUPERUSER}" "$@"
  else
    psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_SUPERUSER}" "$@"
  fi
}

psql_app_user() {
  PGPASSWORD="${PG_PASSWORD}" psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" "$@"
}

is_password_auth_error() {
  case "$1" in
    *"password authentication failed"*|*"no password supplied"*|*"fe_sendauth"*|*"PAM authentication failed"*|*"パスワード認証"*)
      return 0 ;;
    *) return 1 ;;
  esac
}

request_superuser_password_once() {
  if [[ -n "${PG_SUPERUSER_PASSWORD}" ]]; then
    return
  fi

  if [[ -n "${PGPASSWORD:-}" ]]; then
    PG_SUPERUSER_PASSWORD="${PGPASSWORD}"
    return
  fi

  if [[ ! -t 0 ]]; then
    fail "PostgreSQL superuser password is required in non-interactive mode. Set --postgres-password or PG_SETUP_POSTGRES_PASSWORD."
  fi

  read -rsp "Enter PostgreSQL superuser password (${PG_SUPERUSER}): " PG_SUPERUSER_PASSWORD
  printf '\n'
  [[ -n "${PG_SUPERUSER_PASSWORD}" ]] || fail "PostgreSQL superuser password cannot be empty."
}

check_psql_access() {
  local error_output=""

  if error_output="$(psql_superuser -d postgres -c "SELECT 1;" 2>&1 >/dev/null)"; then
    return
  fi

  if is_password_auth_error "${error_output}" && [[ -z "${PG_SUPERUSER_PASSWORD}" ]]; then
    request_superuser_password_once
    if error_output="$(psql_superuser -d postgres -c "SELECT 1;" 2>&1 >/dev/null)"; then
      log "PostgreSQL superuser password accepted (reused for this run)"
      return
    fi
  fi

  fail "Cannot connect to PostgreSQL at ${PG_HOST}:${PG_PORT}. Details: ${error_output}"
}

detect_pg_major_version() {
  local pg_version
  pg_version="$(psql_superuser -d postgres -Atqc "SELECT current_setting('server_version_num')::int / 10000;")"
  [[ "${pg_version}" =~ ^[0-9]+$ ]] || fail "Failed to detect PostgreSQL major version. Output: ${pg_version}"
  echo "${pg_version}"
}

# ---- pg_trgm / pg_vector インストール ----

ensure_pgtrgm_prerequisites() {
  local distro pkg_mgr pg_version
  distro="$(detect_distro)"
  pkg_mgr="$(get_package_manager "${distro}")"

  if psql_superuser -d "${PG_DB}" -Atqc "SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm';" | grep -q 1; then
    log "pg_trgm is already available"
    return
  fi

  if [[ "${distro}" == "macos" ]]; then
    log "Skipping pg_trgm package installation on macOS"
    return
  fi

  ensure_package_manager_available "${pkg_mgr}"
  init_sudo_command
  pg_version="$(detect_pg_major_version)"

  log "Ensuring pg_trgm prerequisites..."
  case "${distro}" in
    ubuntu|debian)
      install_package_or_fail "postgresql-contrib-${pg_version}" "${pkg_mgr}"
      ;;
    rhel|rocky|centos|almalinux|fedora)
      if ! install_package "postgresql${pg_version}-contrib" "${pkg_mgr}"; then
        warn "Versioned contrib package not available. Trying postgresql-contrib."
        install_package_or_fail "postgresql-contrib" "${pkg_mgr}"
      fi
      ;;
  esac
}

install_pgvector() {
  log "Installing pg_vector..."

  local distro pkg_mgr pg_version
  distro="$(detect_distro)"
  pkg_mgr="$(get_package_manager "${distro}")"

  log "Detected: ${distro} (package manager: ${pkg_mgr})"
  ensure_package_manager_available "${pkg_mgr}"

  # 既にインストール済みなら何もしない
  if psql_superuser -d postgres -Atqc "SELECT 1 FROM pg_available_extensions WHERE name = 'vector';" | grep -q 1; then
    log "pg_vector extension is already available"
    return
  fi

  if [[ "${pkg_mgr}" != "brew" ]]; then
    init_sudo_command
  fi

  log "Installing PostgreSQL development files..."
  pg_version="$(detect_pg_major_version)"
  case "${distro}" in
    ubuntu|debian)
      install_package_or_fail "postgresql-server-dev-${pg_version}" "${pkg_mgr}"
      ;;
    rhel|rocky|centos|almalinux|fedora)
      if ! install_package "postgresql${pg_version}-devel" "${pkg_mgr}"; then
        warn "Versioned devel package not available. Trying postgresql-devel."
        install_package_or_fail "postgresql-devel" "${pkg_mgr}"
      fi
      ;;
    macos)
      log "Skipping postgresql-devel on macOS (assuming already installed)"
      ;;
  esac

  if ! command -v gcc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
    log "Installing build tools..."
    case "${distro}" in
      ubuntu|debian)
        install_package_or_fail "build-essential" "${pkg_mgr}"
        ;;
      rhel|rocky|centos|almalinux|fedora)
        install_package_or_fail "gcc" "${pkg_mgr}"
        install_package_or_fail "make" "${pkg_mgr}"
        ;;
      macos)
        fail "Xcode Command Line Tools required. Run: xcode-select --install"
        ;;
    esac
  fi

  if ! command -v git >/dev/null 2>&1; then
    log "Installing git..."
    install_package_or_fail "git" "${pkg_mgr}"
  fi

  log "Compiling and installing pg_vector from source..."
  PGVECTOR_WORK_DIR="$(mktemp -d)"
  run_with_spinner "Cloning pg_vector repository" \
    git clone --branch v0.8.2 --depth 1 https://github.com/pgvector/pgvector.git "${PGVECTOR_WORK_DIR}/pgvector" \
    || fail "Failed to clone pg_vector repository."
  run_with_spinner "Compiling pg_vector" \
    make -C "${PGVECTOR_WORK_DIR}/pgvector" \
    || fail "Failed to compile pg_vector."
  run_with_spinner "Installing pg_vector binaries" \
    run_as_root make -C "${PGVECTOR_WORK_DIR}/pgvector" install \
    || fail "Failed to install pg_vector binaries."

  if ! psql_superuser -d "${PG_DB}" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null; then
    fail "pg_vector installation completed, but extension activation failed."
  fi
  log "pg_vector installation completed"
}

# ---- ユーザ・DB 管理 ----

user_exists() {
  # SQL インジェクション防止: printf でシングルクォートをエスケープ
  local escaped_user
  printf -v escaped_user '%s' "${PG_USER//"'"/"''"}"
  psql_superuser -d postgres -Atqc "SELECT 1 FROM pg_user WHERE usename='${escaped_user}';" | grep -q 1
}

db_exists() {
  local escaped_db
  printf -v escaped_db '%s' "${PG_DB//"'"/"''"}"
  psql_superuser -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='${escaped_db}';" | grep -q 1
}

drop_user_if_exists() {
  if user_exists; then
    log "Dropping existing user: ${PG_USER}"
    psql_superuser -d postgres -c "DROP USER IF EXISTS \"${PG_USER}\" CASCADE;" >/dev/null
  fi
}

drop_db_if_exists() {
  if db_exists; then
    log "Dropping existing database: ${PG_DB}"
    psql_superuser -d postgres -c "DROP DATABASE IF EXISTS \"${PG_DB}\" WITH (FORCE);" >/dev/null
  fi
}

create_user() {
  if user_exists; then
    if [[ "${FORCE}" != "1" ]]; then
      fail "User '${PG_USER}' already exists. Use --force to overwrite."
    fi
    drop_user_if_exists
  fi

  log "Creating PostgreSQL user: ${PG_USER}"
  local escaped_password="${PG_PASSWORD//\'/\'\'}"
  psql_superuser -d postgres -c "CREATE USER \"${PG_USER}\" WITH PASSWORD '${escaped_password}' CREATEDB;" >/dev/null
  log "User created with password (store it safely)"
}

create_database() {
  if db_exists; then
    if [[ "${FORCE}" != "1" ]]; then
      fail "Database '${PG_DB}' already exists. Use --force to overwrite."
    fi
    drop_db_if_exists
  fi

  log "Creating database: ${PG_DB}"
  psql_superuser -d postgres -c "CREATE DATABASE \"${PG_DB}\" OWNER \"${PG_USER}\";" >/dev/null
  log "Database created"
}

enable_extensions() {
  log "Enabling PostgreSQL extensions..."

  if ! psql_superuser -d "${PG_DB}" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;" >/dev/null; then
    fail "Failed to enable pg_trgm extension. Ensure PostgreSQL contrib package is installed."
  fi
  log "Extension pg_trgm enabled"

  if psql_superuser -d "${PG_DB}" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null; then
    log "Extension vector (pg_vector) enabled"
  else
    warn "vector extension not available. Ensure pg_vector is installed."
  fi
}

load_schema() {
  if [[ "${SKIP_SCHEMA}" == "1" ]]; then
    log "Skipping schema initialization"
    return
  fi

  if [[ ! -f "${SQL_FILE}" ]]; then
    fail "SQL file not found: ${SQL_FILE}"
  fi

  log "Loading schema from: ${SQL_FILE}"
  psql_app_user -d "${PG_DB}" -v ON_ERROR_STOP=1 -f "${SQL_FILE}" >/dev/null
  log "Schema loaded successfully"
}

# ---- 認証情報の保存・表示 ----

save_credentials() {
  if [[ -z "${CREDENTIALS_FILE}" ]]; then
    CREDENTIALS_FILE="${HOME}/.deepblue/pg_credentials.json"
  fi

  local credentials_dir
  credentials_dir="$(dirname "${CREDENTIALS_FILE}")"

  if [[ ! -d "${credentials_dir}" ]]; then
    mkdir -p "${credentials_dir}" || { warn "Failed to create credentials directory: ${credentials_dir}"; return 1; }
  fi

  cat > "${CREDENTIALS_FILE}" <<EOF
{
  "user": "${PG_USER}",
  "password": "${PG_PASSWORD}",
  "host": "${PG_HOST}",
  "port": ${PG_PORT},
  "database": "${PG_DB}",
  "connection_url": "postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${PG_DB}"
}
EOF

  chmod 0600 "${CREDENTIALS_FILE}"
  log "Credentials saved: ${CREDENTIALS_FILE}"
  log "File permissions: 0600 (owner read-write only)"
}

print_connection_info() {
  local origin_user="${ORIGIN_USER:-$(id -un)}"
  local connection_url_masked="postgresql://${PG_USER}:***@${PG_HOST}:${PG_PORT}/${PG_DB}"
  local connection_url_full="postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${PG_DB}"

  printf '\n'
  log "Setup completed successfully!"
  log "Connection URL (masked): ${connection_url_masked}"
  printf '\n'
  printf '=== PASSWORD ===\n'
  printf '%s\n' "${PG_PASSWORD}"
  printf '================\n'
  printf '\n'
  printf 'Next steps:\n'
  printf '1. Store the password securely (see above)\n'
  printf '2. Edit ~/.deepblue/settings.json:\n'
  printf '   {\n'
  printf '     "mem": {\n'
  printf '       "sync": {\n'
  printf '         "enabled": true,\n'
  printf '         "interval_hours": 3,\n'
  printf '         "postgres_url": "%s",\n' "${connection_url_full}"
  printf '         "origin_user": "%s"\n' "${origin_user}"
  printf '       }\n'
  printf '     }\n'
  printf '   }\n'
  printf '3. Run: python3 -m deepblue.mem sync\n'
  printf '\n'
}

# ---- 引数パース ----

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --user)
        [[ $# -ge 2 ]] || fail "--user requires a value"
        PG_USER="$2"; shift 2 ;;
      --password)
        [[ $# -ge 2 ]] || fail "--password requires a value"
        PG_PASSWORD="$2"; shift 2 ;;
      --db)
        [[ $# -ge 2 ]] || fail "--db requires a value"
        PG_DB="$2"; shift 2 ;;
      --host)
        [[ $# -ge 2 ]] || fail "--host requires a value"
        PG_HOST="$2"; shift 2 ;;
      --port)
        [[ $# -ge 2 ]] || fail "--port requires a value"
        PG_PORT="$2"; shift 2 ;;
      --postgres-password)
        [[ $# -ge 2 ]] || fail "--postgres-password requires a value"
        PG_SUPERUSER_PASSWORD="$2"; shift 2 ;;
      --origin-user)
        [[ $# -ge 2 ]] || fail "--origin-user requires a value"
        ORIGIN_USER="$2"; shift 2 ;;
      --sql-file)
        [[ $# -ge 2 ]] || fail "--sql-file requires a value"
        SQL_FILE="$2"; shift 2 ;;
      --credentials-file)
        [[ $# -ge 2 ]] || fail "--credentials-file requires a value"
        CREDENTIALS_FILE="$2"; shift 2 ;;
      --no-install-pgvector)
        INSTALL_PGVECTOR=0; shift ;;
      --skip-schema)
        SKIP_SCHEMA=1; shift ;;
      --force)
        FORCE=1; shift ;;
      --help|-h)
        usage; exit 0 ;;
      *)
        fail "Unknown option: $1" ;;
    esac
  done
}

# ---- エントリポイント ----

main() {
  parse_args "$@"

  if [[ -z "${PG_PASSWORD}" ]]; then
    PG_PASSWORD="$(generate_password)"
  fi

  log "PostgreSQL Native Setup"
  log "User: ${PG_USER}, DB: ${PG_DB}, Host: ${PG_HOST}:${PG_PORT}"
  printf '\n'

  require_command psql
  require_command openssl

  local total_steps=6
  if [[ "${INSTALL_PGVECTOR}" == "1" ]]; then
    total_steps=$((total_steps + 1))
  fi
  set_progress_total "${total_steps}"

  start_progress_step "PostgreSQL への接続と認証を確認"
  check_psql_access

  if [[ "${INSTALL_PGVECTOR}" == "1" ]]; then
    start_progress_step "pg_vector をインストール"
    install_pgvector
    printf '\n'
  fi

  start_progress_step "ユーザとデータベースを作成"
  create_user
  create_database
  printf '\n'

  start_progress_step "pg_trgm 前提パッケージを確認"
  ensure_pgtrgm_prerequisites
  printf '\n'

  start_progress_step "拡張機能を有効化"
  enable_extensions
  printf '\n'

  start_progress_step "スキーマを初期化"
  load_schema
  printf '\n'

  start_progress_step "認証情報を保存"
  save_credentials
  printf '\n'

  print_connection_info
}

main "$@"
