"""
DEMO feed: a synthetic NSE session for testing the engine and the dashboard when the market is closed
or no Angel One credentials are set. Prices are SIMULATED – the dashboard shows a DEMO banner and the
demo writes to data/demo/, never to your real paper journal.
"""
from __future__ import annotations

import hashlib
import math
import os
import random
import threading
import time
from datetime import datetime, date, timedelta
from typing import Dict, List

from .core import IST, ROOT, DailyStats, round_tick
from .angel import read_sector_map

SECTOR_PATH = {   # (gap %, intraday drift % by 11:30, afternoon drift %) - simulated scenario
    "AUTO": (0.45, 0.95, 0.35), "METAL": (0.30, 0.75, -0.30), "CAPITAL GOODS": (0.20, 0.45, 0.10),
    "ENERGY": (0.10, 0.25, 0.05), "IT": (0.05, 0.10, 0.10), "BANK": (0.10, 0.10, 0.00),
    "FIN SERVICE": (0.05, 0.05, 0.00), "PSU BANK": (0.00, -0.10, -0.10), "FMCG": (-0.05, -0.20, 0.05),
    "PHARMA": (0.00, 0.00, 0.05), "CEMENT": (-0.10, -0.05, 0.00), "CONSUMPTION": (0.00, 0.05, 0.00),
}
NIFTY_PATH = (0.15, 0.30, 0.05)


def _h(s: str) -> float:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def demo_day(today: date) -> date:
    d = today
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def volume_weights(n=75):
    w = [1 + 2.6 * math.exp(-i / 5.5) + 1.3 * math.exp(-(n - 1 - i) / 6) for i in range(n)]
    s = sum(w)
    return [x / s for x in w]


