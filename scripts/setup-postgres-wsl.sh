#!/usr/bin/env bash
# Native Ubuntu PostgreSQL: an isolated cluster, never a container.
set -euo pipefail
PG_MAJOR=18
PG_PORT=55432
PG_CLUSTER=sourcedesk
VECTOR_VERSION=0.8.1
VECTOR_SHA256=a9094dfb85ccdde3cbb295f1086d4c71a20db1d26bf1d6c39f07a7d164033eb4
if [[ ${EUID} != 0 ]]; then
  echo 'Run as root, or through sudo -n / wsl -d Ubuntu -u root.' >&2
  exit 1
fi
private_install_workspace() {
  local dir mode
  # Source builds and restore copies must never trust repository-owned paths.
  for dir in /root /root/.cache /root/.cache/sourcedesk-build; do
    if [[ ! -e "$dir" && ! -L "$dir" ]]; then
      mkdir --mode=0700 "$dir"
    fi
    [[ -d "$dir" && ! -L "$dir" && $(stat -c '%u' "$dir") == 0 ]] || {
      echo "Unsafe installer cache directory: $dir" >&2; return 1;
    }
    mode=$(stat -c '%a' "$dir")
    (( (8#$mode & 022) == 0 )) || {
      echo "Writable installer cache directory: $dir" >&2; return 1;
    }
  done
  [[ "$mode" == 700 ]] || { echo 'Installer cache must have mode 0700.' >&2; return 1; }
  mktemp -d /root/.cache/sourcedesk-build/install.XXXXXXXX
}
install_packages() (
  # A subshell keeps temporary policies/traps out of normal cluster startup.
  work=$(private_install_workspace)
  policy=/usr/sbin/policy-rc.d
  defaults=/etc/postgresql-common/createcluster.conf
  policy_guard=false
  defaults_guard=false
  cleanup_install() {
    local failed=false
    if "$policy_guard"; then
      if [[ -e "$work/policy-rc.d" || -L "$work/policy-rc.d" ]]; then
        mv -fT -- "$work/policy-rc.d" "$policy" || failed=true
      else
        rm -f -- "$policy" || failed=true
      fi
    fi
    if "$defaults_guard"; then
      if [[ -e "$work/createcluster.conf" || -L "$work/createcluster.conf" ]]; then
        mv -fT -- "$work/createcluster.conf" "$defaults" || failed=true
      else
        rm -f -- "$defaults" || failed=true
      fi
    fi
    if "$failed"; then
      echo "Cannot restore installer guards; recovery copies retained in $work" >&2
      return 1
    fi
    rm -rf -- "$work"
  }
  trap 'status=$?; cleanup_install || status=1; exit "$status"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # The installed maintainer calls read this absolute config path and use both
  # invoke-rc.d and deb-systemd-invoke; PGSYSCONFDIR alone does not guard them.
  if [[ -e "$policy" || -L "$policy" ]]; then
    cp -a -- "$policy" "$work/policy-rc.d"
  fi
  policy_guard=true
  rm -f -- "$policy"
  printf '#!/bin/sh\nexit 101\n' > "$policy"
  chmod 0755 "$policy"
  install -d /etc/postgresql-common
  if [[ -e "$defaults" || -L "$defaults" ]]; then
    cp -a -- "$defaults" "$work/createcluster.conf"
  fi
  defaults_guard=true
  rm -f -- "$defaults"
  printf 'create_main_cluster = false\n' > "$defaults"
  # shellcheck source=/dev/null
  source /etc/os-release
  export DEBIAN_FRONTEND=noninteractive UCF_FORCE_CONFFOLD=1
  unset UCF_FORCE_CONFFNEW DPKG_FORCE
  # postgresql-common regenerates this config through ucf; keep the guard.
  case "$VERSION_CODENAME" in
    resolute)
      apt-get update -qq
      apt-get -o Dpkg::Options::=--force-confold install -y postgresql-18=18.6-0ubuntu0.26.04.1 postgresql-18-pgvector=0.8.1-2
      ;;
    noble)
      # Official PGDG repository. No apt-key and no unsigned repositories.
      apt-get update -qq
      apt-get -o Dpkg::Options::=--force-confold install -y ca-certificates curl
      install -d /usr/share/postgresql-common/pgdg
      curl --fail --silent --show-error https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
      printf '%s\n' 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main' \
        > /etc/apt/sources.list.d/sourcedesk-pgdg.list
      apt-get update -qq
      apt-get -o Dpkg::Options::=--force-confold install -y postgresql-18=18.6-1.pgdg24.04+2 postgresql-server-dev-18=18.6-1.pgdg24.04+2 build-essential
      # PGDG has moved on from vector 0.8.1; build the same pinned release
      # used by Ubuntu 26.04 instead of silently upgrading the extension.
      # Extract verified source only inside the root-owned private workspace.
      curl --fail --silent --show-error --location \
        https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.1.tar.gz -o "$work/vector.tar.gz"
      printf '%s  %s\n' "$VECTOR_SHA256" "$work/vector.tar.gz" | sha256sum --check --status
      tar -xzf "$work/vector.tar.gz" -C "$work"
      make -C "$work/pgvector-$VECTOR_VERSION" PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config OPTFLAGS=''
      make -C "$work/pgvector-$VECTOR_VERSION" PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config install
      ;;
    *) echo "Supported Ubuntu releases: 24.04 (noble), 26.04 (resolute). Found $VERSION_CODENAME" >&2; exit 1 ;;
  esac
)
start_cluster() {
  if [[ ! -f /etc/postgresql/$PG_MAJOR/$PG_CLUSTER/postgresql.conf ]]; then
    if ss -ltnH | grep -Eq ":$PG_PORT[[:space:]]"; then
      echo "Port $PG_PORT is occupied; refusing to alter another database." >&2
      exit 1
    fi
    pg_createcluster "$PG_MAJOR" "$PG_CLUSTER" --port "$PG_PORT" \
      -- --auth-local=peer --auth-host=scram-sha-256
    # Only this new project cluster is changed. No wildcard network listener.
    pg_conftool "$PG_MAJOR" "$PG_CLUSTER" set listen_addresses localhost
  fi
  actual_port=$(pg_conftool "$PG_MAJOR" "$PG_CLUSTER" show port | cut -d= -f2 | tr -d ' ')
  [[ "$actual_port" == "$PG_PORT" ]] || { echo 'Unexpected SourceDesk cluster port; refusing changes.' >&2; exit 1; }
  if ! pg_ctlcluster "$PG_MAJOR" "$PG_CLUSTER" status >/dev/null 2>&1; then
    pg_ctlcluster "$PG_MAJOR" "$PG_CLUSTER" start
  fi
  pg_isready -h 127.0.0.1 -p "$PG_PORT" >/dev/null
  echo "SourceDesk PostgreSQL $PG_MAJOR cluster ready on loopback port $PG_PORT."
}
windows_network() {
  # WSL localhost forwarding is not reliable on every Windows installation.
  # Bind only this distro's private interface, never 0.0.0.0, and admit only
  # the Windows-side WSL gateway, to exactly the two project databases/roles.
  wsl_ip=$(ip -4 -o addr show dev eth0 | awk '{split($4,a,"/"); print a[1]; exit}')
  host_ip=$(ip -4 route show default | awk '{print $3; exit}')
  [[ "$wsl_ip" =~ ^[0-9.]+$ && "$host_ip" =~ ^[0-9.]+$ ]] || { echo 'Cannot determine private WSL network.' >&2; exit 1; }
  config=/etc/postgresql/$PG_MAJOR/$PG_CLUSTER
  desired="localhost,$wsl_ip"
  current=$(pg_conftool "$PG_MAJOR" "$PG_CLUSTER" show listen_addresses | cut -d= -f2- | tr -d " '")
  # Only a changed listener requires restart. Repeated API startup must not
  # disconnect a running worker or another application process.
  restart=false
  if [[ "$current" != "$desired" ]]; then
    pg_conftool "$PG_MAJOR" "$PG_CLUSTER" set listen_addresses "$desired"
    restart=true
  fi
  printf 'host sourcedesk sourcedesk_app %s/32 scram-sha-256\nhost sourcedesk_test sourcedesk_test_app %s/32 scram-sha-256\n' "$host_ip" "$host_ip" > "$config/sourcedesk-network.conf"
  if ! grep -Fxq 'include sourcedesk-network.conf' "$config/pg_hba.conf"; then
    printf '\ninclude sourcedesk-network.conf\n' >> "$config/pg_hba.conf"
  fi
  if "$restart"; then
    pg_ctlcluster "$PG_MAJOR" "$PG_CLUSTER" restart
  else
    pg_ctlcluster "$PG_MAJOR" "$PG_CLUSTER" reload
  fi
  echo "SourceDesk private WSL interface enabled; access restricted to Windows WSL gateway."
}
case "${1:---start}" in
  --install) install_packages; start_cluster ;;
  --install-windows) install_packages; start_cluster; windows_network ;;
  --start) start_cluster ;;
  --start-windows) start_cluster; windows_network ;;
  --psql)
    case "${2:-}" in postgres|sourcedesk|sourcedesk_test) ;; *) echo 'Unsupported project database.' >&2; exit 1 ;; esac
    exec runuser -u postgres -- env -i PATH=/usr/bin:/bin \
      /usr/lib/postgresql/18/bin/psql --no-psqlrc --no-password --quiet --tuples-only --no-align \
      --set ON_ERROR_STOP=1 --host /var/run/postgresql --username postgres --port "$PG_PORT" --dbname "$2"
    ;;
  *) echo 'Usage: setup-postgres-wsl.sh [--install|--start|--psql DATABASE]' >&2; exit 1 ;;
esac
