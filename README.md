# Monitor DHT22 — Raspberry Pi

Porte do projeto Arduino Mega 2560 (DHT22 + display ST7789 TFT + Sinric Pro + servidor web).

## Ligações

| Componente | Pino do Raspberry (BCM) | Pino físico | Observação |
|---|---|---|---|
| DHT22 VCC | 3V3 | pino 1 | **3,3 V, não 5 V** |
| DHT22 DATA | GPIO 4 | pino 7 | resistor de 10 kΩ entre DATA e 3V3 |
| DHT22 GND | GND | pino 6 | |
| Botão | GPIO 17 | pino 11 | outra ponta no GND, usa pull-up interno |
| Display VCC | 3V3 | pino 17 | |
| Display GND | GND | pino 9 | |
| Display SDA (MOSI) | GPIO 10 | pino 19 | biblioteca `st7789` usa nome SPI0 MOSI |
| Display SCL (SCLK) | GPIO 11 | pino 23 | biblioteca `st7789` usa nome SPI0 SCLK |
| Display RST | GPIO 24 | pino 18 | |
| Display DC | GPIO 25 | pino 22 | |
| Display CS | GPIO 8 | pino 24 | SPI0 CE0 |
| Display BL (backlight) | GPIO 18 | pino 12 | PWM via `gpiozero`, controla o brilho |

O display é um TFT SPI 240x240 baseado no controlador ST7789 (rotulado
"SPI-ST7789" na placa, 8 pinos: GND, VCC, SCL, SDA, RST, DC, CS, BL).

## Instalação automática

```bash
git clone https://github.com/rtavares-g/sensor-pi.git ~/sensor-pi
cd ~/sensor-pi
./install.sh
```

O `install.sh` instala as dependências do sistema, habilita I2C/SPI/DHT22,
cria `config.json` a partir do exemplo e pede no terminal o **Device ID**,
**App Key** e **App Secret** do Sinric Pro (nada de editar arquivo à mão -
se deixar algum campo em branco, ele avisa para completar depois com
`nano config.json`), monta o venv e instala o serviço. Se for a primeira
vez habilitando I2C/SPI/DHT22, ele avisa para reiniciar (`sudo reboot`) e,
na volta, basta rodar `sudo systemctl enable --now sensor-quarto` (as
interfaces já ficam habilitadas, então rodar `./install.sh` de novo também
funciona).

