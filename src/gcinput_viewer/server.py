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

// HiDPI support
const dpr = window.devicePixelRatio || 1;
canvas.width = W * dpr;
canvas.height = H * dpr;
canvas.style.width = W + 'px';
canvas.style.height = H + 'px';
ctx.scale(dpr, dpr);

// ---------- Barcode ----------
const BAR_W = 12;
const BAR_GAP = 1;
const BIT_PITCH = BAR_W + BAR_GAP;
const BAR_H_ONE = 18;
const BAR_H_ZERO = 6;
const MARGIN_X = 8;
const GUARD_SPACER = 6;

function crc8atm(bytes) {
  let crc = 0x00;
  for (const b of bytes) {
    crc ^= b;
    for (let i = 0; i < 8; i++) {
      if (crc & 0x80) crc = ((crc << 1) & 0xFF) ^ 0x07;
      else crc = (crc << 1) & 0xFF;
    }
  }
  return crc & 0xFF;
}

function byteToBits(val) {
  const bits = [];
  for (let i = 7; i >= 0; i--) bits.push((val >> i) & 1);
  return bits;
}

function makeRow1Bits(bh, bl, sx, sy) {
  const payload = [bh, bl, sx, sy];
  const crc = crc8atm(payload);
  return [0xA5, ...payload, crc].flatMap(byteToBits);
}

function makeRow2Bits(cx, cy, lt, rt) {
  const payload = [cx, cy, lt, rt];
  const crc = crc8atm(payload);
  return [0x5A, ...payload, crc].flatMap(byteToBits);
}

function drawGuard(ctx, x, y, h, barW, bitPitch) {
  const colors = ['#fff', '#888', '#fff'];
  for (let i = 0; i < 3; i++) {
    ctx.fillStyle = colors[i];
    ctx.fillRect(x + i * bitPitch, y, barW, h);
  }
  return x + 3 * bitPitch + GUARD_SPACER;
}

function drawBarcodeRow(ctx, bits, y, barW, bitPitch, barHOne, barHZero) {
  let x = MARGIN_X;
  x = drawGuard(ctx, x, y, barHOne, barW, bitPitch);
  for (let i = 0; i < bits.length; i++) {
    const h = bits[i] ? barHOne : barHZero;
    const barY = y + (barHOne - h);
    ctx.fillStyle = '#fff';
    ctx.fillRect(x, barY, barW, h);
    x += bitPitch;
  }
  x += GUARD_SPACER;
  drawGuard(ctx, x, y, barHOne, barW, bitPitch);
}

function drawBarcode(ctx, data, barW, bitPitch, barHOne, barHZero) {
  const row1 = makeRow1Bits(data.bh, data.bl, data.sx, data.sy);
  const row2 = makeRow2Bits(data.cx, data.cy, data.lt, data.rt);
  drawBarcodeRow(ctx, row1, 2, barW, bitPitch, barHOne, barHZero);
  drawBarcodeRow(ctx, row2, 26, barW, bitPitch, barHOne, barHZero);
}

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

// Draw octagon gate — Oct(a) shape
function drawOctagon(ctx, cx, cy, a) {
  const k = a / Math.SQRT2;
  const verts = [
    [cx + a, cy],       // 0°
    [cx + k, cy - k],   // 45°
    [cx,     cy - a],   // 90°
    [cx - k, cy - k],   // 135°
    [cx - a, cy],       // 180°
    [cx - k, cy + k],   // 225°
    [cx,     cy + a],   // 270°
    [cx + k, cy + k],   // 315°
  ];
  ctx.beginPath();
  ctx.moveTo(verts[0][0], verts[0][1]);
  for (let i = 1; i < 8; i++) ctx.lineTo(verts[i][0], verts[i][1]);
  ctx.closePath();
}

