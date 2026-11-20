"""
Strategy 5 - Sector Leaders Intraday: pure strategy logic.

Everything in this file is deterministic and has no network access, so it can be
unit-tested (tests/test_core.py) and reused by the live feed, the demo feed and a
future backtest. Formulas follow the developer spec section by section.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time as dtime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def load_config(path: Optional[str] = None) -> dict:
    path = path or os.path.join(ROOT, "config_s5.json")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    prof_name = cfg.get("active_profile", "small")
    cfg["profile_name"] = prof_name
    cfg["P"] = cfg["profiles"][prof_name]
    cfg["C"] = cfg["common"]
    return cfg


def hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def r2(x: Optional[float], n: int = 2) -> Optional[float]:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(x, n)


# ----------------------------------------------------------------------------
# Section 3: daily indicators (computed from daily bars up to and INCLUDING
# the previous trading day)
# ----------------------------------------------------------------------------
def ema(values: List[float], span: int) -> Optional[float]:
    """EMA(span, adjust=False) - same as pandas ewm(span=..., adjust=False)."""
    if len(values) < span:
        return None
    alpha = 2.0 / (span + 1.0)
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1 - alpha) * e
    return e


def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def atr_simple(highs: List[float], lows: List[float], closes: List[float], n: int) -> Optional[float]:
    """ATR = simple mean of the last n True Ranges (NOT Wilder) - spec section 3."""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


@dataclass
class DailyStats:
    symbol: str
    prev_close: float
    ema55: Optional[float]
    sma200: Optional[float]
    atr14: Optional[float]
    pp: float
    r1: float
    r2: float
    pdh: float
    high20: float
    avg_value20: float           # 20-day average traded value in Rs
    grade: str                   # "A" (above both) else "B"
    ema_ok: bool
    sma_ok: bool
    cumvol20: Dict[str, float] = field(default_factory=dict)   # "HH:MM" candle start -> mean cumulative volume
    cumvol_days: int = 0
    overnight_flag: bool = False
    last_date: str = ""


def compute_daily_stats(symbol: str, bars: List[list], cfg: dict) -> Optional[DailyStats]:
    """bars = [[date_str, o, h, l, c, v], ...] oldest first, previous trading day LAST."""
    C = cfg["C"]
    if len(bars) < C["atr"] + 2:
        return None
    o = [float(b[1]) for b in bars]
    h = [float(b[2]) for b in bars]
    l = [float(b[3]) for b in bars]
    c = [float(b[4]) for b in bars]
    v = [float(b[5]) for b in bars]
    e55 = ema(c, C["ema"])
    s200 = sma(c, C["sma"])
    a14 = atr_simple(h, l, c, C["atr"])
    H, L, Cl = h[-1], l[-1], c[-1]
    pp = (H + L + Cl) / 3.0
    stats = DailyStats(
        symbol=symbol,
        prev_close=Cl,
        ema55=e55,
        sma200=s200,
        atr14=a14,
        pp=pp,
        r1=2 * pp - L,
        r2=pp + (H - L),
        pdh=H,
        high20=max(h[-C["high_days"]:]),
        avg_value20=sum(c[i] * v[i] for i in range(-min(20, len(c)), 0)) / min(20, len(c)),
        ema_ok=bool(e55 is not None and Cl > e55),
        sma_ok=bool(s200 is not None and Cl > s200),
        grade="B",
        last_date=str(bars[-1][0])[:10],
    )
    stats.grade = "A" if (stats.ema_ok and stats.sma_ok) else "B"
    if len(c) >= 2 and c[-2] > 0 and abs(c[-1] / c[-2] - 1) > 0.20:
        stats.overnight_flag = True
    return stats


def cumvol_profile(days: Dict[str, List[Tuple[str, float]]], min_candles: int, n_days: int) -> Tuple[Dict[str, float], int]:
    """days = {date: [(HH:MM candle start, volume), ...]}. Returns mean cumulative
    volume per candle start over the last n_days complete days (>= min_candles)."""
    good = [d for d in sorted(days) if len(days[d]) >= min_candles][-n_days:]
    if not good:
        return {}, 0
    acc: Dict[str, List[float]] = {}
    for d in good:
        cum = 0.0
        for t, vol in sorted(days[d]):
            cum += vol
            acc.setdefault(t, []).append(cum)
    prof = {t: sum(vals) / len(vals) for t, vals in acc.items() if len(vals) >= max(1, len(good) // 2)}
    return prof, len(good)


def trend_pass(st: DailyStats, trend: str) -> bool:
    if trend == "ema55":
        return st.ema_ok
    if trend == "sma200":
        return st.sma_ok
    if trend == "both":
        return st.ema_ok and st.sma_ok
    return st.ema_ok or st.sma_ok  # "either"


# ----------------------------------------------------------------------------
# Section 7: sizing and costs (copied exactly)
# ----------------------------------------------------------------------------
def size(p: dict, grade: str, fill: float, R: float, open_notional: float) -> int:
    if R <= 0 or fill <= 0:
        return 0
    risk_rs = p["capital"] * (p["risk_pct_A"] if grade == "A" else p["risk_pct_B"])
    q = math.floor(risk_rs / R + 1e-9)
    q = min(q, math.floor(p["max_pos_value"] * p["capital"] / fill + 1e-9))
    room = p["gross_cap"] * p["capital"] - open_notional
    if q * fill > room:
        q = math.floor(room / fill + 1e-9)
    return max(q, 0)


def costs(buy_val: float, sell_vals: List[float], k: Optional[dict] = None) -> dict:
    k = k or {"brokerage_cap": 20, "brokerage_pct": 0.001, "stt_sell": 0.00025, "stamp_buy": 0.00003,
              "exchange": 0.0000297, "sebi": 0.000001, "gst": 0.18}
    sell = sum(sell_vals)
    brok = min(k["brokerage_cap"], k["brokerage_pct"] * buy_val) + sum(min(k["brokerage_cap"], k["brokerage_pct"] * v) for v in sell_vals)
    stt = k["stt_sell"] * sell
    stamp = k["stamp_buy"] * buy_val
    exch = k["exchange"] * (buy_val + sell)
    sebi = k["sebi"] * (buy_val + sell)
    gst = k["gst"] * (brok + exch + sebi)
    total = brok + stt + stamp + exch + sebi + gst
    return {"brokerage": brok, "stt": stt, "stamp": stamp, "exchange": exch, "sebi": sebi, "gst": gst, "total": total}


def round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


# ----------------------------------------------------------------------------
# Section 4: sector strength
# ----------------------------------------------------------------------------
def sector_strength(stock_chg: Dict[str, float], sector_of: Dict[str, str], universe_count: Dict[str, int],
                    nifty_chg: float, min_members: int, top_n: int) -> List[dict]:
    """Returns ranked list [{sector, members, with_data, chg, rs, rank, leader}]."""
    groups: Dict[str, List[float]] = {}
    for sym, ch in stock_chg.items():
        sec = sector_of.get(sym)
        if sec is None or ch is None:
            continue
        groups.setdefault(sec, []).append(ch)
    rows = []
    for sec, n_univ in universe_count.items():
        if n_univ < min_members:
            continue  # spec: sectors with fewer than 4 universe stocks are ignored
        vals = groups.get(sec, [])
        if not vals:
            continue
        sc = sum(vals) / len(vals)
        rows.append({"sector": sec, "members": n_univ, "with_data": len(vals), "chg": sc, "rs": sc - nifty_chg})
    rows.sort(key=lambda r: r["rs"], reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["leader"] = bool(r["rs"] > 0 and r["rank"] <= top_n)
    return rows


# ----------------------------------------------------------------------------
# Candles (section 9: built locally; volume = diff of cumulative day volume)
# ----------------------------------------------------------------------------
@dataclass
class Candle:
    ts: str      # candle START "HH:MM"
    o: float
    h: float
    l: float
    c: float
    v: float


def bucket_start(t: datetime, open_t: dtime, minutes: int) -> Optional[str]:
    start = t.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
    if t < start:
        return None
    idx = int((t - start).total_seconds() // (minutes * 60))
    b = start + timedelta(minutes=idx * minutes)
    return b.strftime("%H:%M")


def add_minutes(hm: str, minutes: int) -> str:
    h, m = map(int, hm.split(":"))
    total = h * 60 + m + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


class Instrument:
    """Live state for one symbol: day OHLC, cumulative volume and 5-minute candles."""

    def __init__(self, symbol: str, token: str, sector: Optional[str] = None, is_index: bool = False):
        self.symbol = symbol
        self.token = token
        self.sector = sector
        self.is_index = is_index
        self.ltp: Optional[float] = None
        self.day_open: Optional[float] = None
        self.day_high: Optional[float] = None
        self.day_low: Optional[float] = None
        self.prev_close: Optional[float] = None
        self.cum_vol: float = 0.0
        self.atp: Optional[float] = None          # broker average traded price (cross-check only)
        self.last_exch_ts: Optional[datetime] = None
        self.last_recv: float = 0.0
        self.candles: List[Candle] = []
        self._cur: Optional[Candle] = None
        self._boundary_cum: float = 0.0
        self._tpv: float = 0.0
        self._vol: float = 0.0

    # -- ticks ---------------------------------------------------------------
    def on_tick(self, ts: datetime, ltp: float, cum_vol: Optional[float], bucket: Optional[str],
                day_open=None, day_high=None, day_low=None, prev_close=None, atp=None, recv=0.0):
        if ltp is None or ltp <= 0:
            return
        # 1) a tick from a NEW bucket first closes the old candle, using the cumulative
        #    volume as it stood BEFORE this tick (= volume at the boundary)
        if bucket is not None and self._cur is not None and self._cur.ts < bucket:
            self._close_current()
        # 2) update day state
        self.ltp = ltp
        self.last_exch_ts = ts
        self.last_recv = recv
        if prev_close:
            self.prev_close = prev_close
        if atp:
            self.atp = atp
        if day_open:
            self.day_open = day_open
        elif self.day_open is None:
            self.day_open = ltp
        self.day_high = max(x for x in (self.day_high, day_high, ltp) if x)
        self.day_low = min(x for x in (self.day_low, day_low, ltp) if x)
        if cum_vol is not None and cum_vol >= self.cum_vol:
            self.cum_vol = cum_vol
        if bucket is None:
            return
        # 3) update / open the current candle
        if self._cur is None:
            if self.candles and self.candles[-1].ts >= bucket:
                return  # late tick for an already-closed bucket: day state updated, candle left alone
            self._cur = Candle(bucket, ltp, ltp, ltp, ltp, 0.0)
        c = self._cur
        c.h = max(c.h, ltp)
        c.l = min(c.l, ltp)
        c.c = ltp
        c.v = max(0.0, self.cum_vol - self._boundary_cum)

    def _close_current(self):
        c = self._cur
        if c is None:
            return
        c.v = max(0.0, self.cum_vol - self._boundary_cum)
        self._boundary_cum = self.cum_vol
        self.candles.append(c)
        self._tpv += (c.h + c.l + c.c) / 3.0 * c.v
        self._vol += c.v
        self._cur = None

    def close_bucket(self, bucket: str):
        """Called by the scheduler at the end of each 5-min bucket (also for symbols with no ticks)."""
        if self._cur is not None and self._cur.ts <= bucket:
            self._close_current()
        elif self.ltp is not None and (not self.candles or self.candles[-1].ts < bucket):
            # no trade in this bucket: flat candle with zero volume (keeps the candle count honest)
            self.candles.append(Candle(bucket, self.ltp, self.ltp, self.ltp, self.ltp, 0.0))

    def load_candles(self, candles: List[Candle]):
        """Backfill today's closed candles (engine started or restarted mid-session)."""
        self.candles = []
        self._tpv = self._vol = 0.0
        self._cur = None
        for c in candles:
            self.candles.append(c)
            self._tpv += (c.h + c.l + c.c) / 3.0 * c.v
            self._vol += c.v
        if candles:
            self.day_open = candles[0].o
            self.day_high = max(c.h for c in candles)
            self.day_low = min(c.l for c in candles)
            self.ltp = candles[-1].c
            self.cum_vol = self._vol
            self._boundary_cum = self._vol

    # -- derived -------------------------------------------------------------
    def vwap(self) -> Optional[float]:
        """VWAP = cumulative(typical x volume) / cumulative(volume) from 09:15 incl. the current candle."""
        tpv, vol = self._tpv, self._vol
        if self._cur is not None and self._cur.v > 0:
            tpv += (self._cur.h + self._cur.l + self._cur.c) / 3.0 * self._cur.v
            vol += self._cur.v
        return tpv / vol if vol > 0 else None

    def chg(self) -> Optional[float]:
        if self.ltp is None or not self.prev_close:
            return None
        return self.ltp / self.prev_close - 1.0

    def close_at(self, bucket: str) -> Optional[float]:
        for c in reversed(self.candles):
            if c.ts <= bucket:
                return c.c
        return None


