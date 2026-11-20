"""
Angel One SmartAPI integration (spec section 9).

 - login with TOTP, re-login once + retry once on any auth/session error
 - instruments from OpenAPIScripMaster.json (re-downloaded daily; never hard-code tokens)
 - 08:30 batch: daily bars (EMA55, SMA200, ATR14, pivots, high20, liquidity) + 20-day same-time volume profile
 - mid-session start: backfill today's 5-minute candles once (never polled for signals)
 - live feed: SmartWebSocketV2 in QUOTE mode (has volume). REST getMarketData fallback while the socket is down.
 - prices from the socket arrive in paise: scale is verified against the previous close before use
 - the SmartApi library logs request bodies (incl. your PIN). That logging is silenced and redacted here.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

from .core import IST, ROOT, Candle, DailyStats, compute_daily_stats, cumvol_profile, load_config, sma

DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache")

# --------------------------------------------------------------------------- secrets hygiene
_SECRET_PATTERNS = [
    (re.compile(r"('password'\s*:\s*')[^']*(')"), r"\1***\2"),
    (re.compile(r"('totp'\s*:\s*')[^']*(')"), r"\1***\2"),
    (re.compile(r"('X-PrivateKey'\s*:\s*')[^']*(')"), r"\1***\2"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"('clientcode'\s*:\s*')[^']*(')"), r"\1***\2"),
    (re.compile(r"(\"password\"\s*:\s*\")[^\"]*(\")"), r"\1***\2"),
]


def redact(s: str) -> str:
    for pat, rep in _SECRET_PATTERNS:
        s = pat.sub(rep, s)
    return s


class _RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = redact(str(record.getMessage()))
            record.args = ()
        except Exception:
            pass
        return True


def _silence_smartapi_logging():
    """SmartApi writes full request headers + body (PIN, TOTP, JWT, API key) to logs/<date>/app.log on
    every error. Turn that off; our own messages below are redacted."""
    try:
        import logzero  # SmartApi uses logzero
        logzero.logfile = lambda *a, **kw: None
        logzero.loglevel(logging.CRITICAL)
        logzero.logger.addFilter(_RedactFilter())
    except Exception:
        pass
    for name in ("SmartApi", "SmartApi.smartConnect", "smartConnect", "SmartApi.smartWebSocketV2", "smartWebSocketV2",
                 "SmartApi.smartWebSocketOrderUpdate", "smartWebSocketOrderUpdate"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.CRITICAL)
        lg.addFilter(_RedactFilter())
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
        else:
            h.addFilter(_RedactFilter())


try:
    import pyotp
    from SmartApi import SmartConnect
    try:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    except Exception:  # pragma: no cover
        SmartWebSocketV2 = None
    try:
        from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
    except Exception:  # pragma: no cover
        SmartWebSocketOrderUpdate = None
    SMARTAPI_AVAILABLE = True
    _silence_smartapi_logging()
except ImportError:
    SmartConnect = None
    SmartWebSocketV2 = None
    SmartWebSocketOrderUpdate = None
    pyotp = None
    SMARTAPI_AVAILABLE = False

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

AUTH_ERRORS = {"AG8001", "AG8002", "AG8003", "AB8050", "AB8051", "AB1010", "AB1011"}
RATE_ERRORS = {"AB1004", "AB1019", "AB1021", "AB2001"}


def log(msg: str):
    print(f"{datetime.now(IST).strftime('%H:%M:%S')} [angel] {redact(msg)}", flush=True)


def load_env(path: str) -> Dict[str, str]:
    out = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def creds_ok(env: Dict[str, str]) -> bool:
    keys = ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
    return all(env.get(k) and not env[k].startswith("your_") for k in keys)


# --------------------------------------------------------------------------- rate-limited client
class AngelClient:
    def __init__(self, env: Dict[str, str]):
        self.env = env
        self.smart = None
        self.feed_token = None
        self.jwt = None
        self.user_name = ""
        self.connected = False
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._hist_times: List[float] = []

    def login(self) -> bool:
        if not SMARTAPI_AVAILABLE:
            self.last_error = "SmartApi package not installed (pip install -r requirements.txt)"
            return False
        try:
            totp = pyotp.TOTP(self.env["ANGEL_TOTP_SECRET"].replace(" ", "").upper()).now()
            smart = SmartConnect(api_key=self.env["ANGEL_API_KEY"])
            res = smart.generateSession(self.env["ANGEL_CLIENT_ID"], self.env["ANGEL_PIN"], totp)
            if res and res.get("status"):
                d = res.get("data") or {}
                self.smart = smart
                self.jwt = d.get("jwtToken")
                self.feed_token = d.get("feedToken") or smart.getfeedToken()
                self.user_name = d.get("name", "")
                self.connected = True
                self.last_error = None
                log(f"Logged in as {self.user_name.split(' ')[0] if self.user_name else 'user'}")
                return True
            self.last_error = (res or {}).get("message", "login failed")
        except Exception as e:
            self.last_error = f"login error: {type(e).__name__}: {e}"
        self.connected = False
        log(self.last_error or "login failed")
        return False

    def _call(self, fn_name: str, *args, hist: bool = False, tries: int = 6):
        """Call a SmartConnect method. Auth error → re-login once and retry once.
        Rate/network error → exponential backoff 2,4,8…60 s (spec section 9)."""
        relogged = False
        delay = 3
        for attempt in range(tries):
            if hist:
                self._throttle_hist()
            try:
                res = getattr(self.smart, fn_name)(*args)
            except Exception as e:
                res = {"status": False, "message": f"{type(e).__name__}: {e}", "errorcode": "NET"}
            if res and res.get("status"):
                return res
            code = str((res or {}).get("errorcode", ""))
            msg = str((res or {}).get("message", "empty response"))
            if (code in AUTH_ERRORS or "token" in msg.lower() and "invalid" in msg.lower()) and not relogged:
                log(f"{fn_name}: session error {code} – re-login once and retry")
                relogged = True
                if not self.login():
                    return res
                continue
            if code in RATE_ERRORS or code == "NET" or "too many" in msg.lower() or "access denied" in msg.lower():
                log(f"{fn_name}: {code} {msg} – backoff {delay}s")
                time.sleep(delay)
                delay = min(60, delay * 2)
                continue
            return res
        return None

    def _throttle_hist(self):
        """≤ 3 requests/second and ≤ 180/minute for historical candles."""
        with self._lock:
            now = time.time()
            self._hist_times = [t for t in self._hist_times if now - t < 60]
            if len(self._hist_times) >= 50:
                time.sleep(max(0.1, 60 - (now - self._hist_times[0]) + 0.1))
                now = time.time()
            if self._hist_times:
                elapsed = now - self._hist_times[-1]
                if elapsed < 1.05:
                    time.sleep(1.05 - elapsed)
            self._hist_times.append(time.time())

    def candles(self, token: str, interval: str, frm: datetime, to: datetime) -> List[list]:
        res = self._call("getCandleData", {"exchange": "NSE", "symboltoken": str(token), "interval": interval,
                                            "fromdate": frm.strftime("%Y-%m-%d %H:%M"), "todate": to.strftime("%Y-%m-%d %H:%M")},
                         hist=True, tries=1)
        return (res or {}).get("data") or []

    def quotes(self, tokens: List[str]) -> List[dict]:
        out = []
        for i in range(0, len(tokens), 50):          # max 50 tokens per request
            res = self._call("getMarketData", "FULL", {"NSE": tokens[i:i + 50]}, tries=2)
            out.extend(((res or {}).get("data") or {}).get("fetched") or [])
            time.sleep(0.25)
        return out


# --------------------------------------------------------------------------- universe
INDUSTRY_TO_SECTOR = {
    "Financial Services": "FIN SERVICE", "Information Technology": "IT", "Automobile and Auto Components": "AUTO",
    "Healthcare": "PHARMA", "Fast Moving Consumer Goods": "FMCG", "Metals & Mining": "METAL",
    "Oil Gas & Consumable Fuels": "ENERGY", "Power": "ENERGY", "Capital Goods": "CAPITAL GOODS",
    "Construction Materials": "CEMENT", "Consumer Durables": "CONSUMPTION", "Consumer Services": "CONSUMPTION",
    "Realty": "REALTY", "Telecommunication": "TELECOM", "Services": "SERVICES", "Construction": "CAPITAL GOODS",
    "Chemicals": "CHEMICALS", "Textiles": "CONSUMPTION", "Diversified": "SERVICES", "Media Entertainment & Publication": "SERVICES",
}


def read_sector_map(path: str) -> Dict[str, str]:
    out = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(line for line in f if not line.startswith("#")):
                if row.get("symbol") and row.get("sector"):
                    out[row["symbol"].strip().upper()] = row["sector"].strip().upper()
    return out


def fetch_nifty100(url: str) -> Tuple[List[dict], str]:
    """Official Nifty 100 list (Company Name, Industry, Symbol). Cached; falls back to the last good copy."""
    os.makedirs(DATA, exist_ok=True)
    cache = os.path.join(DATA, "nifty100.csv")
    txt, src = None, ""
    if requests is not None:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (QuantLeaf S5)", "Accept": "text/csv,*/*"})
            if r.status_code == 200 and "Symbol" in r.text[:500]:
                txt = r.text
                with open(cache, "w", encoding="utf-8") as f:
                    f.write(txt)
                src = "niftyindices.com (today)"
        except Exception as e:
            log(f"Nifty 100 list download failed: {e}")
    if txt is None and os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as f:
            txt = f.read()
        src = f"cached copy ({datetime.fromtimestamp(os.path.getmtime(cache)).strftime('%d %b %Y')})"
    if txt is None:
        return [], "unavailable"
    rows = [{"symbol": r["Symbol"].strip().upper(), "industry": (r.get("Industry") or "").strip()}
            for r in csv.DictReader(io.StringIO(txt)) if r.get("Symbol")]
    return rows, src


def scrip_master(url: str, day: str) -> Dict[str, str]:
    """symbol -> token for NSE cash equities ('-EQ'). Re-downloaded daily (tokens can change)."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"scrip_master_{day}.json")
    data = None
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif requests is not None:
        log("Downloading OpenAPIScripMaster.json …")
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        data = r.json()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        for old in os.listdir(CACHE):
            if old.startswith("scrip_master_") and old != os.path.basename(path):
                try:
                    os.remove(os.path.join(CACHE, old))
                except OSError:
                    pass
    out = {}
    for it in data or []:
        if it.get("exch_seg") == "NSE" and str(it.get("symbol", "")).endswith("-EQ"):
            out[it["symbol"][:-3].upper()] = str(it["token"])
    return out