class DemoMarket:
    def __init__(self, cfg: dict, day: date, seed: int = 7):
        self.cfg = cfg
        self.day = day
        self.rng = random.Random(seed)
        smap = read_sector_map(os.path.join(ROOT, cfg["files"]["sector_map"]))
        self.members = [{"symbol": s, "token": str(100001 + i), "sector": sec} for i, (s, sec) in enumerate(sorted(smap.items()))]
        self.w = volume_weights()
        self.cumw = [sum(self.w[:i + 1]) for i in range(len(self.w))]
        self.stats: Dict[str, DailyStats] = {}
        self.sim: Dict[str, dict] = {}
        self._build()

    def _build(self):
        rng = self.rng
        leaders_per_sector: Dict[str, int] = {}
        for m in self.members:
            s, sec = m["symbol"], m["sector"]
            u = _h(s)
            pc = round_tick(math.exp(math.log(150) + u * (math.log(9000) - math.log(150))))
            atr_pct = 0.011 + 0.013 * _h(s + "atr")
            gap0, drift, pm = SECTOR_PATH.get(sec, (0.0, 0.0, 0.0))
            strong = drift >= 0.7
            lead = strong and leaders_per_sector.get(sec, 0) < 3 and _h(s + "lead") > 0.35
            if lead:
                leaders_per_sector[sec] = leaders_per_sector.get(sec, 0) + 1
                atr_pct = 0.0145 + 0.006 * _h(s + "atr2")
            gradeA = _h(s + "g") < 0.78 or lead
            H = pc * (1 + atr_pct * (0.25 + 0.5 * _h(s + "H")))
            L = pc * (1 - atr_pct * (0.25 + 0.5 * _h(s + "L")))
            pp = (H + L + pc) / 3
            vol_day = (2e9 + 1.2e10 * _h(s + "v")) / pc
            if _h(s + "liq") < 0.04:
                vol_day = 3e8 / pc
            prof, cum = {}, 0.0
            for i in range(75):
                cum += self.w[i] * vol_day
                t = datetime(2000, 1, 1, 9, 15) + timedelta(minutes=5 * i)
                prof[t.strftime("%H:%M")] = cum
            st = DailyStats(symbol=s, prev_close=pc,
                            ema55=pc * (1 - 0.01 - 0.05 * _h(s + "e")) if gradeA else pc * (1 + 0.02 * _h(s + "e")),
                            sma200=pc * (1 - 0.03 - 0.12 * _h(s + "s")) if gradeA else pc * (1 - 0.05 * _h(s + "s")),
                            atr14=pc * atr_pct, pp=pp, r1=2 * pp - L, r2=pp + (H - L), pdh=H,
                            high20=pc * (1 + (0.035 + 0.04 * _h(s + "h20")) if lead else (0.004 + 0.05 * _h(s + "h20"))),
                            avg_value20=vol_day * pc, grade="A" if gradeA else "B", ema_ok=gradeA, sma_ok=gradeA,
                            cumvol20=prof, cumvol_days=20, last_date=(self.day - timedelta(days=1)).isoformat())
            self.stats[s] = st
            gap = (gap0 + rng.gauss(0, 0.25)) / 100
            boost = (0.45 + 0.35 * _h(s + "b")) / 100 if lead else rng.gauss(0, 0.25) / 100
            self.sim[s] = {"pc": pc, "gap": gap, "drift": drift / 100, "pm": pm / 100, "boost": boost,
                           "sigma": atr_pct * 0.22, "noise": 0.0, "vmult": (2.1 + 0.6 * _h(s + "vm")) if lead else max(0.4, rng.gauss(1.0, 0.25)),
                           "vol_day": vol_day, "lead": lead, "late": 0.4 * _h(s + "late"),
                           "fade": lead and _h(s + "fade") < 0.4}
        self.nifty_pc = 25480.0
        self.vix_pc = 13.4
        self.nifty_noise = 0.0
        self.nifty_stats = {"prev_close": self.nifty_pc, "sma50": self.nifty_pc * 0.985, "market_ok": True,
                            "vix_prev_close": self.vix_pc, "last_date": (self.day - timedelta(days=1)).isoformat()}

    def _ou(self, x: float, dt: float, sd: float, tau: float) -> float:
        """Ornstein-Uhlenbeck step: mean-reverting noise with stationary s.d. `sd`, time constant `tau` seconds."""
        a = math.exp(-dt / tau)
        return x * a + self.rng.gauss(0, sd * math.sqrt(1 - a * a))

    @staticmethod
    def _shape(frac_morning: float, frac_after: float, sim: dict) -> float:
        start = sim.get("late", 0)
        fm = 0.0 if frac_morning <= start else min(1.0, (frac_morning - start) / (1 - start)) ** 0.8
        return fm

    def price(self, s: str, t: datetime) -> float:
        sim = self.sim[s]
        m = (t.hour * 60 + t.minute + t.second / 60) - (9 * 60 + 15)
        fm = max(0.0, min(1.0, m / 135.0))                 # 09:15 → 11:30
        fa = max(0.0, min(1.0, (m - 135) / 225.0))         # 11:30 → 15:15
        dt = max(0.5, (t - sim["t"]).total_seconds()) if sim.get("t") else 2.0
        sim["t"] = t
        sim["noise"] = self._ou(sim["noise"], dt, sim["sigma"], 1200.0)
        shaped = self._shape(fm, fa, sim)
        lead_after = (-1.6 if sim["fade"] else 0.9) * sim["boost"] * fa
        r = sim["gap"] + sim["drift"] * shaped + sim["boost"] * shaped + sim["pm"] * fa + lead_after + sim["noise"] + self.rng.gauss(0, 0.00025)
        return round_tick(sim["pc"] * (1 + r))

    def cum_vol(self, s: str, t: datetime) -> float:
        sim = self.sim[s]
        m = (t.hour * 60 + t.minute + t.second / 60) - (9 * 60 + 15)
        i = max(0, min(74, int(m // 5)))
        part = (m % 5) / 5 if m < 375 else 1
        prev = self.cumw[i - 1] if i > 0 else 0.0
        frac = prev + self.w[i] * part
        return float(int(sim["vol_day"] * sim["vmult"] * frac))

    def nifty(self, t: datetime) -> float:
        m = (t.hour * 60 + t.minute + t.second / 60) - (9 * 60 + 15)
        fm = max(0.0, min(1.0, m / 135.0))
        fa = max(0.0, min(1.0, (m - 135) / 225.0))
        dt = max(0.5, (t - self._nt).total_seconds()) if getattr(self, "_nt", None) else 2.0
        self._nt = t
        self.nifty_noise = self._ou(self.nifty_noise, dt, 0.0012, 1800.0)
        g, d, pm = NIFTY_PATH
        return round(self.nifty_pc * (1 + (g + d * fm + pm * fa) / 100 + self.nifty_noise), 2)


class DemoClock:
    def __init__(self, day: date, start: str = "09:14:50", speed: float = 8.0):
        h, m, *sec = [int(x) for x in start.split(":")]
        self.t0 = datetime(day.year, day.month, day.day, h, m, sec[0] if sec else 0, tzinfo=IST)
        self.w0 = time.time()
        self.speed = speed
        self.end = datetime(day.year, day.month, day.day, 15, 31, tzinfo=IST)

    def now(self) -> datetime:
        t = self.t0 + timedelta(seconds=(time.time() - self.w0) * self.speed)
        return min(t, self.end)

    def wall(self) -> float:
        return self.now().timestamp()


class DemoFeed:
    def __init__(self, market: DemoMarket, engine, clock: DemoClock):
        self.m = market
        self.engine = engine
        self.clock = clock
        self.stop_flag = False
        self.opened: Dict[str, float] = {}

    def emit(self, ts: datetime, rng: random.Random):
        e = self.engine
        n = self.m.nifty(ts)
        e.on_tick("99926000", ts, n, prev_close=self.m.nifty_pc)
        e.on_tick("99926017", ts, round(self.m.vix_pc * (1 - 0.03 * (n / self.m.nifty_pc - 1) * 30), 2), prev_close=self.m.vix_pc)
        for mm in self.m.members:
            if rng.random() < 0.6:
                s = mm["symbol"]
                px = self.m.price(s, ts)
                if s not in self.opened:
                    self.opened[s] = px
                e.on_tick(mm["token"], ts, px, cum_vol=self.m.cum_vol(s, ts), day_open=self.opened[s])

    def backfill(self, upto: datetime):
        """Demo started after 09:15: generate the morning so candles, VWAP and persistence exist
        (same as the live backfill – old closes never create trades)."""
        rng = random.Random(5)
        t = upto.replace(hour=9, minute=15, second=0)
        while t < upto:
            self.emit(t, rng)
            t += timedelta(seconds=4)
        self.last = t - timedelta(seconds=4) if t > upto.replace(hour=9, minute=15, second=0) else None

    def run(self):
        e = self.engine
        e.transport = f"DEMO generator ×{self.clock.speed:g}"
        e.feed_message = "Simulated prices – not real market data"
        last = getattr(self, "last", None)
        rng = random.Random(11)
        while not self.stop_flag:
            now = self.clock.now()
            hm = now.strftime("%H:%M:%S")
            if "09:15:00" <= hm < "15:30:00":
                cur = now.replace(microsecond=0)
                if last is None or cur > last:
                    steps = []
                    t = (last + timedelta(seconds=2)) if last else cur
                    while t <= cur and len(steps) < 40:
                        steps.append(t)
                        t += timedelta(seconds=2)
                    if not steps:
                        steps = [cur]
                    for ts in steps:
                        self.emit(ts, rng)
                    last = steps[-1]
            time.sleep(0.25)
