"""
gcinput-viewer server

GCコントローラの全入力をシリアル経由で受信し、
WebSocket経由でブラウザにリアルタイム可視化する。

シリアルフォーマット:
  I,BH,BL,SX,SY,CX,CY,LT,RT,CC

  BH,BL: ボタンステータス (16進2桁×2)
  SX,SY,CX,CY,LT,RT: アナログ値 (10進 0-255)
  CC: CRC-8 ATM (16進2桁, BH~RTの8バイト)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import serial
from aiohttp import web, WSMsgType


# ---------- CRC-8 (ATM) ----------
def crc8_atm(data: bytes, poly: int = 0x07, init: int = 0x00) -> int:
    crc = init & 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) & 0xFF) ^ poly
            else:
                crc = (crc << 1) & 0xFF
    return crc & 0xFF


# ---------- Latest state ----------
@dataclass
class Latest:
    bh: int = 0
    bl: int = 0
    sx: int = 128
    sy: int = 128
    cx: int = 128
    cy: int = 128
    lt: int = 0
    rt: int = 0
    updated_at: float = 0.0


# ---------- Serial line parsing ----------
LINE_RE = re.compile(
    r"^I,([0-9A-Fa-f]{2}),([0-9A-Fa-f]{2}),"
    r"(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),"
    r"([0-9A-Fa-f]{2})$"
)


def parse_data_line(line: str) -> Optional[Tuple[int, int, int, int, int, int, int, int]]:
    s = line.strip()
    m = LINE_RE.match(s)
    if not m:
        return None

    bh = int(m.group(1), 16)
    bl = int(m.group(2), 16)
    sx = int(m.group(3))
    sy = int(m.group(4))
    cx = int(m.group(5))
    cy = int(m.group(6))
    lt = int(m.group(7))
    rt = int(m.group(8))
    crc_in = int(m.group(9), 16)

    if not all(0 <= v <= 255 for v in (bh, bl, sx, sy, cx, cy, lt, rt)):
        return None

    payload = bytes([bh, bl, sx, sy, cx, cy, lt, rt])
    if crc8_atm(payload) != crc_in:
        return None

    return bh, bl, sx, sy, cx, cy, lt, rt


# ---------- Serial thread ----------
def serial_reader_thread(
    port: str,
    baud: int,
    loop: asyncio.AbstractEventLoop,
    q: asyncio.Queue,
    stop_flag: threading.Event,
) -> None:
    ser = serial.Serial(port, baudrate=baud, timeout=0.5)
    try:
        while not stop_flag.is_set():
            raw = ser.readline()
            if not raw:
                continue

            line = raw.decode("utf-8", errors="ignore")
            parsed = parse_data_line(line)
            if parsed is None:
                continue

            def _put_nowait(data=parsed):
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

            loop.call_soon_threadsafe(_put_nowait)
    finally:
        try:
            ser.close()
        except Exception:
            pass


# ---------- HTML overlay ----------
def build_overlay_html() -> str:
    return r"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<style>
body { margin:0; background:black; overflow:hidden; }
canvas { display:block; }
</style>
</head>
<body>
<canvas id="c" width="860" height="220"></canvas>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const W = 860, H = 220;

// Button bit positions
const BTN = {
  A:     0,  // BL bit0
  B:     1,  // BL bit1
  X:     2,  // BL bit2
  Y:     3,  // BL bit3
  START: 4,  // BL bit4
  DLEFT: 8,  // BH bit0
  DRIGHT:9,  // BH bit1
  DDOWN: 10, // BH bit2
  DUP:   11, // BH bit3
  Z:     12, // BH bit4
  R:     13, // BH bit5
  L:     14, // BH bit6
};

function isPressed(buttons, bit) {
  return (buttons >> bit) & 1;
}

// Draw octagon gate
function drawOctagon(cx, cy, r) {
  ctx.beginPath();
  for (let i = 0; i < 8; i++) {
    const angle = (Math.PI / 8) + (i * Math.PI / 4);
    const x = cx + r * Math.cos(angle);
    const y = cy - r * Math.sin(angle);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
}

// Draw stick (octagon gate + crosshair + dot)
function drawStick(cx, cy, radius, valX, valY) {
  // Octagon gate
  ctx.strokeStyle = '#666';
  ctx.lineWidth = 1.5;
  drawOctagon(cx, cy, radius);
  ctx.stroke();

  // Crosshair
  ctx.strokeStyle = '#333';
  ctx.lineWidth = 0.5;
  ctx.beginPath();
  ctx.moveTo(cx - radius, cy); ctx.lineTo(cx + radius, cy);
  ctx.moveTo(cx, cy - radius); ctx.lineTo(cx, cy + radius);
  ctx.stroke();

  // Current position dot
  const dx = ((valX - 128) / 128) * radius;
  const dy = -((valY - 128) / 128) * radius;
  ctx.fillStyle = '#fff';
  ctx.beginPath();
  ctx.arc(cx + dx, cy + dy, 5, 0, Math.PI * 2);
  ctx.fill();
}

// Draw trigger slider
function drawTrigger(x, y, w, h, value, digitalPressed) {
  // Background
  ctx.fillStyle = '#222';
  ctx.fillRect(x, y, w, h);

  // Fill level (0=bottom, 255=top)
  const fillH = (value / 255) * h;
  ctx.fillStyle = '#888';
  ctx.fillRect(x, y + h - fillH, w, fillH);

  // Digital press highlight border
  if (digitalPressed) {
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, w, h);
  }
}

// Draw D-Pad
function drawDPad(cx, cy, buttons) {
  const armW = 20, armL = 30;
  const dirs = [
    { bit: BTN.DUP,    dx: 0, dy: -1 },
    { bit: BTN.DDOWN,  dx: 0, dy: 1 },
    { bit: BTN.DLEFT,  dx: -1, dy: 0 },
    { bit: BTN.DRIGHT, dx: 1, dy: 0 },
  ];

  // Center square
  ctx.fillStyle = '#333';
  ctx.fillRect(cx - armW/2, cy - armW/2, armW, armW);

  for (const d of dirs) {
    const pressed = isPressed(buttons, d.bit);
    ctx.fillStyle = pressed ? '#aaa' : '#333';

    if (d.dx === 0) {
      // Vertical arm
      const ay = d.dy < 0 ? cy - armW/2 - armL : cy + armW/2;
      ctx.fillRect(cx - armW/2, ay, armW, armL);
    } else {
      // Horizontal arm
      const ax = d.dx < 0 ? cx - armW/2 - armL : cx + armW/2;
      ctx.fillRect(ax, cy - armW/2, armL, armW);
    }
  }
}

// Draw button circle/ellipse
function drawButton(cx, cy, label, pressed, onColor, offColor, shape) {
  ctx.fillStyle = pressed ? onColor : offColor;

  if (shape === 'circleA') {
    ctx.beginPath(); ctx.arc(cx, cy, 18, 0, Math.PI*2); ctx.fill();
  } else if (shape === 'circleB') {
    ctx.beginPath(); ctx.arc(cx, cy, 12, 0, Math.PI*2); ctx.fill();
  } else if (shape === 'circleS') {
    ctx.beginPath(); ctx.arc(cx, cy, 8, 0, Math.PI*2); ctx.fill();
  } else if (shape === 'ellipseH') {
    ctx.beginPath(); ctx.ellipse(cx, cy, 18, 10, 0, 0, Math.PI*2); ctx.fill();
  } else if (shape === 'ellipseV') {
    ctx.beginPath(); ctx.ellipse(cx, cy, 10, 18, 0, 0, Math.PI*2); ctx.fill();
  } else if (shape === 'rectZ') {
    ctx.fillRect(cx - 20, cy - 8, 40, 16);
  }

  // Label
  ctx.fillStyle = pressed ? '#000' : '#888';
  ctx.font = '12px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(label, cx, cy);
}

function draw(data) {
  ctx.clearRect(0, 0, W, H);

  const buttons = (data.bh << 8) | data.bl;
  const barcodeH = 30;

  // Barcode area (stub - gray rect)
  ctx.fillStyle = '#444';
  ctx.fillRect(0, 0, W, barcodeH);
  ctx.fillStyle = '#888';
  ctx.font = '12px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('BARCODE (stub)', W/2, barcodeH/2);

  // Main area starts at y=barcodeH
  const mainY = barcodeH + 10;
  const mainH = H - mainY;
  const midY = mainY + mainH / 2;

  // --- L Trigger ---
  const ltX = 15;
  drawTrigger(ltX, mainY, 30, mainH - 20, data.lt, isPressed(buttons, BTN.L));
  // LT value
  ctx.fillStyle = '#ccc';
  ctx.font = '14px monospace';
  ctx.textAlign = 'center';
  ctx.fillText(String(data.lt).padStart(3, ' '), ltX + 15, mainY + mainH - 8);

  // --- Main Stick ---
  const stickCX = 120;
  const stickCY = midY;
  const stickR = 50;
  drawStick(stickCX, stickCY, stickR, data.sx, data.sy);
  // Stick values (signed)
  ctx.fillStyle = '#ccc';
  ctx.font = '14px monospace';
  ctx.textAlign = 'left';
  const sxSigned = data.sx - 128;
  const sySigned = data.sy - 128;
  const sxStr = (sxSigned >= 0 ? ' ' : '') + String(sxSigned).padStart(3, ' ');
  const syStr = (sySigned >= 0 ? ' ' : '') + String(sySigned).padStart(3, ' ');
  ctx.fillText('X' + sxStr, stickCX + stickR + 8, stickCY - 8);
  ctx.fillText('Y' + syStr, stickCX + stickR + 8, stickCY + 12);

  // --- R Trigger ---
  const rtX = 310;
  drawTrigger(rtX, mainY, 30, mainH - 20, data.rt, isPressed(buttons, BTN.R));
  ctx.fillStyle = '#ccc';
  ctx.font = '14px monospace';
  ctx.textAlign = 'center';
  ctx.fillText(String(data.rt).padStart(3, ' '), rtX + 15, mainY + mainH - 8);

  // --- D-Pad ---
  const dpadCX = 420;
  const dpadCY = midY + 10;
  drawDPad(dpadCX, dpadCY, buttons);

  // --- Buttons ---
  const btnBaseX = 560;
  const btnBaseY = midY;

  // START (center)
  drawButton(btnBaseX - 40, btnBaseY - 30, 'ST', isPressed(buttons, BTN.START),
    '#fff', '#555', 'circleS');

  // Z (top)
  drawButton(btnBaseX + 30, btnBaseY - 45, 'Z', isPressed(buttons, BTN.Z),
    '#8800CC', '#330044', 'rectZ');

  // A (center-right, large green)
  drawButton(btnBaseX + 30, btnBaseY, 'A', isPressed(buttons, BTN.A),
    '#00C400', '#004400', 'circleA');

  // B (left of A, small red)
  drawButton(btnBaseX - 5, btnBaseY + 15, 'B', isPressed(buttons, BTN.B),
    '#C40000', '#440000', 'circleB');

  // X (right of A, horizontal ellipse)
  drawButton(btnBaseX + 68, btnBaseY - 5, 'X', isPressed(buttons, BTN.X),
    '#888', '#333', 'ellipseH');

  // Y (above A, vertical ellipse)
  drawButton(btnBaseX + 10, btnBaseY - 35, 'Y', isPressed(buttons, BTN.Y),
    '#888', '#333', 'ellipseV');

  // --- C-Stick ---
  const cstickCX = 730;
  const cstickCY = midY + 20;
  const cstickR = 30;
  drawStick(cstickCX, cstickCY, cstickR, data.cx, data.cy);
  // C-Stick values
  const cxSigned = data.cx - 128;
  const cySigned = data.cy - 128;
  const cxStr = (cxSigned >= 0 ? ' ' : '') + String(cxSigned).padStart(3, ' ');
  const cyStr = (cySigned >= 0 ? ' ' : '') + String(cySigned).padStart(3, ' ');
  ctx.fillStyle = '#ccc';
  ctx.font = '12px monospace';
  ctx.textAlign = 'left';
  ctx.fillText('X' + cxStr, cstickCX + cstickR + 5, cstickCY - 6);
  ctx.fillText('Y' + cyStr, cstickCX + cstickR + 5, cstickCY + 10);
}

// WebSocket
function connect() {
  const wsProto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
  const ws = new WebSocket(wsProto + location.host + '/ws');

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    draw(msg);
  };

  ws.onclose = () => setTimeout(connect, 500);
  ws.onerror = () => ws.close();
}

connect();
</script>
</body>
</html>
"""


