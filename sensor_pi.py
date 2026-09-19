#!/usr/bin/env python3
"""
Monitor de temperatura e umidade - Raspberry Pi com Display ST7789 TFT
Versão gráfica com Pillow + st7789
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import hashlib
import hmac
import json
import logging
import os
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import web, WSMsgType
import websockets

try:
    from PIL import Image, ImageDraw, ImageFont
    import st7789
    HAS_ST7789 = True
except ImportError:
    HAS_ST7789 = False

# =========================================================================
# CONFIGURACAO
# =========================================================================

RAIZ = Path(__file__).resolve().parent
ARQUIVO_CONFIG = Path(os.environ.get("SENSOR_CONFIG", RAIZ / "config.json"))

PADROES = {
    "dht_gpio": 4,
    "botao_gpio": 17,
    "display_tipo": "lcd",
    "porta_web": 8080,
    "lcd_colunas": 16,
    "lcd_linhas": 2,
    "lcd_endereco": "0x27",
    "st7789": {
        "mosi_gpio": 10,
        "sclk_gpio": 11,
        "cs_gpio": 8,
        "dc_gpio": 25,
        "rst_gpio": 24,
        "width": 240,
        "height": 240,
        "rotation": 0,
        "spi_speed_hz": 40000000,
    },
    "sinric": {
        "device_id": "",
        "app_key": "",
        "app_secret": "",
        "instancia_temperatura": "temperatura",
        "instancia_umidade": "umidade",
    },
}

try:
    with open(ARQUIVO_CONFIG) as f:
        CFG = {**PADROES, **json.load(f)}
except FileNotFoundError:
    CFG = PADROES
except json.JSONDecodeError as e:
    print(f"ERRO: config.json inválido - {e}")
    print(f"Delete {ARQUIVO_CONFIG} ou corrija o JSON")
    sys.exit(1)

# =========================================================================
# LOGGING - Console remoto
# =========================================================================

class Estado:
    """Estado compartilhado da aplicação."""
    def __init__(self) -> None:
        self.temperatura = None
        self.umidade = None
        self.sensor_ok = False
        self.falhas_sensor = 0
        self.sinric_ok = False

estado = Estado()
display = None  # Será inicializado em principal()

class Console:
    """Redireciona print() para log e WebSocket."""
    def __init__(self) -> None:
        self.linhas = deque(maxlen=200)
        self.clientes = set()
        self.original_stdout = sys.stdout

    def write(self, msg: str) -> None:
        if msg.strip():
            hora = datetime.now().strftime("%H:%M:%S")
            linha = f"[{hora}] {msg.strip()}"
            self.linhas.append(linha)
            self.original_stdout.write(linha + "\n")
            self.original_stdout.flush()
            asyncio.create_task(self._broadcast(linha))

    def flush(self) -> None:
        self.original_stdout.flush()

    async def _broadcast(self, msg: str) -> None:
        mortos = set()
        for ws in self.clientes:
            try:
                await ws.send_str(msg + "\n")
            except Exception:
                mortos.add(ws)
        self.clientes -= mortos

    def historico(self, n: int = 50) -> list[str]:
        return list(self.linhas)[-n:]

console = Console()
sys.stdout = console

def log(msg: str) -> None:
    print(msg)

# =========================================================================
# DISPLAY ST7789 COM PILLOW
# =========================================================================

class DisplayST7789:
    """Display gráfico ST7789 com Pillow."""

    def __init__(self, simular: bool) -> None:
        self.simular = simular
        self.display = None
        self.imagem = None
        self.draw = None
        self.fonte_grande = None
        self.fonte_media = None
        self.fonte_pequena = None
        self.historico_temp = deque(maxlen=60)
        self.historico_umid = deque(maxlen=60)
        self.alerta_ate = 0.0
        self.backlight = None

        if simular:
            log("DISPLAY: modo simulado")
            return

        if not HAS_ST7789:
            log("DISPLAY: st7789 não instalado")
            return

        try:
            cfg = CFG["st7789"]
            # cs aqui é o indice do chip-select do spidev (0=CE0, 1=CE1),
            # nao o numero do GPIO. GPIO 8 = CE0 -> cs=0; GPIO 7 = CE1 -> cs=1.
            cs_indice = 0 if cfg["cs_gpio"] == 8 else 1
            self.display = st7789.ST7789(
                height=cfg["height"],
                width=cfg["width"],
                rotation=cfg["rotation"],
                port=0,
                cs=cs_indice,
                dc=cfg["dc_gpio"],
                backlight=None,
                spi_speed_hz=cfg["spi_speed_hz"],
                rst=cfg["rst_gpio"],
            )

            # A biblioteca nao pulsa o RST na construcao - sem isso o chip
            # fica preso em reset de hardware e nunca mostra nada na tela.
            self.display.reset()
            self.display._init()

            # Backlight controlado por GPIO próprio (PWM via gpiozero)
            bl_gpio = cfg.get("bl_gpio")
            if bl_gpio is not None:
                try:
                    from gpiozero import PWMLED
                    self.backlight = PWMLED(bl_gpio)
                    self.backlight.value = 1.0
                    log(f"DISPLAY: backlight no GPIO {bl_gpio}")
                except Exception as erro_bl:
                    log(f"DISPLAY: erro ao iniciar backlight ({erro_bl})")

            # Criar imagem PIL
            self.imagem = Image.new("RGB", (cfg["width"], cfg["height"]), color=(0, 0, 0))
            self.draw = ImageDraw.Draw(self.imagem)

            # Carregar fontes (usar default se não tiver)
            try:
                self.fonte_grande = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
                self.fonte_valor = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
                self.fonte_media = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
                self.fonte_pequena = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
            except:
                self.fonte_grande = ImageFont.load_default()
                self.fonte_valor = ImageFont.load_default()
                self.fonte_media = ImageFont.load_default()
                self.fonte_pequena = ImageFont.load_default()

            log(f"DISPLAY: ST7789 {cfg['width']}x{cfg['height']} pronto")
            self.desenhar_boot()

        except Exception as erro:
            log(f"DISPLAY: erro ao inicializar ({erro})")
            self.display = None

    def desenhar_boot(self) -> None:
        """Tela de inicialização."""
        if not self.display:
            return

        self.imagem = Image.new("RGB", (240, 240), color=(0, 20, 60))
        self.draw = ImageDraw.Draw(self.imagem)

        # Título
        self.draw.text((120, 50), "Iniciando...", fill=(0, 200, 255), font=self.fonte_grande, anchor="mm")

        # Subtítulo
        self.draw.text((120, 150), "Monitor DHT22", fill=(100, 150, 255), font=self.fonte_media, anchor="mm")

        # Versão
        self.draw.text((120, 210), "v1.0 ST7789", fill=(80, 80, 80), font=self.fonte_pequena, anchor="mm")

        self._atualizar()

    def _icone_termometro(self, cx: int, cy: int, altura: int, cor: tuple) -> None:
        """Desenha um termômetro simples. (cx, cy) = centro do bulbo."""
        raio_bulbo = 13
        largura_tubo = 12
        y_topo = cy - altura

        self.draw.rounded_rectangle(
            [cx - largura_tubo // 2, y_topo, cx + largura_tubo // 2, cy],
            radius=largura_tubo // 2,
            outline=cor,
            width=3,
        )
        self.draw.rounded_rectangle(
            [cx - largura_tubo // 2 + 4, y_topo + altura // 3, cx + largura_tubo // 2 - 4, cy],
            radius=(largura_tubo // 2) - 4,
            fill=cor,
        )
        self.draw.ellipse(
            [cx - raio_bulbo, cy - raio_bulbo, cx + raio_bulbo, cy + raio_bulbo],
            fill=cor,
        )

    def _icone_gota(self, cx: int, cy: int, raio: float, cor: tuple) -> None:
        """Desenha uma gota d'água. (cx, cy) = centro aproximado."""
        self.draw.ellipse(
            [cx - raio, cy - raio * 0.3, cx + raio, cy + raio * 1.3],
            fill=cor,
        )
        self.draw.polygon(
            [
                (cx, cy - raio * 1.8),
                (cx - raio * 0.85, cy - raio * 0.15),
                (cx + raio * 0.85, cy - raio * 0.15),
            ],
            fill=cor,
        )

    def desenhar_sensor(self, temp: float, umid: float, sinric_ok: bool, sensor_ok: bool) -> None:
        """Desenha tela com temperatura e umidade."""
        if not self.display:
            return

        # Fundo
        cor_bg = (10, 30, 60) if sensor_ok else (60, 20, 20)
        self.imagem = Image.new("RGB", (240, 240), color=cor_bg)
        self.draw = ImageDraw.Draw(self.imagem)

        # Cores baseadas no status
        cor_temp = (255, 100, 50) if temp > 30 else (100, 200, 255) if temp < 15 else (100, 255, 100)
        cor_umid = (100, 180, 255)

        # --- Temperatura ---
        self._icone_termometro(cx=45, cy=95, altura=55, cor=cor_temp)
        self.draw.text((88, 48), "Temperatura", fill=(160, 160, 185), font=self.fonte_pequena, anchor="lm")
        self.draw.text((88, 82), f"{temp:.1f}°C", fill=cor_temp, font=self.fonte_valor, anchor="lm")

        # Linha divisória
        self.draw.line([(15, 122), (225, 122)], fill=(50, 60, 95), width=1)

        # --- Umidade ---
        self._icone_gota(cx=45, cy=168, raio=15, cor=cor_umid)
        self.draw.text((88, 148), "Umidade", fill=(160, 160, 185), font=self.fonte_pequena, anchor="lm")
        self.draw.text((88, 182), f"{umid:.1f}%", fill=cor_umid, font=self.fonte_valor, anchor="lm")

        # --- Status no rodapé ---
        status_sensor = "✓ OK" if sensor_ok else "✗ FALHA"
        status_sinric = "☁ ON" if sinric_ok else "☁ OFF"

        self.draw.text((12, 225), status_sensor, fill=(0, 255, 100) if sensor_ok else (255, 50, 50), font=self.fonte_pequena, anchor="lm")
        self.draw.text((228, 225), status_sinric, fill=(0, 200, 255) if sinric_ok else (150, 50, 50), font=self.fonte_pequena, anchor="rm")

        self._atualizar()

    def _atualizar(self) -> None:
        """Envia imagem para o display."""
        if self.display and self.imagem:
            try:
                self.display.display(self.imagem)
            except Exception as e:
                log(f"DISPLAY: erro ao atualizar ({e})")

    def passo(self, l0: str, l1: str) -> None:
        """Compatibilidade com interface antiga."""
        if not self.display:
            return

        self.imagem = Image.new("RGB", (240, 240), color=(20, 20, 40))
        self.draw = ImageDraw.Draw(self.imagem)

        self.draw.text((120, 80), l0, fill=(100, 200, 255), font=self.fonte_media, anchor="mm")
        self.draw.text((120, 140), l1, fill=(150, 150, 255), font=self.fonte_pequena, anchor="mm")

        self._atualizar()
        self.alerta_ate = 0.0

    def duas_linhas(self, l0: str, l1: str) -> None:
        """Compatibilidade com interface antiga."""
        self.passo(l0, l1)

    def alerta(self, l0: str, l1: str, duracao: float = 3.0) -> None:
        """Mostra alerta temporário."""
        self.duas_linhas(l0, l1)
        self.alerta_ate = time.monotonic() + duracao

    @property
    def ocupado(self) -> bool:
        return time.monotonic() < self.alerta_ate

    def liberar(self) -> None:
        self.alerta_ate = 0.0

    def set_backlight(self, nivel: float) -> None:
        """Controla brilho do backlight via PWM (GPIO dedicado)."""
        nivel = max(0.0, min(1.0, nivel))
        if self.backlight is not None:
            self.backlight.value = nivel
        log(f"DISPLAY: backlight {nivel:.0%}")

    def backlight_off(self) -> None:
        self.set_backlight(0.0)

    def backlight_on(self, nivel: float = 1.0) -> None:
        self.set_backlight(nivel)


