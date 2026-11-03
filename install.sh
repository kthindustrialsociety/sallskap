#!/usr/bin/env bash
#
# Sällskap installer: turns a fresh Debian or Ubuntu server into a secured host
# ready to run the kit. Safe to run again. See docs/spec.md §10.
#
#   curl -fsSL <url>/install.sh | sudo bash    install
#   bash install.sh --check                    run the checks, change nothing
#
# Overrides, mainly for testing:
#   SALLSKAP_REPO      git URL or path of the kit
#   SALLSKAP_VERSION   branch or tag to install

# /etc/os-release exists only on the target system, not where this is linted.
# shellcheck disable=SC1091

set -Eeuo pipefail

# Named KIT_* because /etc/os-release assigns VERSION, NAME and ID, and sourcing
# it over a readonly variable of the same name kills the script.
readonly KIT_REPO="${SALLSKAP_REPO:-https://github.com/kthindustrialsociety/sallskap.git}"
readonly KIT_VERSION="${SALLSKAP_VERSION:-main}"
readonly INSTALL_DIR=/opt/sallskap
readonly CONFIG_DIR=/etc/sallskap
readonly DEPLOY_USER=sallskap-deploy

readonly MIN_RAM_MB=1800          # a 2 GB server reports about 1900 MB
readonly SUGGESTED_RAM_MB=3400    # a 4 GB server reports about 3700 MB
readonly MIN_DISK_MB=15000
readonly SMTP_PROBE_HOST=smtp.gmail.com

failures=0
warnings=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  warn  %s\n' "$*"; warnings=$((warnings + 1)); }
bad()  { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }
step() { printf '\n==> %s\n' "$*"; }

# ---- Checks: read-only, see docs/spec.md §10.2 ------------------------------

check_os() {
  if [[ ! -r /etc/os-release ]]; then
    bad "This is not a supported Linux system. Choose Ubuntu 24.04 or 26.04, or Debian 12 or 13."
    return
  fi
  local id version
  id=$(. /etc/os-release && printf '%s' "${ID:-}")
  version=$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")
  case "$id:$version" in
    debian:12 | debian:13 | ubuntu:24.04 | ubuntu:26.04) ok "Operating system: $id $version" ;;
    *) bad "Unsupported system ($id $version). Reinstall the server with Ubuntu 24.04 or 26.04, or Debian 12 or 13." ;;
  esac
}

check_arch() {
  local arch
  arch=$(uname -m)
  case "$arch" in
    x86_64 | aarch64) ok "Processor type: $arch" ;;
    *) bad "Unsupported processor type ($arch). Choose an x86-64 or ARM64 server." ;;
  esac
}

check_virtualization() {
  command -v systemd-detect-virt >/dev/null || return 0
  if systemd-detect-virt --container --quiet; then
    bad "This server is a container, which can't run the kit. Choose a full virtual machine (KVM)."
  else
    ok "Server type: full virtual machine or hardware"
  fi
}

check_resources() {
  [[ -r /proc/meminfo ]] || return 0
  local ram_mb disk_mb cpus
  ram_mb=$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)
  disk_mb=$(df -Pm / | awk 'NR == 2 { print $4 }')
  cpus=$(nproc)

  if ((ram_mb < MIN_RAM_MB)); then
    bad "Only ${ram_mb} MB of RAM. Choose a plan with at least 2 GB, ideally 4 GB."
  elif ((ram_mb < SUGGESTED_RAM_MB)); then
    warn "${ram_mb} MB of RAM runs the sites, but 4 GB is suggested for the Minimal preset."
  else
    ok "RAM: ${ram_mb} MB"
  fi

  if ((disk_mb < MIN_DISK_MB)); then
    bad "Only ${disk_mb} MB of free disk. Choose a plan with at least 25 GB."
  else
    ok "Free disk: ${disk_mb} MB"
  fi

  if ((cpus < 2)); then
    warn "1 CPU works for small sites, but 2 are suggested."
  else
    ok "CPUs: $cpus"
  fi
}

check_public_ipv4() {
  if ! command -v curl >/dev/null; then
    warn "Can't check the public IPv4 address because curl is missing."
    return
  fi
  local public_ip local_ip
  public_ip=$(curl -4fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
  if [[ -z "$public_ip" ]]; then
    bad "No public IPv4 address found. Many visitors can't reach IPv6-only sites, so add an IPv4 address in your provider's control panel."
    return
  fi
  local_ip=$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }' || true)
  if [[ -n "$local_ip" && "$local_ip" != "$public_ip" ]]; then
    warn "The public address ($public_ip) isn't on this server ($local_ip), so ports 80 and 443 must be forwarded to it."
  else
    ok "Public IPv4: $public_ip"
  fi
}

check_ports_free() {
  command -v ss >/dev/null || return 0
  local port listeners owner
  for port in 80 443; do
    listeners=$(ss -ltnpH "sport = :$port" 2>/dev/null || true)
    if [[ -z "$listeners" ]]; then
      ok "Port $port is free"
      continue
    fi
    owner=$(printf '%s\n' "$listeners" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -n 1)
    if [[ "$owner" == docker-proxy && -d "$INSTALL_DIR" ]]; then
      ok "Port $port is used by this installation"
    else
      bad "Port $port is already used by ${owner:-another program}. Reinstall the server, or stop that program, and run this again."
    fi
  done
}

