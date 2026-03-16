#!/bin/bash
# ============================================================================
# Barista Pi Setup — End-to-end install script
# Run on the Raspberry Pi after transferring the project to ~/barista
#
# Usage:  chmod +x pi-setup.sh && ./pi-setup.sh
# ============================================================================

set -euo pipefail

BLUE='\033[1;34m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
NC='\033[0m'

step=0
step() {
    step=$((step + 1))
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  Step ${step}: $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

ok()   { echo -e "  ${GREEN}✔ $1${NC}"; }
warn() { echo -e "  ${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "  ${RED}✘ $1${NC}"; }
info() { echo -e "  $1"; }

# ── Preamble ─────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        ☕  Barista Raspberry Pi Setup                       ║${NC}"
echo -e "${GREEN}║        De'Longhi ECAM BLE-to-HTTP Bridge                   ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
info "Date:     $(date)"
info "Host:     $(hostname)"
info "User:     $(whoami)"
info "OS:       $(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '"')"
info "Kernel:   $(uname -r)"
info "Arch:     $(uname -m)"

# ── Step 1: Verify project files ────────────────────────────────────────────

step "Verify project files"

PROJECT_DIR="$HOME/barista"

if [ ! -d "$PROJECT_DIR" ]; then
    fail "Project directory not found at $PROJECT_DIR"
    echo "  Transfer the project first:"
    echo "    scp -r delonghi-coffee-server asaf5767@barista.local:~/barista"
    exit 1
fi
ok "Project directory exists: $PROJECT_DIR"

for f in barista/server.py barista/ble.py barista/protocol.py barista/cli.py pyproject.toml; do
    if [ -f "$PROJECT_DIR/$f" ]; then
        ok "Found $f"
    else
        fail "Missing $f"
        exit 1
    fi
done

if [ -f "$PROJECT_DIR/barista/ui/index.html" ]; then
    ok "Found Web UI: barista/ui/index.html"
else
    warn "Web UI not found — server will work but no UI"
fi

# ── Step 2: System packages ─────────────────────────────────────────────────

step "Update system packages"

info "Running apt update..."
sudo apt update -y 2>&1 | tail -3
ok "apt update done"

info "Installing required system packages..."
sudo apt install -y python3 python3-pip python3-venv python3-dev \
    bluetooth bluez libglib2.0-dev libdbus-1-dev dbus 2>&1 | tail -5
ok "System packages installed"

# Show versions
info "Python:     $(python3 --version 2>&1)"
info "pip:        $(python3 -m pip --version 2>&1 | head -1)"
info "bluetoothd: $(bluetoothctl --version 2>&1 || echo 'not found')"

# ── Step 3: Bluetooth check ─────────────────────────────────────────────────

step "Check Bluetooth"

if systemctl is-active --quiet bluetooth; then
    ok "Bluetooth service is running"
else
    warn "Bluetooth service not running — starting it..."
    sudo systemctl start bluetooth
    sudo systemctl enable bluetooth
    if systemctl is-active --quiet bluetooth; then
        ok "Bluetooth service started"
    else
        fail "Could not start Bluetooth service"
        journalctl -u bluetooth --no-pager -n 10
        exit 1
    fi
fi

if hciconfig 2>/dev/null | grep -q "UP RUNNING"; then
    ok "Bluetooth adapter is UP"
    hciconfig | head -5 | while read -r line; do info "  $line"; done
elif command -v bluetoothctl &>/dev/null; then
    info "Checking via bluetoothctl..."
    bluetoothctl show 2>&1 | head -8 | while read -r line; do info "  $line"; done
    ok "Bluetooth adapter found"
else
    warn "Could not verify Bluetooth adapter — continuing anyway"
fi

# ── Step 4: BLE permissions ─────────────────────────────────────────────────

step "Configure BLE permissions for user '$(whoami)'"

if groups | grep -qw bluetooth; then
    ok "User already in 'bluetooth' group"
else
    info "Adding user to 'bluetooth' group..."
    sudo usermod -aG bluetooth "$(whoami)"
    ok "Added to 'bluetooth' group (takes effect on next login)"
fi

# Grant python3 cap_net_raw so bleak can scan without root
PYTHON3_PATH=$(which python3)
info "Python3 binary: $PYTHON3_PATH"

# Resolve symlinks to get the actual binary
PYTHON3_REAL=$(readlink -f "$PYTHON3_PATH")
info "Python3 real path: $PYTHON3_REAL"

info "Setting CAP_NET_RAW capability on python3..."
sudo setcap 'cap_net_raw,cap_net_admin+eip' "$PYTHON3_REAL" 2>&1 && \
    ok "BLE capabilities set on $PYTHON3_REAL" || \
    warn "Could not set capabilities — may need to run server as root"

# Verify
if getcap "$PYTHON3_REAL" 2>/dev/null | grep -q cap_net_raw; then
    ok "Verified: $(getcap "$PYTHON3_REAL")"
else
    warn "Capability not verified — BLE scanning may require sudo"
fi

# ── Step 5: Python virtual environment ──────────────────────────────────────

step "Create Python virtual environment"

VENV_DIR="$PROJECT_DIR/.venv"

if [ -d "$VENV_DIR" ]; then
    info "Existing venv found, removing for clean install..."
    rm -rf "$VENV_DIR"
fi

info "Creating venv at $VENV_DIR ..."
python3 -m venv "$VENV_DIR"
ok "Virtual environment created"

# Activate
source "$VENV_DIR/bin/activate"
info "Activated venv: $(which python3)"
info "Python: $(python3 --version)"
info "pip:    $(pip --version)"

# Set BLE capabilities on the venv python too
VENV_PYTHON_REAL=$(readlink -f "$VENV_DIR/bin/python3")
info "Venv Python real path: $VENV_PYTHON_REAL"
sudo setcap 'cap_net_raw,cap_net_admin+eip' "$VENV_PYTHON_REAL" 2>&1 && \
    ok "BLE capabilities set on venv python" || \
    warn "Could not set capabilities on venv python"

# ── Step 6: Install project ─────────────────────────────────────────────────

step "Install barista package"

cd "$PROJECT_DIR"
info "Working directory: $(pwd)"

info "Upgrading pip..."
pip install --upgrade pip 2>&1 | tail -2
ok "pip upgraded"

info "Installing barista in editable mode..."
pip install -e . 2>&1
ok "barista installed"

info "Verifying installation..."
pip show barista-coffee 2>&1 | while read -r line; do info "  $line"; done

info "Checking imports..."
python3 -c "from barista.protocol import DINAMICA_BEVERAGES; print(f'  Beverages: {len(DINAMICA_BEVERAGES)}')" && \
    ok "protocol module OK" || fail "protocol import failed"

python3 -c "from barista.ble import DelonghiBLE; print('  BLE driver loaded')" && \
    ok "ble module OK" || fail "ble import failed"

python3 -c "from barista.dbus_ecam import EcamDBusGATT; print('  D-Bus ECAM driver loaded')" && \
    ok "dbus_ecam module OK" || fail "dbus_ecam import failed"

python3 -c "from barista.server import create_app; print('  Server app factory OK')" && \
    ok "server module OK" || fail "server import failed"

# ── Step 7: Create systemd service ──────────────────────────────────────────

step "Create systemd service file"

SERVICE_FILE="/etc/systemd/system/barista.service"
VENV_PYTHON="$VENV_DIR/bin/python3"

info "Service file: $SERVICE_FILE"
info "ExecStart will use: $VENV_PYTHON"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Barista - De'Longhi Coffee Machine BLE Bridge
After=bluetooth.target network.target
Wants=bluetooth.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -m barista start --address 00:A0:50:2A:D2:8F
Restart=on-failure
RestartSec=5
Environment=PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin

# Give BLE access
AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN

[Install]
WantedBy=multi-user.target
EOF

ok "Service file written"
info "Contents:"
cat "$SERVICE_FILE" | while read -r line; do info "  $line"; done

# ── Step 8: Quick smoke test ────────────────────────────────────────────────

step "Smoke test — dry run import"

info "Running full import chain..."
python3 -c "
from barista.cli import main
from barista.server import create_app, cmd_scan, cmd_serve
from barista.ble import DelonghiBLE
from barista.dbus_ecam import EcamDBusGATT
from barista.protocol import (
    BeverageId, DINAMICA_BEVERAGES, cmd_brew, cmd_power_on,
    cmd_recipe_read, cmd_monitor, cmd_steam, cmd_hot_water,
    recipe_to_dict, recipe_from_dict, get_beverage_names,
)
print('  All imports successful')
print(f'  Beverages: {[b.name for b in DINAMICA_BEVERAGES]}')
app = create_app()
print(f'  App routes: {len(app.router.routes())}')
" && ok "Smoke test passed" || fail "Smoke test failed"

# ── Done ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅  Setup complete!                                        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Next steps — run these commands:"
echo ""
echo -e "  ${YELLOW}sudo systemctl daemon-reload${NC}"
echo -e "  ${YELLOW}sudo systemctl enable barista${NC}"
echo -e "  ${YELLOW}sudo systemctl start barista${NC}"
echo ""
echo "  Then check:"
echo ""
echo -e "  ${YELLOW}systemctl status barista${NC}"
echo -e "  ${YELLOW}journalctl -u barista -f${NC}"
echo -e "  ${YELLOW}curl http://localhost:8080/api${NC}"
echo ""
echo "  Access from any device on your network:"
echo ""
echo -e "  Web UI:  ${BLUE}http://barista.local:8080${NC}"
echo -e "  API:     ${BLUE}http://barista.local:8080/api${NC}"
echo ""
echo -e "  ☕ Brew:  ${YELLOW}curl -X POST http://barista.local:8080/api/brew -H 'Content-Type: application/json' -d '{\"beverage\":\"coffee\"}'${NC}"
echo ""