// Draw stick (octagon gate + crosshair + dot)
function drawStick(cx, cy, radius, valX, valY) {
  // Octagon gate
  ctx.strokeStyle = '#666';
  ctx.lineWidth = 1.5;
  drawOctagon(ctx, cx, cy, radius);
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
  const barcodeH = 48;

  // Barcode area (2-row split barcode)
  drawBarcode(ctx, data, BAR_W, BIT_PITCH, BAR_H_ONE, BAR_H_ZERO);

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

// WebSocket + requestAnimationFrame
let latestData = null;

function connect() {
  const wsProto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
  const ws = new WebSocket(wsProto + location.host + '/ws');

  ws.onmessage = (ev) => { latestData = JSON.parse(ev.data); };

  ws.onclose = () => setTimeout(connect, 500);
  ws.onerror = () => ws.close();
}

function loop() {
  if (latestData) { draw(latestData); }
  requestAnimationFrame(loop);
}

connect();
requestAnimationFrame(loop);
</script>
</body>
</html>
"""


# ---------- Widget HTML ----------
def build_widget_html() -> str:
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
<canvas id="c" width="480" height="1080"></canvas>
<script>
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const W = 480, H = 1080;

// HiDPI support
const dpr = window.devicePixelRatio || 1;
canvas.width = W * dpr;
canvas.height = H * dpr;
canvas.style.width = W + 'px';
canvas.style.height = H + 'px';
ctx.scale(dpr, dpr);

// ---------- Barcode ----------
const WGT_BAR_W = 8;
const WGT_BAR_GAP = 1;
const WGT_BIT_PITCH = WGT_BAR_W + WGT_BAR_GAP;
const WGT_BAR_H_ONE = 18;
const WGT_BAR_H_ZERO = 6;
const WGT_MARGIN_X = 8;
const WGT_GUARD_SPACER = 6;

function crc8atm(bytes) {
  let crc = 0x00;
  for (const b of bytes) {
    crc ^= b;
    for (let i = 0; i < 8; i++) {
      if (crc & 0x80) crc = ((crc << 1) & 0xFF) ^ 0x07;
      else crc = (crc << 1) & 0xFF;
    }
  }
  return crc & 0xFF;
}

function byteToBits(val) {
  const bits = [];
  for (let i = 7; i >= 0; i--) bits.push((val >> i) & 1);
  return bits;
}

function makeRow1Bits(bh, bl, sx, sy) {
  const payload = [bh, bl, sx, sy];
  const crc = crc8atm(payload);
  return [0xA5, ...payload, crc].flatMap(byteToBits);
}

function makeRow2Bits(cx, cy, lt, rt) {
  const payload = [cx, cy, lt, rt];
  const crc = crc8atm(payload);
  return [0x5A, ...payload, crc].flatMap(byteToBits);
}

function drawGuard(ctx, x, y, h, barW, bitPitch) {
  const colors = ['#fff', '#888', '#fff'];
  for (let i = 0; i < 3; i++) {
    ctx.fillStyle = colors[i];
    ctx.fillRect(x + i * bitPitch, y, barW, h);
  }
  return x + 3 * bitPitch + WGT_GUARD_SPACER;
}

function drawBarcodeRow(ctx, bits, y, barW, bitPitch, barHOne, barHZero) {
  let x = WGT_MARGIN_X;
  x = drawGuard(ctx, x, y, barHOne, barW, bitPitch);
  for (let i = 0; i < bits.length; i++) {
    const h = bits[i] ? barHOne : barHZero;
    const barY = y + (barHOne - h);
    ctx.fillStyle = '#fff';
    ctx.fillRect(x, barY, barW, h);
    x += bitPitch;
  }
  x += WGT_GUARD_SPACER;
  drawGuard(ctx, x, y, barHOne, barW, bitPitch);
}

function drawBarcode(ctx, data) {
  const row1 = makeRow1Bits(data.bh, data.bl, data.sx, data.sy);
  const row2 = makeRow2Bits(data.cx, data.cy, data.lt, data.rt);
  drawBarcodeRow(ctx, row1, 2, WGT_BAR_W, WGT_BIT_PITCH, WGT_BAR_H_ONE, WGT_BAR_H_ZERO);
  drawBarcodeRow(ctx, row2, 26, WGT_BAR_W, WGT_BIT_PITCH, WGT_BAR_H_ONE, WGT_BAR_H_ZERO);
}

// Button bit positions
const BTN = {
  A:     0,
  B:     1,
  X:     2,
  Y:     3,
  START: 4,
  DLEFT: 8,
  DRIGHT:9,
  DDOWN: 10,
  DUP:   11,
  Z:     12,
  R:     13,
  L:     14,
};

function isPressed(buttons, bit) {
  return (buttons >> bit) & 1;
}

// Draw octagon gate — Oct(a) shape
function drawOctagon(ctx, cx, cy, a) {
  const k = a / Math.SQRT2;
  const verts = [
    [cx + a, cy],       // 0°
    [cx + k, cy - k],   // 45°
    [cx,     cy - a],   // 90°
    [cx - k, cy - k],   // 135°
    [cx - a, cy],       // 180°
    [cx - k, cy + k],   // 225°
    [cx,     cy + a],   // 270°
    [cx + k, cy + k],   // 315°
  ];
  ctx.beginPath();
  ctx.moveTo(verts[0][0], verts[0][1]);
  for (let i = 1; i < 8; i++) ctx.lineTo(verts[i][0], verts[i][1]);
  ctx.closePath();
}

// Draw trigger slider (compact)
function drawTriggerCompact(x, y, w, h, value, digitalPressed) {
  ctx.fillStyle = '#222';
  ctx.fillRect(x, y, w, h);
  const fillH = (value / 255) * h;
  ctx.fillStyle = '#888';
  ctx.fillRect(x, y + h - fillH, w, fillH);
  if (digitalPressed) {
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, w, h);
  }
}

// Draw button (compact, 60% size)
function drawButtonCompact(bx, by, label, pressed, onColor, offColor, r) {
  ctx.fillStyle = pressed ? onColor : offColor;
  ctx.beginPath();
  ctx.arc(bx, by, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = pressed ? '#000' : '#888';
  ctx.font = '10px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(label, bx, by);
}

// Draw D-Pad (compact, 50x50)
function drawDPadCompact(cx, cy, buttons) {
  const armW = 12, armL = 18;
  const dirs = [
    { bit: BTN.DUP,    dx: 0, dy: -1 },
    { bit: BTN.DDOWN,  dx: 0, dy: 1 },
    { bit: BTN.DLEFT,  dx: -1, dy: 0 },
    { bit: BTN.DRIGHT, dx: 1, dy: 0 },
  ];
  ctx.fillStyle = '#333';
  ctx.fillRect(cx - armW/2, cy - armW/2, armW, armW);
  for (const d of dirs) {
    const pressed = isPressed(buttons, d.bit);
    ctx.fillStyle = pressed ? '#aaa' : '#333';
    if (d.dx === 0) {
      const ay = d.dy < 0 ? cy - armW/2 - armL : cy + armW/2;
      ctx.fillRect(cx - armW/2, ay, armW, armL);
    } else {
      const ax = d.dx < 0 ? cx - armW/2 - armL : cx + armW/2;
      ctx.fillRect(ax, cy - armW/2, armL, armW);
    }
  }
}

function draw(data) {
  ctx.clearRect(0, 0, W, H);

  const buttons = (data.bh << 8) | data.bl;

  // --- Barcode (y=0-48) ---
  drawBarcode(ctx, data);

  // --- Compact info area (y=48-200) ---
  const infoY = 52;

  // Buttons row (Z, Y, X, A, B, START) - horizontal
  const btnY = infoY + 20;
  const btnStartX = 30;
  const btnGap = 42;
  drawButtonCompact(btnStartX, btnY, 'ST', isPressed(buttons, BTN.START), '#fff', '#555', 8);
  drawButtonCompact(btnStartX + btnGap, btnY, 'Z', isPressed(buttons, BTN.Z), '#8800CC', '#330044', 10);
  drawButtonCompact(btnStartX + btnGap*2, btnY, 'A', isPressed(buttons, BTN.A), '#00C400', '#004400', 12);
  drawButtonCompact(btnStartX + btnGap*3, btnY, 'B', isPressed(buttons, BTN.B), '#C40000', '#440000', 9);
  drawButtonCompact(btnStartX + btnGap*4, btnY, 'X', isPressed(buttons, BTN.X), '#888', '#333', 10);
  drawButtonCompact(btnStartX + btnGap*5, btnY, 'Y', isPressed(buttons, BTN.Y), '#888', '#333', 10);

  // L/R Triggers (compact vertical sliders)
  const trigY = infoY + 45;
  const trigH = 80;
  const trigW = 20;
  drawTriggerCompact(20, trigY, trigW, trigH, data.lt, isPressed(buttons, BTN.L));
  drawTriggerCompact(50, trigY, trigW, trigH, data.rt, isPressed(buttons, BTN.R));

  // Trigger labels
  ctx.fillStyle = '#ccc';
  ctx.font = '10px monospace';
  ctx.textAlign = 'center';
  ctx.fillText('L', 30, trigY + trigH + 12);
  ctx.fillText('R', 60, trigY + trigH + 12);
  ctx.fillText(String(data.lt).padStart(3, ' '), 30, trigY - 6);
  ctx.fillText(String(data.rt).padStart(3, ' '), 60, trigY - 6);

  // D-Pad (compact, 50×50)
  drawDPadCompact(120, trigY + 40, buttons);

  // Analog values (text)
  const valX = 200;
  const valY = trigY + 5;
  ctx.fillStyle = '#ccc';
  ctx.font = '14px monospace';
  ctx.textAlign = 'left';
  const sxS = data.sx - 128, syS = data.sy - 128;
  const cxS = data.cx - 128, cyS = data.cy - 128;
  const fmt = (v) => (v >= 0 ? ' ' : '') + String(v).padStart(3, ' ');
  ctx.fillText('JoyX:' + fmt(sxS), valX, valY);
  ctx.fillText('JoyY:' + fmt(syS), valX, valY + 18);
  ctx.fillText('C-X: ' + fmt(cxS), valX, valY + 36);
  ctx.fillText('C-Y: ' + fmt(cyS), valX, valY + 54);
  ctx.fillText('  LT:' + String(data.lt).padStart(4, ' '), valX, valY + 72);
  ctx.fillText('  RT:' + String(data.rt).padStart(4, ' '), valX, valY + 90);

  // --- Main Stick (large octagon, y=200-1080) ---
  const stickCX = 240;
  const stickCY = 650;
  const stickR = 300;

  // Octagon gate
  ctx.strokeStyle = '#666';
  ctx.lineWidth = 2;
  drawOctagon(ctx, stickCX, stickCY, stickR);
  ctx.stroke();

  // Crosshair
  ctx.strokeStyle = '#333';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(stickCX - stickR, stickCY);
  ctx.lineTo(stickCX + stickR, stickCY);
  ctx.moveTo(stickCX, stickCY - stickR);
  ctx.lineTo(stickCX, stickCY + stickR);
  ctx.stroke();

  // Current position dot
  const dx = ((data.sx - 128) / 128) * stickR;
  const dy = -((data.sy - 128) / 128) * stickR;
  ctx.fillStyle = '#fff';
  ctx.beginPath();
  ctx.arc(stickCX + dx, stickCY + dy, 10, 0, Math.PI * 2);
  ctx.fill();

  // Stick value labels
  ctx.fillStyle = '#ccc';
  ctx.font = '16px monospace';
  ctx.textAlign = 'center';
  ctx.fillText('X:' + fmt(sxS) + '  Y:' + fmt(syS), stickCX, stickCY + stickR + 30);
}

// WebSocket + requestAnimationFrame
let latestData = null;

function connect() {
  const wsProto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
  const ws = new WebSocket(wsProto + location.host + '/ws');

  ws.onmessage = (ev) => { latestData = JSON.parse(ev.data); };

  ws.onclose = () => setTimeout(connect, 500);
  ws.onerror = () => ws.close();
}

function loop() {
  if (latestData) { draw(latestData); }
  requestAnimationFrame(loop);
}

connect();
requestAnimationFrame(loop);
</script>
</body>
</html>
"""


# ---------- aiohttp handlers ----------
async def overlay_handler(request: web.Request) -> web.Response:
    return web.Response(text=request.app["overlay_html"], content_type="text/html")


async def widget_handler(request: web.Request) -> web.Response:
    return web.Response(text=request.app["widget_html"], content_type="text/html")


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
    app["widget_html"] = build_widget_html()
    app["latest"] = Latest(updated_at=time.time())

    app.router.add_get("/overlay.html", overlay_handler)
    app.router.add_get("/widget.html", widget_handler)
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
    print(f"  フル表示: http://{args.host}:{args.port}/overlay.html")
    print(f"  ウィジェット: http://{args.host}:{args.port}/widget.html")
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
