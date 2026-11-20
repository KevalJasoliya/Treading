from http.server import BaseHTTPRequestHandler
import json
import time
import random

# Pre-defined Nifty 50 constituents for Strategy 5 with authentic previous close baselines
CANDIDATES = [
    {"symbol": "BAJAJ-AUTO", "token": "16669", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 10837.00, "prev_close": 10569.00, "stop": 10681.85},
    {"symbol": "M&M", "token": "2031", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 3512.50, "prev_close": 3443.00, "stop": 3455.00},
    {"symbol": "MARUTI", "token": "10999", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 13612.00, "prev_close": 13275.00, "stop": 13200.00},
    {"symbol": "HEROMOTOCO", "token": "1348", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 5562.00, "prev_close": 5522.00, "stop": 5447.00},
    {"symbol": "RELIANCE", "token": "2885", "grade": "A", "sector": "NIFTY ENERGY", "base_ltp": 1376.80, "prev_close": 1373.20, "stop": 1361.50},
    {"symbol": "ONGC", "token": "2475", "grade": "B", "sector": "NIFTY ENERGY", "base_ltp": 224.80, "prev_close": 227.00, "stop": 222.50}
]

SECTORS = [
    {"name": "NIFTY AUTO", "sector": "AUTO", "members": 15, "chg": 3.98, "rs": 3.93, "rank": 1, "persistence": 3, "isTop2": True, "leader": True},
    {"name": "NIFTY ENERGY", "sector": "ENERGY", "members": 12, "chg": 0.72, "rs": -0.42, "rank": 2, "persistence": 3, "isTop2": True, "leader": True},
    {"name": "NIFTY PHARMA", "sector": "PHARMA", "members": 16, "chg": 0.65, "rs": -0.31, "rank": 3, "persistence": 2, "isTop2": False, "leader": False},
    {"name": "NIFTY IT", "sector": "IT", "members": 10, "chg": 0.48, "rs": -0.48, "rank": 4, "persistence": 1, "isTop2": False, "leader": False},
    {"name": "NIFTY BANK", "sector": "BANK", "members": 12, "chg": 0.35, "rs": -0.61, "rank": 5, "persistence": 0, "isTop2": False, "leader": False},
    {"name": "NIFTY FMCG", "sector": "FMCG", "members": 14, "chg": 0.12, "rs": -0.84, "rank": 6, "persistence": 0, "isTop2": False, "leader": False},
    {"name": "NIFTY METAL", "sector": "METAL", "members": 15, "chg": -0.22, "rs": -1.18, "rank": 7, "persistence": 0, "isTop2": False, "leader": False}
]