# ----------------------------------------------------------------------------
# Section 5: the 12 entry checks
# ----------------------------------------------------------------------------
CHECK_NAMES = {
    1: "Limits", 2: "Market stance", 3: "Gap", 4: "Sector qualifies", 5: "Beating its sector",
    6: "Buyer control", 7: "Real volume", 8: "Near the high", 9: "Not extended",
    10: "Risk distance", 11: "Room (reward floor)", 12: "Size",
}


def stance_label(nifty_chg: Optional[float]) -> str:
    if nifty_chg is None:
        return "—"
    if nifty_chg >= 0.004:
        return "Strong"
    if nifty_chg >= 0:
        return "Mild"
    if nifty_chg >= -0.0035:
        return "Neutral"
    return "Bearish"


def room_and_reward(ltp: float, st: DailyStats, R: float, atr: float, variant: str, buffer: float) -> Tuple[float, float, float]:
    levels = [x for x in [st.pdh, st.high20, st.r1, st.r2] if x is not None and x > ltp]
    room = (min(levels) * buffer - ltp) if levels else math.inf
    target_dist = (1.5 if variant == "E2" else 1.0) * atr
    reward = min(target_dist, room)
    return room, reward, target_dist


def evaluate_checks(ctx: dict) -> List[dict]:
    """ctx holds everything a check needs; returns list of {id, name, passed, value, rule}."""
    C, P = ctx["C"], ctx["P"]
    st: DailyStats = ctx["stats"]
    ins: Instrument = ctx["ins"]
    ltp = ctx["ltp"]
    out = []

    def add(i, passed, value, rule):
        out.append({"id": i, "name": CHECK_NAMES[i], "passed": bool(passed), "value": value, "rule": rule})

    lim = ctx["limits"]
    lim_ok = (lim["entries_today"] < P["max_entries_day"] and lim["open"] < P["max_open"]
              and lim["open_in_sector"] < P["max_per_sector"] and not lim["halted"]
              and not lim["traded_today"])
    lim_txt = (f"entries {lim['entries_today']}/{P['max_entries_day']} · open {lim['open']}/{P['max_open']} · "
               f"sector {lim['open_in_sector']}/{P['max_per_sector']}"
               + (" · HALTED: " + lim["halt_reason"] if lim["halted"] else "")
               + (" · already traded today" if lim["traded_today"] else ""))
    add(1, lim_ok, lim_txt, "within daily/position limits, not halted")

    nc = ctx["nifty_chg"]
    add(2, nc is not None and nc >= C["nifty_min_chg"] and ctx["market_ok"],
        (f"Nifty {nc*100:+.2f}% ({stance_label(nc)})" if nc is not None else "no Nifty data")
        + ("" if ctx["market_ok"] else " · Nifty below 50-day SMA → no entries today"),
        f"Nifty ≥ {C['nifty_min_chg']*100:.2f}% and market_ok")

    gap = (ins.day_open / st.prev_close - 1) if ins.day_open else None
    add(3, gap is not None and C["gap_min"] <= gap <= C["gap_max"],
        f"{gap*100:+.2f}%" if gap is not None else "—",
        f"{C['gap_min']*100:.1f}% … +{C['gap_max']*100:.1f}%")

    sq = ctx["sector_row"]
    add(4, ctx["sector_qualifies"],
        (f"{ins.sector} rank #{sq['rank']} · RS {sq['rs']*100:+.2f}% · persistence {ctx['persistence']}/3" if sq else f"{ins.sector}: not ranked"),
        "top-2, RS > 0 for 3 consecutive 5-min closes")

    svs = ctx["stock_vs_sector"]
    add(5, svs is not None and svs >= C["stock_vs_sector_min"],
        f"{svs*100:+.2f}%" if svs is not None else "—", f"≥ +{C['stock_vs_sector_min']*100:.2f}%")

    vw = ctx["vwap"]
    ok6 = ins.day_open is not None and vw is not None and ltp > ins.day_open and ltp > vw and ltp > st.pp
    add(6, ok6, f"LTP {ltp:.2f} · open {ins.day_open or 0:.2f} · VWAP {vw or 0:.2f} · PP {st.pp:.2f}",
        "LTP > open AND > VWAP AND > pivot")

    vr = ctx["vol_ratio"]
    add(7, vr is not None and vr >= C["vol_ratio_min"], f"{vr:.2f}×" if vr is not None else "no baseline",
        f"same-time volume ≥ {C['vol_ratio_min']}×")

    rp = ctx["range_pos"]
    add(8, rp is not None and rp >= C["range_pos_min"], f"{rp:.2f}" if rp is not None else "—", f"≥ {C['range_pos_min']}")

    mv = ctx["move_atr"]
    add(9, mv is not None and mv <= C["max_move_atr"], f"{mv:.2f} ATR" if mv is not None else "—", f"≤ {C['max_move_atr']} ATR")

    R = ctx["R"]
    rr = (R / ltp) if (R and ltp) else None
    add(10, rr is not None and P["r_min"] <= rr <= C["r_max"],
        f"R ₹{R:.2f} = {rr*100:.2f}% of price" if rr is not None else "no ATR",
        f"{P['r_min']*100:.2f}% … {C['r_max']*100:.2f}%")

    rw = ctx["reward"]
    ok11 = R is not None and rw is not None and rw >= P["room_min_r"] * R
    room_txt = "∞" if ctx["room"] == math.inf else f"₹{ctx['room']:.2f}"
    add(11, ok11, (f"reward ₹{rw:.2f} = {rw/R:.2f}R · room {room_txt}" if (R and rw is not None) else "—"),
        f"reward ≥ {P['room_min_r']}R")

    q = ctx["qty"]
    add(12, q >= 1, f"{q} sh", "qty ≥ 1 after caps")
    return out