def build_universe(cfg: dict, tokens: Dict[str, str]) -> Tuple[List[dict], List[str]]:
    notes = []
    smap = read_sector_map(os.path.join(ROOT, cfg["files"]["sector_map"]))
    official, src = fetch_nifty100(cfg["files"]["nifty100_url"])
    if official:
        notes.append(f"Universe: official Nifty 100 list from {src}")
        syms = official
    else:
        notes.append("Universe: official list unavailable – using sector_map.csv symbols (verify!)")
        syms = [{"symbol": s, "industry": ""} for s in smap]
    members, auto = [], []
    for r in syms:
        s = r["symbol"]
        sec = smap.get(s)
        if sec is None:
            sec = INDUSTRY_TO_SECTOR.get(r["industry"], "OTHER")
            auto.append(f"{s}→{sec}")
        tok = tokens.get(s)
        if not tok:
            notes.append(f"{s}: no NSE -EQ token in scrip master – skipped")
            continue
        members.append({"symbol": s, "token": tok, "sector": sec})
    if auto:
        notes.append("Auto-mapped from NSE industry (add to sector_map.csv to fix): " + ", ".join(auto))
    return members, notes


def read_blocks(cfg: dict, day: str) -> Dict[str, str]:
    out = {}
    p = os.path.join(ROOT, cfg["files"]["event_blocks"])
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            for row in csv.DictReader(line for line in f if line.strip() and not line.startswith("#")):
                if (row.get("date") or "").strip() == day:
                    out[(row.get("symbol") or "").strip().upper()] = "event block: " + (row.get("reason") or "").strip()
    p = os.path.join(ROOT, cfg["files"]["surveillance_exclude"])
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip().upper()
                if s and not s.startswith("#"):
                    out[s] = "ASM/GSM / circuit change"
    return out


