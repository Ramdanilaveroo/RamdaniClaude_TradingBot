#!/usr/bin/env python3
"""
Bybit USDT Perpetual Futures Bot
Strategy : Doji Trend Continuation
Timeframe: 1H
Author   : Built with Claude

Logic:
- LONG  : candle sebelumnya hijau + doji muncul
          → pasang limit BUY  di HIGH doji (ekor atas)
          → kalau harga naik tembus high doji → terisi → Long
          → SL = low doji | TP = 0.7%

- SHORT : candle sebelumnya merah + doji muncul
          → pasang limit SELL di LOW doji (ekor bawah)
          → kalau harga turun tembus low doji → terisi → Short
          → SL = high doji | TP = 0.7%

Order type: Limit (lebih murah fee, entry lebih presisi)
Pending order dibatalkan kalau candle 1H berikutnya close tanpa terisi
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
TP_PCT           = 0.007   # TP 0.7%
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
open_positions: dict  = {}   # symbol → posisi aktif
pending_orders: dict  = {}   # symbol → pending limit order
_instrument_cache: dict = {}
_last_signal: dict    = {}   # symbol → candle ts yang udah diproses

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
#  DOJI DETECTION
# ══════════════════════════════════════════════════
def is_doji(c) -> bool:
    tr = c["high"] - c["low"]
    if tr == 0:
        return False
    return abs(c["close"] - c["open"]) / tr <= DOJI_BODY_PCT

def check_doji_signal(prev, doji):
    """
    Long  : prev hijau + doji → entry limit di HIGH doji
    Short : prev merah + doji → entry limit di LOW doji
    Returns (direction, entry_price, sl_price) atau None
    """
    if not is_doji(doji):
        return None

    # LONG: prev hijau → entry di HIGH doji
    if prev["close"] > prev["open"]:
        entry_price = doji["high"]
        sl_price    = doji["low"]
        return ("long", entry_price, sl_price)

    # SHORT: prev merah → entry di LOW doji
    if prev["close"] < prev["open"]:
        entry_price = doji["low"]
        sl_price    = doji["high"]
        return ("short", entry_price, sl_price)

    return None

# ══════════════════════════════════════════════════
#  ORDER MANAGEMENT
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

def place_limit_order(symbol, direction, entry_price, sl_price_raw):
    """Pasang limit order di high/low doji."""
    if symbol in pending_orders or symbol in open_positions:
        return
    if count_open_positions() >= MAX_POSITIONS:
        return

    lev = max_leverage(symbol)
    set_leverage(symbol, lev)
    time.sleep(0.3)

    pos_value = MARGIN_PER_TRADE * lev
    tick    = tick_size(symbol)
    step    = qty_step(symbol)
    qty_str = round_qty(pos_value / entry_price, step)

    entry_str = round_price(entry_price, tick)
    sl_str    = round_price(sl_price_raw, tick)

    if direction == "long":
        side     = "Buy"
        tp_str   = round_price(entry_price * (1 + TP_PCT), tick)
    else:
        side     = "Sell"
        tp_str   = round_price(entry_price * (1 - TP_PCT), tick)

    # Hitung RR
    sl_dist = abs(entry_price - sl_price_raw)
    tp_dist = entry_price * TP_PCT
    rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0

    r = api_post("/v5/order/create", {
        "category":   CATEGORY,
        "symbol":     symbol,
        "side":       side,
        "orderType":  "Limit",
        "qty":        qty_str,
        "price":      entry_str,
        "stopLoss":   sl_str,
        "takeProfit": tp_str,
        "tpslMode":   "Full",
        "timeInForce": "GTC",   # Good Till Cancel
        "reduceOnly": False,
        "closeOnTrigger": False,
    })

    if r.get("retCode") == 0:
        order_id = r["result"]["orderId"]
        pending_orders[symbol] = {
            "orderId":   order_id,
            "direction": direction,
            "entry":     entry_price,
            "sl":        sl_str,
            "tp":        tp_str,
            "leverage":  lev,
        }
        sl_label = "low doji" if direction == "long" else "high doji"
        msg = (
            f"⏳ <b>PENDING {direction.upper()}</b>\n"
            f"Pair     : {symbol}\n"
            f"Pattern  : Doji Trend Continuation\n"
            f"Entry    : {entry_str} ({'ekor atas' if direction == 'long' else 'ekor bawah'} doji)\n"
            f"SL       : {sl_str} ({sl_label})\n"
            f"TP       : {tp_str} (+0.7%)\n"
            f"RR       : 1:{rr}\n"
            f"Leverage : {lev}x  |  Margin: ${MARGIN_PER_TRADE}"
        )
        notify(msg)
        log.info(msg.replace("<b>","").replace("</b>",""))
    else:
        log.error(f"Limit order failed {symbol}: {r}")

def cancel_order(symbol, order_id):
    """Batalkan pending order yang belum terisi."""
    r = api_post("/v5/order/cancel", {
        "category": CATEGORY,
        "symbol":   symbol,
        "orderId":  order_id,
    })
    return r.get("retCode") == 0

def check_order_status(symbol, order_id):
    """Cek status order: 'filled', 'cancelled', 'pending'."""
    r = api_get("/v5/order/realtime", {
        "category": CATEGORY,
        "symbol":   symbol,
        "orderId":  order_id,
    })
    try:
        status = r["result"]["list"][0]["orderStatus"]
        if status == "Filled":
            return "filled"
        elif status in ("Cancelled", "Rejected", "Deactivated"):
            return "cancelled"
        return "pending"
    except:
        return "cancelled"

# ══════════════════════════════════════════════════
#  SYNC POSITIONS & ORDERS
# ══════════════════════════════════════════════════
def sync_pending_orders(current_candle_ts: dict):
    """
    Cek pending orders:
    - Kalau terisi → pindah ke open_positions
    - Kalau candle baru sudah close tapi belum terisi → cancel
    """
    to_remove = []
    for symbol, info in pending_orders.items():
        status = check_order_status(symbol, info["orderId"])

        if status == "filled":
            # Order terisi → posisi aktif
            open_positions[symbol] = info
            to_remove.append(symbol)
            msg = (
                f"✅ <b>OPEN {info['direction'].upper()} (Terisi!)</b>\n"
                f"Pair     : {symbol}\n"
                f"Entry    : {info['entry']}\n"
                f"SL       : {info['sl']}\n"
                f"TP       : {info['tp']}\n"
                f"Leverage : {info['leverage']}x"
            )
            notify(msg)
            log.info(f"Order filled: {symbol}")

        elif status == "cancelled":
            to_remove.append(symbol)
            log.info(f"Order cancelled: {symbol}")

        else:
            # Masih pending — cek apakah candle baru sudah muncul
            candle_ts = current_candle_ts.get(symbol, 0)
            if candle_ts and candle_ts != info.get("candle_ts", 0):
                # Candle baru sudah close → cancel order lama
                if cancel_order(symbol, info["orderId"]):
                    notify(f"❌ <b>CANCEL</b> {symbol} — candle baru terbentuk, order tidak terisi")
                    log.info(f"Order cancelled (new candle): {symbol}")
                to_remove.append(symbol)

    for s in to_remove:
        pending_orders.pop(s, None)

def sync_closed_positions():
    """Sync posisi yang sudah closed."""
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
                    f"PnL      : {'+' if pnl>=0 else ''}{pnl:.4f} USDT\n"
                    f"Total PnL: {total_realized_pnl:+.4f} USDT"
                )
                closed.append(symbol)
        except:
            pass
    for s in closed:
        open_positions.pop(s, None)

# ══════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════
def get_signal(symbol):
    candles = get_candles(symbol, 5)
    if len(candles) < 3:
        return None, None, None

    prev = candles[-3]
    doji = candles[-2]  # candle 1H terakhir yang sudah closed

    # Cegah double entry pada candle yang sama
    if _last_signal.get(symbol) == doji["ts"]:
        return None, None, None

    result = check_doji_signal(prev, doji)
    if result:
        _last_signal[symbol] = doji["ts"]
        direction, entry_price, sl_price = result
        return direction, entry_price, sl_price

    return None, None, None

# ══════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════
def run():
    log.info("🤖 Bot Doji Trend Continuation 1H started")
    notify(
        "🤖 <b>Bot Trading Aktif - Doji Trend</b>\n"
        f"Strategi : Doji Trend Continuation\n"
        f"Timeframe: 1 Jam\n"
        f"Entry    : Limit di ekor atas/bawah doji\n"
        f"SL       : Low/High doji\n"
        f"TP       : 0.7% dari entry\n"
        f"Pairs    : 40 USDT Perp\n"
        f"Max Pos  : {MAX_POSITIONS}\n"
        f"Margin   : ${MARGIN_PER_TRADE}/trade\n"
        f"Hard Stop: -${MAX_LOSS_TOTAL}"
    )

    while True:
        try:
            # Hard stop check
            if total_realized_pnl <= -MAX_LOSS_TOTAL:
                notify(f"🛑 <b>HARD STOP TRIGGERED</b>\nTotal loss: ${abs(total_realized_pnl):.2f}\nBot stopped.")
                log.critical("HARD STOP TRIGGERED")
                break

            # Sync closed positions
            if open_positions:
                sync_closed_positions()

            # Cek candle timestamp terbaru untuk cancel logic
            current_candle_ts = {}
            for pair in TOP_PAIRS:
                if pair in pending_orders:
                    candles = get_candles(pair, 3)
                    if candles:
                        current_candle_ts[pair] = candles[-2]["ts"]
                    time.sleep(0.2)

            # Sync pending orders
            if pending_orders:
                sync_pending_orders(current_candle_ts)

            # Scan sinyal baru
            total_active = count_open_positions() + len(pending_orders)
            if total_active < MAX_POSITIONS:
                for pair in TOP_PAIRS:
                    if pair in open_positions or pair in pending_orders:
                        continue
                    if count_open_positions() + len(pending_orders) >= MAX_POSITIONS:
                        break

                    direction, entry_price, sl_price = get_signal(pair)
                    if direction:
                        log.info(f"Doji signal {direction.upper()} → {pair} | Entry: {entry_price}")
                        place_limit_order(pair, direction, entry_price, sl_price)
                        time.sleep(1)

            log.info(
                f"Scan done | Open: {count_open_positions()} "
                f"| Pending: {len(pending_orders)} "
                f"| Total PnL: {total_realized_pnl:+.4f}"
            )
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
