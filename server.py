#!/usr/bin/env python3
"""
QuantLeaf · Strategy 5 · Sector Leaders Intraday – server

    python3 server.py              # LIVE Angel One data, PAPER trading (needs .env + pip install -r requirements.txt)
    python3 server.py --demo       # simulated market (for testing, weekends, after hours)
    python3 server.py --demo --demo-start 09:40 --demo-speed 20

Open http://127.0.0.1:8081
Security: listens on 127.0.0.1 only (not your Wi-Fi network). Only index.html and /api/* are served –
.env, logs and data files can never be downloaded.
"""
import argparse
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, date, timedelta

from s5.core import IST, ROOT, load_config, Candle
from s5.engine import Engine, Store, DATA
from s5 import angel

INDEX = os.path.join(ROOT, "index.html")


# --------------------------------------------------------------------------- runners
def run_scheduler(engine: Engine):
    while True:
        try:
            engine.step()
        except Exception:
            traceback.print_exc()
        time.sleep(0.25)


def run_demo(cfg, args) -> Engine:
    from s5.demo import DemoMarket, DemoClock, DemoFeed, demo_day
    day = demo_day(datetime.now(IST).date())
    clock = DemoClock(day, args.demo_start, args.demo_speed)
    demo_dir = os.path.join(DATA, "demo")
    if os.path.isdir(demo_dir):
        for f in ("state.json", "trades.jsonl"):
            try:
                os.remove(os.path.join(demo_dir, f))
            except OSError:
                pass
    engine = Engine(cfg, clock.now, "DEMO", Store(demo_dir), wall=clock.wall)
    mkt = DemoMarket(cfg, day)
    engine.load_universe(day.isoformat(), mkt.members, mkt.stats, mkt.nifty_stats, {})
    engine.batch_info = {"status": "ready", "date": day.isoformat(), "stocks": len(mkt.stats),
                         "notes": ["DEMO: simulated universe and prices"], "universe_source": "sector_map.csv (demo)"}
    feed = DemoFeed(mkt, engine, clock)
    if clock.now().strftime("%H:%M") > "09:15":
        feed.backfill(clock.now())
        engine.alert("info", "Demo: morning candles generated up to the start time (no trades on old candles).")
    threading.Thread(target=feed.run, daemon=True, name="demo").start()
    threading.Thread(target=run_scheduler, args=(engine,), daemon=True, name="sched").start()
    return engine


def run_live(cfg, env) -> Engine:
    engine = Engine(cfg, lambda: datetime.now(IST), "LIVE")
    client = angel.AngelClient(env)
    state = {"feed": None}

    def prepare(day: str):
        engine.feed_message = "Logging in to Angel One…"
        while not client.login():
            engine.feed_message = f"Login failed: {client.last_error} – retrying in 30 s"
            time.sleep(30)
        engine.feed_message = "Downloading instruments…"
        tokens = angel.scrip_master(cfg["files"]["scrip_master_url"], day)
        members, notes = angel.build_universe(cfg, tokens)

        def progress(i, n, sym):
            engine.batch_info = {"status": "running", "date": day, "progress": f"{i}/{n}", "current": sym}
        engine.feed_message = f"08:30 batch: daily data for {len(members)} stocks…"
        stats, nifty, bnotes = angel.daily_batch(client, cfg, day, members, progress)
        blocked = angel.read_blocks(cfg, day)
        engine.load_universe(day, members, stats, nifty, blocked)
        engine.user_name = client.user_name
        engine.batch_info = {"status": "ready", "date": day, "stocks": len(stats), "of": len(members),
                             "nifty_market_ok": nifty.get("market_ok"), "notes": (notes + bnotes)[:80]}
        toks = [m["token"] for m in members] + [cfg["index_tokens"]["NIFTY"], cfg["index_tokens"]["INDIAVIX"]]
        pcs = {m["token"]: stats[m["symbol"]].prev_close for m in members if m["symbol"] in stats}
        if nifty.get("prev_close"):
            pcs[cfg["index_tokens"]["NIFTY"]] = nifty["prev_close"]
        if state["feed"]:
            state["feed"].stop()
        feed = angel.LiveFeed(client, engine, toks, pcs)
        state["feed"] = feed
        feed.snapshot_once()
        feed.start()
        engine.feed_message = "Live feed running"

        now = datetime.now(IST)
        if now.weekday() < 5 and now.strftime("%H:%M") > "09:15":
            cutoff = min(now, now.replace(hour=15, minute=30, second=0, microsecond=0))
            def bf_progress(i, n, sym):
                engine.feed_message = f"Live feed running · Backfilling {i}/{n} {sym}"
            try:
                bf = angel.backfill_today(client, cfg, day, members, cutoff, progress=bf_progress)
                with engine.lock:
                    for sym, cs in bf.items():
                        ins = engine.nifty if sym == "NIFTY" else engine.instruments.get(sym)
                        if ins is not None and cs:
                            ins.load_candles(cs)
                    engine.replay_closes()
                engine.alert("info", f"Backfilled today's candles for {len(bf)} symbols (sector persistence rebuilt; no trades on old candles).")
            except Exception as e:
                engine.alert("warn", f"Backfill skipped ({e}) – building candles from live feed.")
            engine.feed_message = "Live feed running"

    def daily_loop():
        saved_for = None
        while True:
            try:
                now = datetime.now(IST)
                day = now.date().isoformat()
                if engine.day != day and (engine.day is None or now.strftime("%H:%M") >= "08:30"):
                    prepare(day)
                if now.strftime("%H:%M") >= "15:35" and saved_for != day and engine.day == day:
                    save_candles(engine)
                    saved_for = day
            except Exception as e:
                engine.feed_message = f"Error preparing the day: {e} – retrying in 60 s"
                traceback.print_exc()
                time.sleep(60)
            time.sleep(5)

    threading.Thread(target=daily_loop, daemon=True, name="daily").start()
    threading.Thread(target=run_scheduler, args=(engine,), daemon=True, name="sched").start()
    return engine