# ---------- aiohttp handlers ----------
async def overlay_handler(request: web.Request) -> web.Response:
    return web.Response(text=request.app["overlay_html"], content_type="text/html")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    clients: set = request.app["clients"]
    clients.add(ws)

    latest: Latest = request.app["latest"]
    await ws.send_str(
        json.dumps({
            "bh": latest.bh,
            "bl": latest.bl,
            "sx": latest.sx,
            "sy": latest.sy,
            "cx": latest.cx,
            "cy": latest.cy,
            "lt": latest.lt,
            "rt": latest.rt,
        })
    )

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        clients.discard(ws)

    return ws


async def broadcaster(app: web.Application) -> None:
    q: asyncio.Queue = app["queue"]
    latest: Latest = app["latest"]
    clients: set = app["clients"]

    while True:
        bh, bl, sx, sy, cx, cy, lt, rt = await q.get()
        latest.bh = bh
        latest.bl = bl
        latest.sx = sx
        latest.sy = sy
        latest.cx = cx
        latest.cy = cy
        latest.lt = lt
        latest.rt = rt
        latest.updated_at = time.time()

        payload = json.dumps({
            "bh": bh, "bl": bl, "sx": sx, "sy": sy,
            "cx": cx, "cy": cy, "lt": lt, "rt": rt,
        })
        dead = []
        for ws in clients:
            try:
                await ws.send_str(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)