# =========================================================================
# SENSOR DHT22
# =========================================================================

class LeitorIIO:
    """Lê sensor via kernel IIO (melhor método)."""
    def __init__(self) -> None:
        caminho = self._procurar()
        if caminho:
            log(f"SENSOR: dispositivo IIO em {caminho}")
        else:
            log("SENSOR: nenhum dispositivo IIO encontrado (in_temp_input)")
        self.temp = Path(caminho) / "in_temp_input" if caminho else None
        self.umid = Path(caminho) / "in_humidityrelative_input" if caminho else None

    @staticmethod
    def _procurar() -> str | None:
        for d in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
            if (Path(d) / "in_temp_input").exists() and \
               (Path(d) / "in_humidityrelative_input").exists():
                return d
        return None

    def ler(self) -> tuple[float, float] | None:
        if not self.temp or not self.umid:
            return None
        try:
            temp = int(self.temp.read_text().strip()) / 1000.0
            umid = int(self.umid.read_text().strip()) / 1000.0
            return temp, umid
        except Exception:
            return None

class LeitorSimulado:
    """Simula sensor para testes."""
    def __init__(self):
        self.contador = 0

    def ler(self) -> tuple[float, float]:
        self.contador += 1
        temp = 22.0 + 3.0 * (self.contador % 20 - 10) / 10
        umid = 60.0 + 10.0 * ((self.contador + 5) % 20 - 10) / 10
        return temp, umid