# --------------------------------------------------------------------------- batch
def _parse_ts(s: str) -> datetime:
    # "2026-11-18T09:15:00+05:30" → naive IST (spec: strip the timezone)
    return datetime.fromisoformat(s[:19])


def daily_batch(client: AngelClient, cfg: dict, day: str, members: List[dict], progress=None) -> Tuple[Dict[str, DailyStats], dict, List[str]]:
    """Section 3. Cached per day under data/cache/<day>/ so a restart is instant."""
    C = cfg["C"]
    d = date.fromisoformat(day)
    cdir = os.path.join(CACHE, day)
    os.makedirs(cdir, exist_ok=True)
    stats: Dict[str, DailyStats] = {}
    notes: List[str] = []
    to_dt = datetime.combine(d - timedelta(days=1), datetime.min.time()).replace(hour=15, minute=30)

    def daily_bars(token: str, key: str) -> List[list]:
        p = os.path.join(cdir, f"{key}_day.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        raw = client.candles(token, "ONE_DAY", to_dt - timedelta(days=430), to_dt)
        bars = [[r[0][:10], r[1], r[2], r[3], r[4], r[5]] for r in raw if r and r[0][:10] < day]
        with open(p, "w") as f:
            json.dump(bars, f)
        return bars

    def five_min_days(token: str, key: str) -> Dict[str, List[Tuple[str, float]]]:
        p = os.path.join(cdir, f"{key}_5m.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        raw = client.candles(token, "FIVE_MINUTE", to_dt.replace(hour=9, minute=15) - timedelta(days=32), to_dt)
        days: Dict[str, List[Tuple[str, float]]] = {}
        for r in raw:
            t = _parse_ts(r[0])
            if t.date().isoformat() >= day:
                continue
            days.setdefault(t.date().isoformat(), []).append((t.strftime("%H:%M"), float(r[5])))
        with open(p, "w") as f:
            json.dump(days, f)
        return days

    # Nifty: prevClose and market_ok = prevClose > SMA50
    nb = daily_bars(cfg["index_tokens"]["NIFTY"], "NIFTY")
    ncl = [float(b[4]) for b in nb]
    s50 = sma(ncl, C["mkt_sma"])
    nifty = {"prev_close": ncl[-1] if ncl else None, "sma50": s50,
             "market_ok": bool(ncl and s50 and ncl[-1] > s50), "last_date": nb[-1][0] if nb else None}
    vb = daily_bars(cfg["index_tokens"]["INDIAVIX"], "INDIAVIX")
    nifty["vix_prev_close"] = float(vb[-1][4]) if vb else None
    if not ncl:
        notes.append("Nifty daily bars missing – no entries possible today")
    for i, m in enumerate(members):
        if progress:
            progress(i, len(members), m["symbol"])
        try:
            bars = daily_bars(m["token"], m["symbol"])
            st = compute_daily_stats(m["symbol"], bars, cfg)
            if st is None:
                notes.append(f"{m['symbol']}: not enough daily bars")
                continue
            days = five_min_days(m["token"], m["symbol"])
            st.cumvol20, st.cumvol_days = cumvol_profile(days, C["min_candles_per_day"], C["vol_days"])
            if st.cumvol_days < C["vol_days"]:
                notes.append(f"{m['symbol']}: volume baseline uses {st.cumvol_days} full days (<{C['vol_days']})")
            if st.overnight_flag:
                notes.append(f"{m['symbol']}: overnight move > 20% – review split/bonus adjustment")
            stats[m["symbol"]] = st
        except Exception as e:
            notes.append(f"{m['symbol']}: batch error {e}")
    return stats, nifty, notes


def backfill_today(client: AngelClient, cfg: dict, day: str, members: List[dict], now: datetime, progress=None) -> Dict[str, List[Candle]]:
    """Engine started after 09:15: load today's CLOSED 5-minute candles once."""
    cdir = os.path.join(CACHE, day, "today_5m")
    os.makedirs(cdir, exist_ok=True)
    out: Dict[str, List[Candle]] = {}
    frm = now.replace(hour=9, minute=15, second=0, microsecond=0).replace(tzinfo=None)
    to = now.replace(tzinfo=None)
    toks = [("NIFTY", cfg["index_tokens"]["NIFTY"])] + [(m["symbol"], m["token"]) for m in members]
    consecutive_errors = 0
    for i, (sym, tok) in enumerate(toks):
        if consecutive_errors >= 3:
            log("Historical endpoint rate-limited; pausing backfill to protect live WebSocket.")
            break
        if progress:
            progress(i, len(toks), sym)
        p = os.path.join(cdir, f"{sym}.json")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    rows = json.load(f)
                out[sym] = [Candle(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
                continue
            except Exception:
                pass
        raw = client.candles(tok, "FIVE_MINUTE", frm, to)
        if not raw:
            log("Historical candle endpoint throttled by broker – building candles from live feed.")
            break
        cs = []
        for r in raw:
            t = _parse_ts(r[0])
            if t.date().isoformat() != day or t + timedelta(minutes=5) > to:
                continue  # drop the still-forming candle
            cs.append(Candle(t.strftime("%H:%M"), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        out[sym] = cs
        if cs and now.strftime("%H:%M") >= "15:30":
            try:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump([(c.ts, c.o, c.h, c.l, c.c, c.v) for c in cs], f)
            except Exception:
                pass
    return out


# --------------------------------------------------------------------------- order updates feed
class OrderFeed:
    """Live order status stream via Angel One SmartWebSocketOrderUpdate.
    Streams order placement, modifications, execution (fills), rejections, and cancellations in real-time.
    """
    def __init__(self, client: AngelClient, engine):
        self.client = client
        self.engine = engine
        self.ws = None
        self.ws_up = False
        self.stop_flag = False
        self.ws_thread = None

    def _on_open(self, wsapp):
        self.ws_up = True
        self.engine.order_feed_status = {"status": "LIVE", "transport": "SmartWebSocketOrderUpdate", "message": "Connected"}
        log("Order WebSocket open – streaming live order status updates")

    def _on_data(self, wsapp, message, data_type, continue_flag):
        self._on_message(wsapp, message)

    def _on_message(self, wsapp, message):
        try:
            if isinstance(message, (bytes, bytearray)):
                message = message.decode("utf-8", errors="ignore")
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                except Exception:
                    data = {"raw": message}
            elif isinstance(message, dict):
                data = message
            else:
                return

            # Check if this is a heartbeat / pong or system control
            if data == "ping" or data == "pong":
                return
            if isinstance(data, dict):
                if data.get("status-code") == "200" and not data.get("orderData"):
                    return
            self.engine.on_order_update(data)
        except Exception as e:
            log(f"order update parse error: {e}")

    def _on_error(self, wsapp, err=None, *a):
        log(f"Order WebSocket error: {err}")
        self.ws_up = False
        self.engine.order_feed_status = {"status": "ERROR", "transport": "SmartWebSocketOrderUpdate", "message": str(err)}

    def _on_close(self, wsapp, *a):
        if self.ws_up:
            log("Order WebSocket closed")
        self.ws_up = False
        self.engine.order_feed_status = {"status": "DISCONNECTED", "transport": "SmartWebSocketOrderUpdate", "message": "Closed"}

    def _loop(self):
        delay = 2
        while not self.stop_flag:
            if not SMARTAPI_AVAILABLE or SmartWebSocketOrderUpdate is None:
                self.engine.order_feed_status = {"status": "UNAVAILABLE", "transport": "None", "message": "Library not found"}
                return
            try:
                self.ws = SmartWebSocketOrderUpdate(
                    self.client.jwt,
                    self.client.env["ANGEL_API_KEY"],
                    self.client.env["ANGEL_CLIENT_ID"],
                    self.client.feed_token
                )
                self.ws.on_open = self._on_open
                self.ws.on_data = self._on_data
                self.ws.on_message = self._on_message
                self.ws.on_error = self._on_error
                self.ws.on_close = self._on_close
                self.ws.connect()
            except Exception as e:
                log(f"Order WebSocket connect failed: {e}")
            self.ws_up = False
            if self.stop_flag:
                break
            time.sleep(delay)
            delay = min(30, delay * 2)

    def fetch_initial_orders(self):
        """Initial snapshot of today's orders from broker orderBook REST API."""
        try:
            if self.client and self.client.smart:
                res = self.client.smart.orderBook()
                if res and res.get("status") and res.get("data"):
                    for ord_raw in res.get("data", []):
                        self.engine.on_order_update({"orderData": ord_raw}, is_snapshot=True)
        except Exception as e:
            log(f"Failed to fetch initial orderBook: {e}")

    def _poll_orders_loop(self):
        while not self.stop_flag:
            time.sleep(10)
            self.fetch_initial_orders()

    def start(self):
        self.fetch_initial_orders()
        self.ws_thread = threading.Thread(target=self._loop, daemon=True, name="order_ws")
        self.ws_thread.start()
        threading.Thread(target=self._poll_orders_loop, daemon=True, name="order_poll").start()

    def stop(self):
        self.stop_flag = True
        try:
            if self.ws:
                self.ws.close_connection()
        except Exception:
            pass


# --------------------------------------------------------------------------- live feed
class LiveFeed:
    """WebSocket QUOTE feed with automatic reconnect; REST polling only while the socket is down."""

    def __init__(self, client: AngelClient, engine, tokens: List[str], prev_close: Dict[str, float]):
        self.client = client
        self.engine = engine
        self.tokens = tokens
        self.prev_close = prev_close          # token -> previous close (for paise scale check)
        self.ws = None
        self.ws_up = False
        self.stop_flag = False
        self.scale: Optional[float] = None
        self._last_rest: Dict[str, tuple] = {}
        self.ws_thread = None
        self.order_feed = OrderFeed(client, engine)

    # ----- price scale (spec: "prices arrive as integers in paise – verify against a known quote")
    def _price(self, raw, token) -> Optional[float]:
        if raw is None:
            return None
        raw = float(raw)
        if self.scale is None:
            pc = self.prev_close.get(token)
            if pc:
                cand = {100.0: abs(raw / 100.0 / pc - 1), 1.0: abs(raw / pc - 1)}
                best = min(cand, key=cand.get)
                if cand[best] < 0.3:
                    self.scale = best
                    log(f"WebSocket price scale verified: ÷{int(best)} (token {token})")
            if self.scale is None:
                return None
        return raw / self.scale

    def _on_data(self, wsapp, msg):
        try:
            if not isinstance(msg, dict):
                return
            tok = str(msg.get("token", "")).strip("\x00").strip()
            ltp = self._price(msg.get("last_traded_price"), tok)
            if ltp is None:
                return
            ts_ms = msg.get("exchange_timestamp")
            ts = datetime.fromtimestamp(ts_ms / 1000.0, IST) if ts_ms else datetime.now(IST)
            self.engine.on_tick(
                tok, ts, ltp,
                cum_vol=msg.get("volume_trade_for_the_day"),
                day_open=self._price(msg.get("open_price_of_the_day"), tok) or None,
                day_high=self._price(msg.get("high_price_of_the_day"), tok) or None,
                day_low=self._price(msg.get("low_price_of_the_day"), tok) or None,
                prev_close=self._price(msg.get("closed_price"), tok) or None,
                atp=self._price(msg.get("average_traded_price"), tok) or None,
            )
        except Exception as e:
            log(f"tick parse error: {e}")

    def _on_open(self, wsapp):
        self.ws_up = True
        self._had_open = True
        self.engine.transport = "WebSocket (QUOTE)"
        self.engine.feed_message = "Connected to SmartWebSocketV2"
        log(f"WebSocket open – subscribing {len(self.tokens)} tokens in QUOTE mode")
        self.ws.subscribe("s5", 2, [{"exchangeType": 1, "tokens": self.tokens}])

    def _on_close(self, wsapp, *a):
        if self.ws_up:
            log("WebSocket closed")
        self.ws_up = False

    def _on_error(self, wsapp, err=None, *a):
        log(f"WebSocket error: {err}")
        self.ws_up = False

    def _ws_loop(self):
        delay = 2
        while not self.stop_flag:
            if SmartWebSocketV2 is None:
                self.engine.feed_message = "SmartWebSocketV2 unavailable – using REST polling"
                return
            self._had_open = False
            try:
                self.ws = SmartWebSocketV2(self.client.jwt, self.client.env["ANGEL_API_KEY"],
                                           self.client.env["ANGEL_CLIENT_ID"], self.client.feed_token)
                self.ws.on_open = self._on_open
                self.ws.on_data = self._on_data
                self.ws.on_error = self._on_error
                self.ws.on_close = self._on_close
                self.ws.connect()           # blocks until the socket closes
            except Exception as e:
                log(f"WebSocket connect failed: {e}")
            self.ws_up = False
            if self.stop_flag:
                break
            delay = 2 if self._had_open else min(60, delay * 2)
            self.engine.feed_message = f"WebSocket down – reconnecting in {delay}s (REST fallback active)"
            time.sleep(delay)
            if delay >= 16:
                self.client.login()          # session may have expired

    def _rest_loop(self):
        while not self.stop_flag:
            if self.ws_up:
                time.sleep(1)
                continue
            phase = self.engine.session_phase(self.engine.clock())
            if phase in ("PRE-OPEN", "CLOSED"):
                time.sleep(5)
                continue
            self.engine.transport = "REST polling (fallback)"
            try:
                for q in self.client.quotes(self.tokens):
                    self._rest_tick(q)
            except Exception as e:
                log(f"REST quote error: {e}")
            time.sleep(1)

    def _rest_tick(self, q: dict):
        tok = str(q.get("symbolToken"))
        ltp = float(q.get("ltp") or 0)
        vol = q.get("tradeVolume")
        ft = q.get("exchFeedTime") or ""
        key = (ft, ltp, vol)
        if self._last_rest.get(tok) == key:
            return    # nothing new from the exchange → NOT counted as a fresh tick (honest staleness)
        self._last_rest[tok] = key
        try:
            ts = datetime.strptime(ft, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except Exception:
            ts = datetime.now(IST)
        self.engine.on_tick(tok, ts, ltp, cum_vol=float(vol) if vol is not None else None,
                            day_open=float(q.get("open") or 0) or None, day_high=float(q.get("high") or 0) or None,
                            day_low=float(q.get("low") or 0) or None, prev_close=float(q.get("close") or 0) or None,
                            atp=float(q.get("avgPrice") or 0) or None)

    def snapshot_once(self):
        """One REST snapshot (e.g. weekend / after hours) so the board shows last prices."""
        try:
            for q in self.client.quotes(self.tokens):
                self._rest_tick(q)
        except Exception as e:
            log(f"snapshot error: {e}")

    def start(self):
        self.ws_thread = threading.Thread(target=self._ws_loop, daemon=True, name="ws")
        self.ws_thread.start()
        threading.Thread(target=self._rest_loop, daemon=True, name="rest").start()
        self.order_feed.start()

    def stop(self):
        self.stop_flag = True
        try:
            self.order_feed.stop()
        except Exception:
            pass
        try:
            if self.ws:
                self.ws.close_connection()
        except Exception:
            pass