# ----------------------------------------------------------------------------
# Paper positions (sections 6, 7, 10)
# ----------------------------------------------------------------------------
@dataclass
class Position:
    id: str
    symbol: str
    sector: str
    grade: str
    variant: str
    qty: int
    qty_open: int
    entry_time: str
    planned_entry: float
    fill: float
    R: float
    atr: float
    stop: float
    t1: float
    t2: float
    legs: List[dict] = field(default_factory=list)   # exits: {time, qty, price, reason}
    mfe: float = 0.0
    mae: float = 0.0
    t1_done: bool = False
    last_ltp: float = 0.0
    status: str = "OPEN"

    def unrealized(self, ltp: float) -> float:
        return (ltp - self.fill) * self.qty_open

    def realized_gross(self) -> float:
        return sum((leg["price"] - self.fill) * leg["qty"] for leg in self.legs)


def plan_levels(fill: float, atr: float, stop_atr: float, tick: float) -> Tuple[float, float, float, float]:
    R = stop_atr * atr
    return R, round_tick(fill - R, tick), round_tick(fill + 1.0 * atr, tick), round_tick(fill + 1.5 * atr, tick)


def manage_position(pos: Position, low: float, high: float, ltp: float, now_hm: str, slip: float) -> List[dict]:
    """Apply exit rules for one price update (tick or candle low/high). Stop has priority:
    same update touching stop and target = stop (spec section 6)."""
    exits = []
    if pos.qty_open <= 0:
        return exits
    pos.last_ltp = ltp
    pos.mfe = max(pos.mfe, (high - pos.fill) / pos.R)
    pos.mae = min(pos.mae, (low - pos.fill) / pos.R)
    if low <= pos.stop:
        px = pos.stop * (1 - slip)
        exits.append({"time": now_hm, "qty": pos.qty_open, "price": px,
                      "reason": "Stop at entry (E3)" if (pos.t1_done and pos.variant == "E3") else "Stop"})
        return exits
    v = pos.variant
    if v == "E1" and high >= pos.t1:
        exits.append({"time": now_hm, "qty": pos.qty_open, "price": pos.t1, "reason": "Target T1"})
    elif v == "E2" and high >= pos.t2:
        exits.append({"time": now_hm, "qty": pos.qty_open, "price": pos.t2, "reason": "Target T2"})
    elif v in ("E3", "E4"):
        if not pos.t1_done and high >= pos.t1:
            half = pos.qty // 2 if pos.qty >= 2 else pos.qty
            exits.append({"time": now_hm, "qty": half, "price": pos.t1, "reason": "Target T1 (50%)"})
        rem = pos.qty_open - sum(e["qty"] for e in exits)
        if rem > 0 and high >= pos.t2 and (pos.t1_done or exits):
            exits.append({"time": now_hm, "qty": rem, "price": pos.t2, "reason": "Target T2"})
    return exits


