# gcinput-viewer

GCコントローラの全入力をリアルタイムに可視化するWebSocketサーバ + HTMLオーバーレイ。

Pico基板（pico-gc-bridge上のinput_viewerファームウェア）がシリアルでコントローラの全8バイトを送信し、Python WebSocketサーバ経由でブラウザにCanvas描画する。OBSのBrowser Sourceとして利用可能。

## インストール

```bash
uv sync
```

## 使い方

```bash
uv run gcinput-viewer --serial /dev/cu.usbmodemXXXX
```

ブラウザで `http://127.0.0.1:8765/overlay.html` を開く。

### オプション

| オプション | デフォルト | 説明 |
|-----------|-----------|------|
| `--serial` | (必須) | シリアルポート |
| `--baud` | 115200 | ボーレート |
| `--host` | 127.0.0.1 | バインドアドレス |
| `--port` | 8765 | HTTPポート |

## シリアルフォーマット

```
I,BH,BL,SX,SY,CX,CY,LT,RT,CC
```

- `I`: マーカー
- `BH,BL`: ボタンステータス (16進)
- `SX,SY`: メインスティック (10進 0-255)
- `CX,CY`: Cスティック (10進 0-255)
- `LT,RT`: トリガー (10進 0-255)
- `CC`: CRC-8 ATM (16進)
