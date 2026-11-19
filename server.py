#!/usr/bin/env python3
"""
QuantLeaf Strategy 5 - Sector Leaders Intraday Server
Capital Model: ₹15,000 Cash · 5x MIS Intraday Margin (₹75,000 Purchasing Power) · Pure Cash Equity (No F&O)
Live Feed: Angel One SmartAPI
"""

import http.server
import socketserver
import json
import urllib.parse
import os
import sys
import time
import threading
import base64
import hashlib
import hmac
import struct
import random

try:
    import pyotp
    from SmartApi import SmartConnect
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False
    SmartConnect = None
    pyotp = None

PORT = 8081
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(DIRECTORY, ".env")

def load_env():
    creds = {
        "ANGEL_API_KEY": "",
        "ANGEL_CLIENT_ID": "",
        "ANGEL_PIN": "",
        "ANGEL_TOTP_SECRET": ""
    }
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k in creds:
                            creds[k] = v
        except Exception as e:
            print("Error loading .env:", e)
    return creds

CREDENTIALS = load_env()

def get_totp(secret: str) -> str:
    if pyotp:
        try:
            return pyotp.TOTP(secret.strip().replace(" ", "").upper()).now()
        except Exception:
            pass
    clean = secret.replace(" ", "").upper()
    pad = len(clean) % 8
    if pad:
        clean += "=" * (8 - pad)
    key = base64.b32decode(clean, True)
    counter = int(time.time() // 30)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o+4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"

class SmartAPIManager:
    def __init__(self):
        self.smart = None
        self.is_connected = False
        self.last_login_time = 0
        self.last_quote_time = 0
        self.last_error = None
        self.user_name = ""

    def connect(self, api_key=None, client_id=None, pin=None, totp_secret=None):
        if api_key: CREDENTIALS["ANGEL_API_KEY"] = api_key
        if client_id: CREDENTIALS["ANGEL_CLIENT_ID"] = client_id
        if pin: CREDENTIALS["ANGEL_PIN"] = pin
        if totp_secret: CREDENTIALS["ANGEL_TOTP_SECRET"] = totp_secret

        ak = CREDENTIALS.get("ANGEL_API_KEY")
        cid = CREDENTIALS.get("ANGEL_CLIENT_ID")
        p = CREDENTIALS.get("ANGEL_PIN")
        sec = CREDENTIALS.get("ANGEL_TOTP_SECRET")

        if not (ak and cid and p and sec):
            self.is_connected = False
            self.last_error = "Incomplete Angel One credentials"
            return False, self.last_error

        if not SMARTAPI_AVAILABLE:
            self.is_connected = False
            self.last_error = "SmartApi package not available"
            return False, self.last_error

        try:
            totp = get_totp(sec)
            self.smart = SmartConnect(api_key=ak)
            res = self.smart.generateSession(cid, p, totp)
            if res and res.get("status"):
                self.is_connected = True
                self.last_login_time = time.time()
                self.last_error = None
                self.user_name = res.get("data", {}).get("name", "")
                print(f"✓ Angel One SmartAPI Connected for {self.user_name} ({cid})")
                return True, "Connected successfully"
            else:
                self.is_connected = False
                self.last_error = res.get("message", "Login failed") if res else "No response"
                print(f"✗ Angel One Login Error: {self.last_error}")
                return False, self.last_error
        except Exception as e:
            self.is_connected = False
            self.last_error = str(e)
            print(f"✗ Angel One Exception: {e}")
            return False, str(e)

    def fetch_quotes(self, tokens):
        if not self.is_connected or not self.smart:
            return None, "Not connected"
        try:
            res = self.smart.getMarketData("FULL", {"NSE": tokens})
            if res and res.get("status"):
                self.last_quote_time = time.time()
                return res.get("data", {}).get("fetched", []), None
            else:
                if res and ("token" in str(res).lower() or "session" in str(res).lower()):
                    print("Session expired, reconnecting...")
                    self.connect()
                return None, res.get("message", "Quote fetch failed") if res else "Empty response"
        except Exception as e:
            return None, str(e)

api_manager = SmartAPIManager()

# Token mappings
TOKEN_MAP = {
    "99926000": {"symbol": "NIFTY50", "name": "NIFTY 50", "type": "index"},
    "99926017": {"symbol": "India VIX", "name": "INDIA VIX", "type": "index"},
    "16669": {"symbol": "BAJAJ-AUTO", "name": "BAJAJ-AUTO-EQ", "sector": "NIFTY AUTO"},
    "2031": {"symbol": "M&M", "name": "M&M-EQ", "sector": "NIFTY AUTO"},
    "10999": {"symbol": "MARUTI", "name": "MARUTI-EQ", "sector": "NIFTY AUTO"},
    "1348": {"symbol": "HEROMOTOCO", "name": "HEROMOTOCO-EQ", "sector": "NIFTY AUTO"},
    "2885": {"symbol": "RELIANCE", "name": "RELIANCE-EQ", "sector": "NIFTY ENERGY"},
    "2475": {"symbol": "ONGC", "name": "ONGC-EQ", "sector": "NIFTY ENERGY"},
    "11630": {"symbol": "NTPC", "name": "NTPC-EQ", "sector": "NIFTY ENERGY"},
    "526": {"symbol": "BPCL", "name": "BPCL-EQ", "sector": "NIFTY ENERGY"}
}

MARKET_STATE = {
    "nifty": {
        "price": 25471.80,
        "prev_close": 25227.95,
        "change_pct": 0.97,
        "stance": "Strong",
        "market_ok": True
    },
    "india_vix": {
        "price": 12.84,
        "prev_close": 13.15,
        "change_pct": -2.36,
        "stance": "Normal"
    },
    "nifty_breadth": {
        "advances": 34,
        "declines": 16,
        "unchanged": 0,
        "ratio": 2.13,
        "stance": "Bullish Breadth"
    },
    "tokens": {},
    "last_update": time.time(),
    "source": "SIMULATED"
}

# Capital Specification: ₹15,000 Cash · 5x Intraday Equity Margin
CAPITAL_CONFIG = {
    "cash_capital": 15000.00,
    "margin_multiplier": 5.0,
    "buying_power": 75000.00,
    "risk_pct_grade_a": 0.015,     # 1.5% = ₹225
    "risk_pct_grade_b": 0.010,     # 1.0% = ₹150
    "risk_rs_grade_a": 225.00,
    "risk_rs_grade_b": 150.00,
    "day_stop_loss": -450.00,      # -3.0% of cash capital
    "week_stop_loss": -900.00,     # -6.0% of cash capital
    "max_concurrent_positions": 2
}

# Live Candidates sized specifically for ₹15k Cash & 5x MIS Margin
CANDIDATES = [
    {
        "symbol": "BAJAJ-AUTO",
        "token": "16669",
        "grade": "A",
        "sector": "NIFTY AUTO",
        "ltp": 10782.00,
        "prev_close": 10570.00,
        "chg": 2.01,
        "stockVsSector": -2.38,
        "volRatio": 2.25,
        "rangePos": 0.86,
        "atrMove": 0.68,
        "planned_qty": 1,
        "margin_req": 2156.40,
        "plannedRisk": 117.00,
        "risk_rs": 117.00,
        "planned_entry": 10785.00,
        "stop": 10668.00,
        "target1": 10965.00,
        "target2": 11085.00,
        "rr_ratio": "1 : 2.5",
        "status": "WATCHING"
    },
    {
        "symbol": "M&M",
        "token": "2031",
        "grade": "A",
        "sector": "NIFTY AUTO",
        "ltp": 3498.70,
        "prev_close": 3359.10,
        "chg": 4.16,
        "stockVsSector": -0.23,
        "volRatio": 2.85,
        "rangePos": 0.94,
        "atrMove": 0.78,
        "planned_qty": 5,
        "margin_req": 3498.70,
        "plannedRisk": 43.70,
        "risk_rs": 218.50,
        "planned_entry": 3498.00,
        "stop": 3455.00,
        "target1": 3568.00,
        "target2": 3615.00,
        "rr_ratio": "1 : 2.5",
        "status": "WATCHING"
    },
    {
        "symbol": "MARUTI",
        "token": "10999",
        "grade": "A",
        "sector": "NIFTY AUTO",
        "ltp": 13433.00,
        "prev_close": 12632.00,
        "chg": 6.34,
        "stockVsSector": 1.95,
        "volRatio": 3.40,
        "rangePos": 0.92,
        "atrMove": 0.85,
        "planned_qty": 1,
        "margin_req": 2686.60,
        "plannedRisk": 155.00,
        "risk_rs": 155.00,
        "planned_entry": 13435.00,
        "stop": 13280.00,
        "target1": 13670.00,
        "target2": 13825.00,
        "rr_ratio": "1 : 2.5",
        "status": "WATCHING"
    },
    {
        "symbol": "HEROMOTOCO",
        "token": "1348",
        "grade": "A",
        "sector": "NIFTY AUTO",
        "ltp": 5517.50,
        "prev_close": 5253.00,
        "chg": 5.04,
        "stockVsSector": 0.65,
        "volRatio": 2.60,
        "rangePos": 0.95,
        "atrMove": 0.75,
        "planned_qty": 3,
        "margin_req": 3310.50,
        "plannedRisk": 72.50,
        "risk_rs": 217.50,
        "planned_entry": 5520.00,
        "stop": 5447.50,
        "target1": 5635.00,
        "target2": 5710.00,
        "rr_ratio": "1 : 2.5",
        "status": "WATCHING"
    },
    {
        "symbol": "RELIANCE",
        "token": "2885",
        "grade": "A",
        "sector": "NIFTY ENERGY",
        "ltp": 1375.10,
        "prev_close": 1363.00,
        "chg": 0.89,
        "stockVsSector": 0.20,
        "volRatio": 2.10,
        "rangePos": 0.87,
        "atrMove": 0.62,
        "planned_qty": 15,
        "margin_req": 4125.30,
        "plannedRisk": 14.50,
        "risk_rs": 217.50,
        "planned_entry": 1376.00,
        "stop": 1361.50,
        "target1": 1399.00,
        "target2": 1416.00,
        "rr_ratio": "1 : 2.6",
        "status": "WATCHING"
    },
    {
        "symbol": "ONGC",
        "token": "2475",
        "grade": "B",
        "sector": "NIFTY ENERGY",
        "ltp": 225.18,
        "prev_close": 225.00,
        "chg": 0.08,
        "stockVsSector": -0.61,
        "volRatio": 1.45,
        "rangePos": 0.87,
        "atrMove": 0.45,
        "planned_qty": 55,
        "margin_req": 2476.98,
        "plannedRisk": 2.68,
        "risk_rs": 147.40,
        "planned_entry": 225.20,
        "stop": 222.50,
        "target1": 229.30,
        "target2": 232.00,
        "rr_ratio": "1 : 2.5",
        "status": "WATCHING"
    }
]

# Active position sized for ₹15k Cash & 5x Margin
POSITIONS = [
    {
        "id": "POS-001",
        "symbol": "M&M",
        "token": "2031",
        "sector": "NIFTY AUTO",
        "qty": 5,
        "entry_fill": 3360.00,
        "current_ltp": 3498.70,
        "stop": 3450.00,
        "target1": 3568.00,
        "target2": 3615.00,
        "variant": "E2",
        "unrealized_pnl": round((3498.70 - 3360.00) * 5, 2),
        "r_multiple": round(((3498.70 - 3360.00) * 5) / 225.0, 2),
        "margin_blocked": round((3360.00 * 5) / 5, 2),
        "rr_ratio": "1 : 2.5"
    }
]

SECTORS = [
    {"name": "NIFTY AUTO", "members": 15, "chg": 4.39, "rs": 3.42, "rank": 1, "persistence": 3, "isTop2": True},
    {"name": "NIFTY ENERGY", "members": 12, "chg": 0.69, "rs": -0.28, "rank": 2, "persistence": 3, "isTop2": True},
    {"name": "NIFTY PHARMA", "members": 16, "chg": 0.65, "rs": -0.31, "rank": 3, "persistence": 2, "isTop2": False},
    {"name": "NIFTY IT", "members": 10, "chg": 0.48, "rs": -0.48, "rank": 4, "persistence": 1, "isTop2": False},
    {"name": "NIFTY BANK", "members": 12, "chg": 0.35, "rs": -0.61, "rank": 5, "persistence": 0, "isTop2": False},
    {"name": "NIFTY FMCG", "members": 14, "chg": 0.12, "rs": -0.84, "rank": 6, "persistence": 0, "isTop2": False},
    {"name": "NIFTY METAL", "members": 15, "chg": -0.22, "rs": -1.18, "rank": 7, "persistence": 0, "isTop2": False}
]

# Realized Morning Profit from closed trade
REALIZED_PNL_TODAY = 320.00

def background_quote_worker():
    time.sleep(1.0)
    if SMARTAPI_AVAILABLE and CREDENTIALS.get("ANGEL_API_KEY"):
        api_manager.connect()

    tokens_list = list(TOKEN_MAP.keys())

    while True:
        try:
            if api_manager.is_connected:
                quotes, err = api_manager.fetch_quotes(tokens_list)
                if quotes and not err:
                    MARKET_STATE["source"] = "ANGEL_SMARTAPI_LIVE"
                    auto_chgs = []
                    energy_chgs = []

                    for q in quotes:
                        t = str(q.get("symbolToken"))
                        ltp = float(q.get("ltp") or 0.0)
                        prev_close = float(q.get("close") or ltp)
                        chg_pct = float(q.get("percentChange") or 0.0)

                        MARKET_STATE["tokens"][t] = {
                            "ltp": ltp,
                            "close": prev_close,
                            "chg_pct": chg_pct,
                            "high": float(q.get("high") or ltp),
                            "low": float(q.get("low") or ltp),
                            "volume": q.get("tradeVolume", 0)
                        }

                        if t == "99926000":
                            MARKET_STATE["nifty"]["price"] = ltp
                            MARKET_STATE["nifty"]["prev_close"] = prev_close
                            MARKET_STATE["nifty"]["change_pct"] = chg_pct
                            if chg_pct >= 0.40:
                                MARKET_STATE["nifty"]["stance"] = "Strong"
                                adv = min(46, max(28, int(25 + chg_pct * 9)))
                            elif chg_pct >= 0.0:
                                MARKET_STATE["nifty"]["stance"] = "Mild"
                                adv = min(35, max(26, int(25 + chg_pct * 8)))
                            elif chg_pct >= -0.35:
                                MARKET_STATE["nifty"]["stance"] = "Neutral"
                                adv = max(18, int(25 + chg_pct * 8))
                            else:
                                MARKET_STATE["nifty"]["stance"] = "Bearish"
                                adv = max(8, int(25 + chg_pct * 9))
                            dec = 50 - adv
                            ratio = round(adv / max(1, dec), 2)
                            MARKET_STATE["nifty_breadth"] = {
                                "advances": adv,
                                "declines": dec,
                                "unchanged": 0,
                                "ratio": ratio,
                                "stance": "Bullish Breadth" if adv >= dec else "Bearish Breadth"
                            }
                        elif t == "99926017":
                            MARKET_STATE["india_vix"]["price"] = ltp
                            MARKET_STATE["india_vix"]["prev_close"] = prev_close
                            MARKET_STATE["india_vix"]["change_pct"] = chg_pct
                            if ltp < 13.0: MARKET_STATE["india_vix"]["stance"] = "Low Vol"
                            elif ltp <= 17.0: MARKET_STATE["india_vix"]["stance"] = "Normal"
                            elif ltp <= 22.0: MARKET_STATE["india_vix"]["stance"] = "Elevated"
                            else: MARKET_STATE["india_vix"]["stance"] = "High Risk"

                        if t in ["16669", "2031", "10999", "1348"]:
                            auto_chgs.append(chg_pct)
                        elif t in ["2885", "2475", "11630", "526"]:
                            energy_chgs.append(chg_pct)

                        for c in CANDIDATES:
                            if c.get("token") == t:
                                c["ltp"] = ltp
                                c["prev_close"] = prev_close
                                c["chg"] = chg_pct
                                high = float(q.get("high") or ltp)
                                low = float(q.get("low") or ltp)
                                rng = max(high - low, 1.0)
                                c["rangePos"] = round(min(max((ltp - low) / rng, 0.0), 1.0), 2)
                                max_risk_allowed = 225.0
                                stop_price = c.get("stop", ltp * 0.988)
                                risk_per_share = ltp - stop_price
                                if risk_per_share > max_risk_allowed or risk_per_share <= 0:
                                    risk_per_share = max_risk_allowed
                                    stop_price = round(ltp - max_risk_allowed, 2)
                                    c["stop"] = stop_price
                                risk_safe_qty = max(1, int(max_risk_allowed / risk_per_share))
                                max_margin_qty = max(1, int(37500.0 / ltp))
                                c["planned_qty"] = min(risk_safe_qty, max_margin_qty)
                                c["margin_req"] = round((ltp * c["planned_qty"]) / 5.0, 2)
                                c["risk_rs"] = round(min(c["planned_qty"] * risk_per_share, max_risk_allowed), 2)
                                c["target1"] = round(ltp + (risk_per_share * 1.5), 1)
                                c["target2"] = round(ltp + (risk_per_share * 2.5), 1)

                        for p in POSITIONS:
                            if p.get("token") == t:
                                p["current_ltp"] = ltp
                                p["unrealized_pnl"] = round((ltp - p["entry_fill"]) * p["qty"], 2)
                                p["r_multiple"] = round(p["unrealized_pnl"] / 225.0, 2)

                    nifty_chg = MARKET_STATE["nifty"]["change_pct"]
                    if auto_chgs:
                        avg_auto = round(sum(auto_chgs) / len(auto_chgs), 2)
                        SECTORS[0]["chg"] = avg_auto
                        SECTORS[0]["rs"] = round(avg_auto - nifty_chg, 2)
                        for c in CANDIDATES:
                            if c["sector"] == "NIFTY AUTO":
                                c["stockVsSector"] = round(c["chg"] - avg_auto, 2)

                    if energy_chgs:
                        avg_energy = round(sum(energy_chgs) / len(energy_chgs), 2)
                        SECTORS[1]["chg"] = avg_energy
                        SECTORS[1]["rs"] = round(avg_energy - nifty_chg, 2)
                        for c in CANDIDATES:
                            if c["sector"] == "NIFTY ENERGY":
                                c["stockVsSector"] = round(c["chg"] - avg_energy, 2)

                    MARKET_STATE["last_update"] = time.time()
                else:
                    if err: print("Quote fetch warning:", err)
            else:
                MARKET_STATE["source"] = "SIMULATED_TICK"
                for c in CANDIDATES:
                    delta = (random.random() - 0.49) * 0.4
                    c["ltp"] = round(c["ltp"] + delta, 2)
                    c["chg"] = round(((c["ltp"] - c["prev_close"]) / c["prev_close"]) * 100, 2)
                MARKET_STATE["last_update"] = time.time()
        except Exception as e:
            print("Quote loop error:", e)

        time.sleep(1.8)

threading.Thread(target=background_quote_worker, daemon=True).start()

# HTTP Request Handler
class SmartAPIHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-PrivateKey")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/s5/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            if path == "/api/s5/market/live-quote":
                unrealized = sum(p["unrealized_pnl"] for p in POSITIONS)
                total_day_pnl = round(unrealized + REALIZED_PNL_TODAY, 2)
                total_equity = round(CAPITAL_CONFIG["cash_capital"] + total_day_pnl, 2)
                margin_blocked = sum(p["margin_blocked"] for p in POSITIONS)
                free_cash = round(CAPITAL_CONFIG["cash_capital"] - margin_blocked + total_day_pnl, 2)

                resp = {
                    "source": MARKET_STATE["source"],
                    "timestamp": MARKET_STATE["last_update"],
                    "angel_connected": api_manager.is_connected,
                    "user_name": api_manager.user_name,
                    "capital_config": {
                        "cash_capital": CAPITAL_CONFIG["cash_capital"],
                        "margin_multiplier": CAPITAL_CONFIG["margin_multiplier"],
                        "buying_power": CAPITAL_CONFIG["buying_power"],
                        "risk_per_trade": CAPITAL_CONFIG["risk_rs_grade_a"],
                        "day_stop_loss": CAPITAL_CONFIG["day_stop_loss"],
                        "margin_blocked": margin_blocked,
                        "free_cash": free_cash
                    },
                    "nifty": MARKET_STATE["nifty"],
                    "india_vix": MARKET_STATE["india_vix"],
                    "nifty_breadth": MARKET_STATE.get("nifty_breadth", {
                        "advances": 34,
                        "declines": 16,
                        "unchanged": 0,
                        "ratio": 2.13,
                        "stance": "Bullish Breadth"
                    }),
                    "candidates": CANDIDATES,
                    "positions": POSITIONS,
                    "sectors": SECTORS,
                    "day_pnl": total_day_pnl,
                    "equity": total_equity
                }
                self.wfile.write(json.dumps(resp).encode("utf-8"))
            elif path == "/api/s5/angel/status":
                status_resp = {
                    "connected": api_manager.is_connected,
                    "user_name": api_manager.user_name,
                    "last_error": api_manager.last_error,
                    "last_login_time": api_manager.last_login_time,
                    "last_quote_time": api_manager.last_quote_time,
                    "api_key_configured": bool(CREDENTIALS.get("ANGEL_API_KEY")),
                    "client_id": CREDENTIALS.get("ANGEL_CLIENT_ID", "")
                }
                self.wfile.write(json.dumps(status_resp).encode("utf-8"))
            elif path == "/api/s5/sectors":
                self.wfile.write(json.dumps(SECTORS).encode("utf-8"))
            elif path == "/api/s5/candidates":
                self.wfile.write(json.dumps(CANDIDATES).encode("utf-8"))
            elif path == "/api/s5/state":
                unrealized = sum(p["unrealized_pnl"] for p in POSITIONS)
                total_day_pnl = round(unrealized + REALIZED_PNL_TODAY, 2)
                resp = {
                    "mode": "PAPER",
                    "capital": 15000.0,
                    "buying_power": 75000.0,
                    "angel_connected": api_manager.is_connected,
                    "source": MARKET_STATE["source"],
                    "nifty": MARKET_STATE["nifty"],
                    "india_vix": MARKET_STATE["india_vix"],
                    "nifty_breadth": MARKET_STATE.get("nifty_breadth", {
                        "advances": 34,
                        "declines": 16,
                        "unchanged": 0,
                        "ratio": 2.13,
                        "stance": "Bullish Breadth"
                    }),
                    "day_pnl": total_day_pnl,
                    "equity": round(15000.0 + total_day_pnl, 2)
                }
                self.wfile.write(json.dumps(resp).encode("utf-8"))
            else:
                self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return

        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if path == "/api/s5/angel/connect":
            api_key = data.get("api_key")
            client_id = data.get("client_id")
            pin = data.get("pin")
            totp_secret = data.get("totp_secret")
            save_env = data.get("save_to_env", True)

            ok, msg = api_manager.connect(api_key, client_id, pin, totp_secret)
            if ok and save_env:
                try:
                    with open(ENV_PATH, "w", encoding="utf-8") as f:
                        f.write(f"ANGEL_API_KEY={api_key}\n")
                        f.write(f"ANGEL_CLIENT_ID={client_id}\n")
                        f.write(f"ANGEL_PIN={pin}\n")
                        f.write(f"ANGEL_TOTP_SECRET={totp_secret}\n")
                except Exception as e:
                    print("Could not save .env:", e)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": ok,
                "message": msg,
                "connected": api_manager.is_connected,
                "user_name": api_manager.user_name
            }).encode("utf-8"))
            return

        elif path == "/api/s5/halt-clear":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "cleared", "timestamp": time.time()}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

def run():
    server = ThreadedHTTPServer(("0.0.0.0", PORT), SmartAPIHandler)
    print(f"🚀 QuantLeaf S5 Server running at http://0.0.0.0:{PORT}")
    print(f"💰 Account: Cash Capital ₹15,000 | 5x MIS Intraday Buying Power ₹75,000")
    print(f"📡 Angel One SmartAPI Feed configured for Client: {CREDENTIALS.get('ANGEL_CLIENT_ID')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()

if __name__ == "__main__":
    run()
