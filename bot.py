#!/usr/bin/env python3
"""
Bybit USDT Perpetual Futures Scalping Bot
Strategy : EMA(9/21) + RSI(14) | Timeframe: 15m
Author   : Built with Claude
"""

import os, time, json, math, logging, base64
from datetime import datetime
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

# ══════════════════════════════════════════════════
#  CONFIGURATION  ← set via environment variables
# ══════════════════════════════════════════════════
API_KEY          = os.environ["BYBIT_API_KEY"]
PRIVATE_KEY_PEM  = os.environ["BYBIT_PRIVATE_KEY"].replace("\\n", "\n")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL      = "https://api.bybit.com"
CATEGORY      = "linear"   # USDT Perpetual
TIMEFRAME     = "15"       # 15-minute candles
SCAN_INTERVAL = 60         # seconds between full scans
RECV_WINDOW   = "5000"

MARGIN_PER_TRADE = 1.0    # $1 margin per trade
MAX_POSITIONS    = 5      # max concurrent open positions
MAX_LOSS_TOTAL   = 30.0   # hard stop: realized PnL hits -$30
SL_PCT           = 0.005  # stop-loss  = 0.5% dari harga entry
TP_PCT           = 0.005  # take-profit = 0.5% dari harga entry → RR 1:1

TOP_PAIRS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","MATICUSDT",
    "LINKUSDT","LTCUSDT","UNIUSDT","ATOMUSDT","ETCUSDT",
    "XLMUSDT","BCHUSDT","NEARUSDT","ALGOUSDT","FILUSDT",
]

# ══════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════
total_realized_pnl: float = 0.0
open_positions: dict = {}   # symbol → position info
_instrument_cache: dict = {}

# ══════════════════════════════════════════════════
#  RSA SIGNING (Bybit API v5)
# ══════════════════════════════════════════════════
def _load_key():
    return serialization.load_pem_private_key(
        PRIVATE_KEY_PEM.encode(), password=None
    )

PRIVATE_KEY_OBJ = _load_key()

def _sign(payload: str) -> str:
    sig = PRIVATE_KEY_OBJ.sign(
        payload.encode(), asym_padding.PKCS1v15(), hashes.SHA256()
    )
    return base64.b64encode(sig).decode()

def _build_headers(payload: str) -> dict:
    ts = str(int(time.time() * 1000))
    sign_str = ts + API_KEY + RECV_WINDOW + payload
    return {
        "X-BAPI-API-KEY":     API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        _sign(sign_str),
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type":       "application/json",
    }

# ══════════════════════════════════════════════════
#  API WRAPPERS
# ══════════════════════════════════════════════════
def api_get(endpoint: str, params: dict = None) -> dict:
    params = params or {}
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    try:
        r = requests.get(
            f"{BASE_URL}{endpoint}", params=params,
            headers=_build_headers(qs), timeout=10
        )
        return r.json()
    except Exception as e:
        log.error(f"GET {endpoint} error: {e}")
        return {}

def api_post(endpoint: str, body: dict) -> dict:
    payload = json.dumps(body)
    try:
        r = requests.post(
            f"{BASE_URL}{endpoint}", data=payload,
            headers=_build_headers(payload), timeout=10
        )
        return r.json()
    except Exception as e:
        log.error(f"POST {endpoint} error: {e}")
        return {}