def montar_leitor(simular: bool):
    """Detecta qual leitor usar."""
    if simular:
        return LeitorSimulado()

    return LeitorIIO()

# =========================================================================
# SINRIC PRO (Já implementado como antes)
# =========================================================================

class Sinric:
    """Conexão WebSocket para Sinric Pro (protocolo Range)."""
    def __init__(self) -> None:
        self.ws = None
        self.conectado = False
        self.prox_reconectar = 0.0
        self.timestamp_base = 0
        self.timestamp_em = 0.0

    @property
    def configurado(self) -> bool:
        return bool(CFG["sinric"]["device_id"] and CFG["sinric"]["app_key"])

    def agora(self) -> int:
        if self.timestamp_base:
            return int(self.timestamp_base + (time.monotonic() - self.timestamp_em))
        return int(time.time())

    def assinar(self, payload: str) -> str:
        digest = hmac.new(
            CFG["sinric"]["app_secret"].encode(),
            payload.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode()

    def montar(self, instancia: str, valor: float, ts: int) -> str:
        payload = (
            '{"action":"setRangeValue",'
            '"cause":{"type":"PERIODIC_POLL"},'
            f'"createdAt":{ts},'
            f'"deviceId":"{CFG["sinric"]["device_id"]}",'
            f'"instanceId":"{instancia}",'
            '"replyToken":"raspberry",'
            '"type":"event",'
            f'"value":{{"rangeValue":{valor:.1f}}}}}'
        )
        return (
            '{"header":{"payloadVersion":2,"signatureVersion":1},'
            f'"payload":{payload},'
            f'"signature":{{"HMAC":"{self.assinar(payload)}"}}}}'
        )

    def _cabecalhos(self) -> dict[str, str]:
        mac = "00:00:00:00:00:00"
        try:
            import glob
            for caminho in sorted(glob.glob("/sys/class/net/*/address")):
                if "/lo/" not in caminho:
                    mac = Path(caminho).read_text().strip()
                    break
        except Exception:
            pass
        return {
            "appkey": CFG["sinric"]["app_key"],
            "deviceids": CFG["sinric"]["device_id"],
            "restoredevicestates": "false",
            "ip": socket.gethostbyname(socket.gethostname()),
            "mac": mac,
            "platform": "RaspberryPi",
            "SDKVersion": "Py-Range-1.0",
        }

    async def _abrir(self):
        url = f"wss://ws.sinric.pro/"
        cabecalhos = self._cabecalhos()
        try:
            return await websockets.connect(
                url, additional_headers=cabecalhos,
                subprotocols=["arduino"], ping_interval=30, ping_timeout=20,
            )
        except TypeError:
            return await websockets.connect(
                url, extra_headers=cabecalhos,
                subprotocols=["arduino"], ping_interval=30, ping_timeout=20,
            )

    async def tarefa(self) -> None:
        if not self.configurado:
            log("SINRIC: credenciais não configuradas")
            return

        while True:
            try:
                log("SINRIC: conectando...")
                async with await self._abrir() as ws:
                    self.ws = ws
                    self.conectado = True
                    estado.sinric_ok = True
                    log("SINRIC: conectado")

                    async for bruto in ws:
                        try:
                            msg = json.loads(bruto)
                            ts = msg.get("timestamp")
                            if ts:
                                self.timestamp_base = int(ts)
                                self.timestamp_em = time.monotonic()
                        except Exception:
                            continue

            except Exception as erro:
                log(f"SINRIC: desconectado ({type(erro).__name__})")

            self.ws = None
            self.conectado = False
            estado.sinric_ok = False
            await asyncio.sleep(10)

    async def enviar_leitura(self, temp: float, umid: float) -> bool:
        if not self.conectado or not self.ws:
            return False

        ts = self.agora()
        try:
            await self.ws.send(self.montar(CFG["sinric"]["instancia_temperatura"], temp, ts))
            await self.ws.send(self.montar(CFG["sinric"]["instancia_umidade"], umid, ts))
            return True
        except Exception as erro:
            self.conectado = False
            estado.sinric_ok = False
            log(f"SINRIC: falha ao enviar ({erro})")
            return False

sinric = Sinric()

# =========================================================================
# HTTP ROUTES
# =========================================================================

async def rota_painel(_: web.Request) -> web.Response:
    html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Monitor DHT22</title>
<style>
body {{ font-family: sans-serif; margin: 20px; background: #0a1e3c; color: #fff; }}
.container {{ max-width: 600px; margin: 0 auto; }}
h1 {{ color: #64c8ff; }}
.stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 20px 0; }}
.stat {{ background: #1a3a5c; padding: 20px; border-radius: 10px; text-align: center; }}
.stat-value {{ font-size: 2em; color: #64c8ff; font-weight: bold; }}
.stat-label {{ font-size: 0.9em; color: #999; margin-top: 5px; }}
button {{ background: #0066cc; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin: 5px; }}
button:hover {{ background: #0052a3; }}
.links {{ margin-top: 20px; }}
a {{ color: #64c8ff; text-decoration: none; margin-right: 20px; }}
a:hover {{ text-decoration: underline; }}
</style>
</head><body>
<div class='container'>
<h1>🌡️ Monitor DHT22</h1>
<div class='stats'>
<div class='stat'>
<div class='stat-value' id='temp'>--</div>
<div class='stat-label'>Temperatura (°C)</div>
</div>
<div class='stat'>
<div class='stat-value' id='umid'>--</div>
<div class='stat-label'>Umidade (%)</div>
</div>
</div>
<div class='links'>
<a href='/sensor'>📊 JSON</a>
<a href='/backlight'>💡 Backlight</a>
<a href='/logs'>📝 Logs</a>
</div>
</div>
<script>
async function atualizar() {{
    const r = await fetch('/sensor');
    const d = await r.json();
    document.getElementById('temp').textContent = (d.temperature || '--').toFixed(1);
    document.getElementById('umid').textContent = (d.humidity || '--').toFixed(1);
}}
atualizar();
setInterval(atualizar, 5000);
</script>
</body></html>"""
    return web.Response(text=html, content_type="text/html")

async def rota_sensor(_: web.Request) -> web.Response:
    return web.json_response({
        "temperature": estado.temperatura,
        "humidity": estado.umidade,
        "sensorOk": estado.sensor_ok,
        "sinric": estado.sinric_ok,
    })

async def rota_backlight(request: web.Request) -> web.Response:
    global display
    if not request.query_string:
        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Controle de Backlight</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 400px;
            width: 100%;
        }
        h1 {
            text-align: center;
            color: #333;
            margin-bottom: 30px;
            font-size: 24px;
        }
        .display {
            background: #f5f5f5;
            border-radius: 10px;
            padding: 20px;
            text-align: center;
            margin-bottom: 30px;
        }
        .valor {
            font-size: 48px;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 10px;
        }
        .label {
            color: #666;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .slider-container {
            margin-bottom: 30px;
        }
        input[type="range"] {
            width: 100%;
            height: 8px;
            border-radius: 5px;
            background: #ddd;
            outline: none;
            -webkit-appearance: none;
            cursor: pointer;
        }
        input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            background: #667eea;
            cursor: pointer;
            box-shadow: 0 2px 8px rgba(102, 126, 234, 0.4);
        }
        input[type="range"]::-moz-range-thumb {
            width: 24px;
            height: 24px;
            border-radius: 50%;
            background: #667eea;
            cursor: pointer;
            border: none;
            box-shadow: 0 2px 8px rgba(102, 126, 234, 0.4);
        }
        .botoes {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        button {
            flex: 1;
            padding: 12px;
            border: none;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .btn-on {
            background: #4CAF50;
            color: white;
        }
        .btn-on:hover {
            background: #45a049;
            box-shadow: 0 4px 12px rgba(76, 175, 80, 0.3);
        }
        .btn-off {
            background: #f44336;
            color: white;
        }
        .btn-off:hover {
            background: #da190b;
            box-shadow: 0 4px 12px rgba(244, 67, 54, 0.3);
        }
        .presets {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
        .preset {
            padding: 10px;
            border: 2px solid #eee;
            border-radius: 10px;
            background: white;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.2s;
            text-align: center;
        }
        .preset:hover {
            border-color: #667eea;
            background: #f0f4ff;
            color: #667eea;
        }
        .status {
            text-align: center;
            color: #999;
            font-size: 12px;
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>💡 Backlight</h1>

        <div class="display">
            <div class="valor" id="valor">100%</div>
            <div class="label">Brilho</div>
        </div>

        <div class="botoes">
            <button class="btn-on" onclick="ligar()">Ligar</button>
            <button class="btn-off" onclick="desligar()">Desligar</button>
        </div>

        <div class="slider-container">
            <input type="range" id="slider" min="0" max="100" value="100"
                   oninput="ajustar(this.value)">
        </div>

        <div class="presets">
            <div class="preset" onclick="definir(25)">25%</div>
            <div class="preset" onclick="definir(50)">50%</div>
            <div class="preset" onclick="definir(75)">75%</div>
            <div class="preset" onclick="definir(100)">100%</div>
        </div>

        <div class="status">
            <span id="status">Pronto</span>
        </div>
    </div>

    <script>
        const slider = document.getElementById('slider');
        const valor = document.getElementById('valor');
        const status = document.getElementById('status');

        function atualizarDisplay(v) {
            valor.textContent = v + '%';
            slider.value = v;
        }

        function ajustar(v) {
            atualizarDisplay(v);
            enviar(v / 100);
        }

        function definir(v) {
            ajustar(v);
        }

        function ligar() {
            enviar(1.0, '?on');
            atualizarDisplay(100);
        }

        function desligar() {
            enviar(0.0, '?off');
            atualizarDisplay(0);
        }

        function enviar(nivel, param) {
            param = param || ('?nivel=' + nivel.toFixed(2));

            fetch('/backlight' + param)
                .then(r => r.json())
                .then(d => {
                    status.textContent = '✓ Atualizado';
                    setTimeout(() => status.textContent = 'Pronto', 1500);
                    console.log('Backlight:', d);
                })
                .catch(e => {
                    status.textContent = '✗ Erro: ' + e.message;
                    console.error(e);
                });
        }
    </script>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    if "off" in request.query_string:
        display.backlight_off()
        return web.json_response({"backlight": 0.0})
    if "on" in request.query_string:
        display.backlight_on(1.0)
        return web.json_response({"backlight": 1.0})

    try:
        nivel = float(request.query.get("nivel", 1.0))
        display.set_backlight(nivel)
        return web.json_response({"backlight": max(0.0, min(1.0, nivel))})
    except ValueError:
        return web.json_response({"erro": "nivel deve ser 0.0 a 1.0"}, status=400)

async def rota_logs(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() != "websocket":
        html = f"""<html><head><title>Logs</title><style>
body {{ background: #000; color: #0f0; font-family: monospace; padding: 20px; }}
pre {{ max-height: 600px; overflow-y: auto; }}
</style></head><body>
<h1>Console Remoto</h1>
<pre>{''.join(console.historico())}</pre>
<p><a href='/'>← Voltar</a></p>
</body></html>"""
        return web.Response(text=html, content_type="text/html")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    console.clientes.add(ws)

    try:
        async for msg in ws:
            pass
    finally:
        console.clientes.discard(ws)

    return ws

# =========================================================================
# SERVIDOR
# =========================================================================

async def subir_servidor() -> web.AppRunner:
    app = web.Application()
    app.add_routes([
        web.get("/", rota_painel),
        web.get("/sensor", rota_sensor),
        web.get("/backlight", rota_backlight),
        web.get("/logs", rota_logs),
    ])

    runner = web.AppRunner(app)
    await runner.setup()

    porta = CFG.get("porta_web", 8080)
    await web.TCPSite(runner, "0.0.0.0", porta).start()
    log(f"WEB: http://192.168.1.10:{porta}/")
    log(f"WEB: http://192.168.1.10:{porta}/logs")

    return runner

# =========================================================================
# LOOP PRINCIPAL
# =========================================================================

async def ciclo(leitor, display_obj) -> None:
    prox_sinric = 0.0

    while True:
        await asyncio.sleep(5)

        try:
            leitura = leitor.ler()
            if leitura:
                estado.temperatura, estado.umidade = leitura
                estado.sensor_ok = True
                estado.falhas_sensor = 0
                display_obj.desenhar_sensor(
                    estado.temperatura,
                    estado.umidade,
                    estado.sinric_ok,
                    True
                )
            else:
                estado.falhas_sensor += 1
                if estado.falhas_sensor > 3:
                    estado.sensor_ok = False
        except Exception as e:
            estado.sensor_ok = False
            log(f"SENSOR: erro ({e})")

        # Atualizar Sinric a cada 60s
        if time.monotonic() >= prox_sinric and estado.sensor_ok:
            await sinric.enviar_leitura(estado.temperatura, estado.umidade)
            prox_sinric = time.monotonic() + 60

async def principal(simular: bool) -> None:
    global display

    display = DisplayST7789(simular)
    display.passo("Iniciando...", "Monitor DHT22")
    await asyncio.sleep(1)

    leitor = montar_leitor(simular)
    log(f"SENSOR: {'simulado' if simular else 'usando kernel (IIO)'}")

    # Obter IP
    try:
        ip = socket.gethostbyname(socket.gethostname())
        gw = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout.split()[2]
        dns = subprocess.run(["grep", "nameserver", "/etc/resolv.conf"], capture_output=True, text=True).stdout.split()[1]
        log(f"REDE: IP {ip}  gateway {gw}  DNS {dns}")
    except Exception:
        pass

    # Iniciar Sinric em background
    asyncio.create_task(sinric.tarefa())

    # Iniciar servidor HTTP
    runner = await subir_servidor()

    # Loop principal
    try:
        await ciclo(leitor, display)
    except KeyboardInterrupt:
        log("Encerrando...")
        await runner.cleanup()

# =========================================================================

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simular", action="store_true", help="Modo simulado")
    args = parser.parse_args()

    try:
        asyncio.run(principal(args.simular))
    except KeyboardInterrupt:
        log("Encerrado pelo usuário")
    except Exception as e:
        log(f"ERRO: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
