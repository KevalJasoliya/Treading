"""
Strategy 5 engine: owns the live state and runs the rules on every candle close / tick.
Feeds (Angel One live, or the demo generator) push ticks in; the HTTP server reads snapshot().
PAPER mode only: fills are simulated (spec section 10). LIVE orders are locked until the proof gate passes.
"""
from __future__ import annotations

import csv
import json
import math
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, date, timedelta
from typing import Callable, Dict, List, Optional

from .core import (IST, ROOT, Candle, CHECK_NAMES, DailyStats, Instrument, Position, add_minutes, bucket_start,
                   close_summary, costs, evaluate_checks, hhmm, manage_position, plan_levels, r2, room_and_reward,
                   sector_strength, size, stance_label, trend_pass)

DATA = os.path.join(ROOT, "data")


def _ensure(p):
    os.makedirs(p, exist_ok=True)
    return p


class Store:
    """Very small file store: JSON state + JSON-lines journals under ./data."""

    def __init__(self, root: str = DATA):
        self.root = _ensure(root)
        _ensure(os.path.join(root, "signals"))
        _ensure(os.path.join(root, "features"))
        _ensure(os.path.join(root, "candles"))

    def path(self, *p):
        return os.path.join(self.root, *p)

    def load_state(self) -> dict:
        try:
            with open(self.path("state.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_state(self, st: dict):
        tmp = self.path("state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, default=str)
        os.replace(tmp, self.path("state.json"))

    def append(self, name: str, row: dict):
        with open(self.path(name), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def read_lines(self, name: str) -> List[dict]:
        out = []
        try:
            with open(self.path(name), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
        except FileNotFoundError:
            pass
        return out

    def feature_row(self, day: str, row: dict):
        p = self.path("features", f"{day}.csv")
        new = not os.path.exists(p)
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)


class Engine:
    def __init__(self, cfg: dict, clock: Callable[[], datetime], source: str, store: Optional[Store] = None,
                 wall: Callable[[], float] = time.time):
        self.cfg = cfg
        self.C = cfg["C"]
        self.P = cfg["P"]
        self.K = cfg["costs"]
        self.clock = clock
        self.wall = wall
        self.source = source            # "LIVE" | "DEMO"
        self.mode = "PAPER"
        self.lock = threading.RLock()
        self.store = store or Store()
        self.alerts: List[dict] = []
        self.transport = "—"
        self.feed_message = "Starting…"
        self.batch_info: dict = {"status": "pending"}
        self.reset_day(None)
        st = self.store.load_state()
        self.equity_peak = float(st.get("equity_peak", self.P["capital"]))
        self.account_halt = st.get("account_halt")      # {"reason":..., "since":...} or None
        self.manual_halt = st.get("manual_halt")

    # ------------------------------------------------------------------ setup
    def reset_day(self, day: Optional[str]):
        self.day = day
        self.instruments: Dict[str, Instrument] = {}
        self.by_token: Dict[str, Instrument] = {}
        self.stats: Dict[str, DailyStats] = {}
        self.sector_of: Dict[str, str] = {}
        self.universe_count: Dict[str, int] = {}
        self.tradeable: Dict[str, str] = {}            # symbol -> "" if ok else blocked reason
        self.nifty: Optional[Instrument] = None
        self.vix: Optional[Instrument] = None
        self.nifty_stats: dict = {}
        self.sector_hist: Dict[str, List[bool]] = {}
        self.sector_rows: List[dict] = []
        self.last_close: Optional[str] = None
        self.closes_done: set = set()
        self.evals: Dict[str, dict] = {}
        self.status: Dict[str, dict] = {}               # symbol -> {state, reason, time}
        self.pending: Dict[str, dict] = {}
        self.positions: Dict[str, Position] = {}
        self.closed_today: List[dict] = []
        self.skipped: List[dict] = []
        self.traded_today: set = set()
        self.entries_today = 0
        self.day_halt: Optional[str] = None
        self.time_exit_done = False
        self.ticks = 0
        self.last_tick_wall = 0.0
        self.stale_flattened = False

    def load_universe(self, day: str, members: List[dict], stats: Dict[str, DailyStats], nifty_stats: dict,
                      blocked: Dict[str, str]):
        """members: [{symbol, token, sector}] ; stats from the 08:30 batch ; blocked: symbol -> reason."""
        with self.lock:
            self.reset_day(day)
            for m in members:
                ins = Instrument(m["symbol"], str(m["token"]), m["sector"])
                st = stats.get(m["symbol"])
                if st:
                    ins.prev_close = st.prev_close
                self.instruments[m["symbol"]] = ins
                self.by_token[ins.token] = ins
                self.sector_of[m["symbol"]] = m["sector"]
                self.universe_count[m["sector"]] = self.universe_count.get(m["sector"], 0) + 1
            self.stats = stats
            tk = self.cfg["index_tokens"]
            self.nifty = Instrument("NIFTY 50", tk["NIFTY"], None, True)
            self.nifty.prev_close = nifty_stats.get("prev_close")
            self.by_token[self.nifty.token] = self.nifty
            self.vix = Instrument("INDIA VIX", tk["INDIAVIX"], None, True)
            self.vix.prev_close = nifty_stats.get("vix_prev_close")
            self.by_token[self.vix.token] = self.vix
            self.nifty_stats = nifty_stats
            for sym in self.instruments:
                self.tradeable[sym] = self._tradeable_reason(sym, blocked)
            self._restore_today()
            self.alert("info", f"Universe loaded: {len(members)} stocks, {len([s for s in self.universe_count if self.universe_count[s] >= self.C['min_sector_members']])} sectors with ≥{self.C['min_sector_members']} stocks.")

    def _tradeable_reason(self, sym: str, blocked: Dict[str, str]) -> str:
        st = self.stats.get(sym)
        if st is None:
            return "no daily data"
        if st.atr14 is None or st.sma200 is None or st.ema55 is None:
            return "not enough history (needs 200 days)"
        if not trend_pass(st, self.P["trend"]):
            return f"trend filter ({self.P['trend']}): grade {st.grade}"
        if not self.nifty_stats.get("market_ok", False):
            return "market_ok false (Nifty below 50-day SMA)"
        if st.avg_value20 < self.C["min_traded_value_20d"]:
            return f"liquidity ₹{st.avg_value20/1e7:.0f} cr < ₹{self.C['min_traded_value_20d']/1e7:.0f} cr"
        if sym in blocked:
            return blocked[sym]
        if "ALL" in blocked:
            return blocked["ALL"]
        if st.overnight_flag:
            return "overnight move > 20% (review)"
        if not st.cumvol20:
            return "no same-time volume baseline"
        return ""

    def _restore_today(self):
        """Engine restart: rebuild today's trades/positions; never re-enter a stock traded today."""
        day = self.day
        for t in self.store.read_lines("trades.jsonl"):
            if t.get("date") == day:
                self.closed_today.append(t)
                self.traded_today.add(t["symbol"])
                self.entries_today += 1
        st = self.store.load_state()
        if st.get("day") == day:
            for p in st.get("open_positions", []):
                pos = Position(**p)
                self.positions[pos.symbol] = pos
                self.traded_today.add(pos.symbol)
                self.entries_today += 1
            self.day_halt = st.get("day_halt")
            self.skipped = st.get("skipped", [])
        if self.positions or self.closed_today:
            self.alert("warn", f"Restart: restored {len(self.positions)} open and {len(self.closed_today)} closed paper trades for today.")

    def _persist(self):
        self.store.save_state({
            "day": self.day, "equity_peak": self.equity_peak, "account_halt": self.account_halt,
            "manual_halt": self.manual_halt, "day_halt": self.day_halt, "skipped": self.skipped[-200:],
            "open_positions": [asdict(p) for p in self.positions.values()],
        })

    def alert(self, level: str, msg: str):
        self.alerts.append({"time": self.clock().strftime("%H:%M:%S"), "level": level, "msg": msg})
        self.alerts = self.alerts[-60:]
        print(f"[{level.upper()}] {msg}", flush=True)

    # ------------------------------------------------------------------ time
    def hm(self, t: Optional[datetime] = None) -> str:
        return (t or self.clock()).strftime("%H:%M")

    def session_phase(self, now: datetime) -> str:
        hm = now.strftime("%H:%M")
        if now.weekday() >= 5:
            return "CLOSED"
        if hm < self.C["session_open"]:
            return "PRE-OPEN"
        if hm >= self.C["session_close"]:
            return "CLOSED"
        if hm >= self.C["time_exit"]:
            return "FLAT"
        if self.C["scan_first_close"] <= hm <= self.C["scan_last_close"]:
            return "SCAN"
        if hm < self.C["scan_first_close"]:
            return "OPEN"
        return "MANAGE"

    # ------------------------------------------------------------------ ticks
    def on_tick(self, token: str, ts: datetime, ltp: float, cum_vol=None, day_open=None, day_high=None,
                day_low=None, prev_close=None, atp=None):
        with self.lock:
            ins = self.by_token.get(str(token))
            if ins is None or ltp is None or ltp <= 0:
                return
            now_w = self.wall()
            b = bucket_start(ts, hhmm(self.C["session_open"]), self.C["candle_minutes"])
            if b is not None and (ts.strftime("%H:%M") >= self.C["session_close"] or ts.date().isoformat() != self.day):
                b = None    # after-hours or a quote from a previous day: update price only, build no candle
            ins.on_tick(ts, ltp, cum_vol, b, day_open, day_high, day_low,
                        prev_close if ins.is_index else None, atp, now_w)
            self.ticks += 1
            self.last_tick_wall = now_w
            self.stale_flattened = False
            sym = ins.symbol
            if sym in self.pending:
                self._try_fill(sym, ltp)
            pos = self.positions.get(sym)
            if pos is not None:
                self._apply_exits(pos, manage_position(pos, ltp, ltp, ltp, self.hm(), self.C["slip_model"]))

    # ------------------------------------------------------------------ scheduler
    def step(self):
        """Called ~4×/second: closes candles on time, runs scans, risk checks, time exit."""
        with self.lock:
            if self.day is None:
                return
            now = self.clock()
            minutes = self.C["candle_minutes"]
            grace = timedelta(seconds=self.C["candle_close_grace_s"])
            t = now - grace
            hm_open = self.C["session_open"]
            close_cap = self.C["session_close"]
            # every boundary that has passed (including several, if we were late)
            cur_bucket = bucket_start(t, hhmm(hm_open), minutes)
            if cur_bucket is not None:
                last = min(cur_bucket, close_cap)
                b = hm_open
                while b < last:
                    close_t = add_minutes(b, minutes)
                    if close_t not in self.closes_done and close_t <= last:
                        self._on_candle_close(b, close_t)
                    b = add_minutes(b, minutes)
            self._risk_tick(now)
            self._expire_pending(now)

    def _on_candle_close(self, bucket: str, close_t: str):
        self.closes_done.add(close_t)
        for ins in list(self.instruments.values()) + [self.nifty, self.vix]:
            if ins is not None:
                ins.close_bucket(bucket)
        self.last_close = close_t
        now = self.clock()
        close_dt = now.replace(hour=int(close_t[:2]), minute=int(close_t[3:]), second=0, microsecond=0)
        # entries only on a FRESH close. After a restart, backfill or computer sleep, old closes
        # rebuild sector persistence but never trigger trades.
        fresh = (now - close_dt).total_seconds() <= 30
        self._sector_update(bucket, close_t)
        self._scan(bucket, close_t, allow_entries=fresh)
        self._persist()

    def replay_closes(self):
        """Replay all candle closes up to the latest closed bucket to rebuild sector persistence and candidate scans."""
        self.closes_done.clear()
        self.sector_hist.clear()
        hm_open = self.C["session_open"]
        close_cap = self.C["session_close"]
        minutes = self.C["candle_minutes"]
        buckets = sorted(set(c.ts for ins in [self.nifty] + list(self.instruments.values()) if ins for c in ins.candles))
        if not buckets:
            return
        last_b = max(buckets)
        b = hm_open
        while b <= last_b and b < close_cap:
            close_t = add_minutes(b, minutes)
            self._on_candle_close(b, close_t)
            b = close_t

    # ------------------------------------------------------------------ section 4
    def _nifty_chg_at(self, bucket: str) -> Optional[float]:
        if self.nifty is None or not self.nifty.prev_close:
            return None
        c = self.nifty.close_at(bucket)
        return (c / self.nifty.prev_close - 1) if c else None

    def _sector_update(self, bucket: str, close_t: str):
        nc = self._nifty_chg_at(bucket)
        if nc is None:
            return
        chg = {}
        for sym, ins in self.instruments.items():
            st = self.stats.get(sym)
            c = ins.close_at(bucket)
            if st and c:
                chg[sym] = c / st.prev_close - 1
        rows = sector_strength(chg, self.sector_of, self.universe_count, nc,
                               self.C["min_sector_members"], self.C["top_sectors"])
        seen = set()
        for r in rows:
            h = self.sector_hist.setdefault(r["sector"], [])
            h.append(r["leader"])
            seen.add(r["sector"])
        for s, h in self.sector_hist.items():
            if s not in seen:
                h.append(False)
        n = self.C["sector_persist_candles"]
        for r in rows:
            h = self.sector_hist[r["sector"]]
            p = 0
            for v in reversed(h):
                if not v:
                    break
                p += 1
            r["persistence"] = min(p, n)
            r["qualifies"] = bool(close_t >= self.C["scan_first_close"] and p >= n)
            r["history"] = h[-12:]
        self.sector_rows = rows
        self.stock_chg_close = chg
        self.nifty_chg_close = nc

    # ------------------------------------------------------------------ section 5
    def _limits(self, sym: str) -> dict:
        sec = self.sector_of.get(sym)
        halted, reason = self._halted()
        return {
            "entries_today": self.entries_today,
            "open": len(self.positions) + len(self.pending),
            "open_in_sector": sum(1 for p in self.positions.values() if p.sector == sec) + sum(1 for p in self.pending.values() if p["sector"] == sec),
            "halted": halted, "halt_reason": reason or "",
            "traded_today": sym in self.traded_today,
        }

    def _halted(self):
        if self.manual_halt:
            return True, "manual halt: " + self.manual_halt.get("reason", "")
        if self.account_halt:
            return True, "account stop: " + self.account_halt.get("reason", "")
        if self.day_halt:
            return True, self.day_halt
        wk = self.week_pnl()
        if wk <= self.P["week_stop"] * self.P["capital"]:
            return True, f"week stop (₹{wk:,.0f}) – no entries until Monday"
        age = self.feed_age()
        if age is not None and age >= self.C["feed_stale_warn_s"]:
            return True, f"feed stale {age:.0f}s"
        return False, None

    def _open_notional(self) -> float:
        return sum(p.fill * p.qty_open for p in self.positions.values()) + sum(p["planned"] * p["qty"] for p in self.pending.values())

    def evaluate(self, sym: str, bucket: str, close_t: str) -> Optional[dict]:
        ins = self.instruments[sym]
        st = self.stats.get(sym)
        if st is None or not ins.candles:
            return None
        closed = [c for c in ins.candles if c.ts <= bucket]
        if not closed:
            return None
        ltp = closed[-1].c
        day_high = max(c.h for c in closed)
        day_low = min(c.l for c in closed)
        cum_v = sum(c.v for c in closed)
        tpv = sum((c.h + c.l + c.c) / 3 * c.v for c in closed)
        vwap = tpv / cum_v if cum_v > 0 else None
        stock_chg = ltp / st.prev_close - 1
        sec_row = next((r for r in self.sector_rows if r["sector"] == ins.sector), None)
        sector_chg = sec_row["chg"] if sec_row else None
        svs = (stock_chg - sector_chg) if sector_chg is not None else None
        base = st.cumvol20.get(bucket)
        vol_ratio = (cum_v / base) if base else None
        rng = day_high - day_low
        range_pos = ((ltp - day_low) / rng) if rng > 0 else None
        atr = st.atr14
        move_atr = ((ltp - ins.day_open) / atr) if (atr and ins.day_open) else None
        R = self.C["stop_atr"] * atr if atr else None
        variant = self.P.get("exit_variant", "E2")
        room, reward, tdist = room_and_reward(ltp, st, R, atr, variant, self.C["resist_buffer"]) if R else (None, None, None)
        qty = size(self.P, st.grade, ltp, R, self._open_notional()) if R else 0
        ctx = {
            "C": self.C, "P": self.P, "stats": st, "ins": ins, "ltp": ltp,
            "limits": self._limits(sym), "nifty_chg": self.nifty_chg_close if hasattr(self, "nifty_chg_close") else None,
            "market_ok": self.nifty_stats.get("market_ok", False), "sector_row": sec_row,
            "sector_qualifies": bool(sec_row and sec_row.get("qualifies")), "persistence": sec_row.get("persistence", 0) if sec_row else 0,
            "stock_vs_sector": svs, "vwap": vwap, "vol_ratio": vol_ratio, "range_pos": range_pos, "move_atr": move_atr,
            "R": R, "room": room, "reward": reward, "qty": qty,
        }
        checks = evaluate_checks(ctx)
        first_fail = next((c["id"] for c in checks if not c["passed"]), None)
        _, stop, t1, t2 = plan_levels(ltp, atr, self.C["stop_atr"], self.C["tick"]) if atr else (None, None, None, None)
        tgt = t2 if variant == "E2" else t1
        return {
            "symbol": sym, "sector": ins.sector, "grade": st.grade, "eval_time": close_t, "ltp_close": r2(ltp),
            "stock_chg": stock_chg, "sector_chg": sector_chg, "stock_vs_sector": svs, "vol_ratio": vol_ratio,
            "range_pos": range_pos, "move_atr": move_atr, "vwap": vwap, "gap": (ins.day_open / st.prev_close - 1) if ins.day_open else None,
            "R": R, "atr": atr, "room": None if room in (None, math.inf) else room, "reward": reward,
            "checks": checks, "first_fail": first_fail, "passed": sum(c["passed"] for c in checks),
            "planned": {"entry": r2(ltp), "limit": r2(ltp * (1 + self.C["entry_limit_pad"])), "stop": stop, "t1": t1, "t2": t2,
                        "rr": r2((tgt - ltp) / R) if (R and tgt) else None, "qty": qty,
                        "risk_rs": r2(qty * R) if R else None, "notional": r2(qty * ltp)},
        }

    def _scan(self, bucket: str, close_t: str, allow_entries: bool = True):
        if not self.sector_rows:
            return
        in_window = self.C["scan_first_close"] <= close_t <= self.C["scan_last_close"] and allow_entries
        day = self.day
        passing = []
        for sym in self.instruments:
            reason = self.tradeable.get(sym, "")
            ev = self.evaluate(sym, bucket, close_t)
            if ev is None:
                continue
            ev["tradeable"] = reason == ""
            ev["blocked_reason"] = reason
            self.evals[sym] = ev
            cur = self.status.get(sym, {}).get("state")
            if reason:
                self.status[sym] = {"state": "NOT TRADEABLE", "reason": reason}
                continue
            if cur in ("IN_TRADE", "CLOSED", "SKIPPED", "TRIGGERED"):
                continue
            # check 3 is evaluated once: failing gap = out for the day
            if ev["checks"][2]["passed"] is False:
                self._skip(sym, close_t, f"gap {ev['checks'][2]['value']} outside range", 3)
                continue
            self.status[sym] = {"state": "WATCHING", "reason": f"first fail #{ev['first_fail']} {CHECK_NAMES.get(ev['first_fail'], '')}" if ev["first_fail"] else "all checks pass"}
            if in_window and ev["first_fail"] is None:
                passing.append(ev)
            # feature log (section 8): every stock in a qualifying sector
            sec_row = next((r for r in self.sector_rows if r["sector"] == ev["sector"]), None)
            if sec_row and sec_row.get("qualifies"):
                self.store.feature_row(day, {
                    "date": day, "time": close_t, "stock": sym, "sector": ev["sector"], "sector_rs": r2(sec_row["rs"] * 100, 3),
                    "sector_rank": sec_row["rank"], "persistence": sec_row["persistence"], "stock_chg": r2(ev["stock_chg"] * 100, 3),
                    "stock_vs_sector": r2((ev["stock_vs_sector"] or 0) * 100, 3), "range_pos": r2(ev["range_pos"], 3),
                    "vol_ratio": r2(ev["vol_ratio"], 3), "move_atr": r2(ev["move_atr"], 3), "gap": r2((ev["gap"] or 0) * 100, 3),
                    "above_pp": ev["checks"][5]["passed"], "nifty_chg": r2(self.nifty_chg_close * 100, 3),
                    "first_fail": ev["first_fail"] or 0, "entry": ev["first_fail"] is None and in_window,
                })
        # rank: stock-vs-sector desc, then volume ratio desc; take until limits hit
        passing.sort(key=lambda e: (e["stock_vs_sector"] or 0, e["vol_ratio"] or 0), reverse=True)
        for ev in passing:
            sym = ev["symbol"]
            lim = self._limits(sym)
            if (lim["halted"] or lim["entries_today"] >= self.P["max_entries_day"] or lim["open"] >= self.P["max_open"]
                    or lim["open_in_sector"] >= self.P["max_per_sector"]):
                continue
            qty = size(self.P, ev["grade"], ev["ltp_close"], ev["R"], self._open_notional())
            if qty < 1:
                continue
            self._place_entry(ev, qty, close_t)

    def _skip(self, sym: str, t: str, reason: str, check: Optional[int] = None):
        self.status[sym] = {"state": "SKIPPED", "reason": reason}
        row = {"time": t, "symbol": sym, "reason": reason, "check": check}
        self.skipped.append(row)
        self.store.append(os.path.join("signals", f"{self.day}.jsonl"), {"state": "SKIPPED", "date": self.day, **row})

    # ------------------------------------------------------------------ section 10 (paper)
    def _place_entry(self, ev: dict, qty: int, close_t: str):
        sym = ev["symbol"]
        limit = ev["ltp_close"] * (1 + self.C["entry_limit_pad"])
        self.pending[sym] = {"symbol": sym, "sector": ev["sector"], "grade": ev["grade"], "qty": qty, "planned": ev["ltp_close"],
                             "limit": limit, "atr": ev["atr"], "deadline": self.clock() + timedelta(seconds=self.C["entry_wait_s"]),
                             "close_t": close_t}
        self.entries_today += 1
        self.traded_today.add(sym)
        self.status[sym] = {"state": "TRIGGERED", "reason": f"paper LIMIT BUY {qty} @ ≤ ₹{limit:.2f} (candle {close_t})"}
        self.store.append(os.path.join("signals", f"{self.day}.jsonl"), {
            "state": "TRIGGERED", "date": self.day, "time": close_t, "symbol": sym, "qty": qty, "planned": ev["planned"],
            "first_fail_check": None, "profile": self.cfg["profile_name"], "mode": self.mode})
        self.alert("info", f"Candidate triggered: {sym} – paper limit buy {qty} @ ≤ ₹{limit:.2f}")

    def _try_fill(self, sym: str, ltp: float):
        o = self.pending[sym]
        if ltp > o["limit"]:
            return
        fill = min(ltp * (1 + self.C["slip_model"]), o["limit"])
        del self.pending[sym]
        st = self.stats[sym]
        R, stop, t1, t2 = plan_levels(fill, o["atr"], self.C["stop_atr"], self.C["tick"])
        variant = self.P.get("exit_variant", "E2")
        pos = Position(id=f"{self.day}-{sym}", symbol=sym, sector=o["sector"], grade=o["grade"], variant=variant,
                       qty=o["qty"], qty_open=o["qty"], entry_time=self.clock().strftime("%H:%M:%S"),
                       planned_entry=o["planned"], fill=fill, R=R, atr=o["atr"], stop=stop, t1=t1, t2=t2, last_ltp=ltp)
        self.positions[sym] = pos
        self.status[sym] = {"state": "IN_TRADE", "reason": f"filled {pos.qty} @ ₹{fill:.2f}"}
        # step 2: re-run checks 10 and 11 with the actual fill
        rr = R / fill
        _, reward, _ = room_and_reward(fill, st, R, o["atr"], variant, self.C["resist_buffer"])
        tgt = t2 if variant == "E2" else t1
        if not (self.P["r_min"] <= rr <= self.C["r_max"]) or reward < self.P["room_min_r"] * R or fill >= tgt:
            self.alert("warn", f"{sym}: fill ₹{fill:.2f} fails checks 10/11 – aborted, market sell.")
            self._apply_exits(pos, [{"time": self.hm(), "qty": pos.qty_open, "price": ltp * (1 - self.C["slip_model"]), "reason": "Aborted after fill"}])
            return
        self.alert("info", f"PAPER fill {sym} {pos.qty} @ ₹{fill:.2f} · stop ₹{stop:.2f} · T2 ₹{t2:.2f}")
        self._persist()

    def _expire_pending(self, now: datetime):
        for sym in list(self.pending):
            if now >= self.pending[sym]["deadline"]:
                del self.pending[sym]
                self._skip(sym, now.strftime("%H:%M:%S"), "no_fill within 10 s (never chase)")

    def _apply_exits(self, pos: Position, exits: List[dict]):
        if not exits:
            return
        for e in exits:
            q = min(e["qty"], pos.qty_open)
            if q <= 0:
                continue
            pos.legs.append({**e, "qty": q})
            pos.qty_open -= q
            if e["reason"].startswith("Target T1") and pos.variant == "E3":
                pos.stop = pos.fill          # E3: stop moves to entry for the remainder
            if e["reason"].startswith("Target T1"):
                pos.t1_done = True
        if pos.qty_open <= 0:
            summary = close_summary(pos, self.K)
            summary.update({"date": self.day, "mode": self.mode, "profile": self.cfg["profile_name"], "source": self.source})
            self.closed_today.append(summary)
            self.store.append("trades.jsonl", summary)
            del self.positions[pos.symbol]
            self.status[pos.symbol] = {"state": "CLOSED", "reason": f"{summary['reason']} · net ₹{summary['net']:+,.2f} ({summary['r_multiple']:+.2f}R)"}
            self.alert("info", f"PAPER exit {pos.symbol}: {summary['reason']} · net ₹{summary['net']:+,.2f} ({summary['r_multiple']:+.2f}R)")
            eq = self.equity()
            self.equity_peak = max(self.equity_peak, eq)
            if eq <= self.equity_peak * (1 + self.P["account_stop"]) and not self.account_halt:
                self.account_halt = {"reason": f"equity ₹{eq:,.0f} ≤ {abs(self.P['account_stop'])*100:.0f}% below peak ₹{self.equity_peak:,.0f}", "since": str(self.clock())}
                self.alert("error", "ACCOUNT STOP hit – halted until the owner clears it.")
        self._persist()

    def flatten_all(self, reason: str):
        for pos in list(self.positions.values()):
            px = (pos.last_ltp or pos.fill) * (1 - self.C["slip_model"])
            self._apply_exits(pos, [{"time": self.hm(), "qty": pos.qty_open, "price": px, "reason": reason}])
        for sym in list(self.pending):
            del self.pending[sym]
            self._skip(sym, self.hm(), f"cancelled: {reason}")

    # ------------------------------------------------------------------ risk manager
    def realized_today(self) -> float:
        return sum(t["net"] for t in self.closed_today)

    def open_mtm(self) -> float:
        tot = 0.0
        for p in self.positions.values():
            ltp = self.instruments[p.symbol].ltp or p.fill
            tot += p.unrealized(ltp) + p.realized_gross()
        return tot

    def day_pnl(self) -> float:
        return self.realized_today() + self.open_mtm()

    def week_pnl(self) -> float:
        if not self.day:
            return 0.0
        d = date.fromisoformat(self.day)
        monday = (d - timedelta(days=d.weekday())).isoformat()
        prior = sum(t.get("net", 0) for t in self.store.read_lines("trades.jsonl")
                    if monday <= t.get("date", "") < self.day and t.get("source") == self.source)
        return prior + self.realized_today()

    def equity(self) -> float:
        hist = sum(t.get("net", 0) for t in self.store.read_lines("trades.jsonl")
                   if t.get("date", "") < (self.day or "") and t.get("source") == self.source)
        return self.P["capital"] + hist + self.realized_today()

    def feed_age(self) -> Optional[float]:
        now = self.clock()
        ph = self.session_phase(now)
        if ph in ("PRE-OPEN", "CLOSED"):
            return None
        if now.strftime("%H:%M:%S") < self.C["session_open"] + ":30":
            return None   # first ticks may take a few seconds after 09:15
        if not self.last_tick_wall:
            return float("inf")
        return max(0.0, self.wall() - self.last_tick_wall)

    def _risk_tick(self, now: datetime):
        if not self.day:
            return
        hm = now.strftime("%H:%M")
        # day stop: realised + open MTM
        dp = self.day_pnl()
        if dp <= self.P["day_stop"] * self.P["capital"] and not self.day_halt:
            self.day_halt = f"day stop hit (₹{dp:,.2f}) – flat, no new entries today"
            self.alert("error", self.day_halt)
            self.flatten_all("Day stop")
        # stale feed
        age = self.feed_age()
        if age is not None and age >= self.C["feed_stale_flatten_s"] and (self.positions or self.pending) and not self.stale_flattened:
            self.stale_flattened = True
            self.alert("error", f"Feed stale {age:.0f}s ≥ {self.C['feed_stale_flatten_s']}s – flattening paper positions.")
            self.flatten_all("Stale feed")
        # 15:15 time exit
        if hm >= self.C["time_exit"] and not self.time_exit_done and self.session_phase(now) != "CLOSED":
            self.time_exit_done = True
            if self.positions or self.pending:
                self.flatten_all("Time exit 15:15")

    # ------------------------------------------------------------------ owner actions
    def halt_clear(self, reason: str) -> dict:
        with self.lock:
            cleared = []
            if self.account_halt:
                cleared.append("account")
                self.account_halt = None
                self.equity_peak = self.equity()
            if self.manual_halt:
                cleared.append("manual")
                self.manual_halt = None
            self.store.append("halt_log.jsonl", {"time": str(self.clock()), "action": "clear", "reason": reason, "cleared": cleared})
            self.alert("warn", f"Owner cleared halt ({', '.join(cleared) or 'nothing to clear'}): {reason}")
            self._persist()
            return {"cleared": cleared}

    def halt(self, reason: str) -> dict:
        with self.lock:
            self.manual_halt = {"reason": reason, "since": str(self.clock())}
            self.store.append("halt_log.jsonl", {"time": str(self.clock()), "action": "halt", "reason": reason})
            self.alert("error", f"Manual halt: {reason}")
            self.flatten_all("Manual halt")
            self._persist()
            return {"halted": True}

    # ------------------------------------------------------------------ snapshot
    def feed_status(self) -> dict:
        now = self.clock()
        ph = self.session_phase(now)
        age = self.feed_age()
        if self.day is None:
            st = "CONNECTING"
        elif ph == "PRE-OPEN":
            st = "PRE-OPEN"
        elif ph == "CLOSED":
            st = "MARKET CLOSED"
        elif age is None:
            st = "WAITING"
        elif age == float("inf"):
            st = "NO DATA"
        elif age >= self.C["feed_stale_flatten_s"]:
            st = "STALE"
        elif age >= self.C["feed_stale_warn_s"]:
            st = "DELAYED"
        else:
            st = "LIVE"
        return {"status": st, "age_s": None if age in (None, float("inf")) else round(age, 1), "transport": self.transport,
                "ticks": self.ticks, "message": self.feed_message, "warn_s": self.C["feed_stale_warn_s"],
                "flatten_s": self.C["feed_stale_flatten_s"],
                "last_tick": datetime.fromtimestamp(self.last_tick_wall, IST).strftime("%H:%M:%S") if self.last_tick_wall else None}

    def snapshot(self) -> dict:
        with self.lock:
            now = self.clock()
            P, C = self.P, self.C
            nifty = self.nifty
            nchg = nifty.chg() if nifty else None
            # live breadth across the universe
            adv = dec = unch = 0
            for ins in self.instruments.values():
                ch = ins.chg() if ins.prev_close else None
                if ch is None:
                    continue
                if ch > 0.0001:
                    adv += 1
                elif ch < -0.0001:
                    dec += 1
                else:
                    unch += 1
            cands = []
            top_secs = {r["sector"] for r in self.sector_rows if r["rank"] <= C["top_sectors"]}
            for sym, ev in self.evals.items():
                ins = self.instruments[sym]
                stt = self.status.get(sym, {"state": "WATCHING", "reason": ""})
                cands.append({
                    **{k: v for k, v in ev.items() if k not in ("checks",)},
                    "checks": ev["checks"],
                    "ltp": r2(ins.ltp), "chg": r2((ins.chg() or 0) * 100, 2),
                    "stock_vs_sector": r2((ev["stock_vs_sector"] or 0) * 100, 2) if ev["stock_vs_sector"] is not None else None,
                    "stock_chg": r2(ev["stock_chg"] * 100, 2), "sector_chg": r2((ev["sector_chg"] or 0) * 100, 2),
                    "vol_ratio": r2(ev["vol_ratio"]), "range_pos": r2(ev["range_pos"]), "move_atr": r2(ev["move_atr"]),
                    "vwap": r2(ev["vwap"]), "gap": r2((ev["gap"] or 0) * 100, 2), "R": r2(ev["R"]), "atr": r2(ev["atr"]),
                    "room": r2(ev["room"]), "reward": r2(ev["reward"]),
                    "first_fail_name": CHECK_NAMES.get(ev["first_fail"], "") if ev["first_fail"] else "",
                    "status": stt["state"], "status_reason": stt.get("reason", ""), "in_top_sector": ev["sector"] in top_secs,
                })
            cands.sort(key=lambda c: (c["status"] in ("IN_TRADE", "TRIGGERED"), c["passed"], c["stock_vs_sector"] or -99), reverse=True)
            positions = []
            for p in self.positions.values():
                ltp = self.instruments[p.symbol].ltp or p.fill
                unr = p.unrealized(ltp) + p.realized_gross()
                positions.append({"symbol": p.symbol, "sector": p.sector, "variant": p.variant, "qty": p.qty, "qty_open": p.qty_open,
                                  "entry_time": p.entry_time, "fill": r2(p.fill), "ltp": r2(ltp), "stop": r2(p.stop), "t1": r2(p.t1),
                                  "t2": r2(p.t2), "R": r2(p.R), "risk_rs": r2(p.R * p.qty), "pnl": r2(unr),
                                  "r_now": r2(unr / (p.R * p.qty)) if p.R else None, "mfe_r": r2(p.mfe), "mae_r": r2(p.mae),
                                  "est_costs": r2(costs(p.fill * p.qty, [ltp * p.qty], self.K)["total"])})
            for o in self.pending.values():
                positions.append({"symbol": o["symbol"], "sector": o["sector"], "variant": "PENDING", "qty": o["qty"], "qty_open": o["qty"],
                                  "entry_time": o["close_t"], "fill": None, "ltp": r2(self.instruments[o["symbol"]].ltp), "stop": None,
                                  "t1": None, "t2": None, "R": None, "risk_rs": None, "pnl": 0.0, "r_now": None, "pending_limit": r2(o["limit"])})
            all_tr = [t for t in self.store.read_lines("trades.jsonl") if t.get("source") == self.source]
            n = len(all_tr)
            exp_r = sum(t.get("r_multiple", 0) for t in all_tr) / n if n else None
            wins = sum(1 for t in all_tr if t.get("net", 0) > 0)
            halted, hreason = self._halted()
            secs_to_exit = None
            te = now.replace(hour=int(C["time_exit"][:2]), minute=int(C["time_exit"][3:]), second=0, microsecond=0)
            if now < te:
                secs_to_exit = int((te - now).total_seconds())
            return {
                "server_time": now.isoformat(), "ist": now.strftime("%H:%M:%S"), "date": self.day,
                "mode": self.mode, "source": self.source, "profile": self.cfg["profile_name"],
                "gate": self.cfg["proof_gate"],
                "feed": self.feed_status(),
                "session": {"phase": self.session_phase(now), "last_close": self.last_close, "scan_first": C["scan_first_close"],
                            "scan_last": C["scan_last_close"], "time_exit": C["time_exit"], "secs_to_exit": secs_to_exit},
                "nifty": {"ltp": r2(nifty.ltp) if nifty else None, "prev_close": r2(nifty.prev_close) if nifty else None,
                          "chg": r2(nchg * 100, 2) if nchg is not None else None, "stance": stance_label(nchg),
                          "market_ok": self.nifty_stats.get("market_ok"), "sma50": r2(self.nifty_stats.get("sma50"))},
                "vix": {"ltp": r2(self.vix.ltp) if self.vix else None,
                        "chg": r2(self.vix.chg() * 100, 2) if (self.vix and self.vix.chg() is not None) else None},
                "breadth": {"adv": adv, "dec": dec, "unch": unch, "total": adv + dec + unch},
                "sectors": [{**r, "chg": r2(r["chg"] * 100, 2), "rs": r2(r["rs"] * 100, 2)} for r in self.sector_rows],
                "candidates": cands,
                "positions": positions,
                "trades": self.closed_today,
                "skipped": self.skipped[-50:],
                "meters": {
                    "capital": P["capital"], "risk_A": r2(P["capital"] * P["risk_pct_A"]), "risk_B": r2(P["capital"] * P["risk_pct_B"]),
                    "gross_cap_rs": r2(P["gross_cap"] * P["capital"]), "max_pos_rs": r2(P["max_pos_value"] * P["capital"]),
                    "open_notional": r2(self._open_notional()),
                    "day_pnl": r2(self.day_pnl()), "realized_today": r2(self.realized_today()),
                    "day_stop_rs": r2(P["day_stop"] * P["capital"]),
                    "week_pnl": r2(self.week_pnl()), "week_stop_rs": r2(P["week_stop"] * P["capital"]),
                    "equity": r2(self.equity()), "equity_peak": r2(self.equity_peak),
                    "account_stop_level": r2(self.equity_peak * (1 + P["account_stop"])),
                    "entries_today": self.entries_today, "max_entries_day": P["max_entries_day"],
                    "open": len(self.positions), "max_open": P["max_open"],
                },
                "halt": {"halted": halted, "reason": hreason},
                "stats": {"trades": n, "expectancy_r": r2(exp_r), "win_rate": r2(wins / n * 100, 1) if n else None},
                "alerts": self.alerts[-25:],
                "batch": self.batch_info,
                "profile_rules": {k: v for k, v in P.items() if not k.startswith("_")},
            }

    def chart(self, sym: str) -> dict:
        with self.lock:
            ins = self.instruments.get(sym) or (self.nifty if sym in ("NIFTY", "NIFTY 50") else None)
            if ins is None:
                return {"error": "unknown symbol"}
            cs = list(ins.candles) + ([ins._cur] if ins._cur else [])
            vw, tpv, vol = [], 0.0, 0.0
            for c in cs:
                tpv += (c.h + c.l + c.c) / 3 * c.v
                vol += c.v
                vw.append(r2(tpv / vol) if vol else None)
            st = self.stats.get(sym)
            pos = self.positions.get(sym)
            ev = self.evals.get(sym, {})
            levels = {}
            if st:
                s1 = 2 * st.pp - st.pdh
                s2 = 2 * st.pp - st.r2
                levels.update({
                    "pp": r2(st.pp), "r1": r2(st.r1), "r2": r2(st.r2),
                    "s1": r2(s1), "s2": r2(s2),
                    "pdh": r2(st.pdh), "prev_close": r2(st.prev_close)
                })
            if pos:
                levels.update({"fill": r2(pos.fill), "stop": r2(pos.stop), "t1": r2(pos.t1), "t2": r2(pos.t2)})
            elif ev.get("planned"):
                levels.update({"plan_stop": ev["planned"]["stop"], "plan_t2": ev["planned"]["t2"]})
            return {"symbol": ins.symbol, "sector": ins.sector, "ltp": r2(ins.ltp), "chg": r2((ins.chg() or 0) * 100, 2),
                    "candles": [{"t": c.ts, "o": r2(c.o), "h": r2(c.h), "l": r2(c.l), "c": r2(c.c), "v": c.v} for c in cs],
                    "vwap": vw, "levels": levels}
