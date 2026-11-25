#!/usr/bin/env bash
# Install runout as a systemd service on Raspberry Pi OS / Debian.
#
#   curl -fsSL https://raw.githubusercontent.com/runout/runout/main/deploy/install.sh | sudo bash
# or, from a clone:
#   sudo ./deploy/install.sh
set -euo pipefail

REPO="${RUNOUT_REPO:-https://github.com/cfer50/runout}"
PREFIX=/opt/runout
CONF=/etc/runout

if [[ $EUID -ne 0 ]]; then
  echo "run me with sudo" >&2
  exit 1
fi

echo "==> installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip alsa-utils git >/dev/null

echo "==> creating the runout user"
if ! id runout >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/runout --create-home \
          --shell /usr/sbin/nologin --groups audio runout
fi

echo "==> installing runout into $PREFIX"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/../pyproject.toml" ]]; then
  "$PREFIX/venv/bin/pip" install -q "$SCRIPT_DIR/.."
  SRC="$SCRIPT_DIR/.."
else
  "$PREFIX/venv/bin/pip" install -q "git+$REPO"
  SRC="$(mktemp -d)"
  git clone -q --depth 1 "$REPO" "$SRC"
fi
ln -sf "$PREFIX/venv/bin/runout" /usr/local/bin/runout

echo "==> config in $CONF"
mkdir -p "$CONF"
[[ -f "$CONF/config.toml" ]] || cp "$SRC/config.example.toml" "$CONF/config.toml"
if [[ ! -f "$CONF/env" ]]; then
  cp "$SRC/deploy/env.example" "$CONF/env"
fi
chown -R root:runout "$CONF"
chmod 640 "$CONF/config.toml"
chmod 640 "$CONF/env"

echo "==> systemd service"
cp "$SRC/deploy/runout.service" /etc/systemd/system/runout.service
systemctl daemon-reload
systemctl enable runout >/dev/null

cat <<EOF

runout is installed but not started yet.

  1. Edit $CONF/config.toml  (trigger, mic, targets)
  2. Put secrets in $CONF/env
  3. Test:   sudo -u runout runout -c $CONF/config.toml check
  4. Start:  sudo systemctl start runout
  5. Watch:  journalctl -u runout -f
EOF