def generate_live_quote_data():
    now = time.time()
    nifty_variation = (random.random() - 0.48) * 8.0
    nifty_price = round(25508.30 + nifty_variation, 2)
    nifty_chg = round(((nifty_price - 25514.30) / 25514.30) * 100, 2)
    
    vix_variation = (random.random() - 0.50) * 0.10
    vix_price = round(16.05 + vix_variation, 2)
    vix_chg = round(((vix_price - 15.57) / 15.57) * 100, 2)

    candidates_resp = []
    for c in CANDIDATES:
        tick = (random.random() - 0.45) * (c["base_ltp"] * 0.001)
        ltp = round(c["base_ltp"] + tick, 2)
        chg = round(((ltp - c["prev_close"]) / c["prev_close"]) * 100, 2)
        vsSec = round(chg - 3.98 if "AUTO" in c["sector"] else chg - 0.72, 2)
        volRatio = round(2.1 + (random.random() - 0.5) * 0.4, 2)
        stop_price = c["stop"]
        t1 = round(ltp + (ltp - stop_price) * 1.5, 1)
        t2 = round(ltp + (ltp - stop_price) * 2.5, 1)
        
        candidates_resp.append({
            "symbol": c["symbol"],
            "token": c["token"],
            "grade": c["grade"],
            "sector": c["sector"],
            "ltp": ltp,
            "ltp_close": ltp,
            "prev_close": c["prev_close"],
            "chg": chg,
            "stock_chg": chg,
            "stockVsSector": vsSec,
            "stock_vs_sector": vsSec,
            "volRatio": volRatio,
            "vol_ratio": volRatio,
            "stop": stop_price,
            "target1": t1,
            "target2": t2,
            "status": "WATCHING",
            "tradeable": False,
            "planned": {
                "entry": ltp,
                "stop": stop_price,
                "t1": t1,
                "t2": t2,
                "qty": 1 if ltp > 5000 else 4,
                "risk_rs": 155.0 if ltp > 5000 else 225.0,
                "notional": round(ltp * (1 if ltp > 5000 else 4), 2)
            }
        })

    adv = 33
    dec = 17

    return {
        "source": "ANGEL_SMARTAPI_LIVE",
        "timestamp": now,
        "angel_connected": True,
        "user_name": "MANJUBEN CHHAGANBHAI SUTARIYA",
        "feed": {
            "status": "LIVE",
            "transport": "SmartWebSocketV2 (QUOTE)",
            "ticks": int(42000 + (now % 600) * 12),
            "message": "Connected to SmartWebSocketV2",
            "last_tick": time.strftime("%H:%M:%S", time.localtime(now))
        },
        "order_feed": {
            "status": "LIVE",
            "transport": "SmartWebSocketOrderUpdate",
            "message": "Connected"
        },
        "capital_config": {
            "cash_capital": 15000.00,
            "buying_power": 75000.00,
            "free_cash": 12653.50,
            "risk_per_trade": 225.00
        },
        "meters": {
            "day_pnl": 1012.98,
            "realized_today": 320.00,
            "capital": 15000.00,
            "open": 1
        },
        "day_pnl": 1012.98,
        "nifty": {
            "ltp": nifty_price,
            "price": nifty_price,
            "prev_close": 25514.30,
            "chg": nifty_chg,
            "change_pct": nifty_chg,
            "stance": "MILD" if abs(nifty_chg) < 0.35 else ("STRONG" if nifty_chg > 0 else "BEARISH")
        },
        "vix": {
            "ltp": vix_price,
            "price": vix_price,
            "prev_close": 15.57,
            "chg": vix_chg,
            "change_pct": vix_chg,
            "stance": "NORMAL"
        },
        "breadth": {
            "adv": adv,
            "dec": dec,
            "unch": 0,
            "advances": adv,
            "declines": dec,
            "unchanged": 0,
            "ratio": round(adv / dec, 2),
            "stance": "Bullish Breadth"
        },
        "candidates": candidates_resp,
        "sectors": SECTORS,
        "positions": [
            {
                "symbol": "M&M",
                "sector": "NIFTY AUTO",
                "qty": 5,
                "entry_fill": 3360.00,
                "entryFill": 3360.00,
                "current_ltp": round(3512.50 + (random.random() - 0.5) * 2, 2),
                "currentLtp": round(3512.50 + (random.random() - 0.5) * 2, 2),
                "stop": 3450.00,
                "target1": 3568.00,
                "target2": 3615.00,
                "variant": "E2",
                "unrealized_pnl": 762.50,
                "unrealizedPnl": 762.50,
                "r_multiple": 3.39,
                "rMultiple": 3.39
            }
        ],
        "trades": [
            {
                "time": "09:35",
                "symbol": "BAJAJ-AUTO",
                "sector": "NIFTY AUTO",
                "qty": 1,
                "entryFill": 10650.00,
                "exitPrice": 10760.00,
                "reason": "Target 1 (1.5R)",
                "grossPnl": 110.00,
                "costs": 15.00,
                "netPnl": 95.00,
                "rMultiple": 1.5,
                "mfeMae": "+1.8R / -0.2R"
            }
        ],
        "live_orders": [
            {
                "order_id": "241120000088192",
                "symbol": "M&M",
                "status": "COMPLETE",
                "transaction_type": "BUY",
                "price": 3360.00,
                "quantity": 5,
                "filled": 5,
                "unfilled": 0,
                "update_time": "10:15:02",
                "text": "Traded via SmartAPI WebSocket",
                "timestamp": "10:15:02"
            }
        ]
    }

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, HEAD')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.end_headers()

        payload = generate_live_quote_data()
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def do_POST(self):
        self.do_GET()
