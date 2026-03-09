#!/usr/bin/env python3
"""JoyBus 3カラムシーケンス図ビューア.

debug_probe の CSV 出力をシリアルポートまたはファイルから読み取り、
PAD | Pico | CONSOLE の3カラムシーケンス図として表示する。
"""

from __future__ import annotations

import argparse
import sys

# ANSI color codes
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

# Column widths
PAD_W = 22
PICO_W = 16
CON_W = 22

# Command name lookup
CMD_NAMES: dict[int, str] = {
    0x00: "Id",
    0x40: "Status",
    0x41: "Origin",
    0x42: "Recalibrate",
    0xFF: "Reset",
}

# Border characters
BORDER_TOP = (
    f"{CYAN}┌─ PAD (controller) ──┬───── Pico ─────┬─ CONSOLE (game) ──┐{RESET}"
)
BORDER_BOT = (
    f"{CYAN}└─────────────────────┴────────────────┴────────────────────┘{RESET}"
)


def ts_ms(us_str: str) -> str:
    """Convert microsecond timestamp string to ms with 3 decimal places."""
    try:
        us = int(us_str)
        return f"{us / 1000:.3f}ms"
    except ValueError:
        return us_str


def hex_abbr(hex_data: str) -> str:
    """Abbreviate hex data to max 12 chars."""
    parts = []
    for i in range(0, len(hex_data), 2):
        parts.append(hex_data[i : i + 2].upper())
    formatted = " ".join(parts)
    if len(formatted) > 12:
        return formatted[:9] + "..."
    return formatted


def cmd_name(hex_data: str) -> str:
    """Resolve command byte to name."""
    if len(hex_data) >= 2:
        try:
            byte = int(hex_data[:2], 16)
            return CMD_NAMES.get(byte, f"0x{byte:02X}")
        except ValueError:
            pass
    return "???"


def render_line(pad: str, pico: str, con: str) -> str:
    """Render a single 3-column line with borders."""
    pad_s = pad.ljust(PAD_W)[:PAD_W]
    pico_s = pico.ljust(PICO_W)[:PICO_W]
    con_s = con.ljust(CON_W)[:CON_W]
    return f"{CYAN}│{RESET}{pad_s}{CYAN}│{RESET}{pico_s}{CYAN}│{RESET}{con_s}{CYAN}│{RESET}"


def _visible_len(s: str) -> int:
    """Calculate visible length excluding ANSI escape sequences."""
    import re

    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def render_line_raw(pad: str, pico: str, con: str) -> str:
    """Render a line using raw strings with ANSI codes, padding by visible length."""
    pad_vis = _visible_len(pad)
    pico_vis = _visible_len(pico)
    con_vis = _visible_len(con)
    pad_s = pad + " " * max(0, PAD_W - pad_vis) if pad_vis <= PAD_W else pad
    pico_s = pico + " " * max(0, PICO_W - pico_vis) if pico_vis <= PICO_W else pico
    con_s = con + " " * max(0, CON_W - con_vis) if con_vis <= CON_W else con
    return f"{CYAN}│{RESET}{pad_s}{CYAN}│{RESET}{pico_s}{CYAN}│{RESET}{con_s}{CYAN}│{RESET}"


def process_t_line(parts: list[str]) -> None:
    """Process T (data frame) line: T,ts,port,dir,len,hex."""
    if len(parts) < 6:
        return
    ts = parts[1]
    port = parts[2]
    direction = parts[3]
    hex_data = parts[5]

    ts_str = f"{DIM}{ts_ms(ts)}{RESET}"
    name = f"{YELLOW}{cmd_name(hex_data)}{RESET}"
    h = f"{hex_abbr(hex_data)}"

    if port == "P" and direction == "T":
        # Pico→Pad送信: PADカラム
        print(render_line_raw(f"  {ts_str}", "", ""))
        arrow = f"    {CYAN}◀── {name} ──┤{RESET}"
        print(render_line_raw(arrow, "", ""))
    elif port == "P" and direction == "R":
        # Pad→Pico受信: PADカラム
        print(render_line_raw(f"  {ts_str}", "", ""))
        arrow = f"  {h} {CYAN}────▶│{RESET}"
        print(render_line_raw(arrow, "", ""))
    elif port == "C" and direction == "R":
        # Console→Pico受信: CONカラム
        print(render_line_raw("", "", f"  {ts_str}"))
        arrow = f"{CYAN}├── {name} ──▶{RESET}"
        print(render_line_raw("", "", arrow))
    elif port == "C" and direction == "T":
        # Pico→Console送信: CONカラム
        print(render_line_raw("", "", f"  {ts_str}"))
        arrow = f"{CYAN}│◀── {RESET}{h}"
        print(render_line_raw("", "", arrow))