async def run_server(args: argparse.Namespace) -> None:
    app = web.Application()
    app["clients"] = set()
    app["queue"] = asyncio.Queue(maxsize=args.queue_size)
    app["overlay_html"] = build_overlay_html()
    app["latest"] = Latest(updated_at=time.time())

    app.router.add_get("/overlay.html", overlay_handler)
    app.router.add_get("/ws", ws_handler)

    loop = asyncio.get_running_loop()
    stop_flag = threading.Event()
    t = threading.Thread(
        target=serial_reader_thread,
        args=(args.serial, args.baud, loop, app["queue"], stop_flag),
        daemon=True,
    )
    t.start()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    asyncio.create_task(broadcaster(app))

    print("gcinput-viewer サーバ起動")
    print(f"  OBS Browser Source: http://{args.host}:{args.port}/overlay.html")
    print(f"  WebSocket: ws://{args.host}:{args.port}/ws")
    print(f"  シリアルポート: {args.serial} ({args.baud} baud)")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        stop_flag.set()
        await runner.cleanup()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="GCコントローラ入力可視化サーバ")
    ap.add_argument("--serial", required=True, help="シリアルポート (例: /dev/cu.usbmodemXXXX)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--host", default="127.0.0.1", help="バインドアドレス (LAN: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--queue-size", type=int, default=256)
    return ap


def cli_main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_server(args))


if __name__ == "__main__":
    cli_main()
