#!/bin/bash
# Instala/reinstala o monitor de temperatura e umidade (DHT22 + display
# ST7789 + Sinric Pro).
# Uso: ./install.sh   (rodar de dentro da pasta clonada do repositorio)

set -e

REPO_URL="https://github.com/rtavares-g/sensor-pi.git"
INSTALL_DIR="$HOME/sensor-pi"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    if [ ! -d "$INSTALL_DIR" ]; then
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    cd "$INSTALL_DIR"
else
    cd "$SCRIPT_DIR"
fi

sudo apt update
sudo apt install -y python3-venv python3-pip python3-lgpio

sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0

CONFIG_TXT="/boot/firmware/config.txt"
REBOOT_NEEDED=0
if ! grep -q "^dtoverlay=dht11,gpiopin=4" "$CONFIG_TXT"; then
    echo 'dtoverlay=dht11,gpiopin=4' | sudo tee -a "$CONFIG_TXT" > /dev/null
    REBOOT_NEEDED=1
fi

if [ ! -f config.json ]; then
    cp config.example.json config.json
    chmod 600 config.json

    echo
    echo "==> Configuracao do Sinric Pro (deixe em branco para pular e editar depois)"
    read -rp "Device ID: " SINRIC_DEVICE_ID
    read -rp "App Key: " SINRIC_APP_KEY
    read -rsp "App Secret: " SINRIC_APP_SECRET
    echo

    python3 - "$SINRIC_DEVICE_ID" "$SINRIC_APP_KEY" "$SINRIC_APP_SECRET" <<'PYEOF'
import json
import sys

device_id, app_key, app_secret = sys.argv[1:4]

with open("config.json") as f:
    cfg = json.load(f)

if device_id:
    cfg["sinric"]["device_id"] = device_id
if app_key:
    cfg["sinric"]["app_key"] = app_key
if app_secret:
    cfg["sinric"]["app_secret"] = app_secret

with open("config.json", "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF

    if [ -z "$SINRIC_DEVICE_ID" ] || [ -z "$SINRIC_APP_KEY" ] || [ -z "$SINRIC_APP_SECRET" ]; then
        echo "==> Algum campo do Sinric ficou em branco - edite depois com: nano $(pwd)/config.json"
    fi
fi

if [ ! -d venv ]; then
    python3 -m venv --system-site-packages venv
fi
./venv/bin/pip install -r requirements.txt

sudo cp sensor-quarto.service /etc/systemd/system/
sudo systemctl daemon-reload

if [ "$REBOOT_NEEDED" = "1" ]; then
    echo "==> I2C, SPI e o overlay do DHT22 foram habilitados agora."
    echo "==> Reinicie (sudo reboot) e depois rode:"
    echo "        sudo systemctl enable --now sensor-quarto"
    exit 0
fi

sudo systemctl enable --now sensor-quarto
systemctl status sensor-quarto --no-pager