def close_summary(pos: Position, cost_k: dict) -> dict:
    buy_val = pos.fill * pos.qty
    sells = [leg["price"] * leg["qty"] for leg in pos.legs]
    cst = costs(buy_val, sells, cost_k)
    gross = pos.realized_gross()
    net = gross - cst["total"]
    risk_rs = pos.R * pos.qty
    return {
        "id": pos.id, "symbol": pos.symbol, "sector": pos.sector, "grade": pos.grade, "variant": pos.variant,
        "qty": pos.qty, "entry_time": pos.entry_time, "exit_time": pos.legs[-1]["time"] if pos.legs else None,
        "planned_entry": r2(pos.planned_entry), "fill": r2(pos.fill), "stop": r2(pos.stop), "t1": r2(pos.t1), "t2": r2(pos.t2),
        "exit_price": r2(sum(l["price"] * l["qty"] for l in pos.legs) / max(1, sum(l["qty"] for l in pos.legs))),
        "legs": [{**l, "price": r2(l["price"])} for l in pos.legs],
        "reason": " + ".join(dict.fromkeys(l["reason"] for l in pos.legs)),
        "gross": r2(gross), "costs": r2(cst["total"]), "cost_lines": {k: r2(v) for k, v in cst.items()},
        "net": r2(net), "r_multiple": r2(net / risk_rs if risk_rs else 0.0), "risk_rs": r2(risk_rs),
        "mfe_r": r2(pos.mfe), "mae_r": r2(pos.mae),
    }