No final, o script também pergunta se você quer configurar HTTPS com
Nginx + Let's Encrypt agora (opcional). Se responder que sim, ele pede o
domínio e o e-mail para o Certbot e configura tudo sozinho - veja os
pré-requisitos (domínio e portas 80/443) na seção
[HTTPS com Nginx + Let's Encrypt](#https-com-nginx--lets-encrypt-opcional)
abaixo.

## Instalação manual (passo a passo)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip python3-lgpio

# habilita o I2C (usado só se quiser voltar pro LCD), o SPI (display) e o
# driver do DHT22 no kernel
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
echo 'dtoverlay=dht11,gpiopin=4' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

Após reiniciar, confirme:

```bash
ls /dev/spidev*                                            # deve mostrar spidev0.0 e spidev0.1
cat /sys/bus/iio/devices/iio:device0/in_temp_input          # temperatura em milésimos de grau (pode ser device1, device2...)
```

Depois:

```bash
mkdir -p ~/sensor-pi && cd ~/sensor-pi
# copie sensor_pi.py, config.json (baseado no config.example.json),
# requirements.txt e sensor-quarto.service para cá

cp config.example.json config.json
nano config.json             # preencha device_id / app_key / app_secret do Sinric Pro
chmod 600 config.json        # o arquivo guarda as chaves do Sinric

# venv com acesso aos pacotes do sistema (necessário para o python3-lgpio)
python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt
```

Teste:

```bash
./venv/bin/python sensor_pi.py
```

Deve aparecer no console `DISPLAY: ST7789 240x240 pronto`, `DISPLAY: backlight no GPIO 18`
e a tela deve acender com a tela de boot, seguida da tela de temperatura/umidade.

## Serviço automático

```bash
sudo cp sensor-quarto.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sensor-quarto
systemctl status sensor-quarto
journalctl -u sensor-quarto -f
```

## Endereços

| Caminho | Conteúdo |
|---|---|
| `/` | painel com temperatura e umidade |
| `/logs` | console remoto ao vivo (WebSocket) |
| `/sensor` | JSON com tudo |
| `/backlight` | página interativa para controlar o brilho do display |

## HTTPS com Nginx + Let's Encrypt (opcional)

Se quiser acessar o painel por fora da rede local com um domínio próprio e
certificado válido (em vez de `http://192.168.x.x:8080`), coloque um Nginx
na frente da aplicação como proxy reverso.

### 1. Pré-requisitos

- Um domínio (ou subdomínio) apontando para o IP público da sua rede
  (registro DNS tipo `A`).
- Porta **80** e **443** redirecionadas no roteador para o IP local do
  Raspberry Pi (port forwarding).

### 2. Instalar Nginx e Certbot

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 3. Configurar o proxy reverso

Substitua `seu-dominio.exemplo.com` pelo seu domínio real:

```bash
sudo nano /etc/nginx/sites-available/sensor-quarto
```

```nginx
server {
    listen 80;
    server_name seu-dominio.exemplo.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # obrigatório para o /logs (console remoto via WebSocket)
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

Sem os três headers de WebSocket (`Upgrade`, `Connection`, `proxy_read_timeout`),
o `/logs` fica preso em "desconectado, tentando de novo..." — o Nginx não
repassa o handshake de upgrade do WebSocket por padrão.

Ative o site e teste a configuração:

```bash
sudo ln -s /etc/nginx/sites-available/sensor-quarto /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Confirme que `http://seu-dominio.exemplo.com` já abre o painel (ainda sem
HTTPS) antes de seguir pro próximo passo.

### 4. Emitir o certificado com Certbot

```bash
sudo certbot --nginx -d seu-dominio.exemplo.com
```

O Certbot pergunta um e-mail (para avisos de expiração) e se quer
redirecionar HTTP para HTTPS automaticamente — responda que sim. Ele edita
o bloco do Nginx sozinho, adicionando `listen 443 ssl` e os caminhos do
certificado.

### 5. Renovação automática

O pacote `certbot` já instala um timer systemd que renova sozinho antes do
vencimento (certificados Let's Encrypt duram 90 dias). Confirme que está
ativo:

```bash
systemctl status certbot.timer
sudo certbot renew --dry-run     # simula a renovação sem gerar certificado novo
```

### 6. Testar

```bash
curl -I https://seu-dominio.exemplo.com/
```

Deve responder `200 OK` com um certificado válido. O `/logs` (console
remoto) deve conectar normalmente via `wss://` também.

## Testar sem hardware

```bash
./venv/bin/python sensor_pi.py --simular
```

Roda num PC comum, com sensor falso e sem display nem botão.

## Notas de hardware / problemas conhecidos

- **`gpiozero` sem PWM / "PWM is not supported on pin GPIO18"**: falta o
  `python3-lgpio` do sistema, ou o venv foi criado sem `--system-site-packages`.
  Reinstale o venv como no passo de instalação acima.
- **`[Errno 2] No such file or directory` ao iniciar o display**: o parâmetro
  `cs` da biblioteca `st7789` é o índice do chip-select do spidev (0 ou 1),
  não o número do GPIO — por isso o código usa `cs=0` para CE0 (GPIO 8), não
  `cs=8`.
- **Tela acende mas fica preta**: a biblioteca `st7789` (pimoroni, v1.0.1)
  não pulsa o pino RST durante a construção do objeto — sem chamar
  `display.reset()` manualmente, o chip fica preso em reset de hardware e
  nunca processa nenhum comando. O código já faz isso logo após criar o
  objeto `ST7789`.
- **Sensor não atualiza, tela trava na tela de boot**: o driver de kernel
  (overlay `dht11`) expõe os arquivos `in_temp_input` e
  `in_humidityrelative_input` em `/sys/bus/iio/devices/iio:deviceN/` — não
  `in_temp_raw`/`in_humidity_raw`, e o número do device pode não ser sempre
  0. O código busca automaticamente o device certo.
- **Logs não aparecem em `journalctl -u sensor-quarto -f`**: o Python
  precisa rodar com `PYTHONUNBUFFERED=1` (já configurado no
  `sensor-quarto.service`) para não segurar a saída em buffer quando não
  há terminal interativo.

## O que mudou em relação ao Arduino

| Antes | Agora |
|---|---|
| ~400 linhas de DHCP, link e recuperação de IP | o sistema operacional cuida |
| SHA-1 e WebSocket escritos à mão | `aiohttp` e `websockets` |
| 1 espectador de logs (limite de sockets do W5100) | quantos quiserem |
| 16 linhas de histórico (RAM de 8 KB) | 200 linhas |
| Regravar a placa para mudar uma chave | editar `config.json` e reiniciar o serviço |
| LCD I2C 16x2 | display TFT SPI 240x240 colorido |

O que **não** mudou: o ciclo único que atualiza sensor, display, console e
Sinric com os mesmos valores; o botão; e o envelope JSON do Sinric, com
assinatura HMAC-SHA256 idêntica.