check_outgoing_mail() {
  command -v timeout >/dev/null || return 0
  if timeout 5 bash -c "exec 3<>/dev/tcp/$SMTP_PROBE_HOST/587" 2>/dev/null; then
    ok "Outgoing mail on port 587 works"
  else
    warn "Outgoing mail on port 587 seems blocked. The setup wizard will need a relay or another port."
  fi
}

run_checks() {
  step "Checking this server"
  check_os
  check_arch
  check_virtualization
  check_resources
  check_public_ipv4
  check_ports_free
  check_outgoing_mail
  say ""
  say "Checks finished: $failures failed, $warnings warnings."
}

# ---- Install: every step is safe to repeat ----------------------------------

install_packages() {
  step "Installing base packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git python3 ufw unattended-upgrades >/dev/null
}

enable_automatic_updates() {
  step "Turning on automatic security updates"
  cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
  cat >/etc/apt/apt.conf.d/52sallskap-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
}

# Only the keys of someone who can administer the server count. Locking out
# passwords on the strength of another user's key would lock the operator out.
admin_has_ssh_key() {
  local key_files=(/root/.ssh/authorized_keys)
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
    key_files+=("$(getent passwd "$SUDO_USER" | cut -d: -f6)/.ssh/authorized_keys")
  fi
  grep -Eqs '^[^#]*(ssh-|ecdsa-|sk-)' "${key_files[@]}"
}

secure_ssh() {
  step "Securing SSH"
  # Sorts before the 50-cloud-init.conf many images ship, and sshd keeps the
  # first value it reads.
  local conf=/etc/ssh/sshd_config.d/01-sallskap.conf
  if ! admin_has_ssh_key; then
    warn "No SSH key found, so password login stays on. Add a key and run this again."
    rm -f "$conf"
    return
  fi
  cat >"$conf" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  if ! sshd -t; then
    rm -f "$conf"
    warn "The SSH settings didn't validate, so they were left unchanged."
    return
  fi
  systemctl reload ssh 2>/dev/null || systemctl restart ssh
  ok "SSH accepts keys only"
}

configure_firewall() {
  step "Configuring the firewall"
  local ssh_port
  ssh_port=$(sshd -T 2>/dev/null | awk '$1 == "port" { print $2; exit }' || true)
  ssh_port=${ssh_port:-22}

  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow "$ssh_port/tcp" >/dev/null   # before enabling, so this session survives
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow 443/udp >/dev/null           # HTTP/3
  ufw --force enable >/dev/null
  ok "Open: $ssh_port/tcp, 80/tcp, 443/tcp, 443/udp"
}

install_docker() {
  step "Installing Docker"
  local id codename arch
  id=$(. /etc/os-release && printf '%s' "$ID")
  codename=$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")
  arch=$(dpkg --print-architecture)

  # Log rotation keeps container logs from filling the disk. Written first so
  # the daemon starts with it. Ports published by Docker bypass ufw, so only
  # Caddy may publish any: see docs/spec.md §12.
  install -d -m 0755 /etc/docker
  if [[ ! -e /etc/docker/daemon.json ]]; then
    cat >/etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
  fi

  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$id/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
    "$arch" "$id" "$codename" >/etc/apt/sources.list.d/docker.list

  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null
  ok "$(docker --version)"
}

fetch_kit() {
  step "Fetching Sällskap ($KIT_VERSION)"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch -q --depth 1 origin "$KIT_VERSION"
    git -C "$INSTALL_DIR" checkout -q --detach FETCH_HEAD
  else
    git clone -q --depth 1 --branch "$KIT_VERSION" "$KIT_REPO" "$INSTALL_DIR"
  fi
  ln -sf "$INSTALL_DIR/kit/kit.py" /usr/local/bin/kit
  ok "Installed in $INSTALL_DIR"
}

# Sites are published over SSH by this account. Its keys can only run
# `kit site receive`, and it owns nothing but the site folders.
prepare_folders() {
  step "Preparing folders"
  if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/var/lib/$DEPLOY_USER" --shell /bin/sh "$DEPLOY_USER"
  fi
  install -d -m 0700 "$CONFIG_DIR"
  install -d -m 0755 "$CONFIG_DIR/generated" /srv/website /srv/caddy/data /srv/caddy/config
  ok "Data in /srv, config in $CONFIG_DIR"
}

start_core() {
  step "Starting Sällskap"
  if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    say "  The server is ready. Next, set your primary domain: kit init example.org"
    return
  fi
  kit apply --build
  ok "Core and the web server are running"
}

on_error() {
  say ""
  say "The installer stopped on line $1. Nothing is lost: fix the problem and run it again."
}

main() {
  local check_only=false
  case "${1:-}" in
    "") ;;
    --check) check_only=true ;;
    -h | --help)
      sed -n '3,11p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'
      return 0
      ;;
    *)
      say "Unknown option: $1. Use --check, or no option to install."
      return 64
      ;;
  esac

  run_checks

  if ((failures > 0)); then
    say "Fix the failed checks above, then run this again."
    return 2
  fi
  if [[ "$check_only" == true ]]; then
    say "This server can run Sällskap."
    return 0
  fi
  if ((EUID != 0)); then
    say "The install must run as root. Run it again with sudo."
    return 1
  fi

  trap 'on_error "$LINENO"' ERR
  install_packages
  enable_automatic_updates
  secure_ssh
  configure_firewall
  install_docker
  prepare_folders
  fetch_kit
  start_core

  say ""
  say "Done, with $warnings warnings."
}

# Called last, so a download cut off midway runs nothing.
main "$@"