def process_s_line(parts: list[str]) -> None:
    """Process S (state transition) line: S,ts,from,to."""
    if len(parts) < 4:
        return
    from_state = parts[2]
    to_state = parts[3]
    line1 = f"  {GREEN}{from_state}{RESET}"
    line2 = f"  {GREEN}→ {to_state}{RESET}"
    print(render_line_raw("", line1, ""))
    print(render_line_raw("", line2, ""))


def process_m_line(parts: list[str]) -> None:
    """Process M (message) line: M,ts,msg."""
    if len(parts) < 3:
        return
    msg = parts[2]
    print(render_line_raw("", f"  {msg}", ""))


def process_u_line(parts: list[str]) -> None:
    """Process U (summary) line: U,ts,port,polls,ok,timeout."""
    if len(parts) < 6:
        return
    port_ch = "P" if parts[2] == "P" else "C"
    polls = parts[3]
    ok = parts[4]
    timeout = parts[5]
    err_s = f"{RED}{timeout}e{RESET}" if int(timeout or "0") > 0 else f"{timeout}e"
    summary = f"[{port_ch}:{polls}/{ok}/{err_s}]"
    print(render_line_raw("", summary, ""))


def process_line(line: str) -> None:
    """Parse and render a single CSV line."""
    line = line.strip()
    if not line or line.startswith("#"):
        return
    parts = line.split(",")
    if not parts:
        return

    record_type = parts[0]
    if record_type == "T":
        process_t_line(parts)
    elif record_type == "S":
        process_s_line(parts)
    elif record_type == "M":
        process_m_line(parts)
    elif record_type == "U":
        process_u_line(parts)
    # Unknown line types silently skipped


def run_file_mode(path: str) -> None:
    """Read CSV file and display sequence diagram."""
    print(BORDER_TOP)
    print(render_line("", "", ""))
    try:
        with open(path) as f:
            for line in f:
                process_line(line)
    except FileNotFoundError:
        print(f"{RED}Error: file not found: {path}{RESET}", file=sys.stderr)
        sys.exit(1)
    print(render_line("", "", ""))
    print(BORDER_BOT)


def run_serial_mode(port: str, baud: int) -> None:
    """Read from serial port and display sequence diagram in real-time."""
    try:
        import serial  # type: ignore[import-untyped]
    except ImportError:
        print(f"{RED}Error: pyserial not installed. Run: uv pip install pyserial{RESET}", file=sys.stderr)
        sys.exit(1)

    print(BORDER_TOP)
    print(render_line("", "", ""))
    try:
        ser = serial.Serial(port, baud, timeout=1)
        while True:
            raw = ser.readline()
            if raw:
                line = raw.decode("utf-8", errors="replace")
                process_line(line)
    except serial.SerialException as e:
        print(f"{RED}Serial error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        print(render_line("", "", ""))
        print(BORDER_BOT)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JoyBus 3カラムシーケンス図ビューア",
        epilog="例: python tools/joybus_viewer.py /dev/tty.usbmodem*\n"
        "     python tools/joybus_viewer.py --file capture.csv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("port", nargs="?", help="シリアルポート (例: /dev/tty.usbmodem*)")
    parser.add_argument("--file", metavar="PATH", help="CSVファイルから再生")
    parser.add_argument("--baud", type=int, default=115200, help="ボーレート (デフォルト: 115200)")
    args = parser.parse_args()

    if args.file:
        run_file_mode(args.file)
    elif args.port:
        run_serial_mode(args.port, args.baud)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
