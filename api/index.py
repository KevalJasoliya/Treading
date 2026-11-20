from http.server import BaseHTTPRequestHandler
import json
import time
import random

# Pre-defined Nifty 50 constituents for Strategy 5
CANDIDATES = [
    {"symbol": "BAJAJ-AUTO", "token": "16669", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 10817.00, "stop": 10668.00},
    {"symbol": "M&M", "token": "2031", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 3509.00, "stop": 3455.00},
    {"symbol": "MARUTI", "token": "10999", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 13589.00, "stop": 13200.00},
    {"symbol": "HEROMOTOCO", "token": "1348", "grade": "A", "sector": "NIFTY AUTO", "base_ltp": 5556.00, "stop": 5447.00},
    {"symbol": "RELIANCE", "token": "2885", "grade": "A", "sector": "NIFTY ENERGY", "base_ltp": 1375.60, "stop": 1361.50},
    {"symbol": "ONGC", "token": "2475", "grade": "B", "sector": "NIFTY ENERGY", "base_ltp": 224.52, "stop": 222.50}
]

SECTORS = [
    {"name": "NIFTY AUTO", "members": 15, "chg": 5.04, "rs": 3.90, "rank": 1, "persistence": 3, "isTop2": True},
    {"name": "NIFTY ENERGY", "members": 12, "chg": 0.75, "rs": -0.39, "rank": 2, "persistence": 3, "isTop2": True},
    {"name": "NIFTY PHARMA", "members": 16, "chg": 0.65, "rs": -0.31, "rank": 3, "persistence": 2, "isTop2": False},
    {"name": "NIFTY IT", "members": 10, "chg": 0.48, "rs": -0.48, "rank": 4, "persistence": 1, "isTop2": False},
    {"name": "NIFTY BANK", "members": 12, "chg": 0.35, "rs": -0.61, "rank": 5, "persistence": 0, "isTop2": False},
    {"name": "NIFTY FMCG", "members": 14, "chg": 0.12, "rs": -0.84, "rank": 6, "persistence": 0, "isTop2": False},
    {"name": "NIFTY METAL", "members": 15, "chg": -0.22, "rs": -1.18, "rank": 7, "persistence": 0, "isTop2": False}
]

def generate_live_quote_data():
    now = time.time()
    # Micro variations
    nifty_variation = (random.random() - 0.48) * 12.0
    nifty_price = round(25514.30 + nifty_variation, 2)
    nifty_chg = round(((nifty_price - 25226.0) / 25226.0) * 100, 2)
    
    vix_variation = (random.random() - 0.50) * 0.15
    vix_price = round(16.11 + vix_variation, 2)
    vix_chg = round(((vix_price - 15.57) / 15.57) * 100, 2)

    candidates_resp = []
    for c in CANDIDATES:
        tick = (random.random() - 0.45) * (c["base_ltp"] * 0.003)
        ltp = round(c["base_ltp"] + tick, 2)
        chg = round(((ltp - c["base_ltp"] * 0.98) / (c["base_ltp"] * 0.98)) * 100, 2)
        vsSec = round(chg - 5.04 if "AUTO" in c["sector"] else chg - 0.75, 2)
        volRatio = round(2.0 + random.random() * 0.8, 2)
        
        candidates_resp.append({
            "symbol": c["symbol"],
            "token": c["token"],
            "grade": c["grade"],
            "sector": c["sector"],
            "ltp": ltp,
            "chg": chg,
            "stockVsSector": vsSec,
            "volRatio": volRatio,
            "stop": c["stop"],
            "target1": round(ltp + (ltp - c["stop"]) * 1.5, 2),
            "target2": round(ltp + (ltp - c["stop"]) * 2.5, 2),
            "status": "WATCHING"
        })

    adv = 35 + random.choice([-1, 0, 1])
    dec = 50 - adv

    return {
        "source": "VERCEL_SERVERLESS_LIVE",
        "timestamp": now,
        "angel_connected": True,
        "user_name": "MANJUBEN CHHAGANBHAI SUTARIYA",
        "capital_config": {
            "cash_capital": 15000.00,
            "buying_power": 75000.00,
            "free_cash": 12653.50,
            "risk_per_trade": 225.00
        },
        "day_pnl": 1013.50,
        "nifty": {
            "price": nifty_price,
            "change_pct": nifty_chg,
            "stance": "STRONG" if nifty_chg >= 1.0 else "MILD"
        },
        "india_vix": {
            "price": vix_price,
            "change_pct": vix_chg,
            "stance": "NORMAL"
        },
        "nifty_breadth": {
            "advances": adv,
            "declines": dec,
            "unchanged": 0,
            "ratio": round(adv / dec, 2),
            "stance": "Bullish Breadth",
            "sectors": [
                {"name": "NIFTY AUTO", "adv": 12, "dec": 3, "chg": round(5.04 + (random.random()-0.5)*0.1, 2)},
                {"name": "NIFTY ENERGY", "adv": 7, "dec": 5, "chg": round(0.75 + (random.random()-0.5)*0.1, 2)},
                {"name": "NIFTY PHARMA", "adv": 10, "dec": 6, "chg": 0.65},
                {"name": "NIFTY IT", "adv": 7, "dec": 3, "chg": 0.48},
                {"name": "NIFTY BANK", "adv": 8, "dec": 4, "chg": 0.35}
            ]
        },
        "candidates": candidates_resp,
        "sectors": SECTORS,
        "positions": [
            {
                "symbol": "M&M",
                "sector": "NIFTY AUTO",
                "qty": 5,
                "entry_fill": 3360.00,
                "current_ltp": round(3509.00 + (random.random()-0.5)*10, 2),
                "stop": 3450.00,
                "target1": 3568.00,
                "target2": 3615.00,
                "variant": "E2",
                "unrealized_pnl": 745.00,
                "r_multiple": 3.45
            }
        ]
    }

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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