def save_candles(engine: Engine):
    """Spec section 13: store all live candles daily (data/candles/<day>.csv)."""
    with engine.lock:
        p = os.path.join(DATA, "candles", f"{engine.day}.csv")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("symbol,ts,o,h,l,c,v\n")
            for ins in [engine.nifty] + list(engine.instruments.values()):
                for c in ins.candles:
                    f.write(f"{ins.symbol},{engine.day} {c.ts},{c.o},{c.h},{c.l},{c.c},{int(c.v)}\n")
    print(f"Saved candles → {p}")


# --------------------------------------------------------------------------- HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    engine: Engine = None
    server_version = "QuantLeafS5"

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
            if any(p in msg for p in ("/api/s5/state", "/api/s5/chart", "/api/s5/candidates", "/api/s5/market/live-quote")):
                return
            sys.stderr.write("%s - %s\n" % (self.address_string(), msg))
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_HEAD(self):
        return self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        e = self.engine
        try:
            if u.path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path in ("/api/s5/state", "/api/s5/market/live-quote"):
                return self._json(e.snapshot())
            if u.path == "/api/s5/sectors":
                return self._json(e.snapshot()["sectors"])
            if u.path == "/api/s5/candidates":
                return self._json(e.snapshot()["candidates"])
            if u.path == "/api/s5/chart":
                return self._json(e.chart((q.get("symbol") or [""])[0]))
            if u.path == "/api/s5/signals":
                d = (q.get("date") or [e.day or ""])[0]
                if not (len(d) == 10 and d[4] == "-" and d[7] == "-"):
                    return self._json({"error": "date=YYYY-MM-DD"}, 400)
                return self._json(e.store.read_lines(os.path.join("signals", f"{d}.jsonl")))
            if u.path == "/api/s5/trades":
                frm = (q.get("from") or ["0000-00-00"])[0]
                to = (q.get("to") or ["9999-99-99"])[0]
                return self._json([t for t in e.store.read_lines("trades.jsonl") if frm <= t.get("date", "") <= to])
        except Exception as ex:
            traceback.print_exc()
            return self._json({"error": str(ex)}, 500)
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except Exception:
            data = {}
        # owner-only actions: must come from this computer, same origin, with a written reason
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._json({"error": "owner actions only from this computer"}, 403)
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._json({"error": "cross-site request refused"}, 403)
        reason = str(data.get("reason", "")).strip()
        if u.path in ("/api/s5/halt-clear", "/api/s5/halt"):
            if len(reason) < 5:
                return self._json({"error": "a written reason (≥ 5 characters) is required"}, 400)
            res = self.engine.halt_clear(reason) if u.path.endswith("clear") else self.engine.halt(reason)
            return self._json(res)
        self._send(404, b'{"error":"not found"}')


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="QuantLeaf Strategy 5 server")
    ap.add_argument("--demo", action="store_true", help="simulated market instead of Angel One")
    ap.add_argument("--demo-speed", type=float, default=8.0, help="demo clock speed (default 8× real time)")
    ap.add_argument("--demo-start", default="09:14:50", help="demo start time HH:MM[:SS]")
    ap.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (this computer only)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8081)))
    args = ap.parse_args()

    cfg = load_config()
    env = angel.load_env(os.path.join(ROOT, ".env"))
    if args.demo:
        engine = run_demo(cfg, args)
        print("▶ DEMO mode – simulated prices (not real market data)")
    elif not angel.SMARTAPI_AVAILABLE:
        print("✗ SmartApi not installed. Run:  pip install -r requirements.txt   (or start with --demo)")
        sys.exit(1)
    elif not angel.creds_ok(env):
        print("✗ Angel One credentials missing in .env (copy .env.example → .env).  Or start with --demo")
        sys.exit(1)
    else:
        engine = run_live(cfg, env)
        print("▶ LIVE Angel One data · PAPER trading (no real orders)")

    Handler.engine = engine
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠ Listening on {args.host}: anyone on your network can open the dashboard.")
    srv = ThreadedServer((args.host, args.port), Handler)
    print(f"🚀 QuantLeaf S5 dashboard → http://127.0.0.1:{args.port}")
    print(f"   Profile '{cfg['profile_name']}' · capital ₹{cfg['P']['capital']:,} · risk/trade ₹{cfg['P']['capital']*cfg['P']['risk_pct_A']:,.0f} · gross cap ₹{cfg['P']['capital']*cfg['P']['gross_cap']:,.0f}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
        srv.shutdown()


if __name__ == "__main__":
    main()
