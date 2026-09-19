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
    echo "==> config.json criado a partir do exemplo."
    echo "==> Edite config.json com as chaves do Sinric Pro antes de continuar: nano config.json"
    echo
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