# ══════════════════════════════════════════════════
#  TELEGRAM NOTIFICATIONS
# ══════════════════════════════════════════════════
def notify(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        log.warning(f"Telegram notify error: {e}")

# ══════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════
def ema(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(values: list, period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))

# ══════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════
def get_candles(symbol: str, limit: int = 60):
    r = api_get("/v5/market/kline", {
        "category": CATEGORY, "symbol": symbol,
        "interval": TIMEFRAME, "limit": str(limit)
    })
    if r.get("retCode") != 0:
        return [], []
    rows = r["result"]["list"][::-1]   # oldest first
    closes  = [float(row[4]) for row in rows]
    volumes = [float(row[5]) for row in rows]
    return closes, volumes

def get_last_price(symbol: str) -> float:
    r = api_get("/v5/market/tickers", {"category": CATEGORY, "symbol": symbol})
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except:
        return 0.0

# ══════════════════════════════════════════════════
#  INSTRUMENT INFO
# ══════════════════════════════════════════════════
def get_instrument(symbol: str) -> dict:
    if symbol not in _instrument_cache:
        r = api_get("/v5/market/instruments-info",
                    {"category": CATEGORY, "symbol": symbol})
        try:
            _instrument_cache[symbol] = r["result"]["list"][0]
        except:
            _instrument_cache[symbol] = {}
    return _instrument_cache[symbol]

def max_leverage(symbol: str) -> int:
    try:
        return int(float(get_instrument(symbol)["leverageFilter"]["maxLeverage"]))
    except:
        return 50

def tick_size(symbol: str) -> float:
    try:
        return float(get_instrument(symbol)["priceFilter"]["tickSize"])
    except:
        return 0.01

def qty_step(symbol: str) -> float:
    try:
        return float(get_instrument(symbol)["lotSizeFilter"]["qtyStep"])
    except:
        return 0.001

def round_price(price: float, tick: float) -> str:
    dec = max(0, round(-math.log10(tick))) if tick < 1 else 0
    rounded = round(price / tick) * tick
    return f"{rounded:.{dec}f}"

def round_qty(qty: float, step: float) -> str:
    dec = max(0, round(-math.log10(step))) if step < 1 else 0
    qty = math.floor(qty / step) * step
    return f"{qty:.{dec}f}"

# ══════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════
def get_signal(symbol: str):
    """Returns 'long', 'short', or None."""
    closes, volumes = get_candles(symbol, 60)
    if len(closes) < 30 or len(volumes) < 5:
        return None

    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    r   = rsi(closes, 14)

    # Volume filter: current bar must be above average
    vol_avg = sum(volumes[:-1]) / len(volumes[:-1])
    if volumes[-1] < vol_avg * 1.2:
        return None

    # LONG  : bullish trend + RSI not overbought
    if e9 > e21 and 40 < r < 65:
        return "long"

    # SHORT : bearish trend + RSI not oversold
    if e9 < e21 and 35 < r < 60:
        return "short"

    return None

# ══════════════════════════════════════════════════
#  POSITION MANAGEMENT
# ══════════════════════════════════════════════════
def count_open_positions() -> int:
    r = api_get("/v5/position/list",
                {"category": CATEGORY, "settleCoin": "USDT"})
    try:
        return sum(1 for p in r["result"]["list"] if float(p["size"]) > 0)
    except:
        return 0

def set_leverage(symbol: str, lev: int):
    api_post("/v5/position/set-leverage", {
        "category":    CATEGORY,
        "symbol":      symbol,
        "buyLeverage": str(lev),
        "sellLeverage": str(lev),
    })

def place_order(symbol: str, direction: str):
    if symbol in open_positions:
        return
    if count_open_positions() >= MAX_POSITIONS:
        return

    lev = max_leverage(symbol)
    set_leverage(symbol, lev)
    time.sleep(0.3)

    price = get_last_price(symbol)
    if price == 0:
        return

    # position value = margin × leverage
    pos_value = MARGIN_PER_TRADE * lev
    tick = tick_size(symbol)
    step = qty_step(symbol)

    qty     = pos_value / price
    qty_str = round_qty(qty, step)

    # SL & TP berdasarkan 0.5% pergerakan harga
    if direction == "long":
        side     = "Buy"
        sl_price = round_price(price * (1 - SL_PCT), tick)
        tp_price = round_price(price * (1 + TP_PCT), tick)
    else:
        side     = "Sell"
        sl_price = round_price(price * (1 + SL_PCT), tick)
        tp_price = round_price(price * (1 - TP_PCT), tick)

    body = {
        "category":       CATEGORY,
        "symbol":         symbol,
        "side":           side,
        "orderType":      "Market",
        "qty":            qty_str,
        "stopLoss":       sl_price,
        "takeProfit":     tp_price,
        "tpslMode":       "Full",
        "reduceOnly":     False,
        "closeOnTrigger": False,
    }

    r = api_post("/v5/order/create", body)
    if r.get("retCode") == 0:
        open_positions[symbol] = {
            "orderId":   r["result"]["orderId"],
            "direction": direction,
            "entry":     price,
            "sl":        sl_price,
            "tp":        tp_price,
            "leverage":  lev,
        }
        msg = (
            f"✅ <b>OPEN {direction.upper()}</b>\n"
            f"Pair     : {symbol}\n"
            f"Entry    : {price}\n"
            f"SL       : {sl_price}\n"
            f"TP       : {tp_price}\n"
            f"Leverage : {lev}x  |  Margin: ${MARGIN_PER_TRADE}\n"
            f"Positions: {count_open_positions()}/{MAX_POSITIONS}"
        )
        notify(msg)
        log.info(msg.replace("<b>","").replace("</b>",""))
    else:
        log.error(f"Order failed {symbol}: {r}")

def sync_closed():
    """Detect closed positions and update realized PnL."""
    global total_realized_pnl
    closed = []
    for symbol, info in open_positions.items():
        r = api_get("/v5/position/list",
                    {"category": CATEGORY, "symbol": symbol})
        try:
            pos  = r["result"]["list"][0]
            size = float(pos["size"])
            if size == 0:
                pnl_r = api_get("/v5/position/closed-pnl",
                                {"category": CATEGORY, "symbol": symbol, "limit": "1"})
                pnl = 0.0
                try:
                    pnl = float(pnl_r["result"]["list"][0]["closedPnl"])
                except:
                    pass
                total_realized_pnl += pnl
                emoji = "🟢" if pnl >= 0 else "🔴"
                msg = (
                    f"{emoji} <b>CLOSED {info['direction'].upper()}</b>\n"
                    f"Pair     : {symbol}\n"
                    f"PnL      : {'+' if pnl>=0 else ''}{pnl:.4f} USDT\n"
                    f"Total PnL: {total_realized_pnl:+.4f} USDT"
                )
                notify(msg)
                log.info(msg.replace("<b>","").replace("</b>",""))
                closed.append(symbol)
        except:
            pass
    for s in closed:
        open_positions.pop(s, None)

# ══════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════
def run():
    log.info("🤖 Bot started")
    notify(
        "🤖 <b>Bot Trading Aktif</b>\n"
        f"Pairs    : Top 20 USDT Perp\n"
        f"Timeframe: 15m\n"
        f"Max Pos  : {MAX_POSITIONS}\n"
        f"Margin   : ${MARGIN_PER_TRADE}/trade\n"
        f"Hard Stop: -${MAX_LOSS_TOTAL}"
    )

    while True:
        try:
            # ── Hard stop check ──────────────────────
            if total_realized_pnl <= -MAX_LOSS_TOTAL:
                msg = (
                    f"🛑 <b>HARD STOP TRIGGERED</b>\n"
                    f"Total loss: ${abs(total_realized_pnl):.2f}\n"
                    f"Bot stopped. Review & restart manually."
                )
                notify(msg)
                log.critical(msg.replace("<b>","").replace("</b>",""))
                break

            # ── Sync closed positions ────────────────
            if open_positions:
                sync_closed()

            # ── Scan for new entries ─────────────────
            if count_open_positions() < MAX_POSITIONS:
                for pair in TOP_PAIRS:
                    if pair in open_positions:
                        continue
                    if count_open_positions() >= MAX_POSITIONS:
                        break
                    sig = get_signal(pair)
                    if sig:
                        log.info(f"Signal {sig.upper()} → {pair}")
                        place_order(pair, sig)
                        time.sleep(1)   # rate-limit buffer

            log.info(
                f"Scan done | Open: {count_open_positions()} "
                f"| Total PnL: {total_realized_pnl:+.4f}"
            )
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            notify("⚠️ Bot dihentikan manual.")
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
  
