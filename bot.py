#!/usr/bin/env python3
"""
Bybit USDT Perpetual Futures Scalping Bot
Strategy : Doji Candlestick Pattern Only
Timeframe: 1H | SL dinamis (low/high doji) | TP 1% fixed
Author   : Built with Claude

Logic:
- BULLISH DOJI : doji muncul setelah candle bearish → entry LONG
                 SL = low candle doji
- BEARISH DOJI : doji muncul setelah candle bullish → entry SHORT
                 SL = high candle doji
"""

import os, time, json, math, logging, base64
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

# ══════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════
API_KEY          = os.environ["BYBIT_API_KEY"]
PRIVATE_KEY_PEM  = os.environ["BYBIT_PRIVATE_KEY"].replace("\\n", "\n")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL         = "https://api.bybit.com"
CATEGORY         = "linear"
TIMEFRAME        = "60"    # 1 JAM
SCAN_INTERVAL    = 120     # scan setiap 2 menit

MARGIN_PER_TRADE = 1.5
MAX_POSITIONS    = 5
MAX_LOSS_TOTAL   = 30.0
SL_PCT_FIXED     = 0.004   # SL fixed 0.4% dari harga entry
TP_PCT           = 0.007   # TP fixed 0.7% dari harga entry
RECV_WINDOW      = "5000"

# Doji threshold
DOJI_BODY_PCT    = 0.05    # body <= 5% dari total range = doji

TOP_PAIRS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","DOGEUSDT","DOTUSDT","MATICUSDT",
    "LINKUSDT","LTCUSDT","UNIUSDT","ATOMUSDT","ETCUSDT",
    "XLMUSDT","BCHUSDT","NEARUSDT","ALGOUSDT","FILUSDT",
    "APTUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT",
    "SEIUSDT","TIAUSDT","FETUSDT","WLDUSDT","STXUSDT",
    "RUNEUSDT","AAVEUSDT","MKRUSDT","SNXUSDT","CRVUSDT",
    "LDOUSDT","RNDRUSDT","GRTUSDT","FLOWUSDT","KAVAUSDT",
]

# ══════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════
total_realized_pnl: float = 0.0
open_positions: dict = {}
_instrument_cache: dict = {}
_last_signal: dict = {}

# ══════════════════════════════════════════════════
#  RSA SIGNING
# ══════════════════════════════════════════════════
PRIVATE_KEY_OBJ = serialization.load_pem_private_key(PRIVATE_KEY_PEM.encode(), password=None)

