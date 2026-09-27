# Clima Quarto — Raspberry Pi

Porte do projeto Arduino Mega 2560 (DHT22 + display ST7789 TFT + Sinric Pro + servidor web).

## Ligações

| Componente | Pino do Raspberry (BCM) | Pino físico | Observação |
|---|---|---|---|
| DHT22 VCC | GPIO 22 | pino 15 | alimentado pelo GPIO (3,3 V) para o programa religar o sensor quando ele trava; veja abaixo |
| DHT22 DATA | GPIO 4 | pino 7 | resistor de 10 kΩ entre DATA e 3V3 |
| DHT22 GND | GND | pino 6 | |
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


### Religação automática do DHT22

O DHT22 às vezes trava e para de responder (no `dmesg` aparece
`dht11 dht11@4: Only 0 signal edges detected` a cada leitura) até perder a
alimentação. Por isso o VCC dele fica num GPIO (`dht_vcc_gpio` no
`config.json`, padrão do exemplo: 22): depois de 30s de falhas seguidas o
programa desliga o sensor por 3s e liga de novo, e repete a cada 1min se
ele continuar sem responder. No log aparece `SENSOR: religado`.

O sensor consome menos de 2,5 mA, bem dentro do que um GPIO fornece. Para
alimentar pelo 3V3 fixo (sem religação), ligue o VCC no pino 1 e use
`"dht_vcc_gpio": null`.

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

No final, ele pergunta se você quer configurar HTTPS com Nginx + Let's
Encrypt. Responda **não**: o acesso externo é feito pelo Cloudflare Tunnel
(veja [Acesso remoto](#acesso-remoto-cloudflare-tunnel)), que não precisa
de Nginx nem de portas abertas no roteador.

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

## Acesso remoto (Cloudflare Tunnel)

O painel escuta só em `127.0.0.1:8080` - não é acessível pela rede
local nem pela internet diretamente. O acesso externo passa pelo
Cloudflare Tunnel (`cloudflared`, rodando como serviço neste Raspberry Pi)
e é protegido pelo Cloudflare Access (login antes de chegar ao painel):

| | |
|---|---|
| Domínio | `https://clima.quarto.rtavares.net/` |
| Rota no túnel (Public Hostname) | `clima.quarto.rtavares.net` → `HTTP` `localhost:8080` |
| Autenticação | aplicação no Cloudflare Access (Zero Trust → Access → Applications) |
| Certificado | Advanced Certificate Manager (subdomínio de dois níveis não é coberto pelo Universal SSL) |

Nenhuma porta precisa ficar aberta no roteador. Para voltar a liberar o
painel na rede local, troque `host_web` no `config.json` para `0.0.0.0` e reinicie o serviço.

## Endereços

No próprio Pi, em `http://127.0.0.1:8080`, ou de fora em
`https://clima.quarto.rtavares.net`:

| Caminho | Conteúdo |
|---|---|
| `/` | painel com temperatura e umidade |
| `/logs` | console remoto ao vivo (WebSocket) |
| `/sensor` | JSON com tudo |
| `/backlight` | página interativa para controlar o brilho do display |

## Testar sem hardware

```bash
./venv/bin/python sensor_pi.py --simular
```

Roda num PC comum, com sensor falso e sem display.

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
Sinric com os mesmos valores; e o envelope JSON do Sinric, com
assinatura HMAC-SHA256 idêntica.