def _sign(payload: str) -> str:
    sig = PRIVATE_KEY_OBJ.sign(payload.encode(), asym_padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()

def _build_headers(payload: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "X-BAPI-API-KEY":     API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        _sign(ts + API_KEY + RECV_WINDOW + payload),
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "Content-Type":       "application/json",
    }

# ══════════════════════════════════════════════════
#  API WRAPPERS
# ══════════════════════════════════════════════════
def api_get(endpoint, params=None):
    params = params or {}
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    try:
        r = requests.get(f"{BASE_URL}{endpoint}", params=params, headers=_build_headers(qs), timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"GET {endpoint} error: {e}")
        return {}

def api_post(endpoint, body):
    payload = json.dumps(body)
    try:
        r = requests.post(f"{BASE_URL}{endpoint}", data=payload, headers=_build_headers(payload), timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"POST {endpoint} error: {e}")
        return {}

# ══════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════
def notify(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except:
        pass

# ══════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════
def get_candles(symbol, limit=5):
    r = api_get("/v5/market/kline", {
        "category": CATEGORY, "symbol": symbol,
        "interval": TIMEFRAME, "limit": str(limit)
    })
    if r.get("retCode") != 0:
        return []
    rows = r["result"]["list"][::-1]
    return [{
        "ts":     int(row[0]),
        "open":   float(row[1]),
        "high":   float(row[2]),
        "low":    float(row[3]),
        "close":  float(row[4]),
        "volume": float(row[5]),
    } for row in rows]

def get_last_price(symbol):
    r = api_get("/v5/market/tickers", {"category": CATEGORY, "symbol": symbol})
    try:
        return float(r["result"]["list"][0]["lastPrice"])
    except:
        return 0.0

# ══════════════════════════════════════════════════
#  INSTRUMENT INFO
# ══════════════════════════════════════════════════
def get_instrument(symbol):
    if symbol not in _instrument_cache:
        r = api_get("/v5/market/instruments-info", {"category": CATEGORY, "symbol": symbol})
        try:
            _instrument_cache[symbol] = r["result"]["list"][0]
        except:
            _instrument_cache[symbol] = {}
    return _instrument_cache[symbol]

def max_leverage(symbol):
    try:
        return int(float(get_instrument(symbol)["leverageFilter"]["maxLeverage"]))
    except:
        return 50

def tick_size(symbol):
    try:
        return float(get_instrument(symbol)["priceFilter"]["tickSize"])
    except:
        return 0.01

def qty_step(symbol):
    try:
        return float(get_instrument(symbol)["lotSizeFilter"]["qtyStep"])
    except:
        return 0.001

def round_price(price, tick):
    dec = max(0, round(-math.log10(tick))) if tick < 1 else 0
    return f"{round(price / tick) * tick:.{dec}f}"

def round_qty(qty, step):
    dec = max(0, round(-math.log10(step))) if step < 1 else 0
    return f"{math.floor(qty / step) * step:.{dec}f}"

# ══════════════════════════════════════════════════
#  DOJI PATTERN
# ══════════════════════════════════════════════════
def is_doji(c) -> bool:
    """Candle doji: body <= 5% dari total range."""
    tr = c["high"] - c["low"]
    if tr == 0:
        return False
    body = abs(c["close"] - c["open"])
    return body / tr <= DOJI_BODY_PCT

def check_doji(prev, curr):
    """
    Bullish Doji : doji setelah candle bearish → Long, SL = low doji
    Bearish Doji : doji setelah candle bullish → Short, SL = high doji
    Returns (direction, sl_price, pattern) atau None
    """
    if not is_doji(curr):
        return None

    # BULLISH DOJI
    if prev["close"] < prev["open"]:
        return ("long", curr["low"], "Bullish Doji ⭐")

    # BEARISH DOJI
    if prev["close"] > prev["open"]:
        return ("short", curr["high"], "Bearish Doji ⭐")

    return None

# ══════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════
def get_signal(symbol):
    candles = get_candles(symbol, 5)
    if len(candles) < 3:
        return None, None, None

    prev = candles[-3]
    curr = candles[-2]  # candle 1H terakhir yang sudah closed

    # Cegah double entry pada candle yang sama
    candle_ts = curr["ts"]
    if _last_signal.get(symbol) == candle_ts:
        return None, None, None

    result = check_doji(prev, curr)
    if result:
        _last_signal[symbol] = candle_ts
        direction, sl_price, pattern = result
        return direction, sl_price, pattern

    return None, None, None

# ══════════════════════════════════════════════════
#  POSITION MANAGEMENT
# ══════════════════════════════════════════════════
def count_open_positions():
    r = api_get("/v5/position/list", {"category": CATEGORY, "settleCoin": "USDT"})
    try:
        return sum(1 for p in r["result"]["list"] if float(p["size"]) > 0)
    except:
        return 0

def set_leverage(symbol, lev):
    api_post("/v5/position/set-leverage", {
        "category": CATEGORY, "symbol": symbol,
        "buyLeverage": str(lev), "sellLeverage": str(lev),
    })

def place_order(symbol, direction, sl_price_raw, pattern):
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

    pos_value = MARGIN_PER_TRADE * lev
    tick    = tick_size(symbol)
    step    = qty_step(symbol)
    qty_str = round_qty(pos_value / price, step)

    # SL fixed 0.4% dari entry
    if direction == "long":
        side     = "Buy"
        sl_price = round_price(price * (1 - SL_PCT_FIXED), tick)
        tp_price = round_price(price * (1 + TP_PCT), tick)
    else:
        side     = "Sell"
        sl_price = round_price(price * (1 + SL_PCT_FIXED), tick)
        tp_price = round_price(price * (1 - TP_PCT), tick)

    # Hitung RR actual
    sl_dist = price * SL_PCT_FIXED
    tp_dist = price * TP_PCT
    rr = round(tp_dist / sl_dist, 2)

    r = api_post("/v5/order/create", {
        "category": CATEGORY, "symbol": symbol,
        "side": side, "orderType": "Market", "qty": qty_str,
        "stopLoss": sl_price, "takeProfit": tp_price,
        "tpslMode": "Full", "reduceOnly": False, "closeOnTrigger": False,
    })

    if r.get("retCode") == 0:
        open_positions[symbol] = {
            "direction": direction, "entry": price,
            "sl": sl_price, "tp": tp_price,
            "leverage": lev, "pattern": pattern,
        }
        sl_label = "-0.4%"
        msg = (
            f"✅ <b>OPEN {direction.upper()}</b>\n"
            f"Pair     : {symbol}\n"
            f"Pattern  : {pattern}\n"
            f"Entry    : {price}\n"
            f"SL       : {sl_price} ({sl_label})\n"
            f"TP       : {tp_price} (+1.0%)\n"
            f"RR       : 1:{rr}\n"
            f"Leverage : {lev}x  |  Margin: ${MARGIN_PER_TRADE}\n"
            f"Positions: {count_open_positions()}/{MAX_POSITIONS}"
        )
        notify(msg)
        log.info(msg.replace("<b>","").replace("</b>",""))
    else:
        log.error(f"Order failed {symbol}: {r}")

def sync_closed():
    global total_realized_pnl
    closed = []
    for symbol, info in open_positions.items():
        r = api_get("/v5/position/list", {"category": CATEGORY, "symbol": symbol})
        try:
            if float(r["result"]["list"][0]["size"]) == 0:
                pnl = 0.0
                try:
                    pnl_r = api_get("/v5/position/closed-pnl",
                                    {"category": CATEGORY, "symbol": symbol, "limit": "1"})
                    pnl = float(pnl_r["result"]["list"][0]["closedPnl"])
                except:
                    pass
                total_realized_pnl += pnl
                emoji = "🟢" if pnl >= 0 else "🔴"
                notify(
                    f"{emoji} <b>CLOSED {info['direction'].upper()}</b>\n"
                    f"Pair     : {symbol}\n"
                    f"Pattern  : {info.get('pattern', '-')}\n"
                    f"PnL      : {'+' if pnl>=0 else ''}{pnl:.4f} USDT\n"
                    f"Total PnL: {total_realized_pnl:+.4f} USDT"
                )
                closed.append(symbol)
        except:
            pass
    for s in closed:
        open_positions.pop(s, None)

# ══════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════
def run():
    log.info("🤖 Bot Doji 1H started")
    notify(
        "🤖 <b>Bot Trading Aktif - Doji 1H</b>\n"
        f"Strategi : Bullish & Bearish Doji\n"
        f"Timeframe: 1 Jam\n"
        f"SL       : 0.4% dari harga entry\n"
        f"TP       : 0.7% dari harga entry\n"
        f"RR       : 1:1.75\n"
        f"Pairs    : 40 USDT Perp\n"
        f"Max Pos  : {MAX_POSITIONS}\n"
        f"Margin   : ${MARGIN_PER_TRADE}/trade\n"
        f"Hard Stop: -${MAX_LOSS_TOTAL}"
    )

    while True:
        try:
            if total_realized_pnl <= -MAX_LOSS_TOTAL:
                notify(f"🛑 <b>HARD STOP TRIGGERED</b>\nTotal loss: ${abs(total_realized_pnl):.2f}\nBot stopped.")
                log.critical("HARD STOP TRIGGERED")
                break

            if open_positions:
                sync_closed()

            if count_open_positions() < MAX_POSITIONS:
                for pair in TOP_PAIRS:
                    if pair in open_positions:
                        continue
                    if count_open_positions() >= MAX_POSITIONS:
                        break
                    direction, sl_price, pattern = get_signal(pair)
                    if direction:
                        log.info(f"Signal {direction.upper()} → {pair} | {pattern}")
                        place_order(pair, direction, sl_price, pattern)
                        time.sleep(1)

            log.info(f"Scan done | Open: {count_open_positions()} | Total PnL: {total_realized_pnl:+.4f}")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            notify("⚠️ Bot dihentikan manual.")
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
