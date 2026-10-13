#!/usr/bin/env python3
"""
RFD WET data job. Runs on GitHub Actions every 15 minutes and writes JSON next to the app,
for the data a web page can't fetch itself (CDEC blocks browsers) or that is too heavy to
fetch on a phone.

  data/cdec.json     Stampede, Boca, Prosser, Donner: daily storage (400 d), inflow and
                     outflow (120 d), hourly Stampede storage, 10-year median storage by day;
                     Squaw Creek (Olympic Valley PSD / UES) flow once it reaches CDEC.
  data/normals.json  USGS daily flow percentiles (p10..p90) for every gauge, weekly.
  data/daily.json    Daily mean and peak flow for every gauge, last 400 days, daily.
  data/travel.json   Travel times between gauges learned from the last 60 days, daily.

Files are only rewritten when their content changes, so the repo only gets a commit when
something new arrived. stdlib only.
"""
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
UA = {"User-Agent": "RFD-WET data job (github.com/wwelles14/RFD-WET)"}
CDEC = "https://cdec.water.ca.gov/dynamicapp"
USGS = "https://waterservices.usgs.gov/nwis"
PST = timezone(timedelta(hours=-8))          # CDEC dates are Pacific Standard Time all year
RESERVOIRS = ["STP", "BOC", "PRS", "DNN"]
SITES = {   # every USGS gauge the app shows (flow), key -> site number
    "tahoe": "10337500", "truckee": "10338000", "farad": "10346000", "mogul": "10347460",
    "reno": "10348000", "sparks": "10348200", "vista": "10350000",
    "donner": "10338500", "prosser": "10340500", "lt_in": "10344400", "boca": "10344500",
    "hunter": "10347600", "steamboat": "10349300", "ntd": "10348245", "galena": "10348850",
}
TRAVEL = [("truckee", "farad"), ("boca", "farad"), ("farad", "mogul"), ("farad", "reno"), ("reno", "vista")]
NAME_RX = re.compile(r"WASHESHU|SQUAW|OLYMPIC", re.I)
NF_RX = re.compile(r"\bN(ORTH)?\.?\s*F(OR)?K|\bNF\b|N\.F\.", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, tries=3, timeout=60):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            log(f"retry {i + 1} {url[:90]}: {e}")
            time.sleep(4 * (i + 1))


def path(name):
    return os.path.join(ROOT, name)


def load(name):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save(name, obj):
    """Write only when the content changed (keeps commits to real updates)."""
    os.makedirs(ROOT, exist_ok=True)
    new = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    try:
        with open(path(name), encoding="utf-8") as f:
            if f.read() == new:
                return False
    except OSError:
        pass
    with open(path(name), "w", encoding="utf-8") as f:
        f.write(new)
    log(f"wrote {name} ({len(new) // 1024} KB)")
    return True


def due(meta, key, hours):
    t = meta.get(key)
    return not t or time.time() - t > hours * 3600


def ms(dt):
    return int(dt.timestamp() * 1000)


# ------------------------------------------------------------------ CDEC
def cdec_json(stations, sensors, dur, start, end):
    q = urllib.parse.urlencode({"Stations": ",".join(stations), "SensorNums": ",".join(map(str, sensors)),
                                "dur_code": dur, "Start": start.isoformat(), "End": end.isoformat()})
    rows = json.loads(get(f"{CDEC}/req/JSONDataServlet?{q}") or "[]")
    out = {}
    for r in rows or []:
        v = r.get("value")
        if not isinstance(v, (int, float)) or v == -9999:
            continue
        try:
            t = datetime.strptime(str(r.get("date")), "%Y-%m-%d %H:%M").replace(tzinfo=PST)
        except ValueError:
            continue
        out.setdefault((r.get("stationId"), int(r.get("SENSOR_NUM"))), []).append([ms(t), v])
    for k in out:
        out[k].sort()
    return out


def merge(old, new, keep_days):
    m = {p[0]: p[1] for p in (old or [])}
    m.update({p[0]: p[1] for p in (new or [])})
    cut = ms(datetime.now(timezone.utc)) - keep_days * 864e5
    return [[t, m[t]] for t in sorted(m) if t >= cut]


def update_cdec():
    old = load("cdec.json")
    meta = old.get("meta", {})
    res = old.get("reservoirs", {})
    today = datetime.now(PST).date()
    # every run: the last 10 days (CDEC posts daily values once a day, hourly Stampede storage hourly)
    recent = cdec_json(RESERVOIRS, [15, 76, 23], "D", today - timedelta(days=10), today + timedelta(days=1))
    long_due = due(meta, "long", 20) or not res
    if long_due:
        recent.update(cdec_json(RESERVOIRS, [15], "D", today - timedelta(days=400), today + timedelta(days=1)))
        recent.update({k: v for k, v in cdec_json(RESERVOIRS, [76, 23], "D", today - timedelta(days=120), today + timedelta(days=1)).items()})
    hourly = cdec_json(RESERVOIRS, [15], "H", today - timedelta(days=3), today + timedelta(days=1))
    for sid in RESERVOIRS:
        r = res.setdefault(sid, {})
        r["storage"] = merge(r.get("storage"), recent.get((sid, 15)), 400)
        r["inflow"] = merge(r.get("inflow"), recent.get((sid, 76)), 120)
        r["outflow"] = merge(r.get("outflow"), recent.get((sid, 23)), 120)
        r["hourly"] = merge(r.get("hourly"), hourly.get((sid, 15)), 3)
        for k in ("inflow", "outflow", "hourly"):
            if not r[k]:
                r.pop(k)
    median = old.get("median", {})
    if due(meta, "median", 24 * 7) or not median:
        # median storage for each day of the year over the last 10 full water years
        y = today.year if today.month >= 10 else today.year - 1
        hist = cdec_json(RESERVOIRS, [15], "D", date(y - 10, 10, 1), date(y, 9, 30))
        for sid in RESERVOIRS:
            byday = {}
            for t, v in hist.get((sid, 15), []):
                d = datetime.fromtimestamp(t / 1000, PST)
                byday.setdefault(date(2000, d.month, d.day).timetuple().tm_yday, []).append(v)   # leap-year index, Feb 29 = 60
            arr = []
            for doy in range(1, 367):
                vals = sorted(byday.get(doy) or byday.get(doy - 1) or [])
                arr.append(round(vals[len(vals) // 2]) if vals else None)
            if any(arr):
                median[sid] = arr
        meta["median"] = time.time()
    if long_due:
        meta["long"] = time.time()
    squaw = old.get("squaw")
    try:
        squaw = update_squaw(squaw, meta)
    except Exception as e:  # noqa: BLE001
        log(f"squaw: {e}")
    save("cdec.json", {"reservoirs": res, "median": median, "squaw": squaw, "meta": meta})


def update_squaw(prev, meta):
    """Olympic Valley PSD / UES North Fork Washeshu (Squaw) Creek flow, once it is on CDEC."""
    sid = (prev or {}).get("id")
    if not sid or due(meta, "squaw_search", 24 * 7):
        q = {"sta": "", "sensor": "211", "collect": "NONE SPECIFIED", "dur": "", "active": "", "loc_chk": "on",
             "lon1": "-120.33", "lon2": "-120.17", "lat1": "39.16", "lat2": "39.24", "elev1": "-5", "elev2": "99000",
             "nearby": "", "basin": "NONE SPECIFIED", "hydro": "NONE SPECIFIED", "county": "NONE SPECIFIED",
             "agency_num": "0", "display": "sta"}
        html = get(f"{CDEC}/staSearch?" + urllib.parse.urlencode(q))
        found = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            m = re.search(r"station_id=([A-Z0-9]{3})", row)
            if not m:
                continue
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
            name = next((c for c in cells if len(c) > 3 and c != m.group(1)), "")
            if NAME_RX.search(name) and NF_RX.search(name) and not re.search(r"SNOW|PALISADES", name, re.I):
                found.append((m.group(1), name))
        meta["squaw_search"] = time.time()
        log(f"squaw search: {found or 'none yet'}")
        if not found:
            return None
        sid, name = found[0]
        prev = {"id": sid, "name": name}
    end = datetime.now(PST).date() + timedelta(days=1)
    for dur in ("H", "E", "D"):
        pts = cdec_json([sid], [20], dur, end - timedelta(days=35), end).get((sid, 20))
        if pts:
            return {"id": sid, "name": prev.get("name"), "dur": dur, "flow": [p for p in pts if p[1] >= 0]}
    return {"id": sid, "name": prev.get("name"), "flow": []}


# ------------------------------------------------------------------ USGS
def usgs_iv(sites, period, params="00060"):
    q = urllib.parse.urlencode({"format": "json", "sites": ",".join(sites), "parameterCd": params, "period": period, "siteStatus": "all"})
    j = json.loads(get(f"{USGS}/iv/?{q}", timeout=180))
    out = {}
    for ts in j.get("value", {}).get("timeSeries", []):
        site = ts["sourceInfo"]["siteCode"][0]["value"]
        nd = ts["variable"].get("noDataValue")
        pts = []
        for blk in ts.get("values", []):
            for v in blk.get("value", []):
                try:
                    x = float(v["value"])
                except (TypeError, ValueError):
                    continue
                if x == nd or x <= -999:
                    continue
                pts.append((ms(datetime.fromisoformat(v["dateTime"])), x))
        if len(pts) > len(out.get(site, [])):
            out[site] = sorted(pts)
    return out


def update_normals():
    old = load("normals.json")
    if not due(old.get("meta", {}), "at", 24 * 7) and old.get("sites"):
        return
    sites = {}
    for key, site in SITES.items():
        try:
            txt = get(f"{USGS}/stat/?format=rdb&sites={site}&statReportType=daily&statTypeCd=p10,p25,p50,p75,p90&parameterCd=00060")
        except Exception as e:  # noqa: BLE001
            log(f"normals {key}: {e}")
            if key in old.get("sites", {}):
                sites[key] = old["sites"][key]
            continue
        lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("#")]
        if len(lines) < 3:
            continue
        hdr = lines[0].split("\t")
        ix = {h: i for i, h in enumerate(hdr)}
        best = {}
        for ln in lines[2:]:
            f = ln.split("\t")
            try:
                ts, m, d, n = f[ix["ts_id"]], int(f[ix["month_nu"]]), int(f[ix["day_nu"]]), int(f[ix["count_nu"]] or 0)
                vals = [float(f[ix[c]]) if f[ix[c]] else None for c in ("p10_va", "p25_va", "p50_va", "p75_va", "p90_va")]
            except (KeyError, ValueError, IndexError):
                continue
            best.setdefault(ts, {})[(m, d)] = (n, vals)
        if not best:
            continue
        ts = max(best, key=lambda k: sum(n for n, _ in best[k].values()))
        arr = []
        for doy in range(1, 367):
            dt = date(2000, 1, 1) + timedelta(days=doy - 1)     # leap year: 366 days
            rec = best[ts].get((dt.month, dt.day))
            arr.append([round(v, 1) if v is not None else None for v in rec[1]] if rec else None)
        years = max((n for n, _ in best[ts].values()), default=0)
        sites[key] = {"p": arr, "years": years}
    save("normals.json", {"sites": sites, "meta": {"at": time.time()}})


def update_daily():
    """Daily mean and peak (local day) per gauge, 400 days, kept up to date incrementally."""
    old = load("daily.json")
    meta = old.get("meta", {})
    if not due(meta, "at", 6) and old.get("sites"):
        return
    sites = old.get("sites", {})
    need_full = [k for k in SITES if not sites.get(k)]
    groups = []
    if need_full:
        groups += [([k], "P400D") for k in need_full]
    rest = [k for k in SITES if k not in need_full]
    if rest:
        groups.append((rest, "P3D"))
    for keys, period in groups:
        try:
            data = usgs_iv([SITES[k] for k in keys], period)
        except Exception as e:  # noqa: BLE001
            log(f"daily {keys}: {e}")
            continue
        for k in keys:
            pts = data.get(SITES[k], [])
            days = {}
            for t, v in pts:
                d = datetime.fromtimestamp(t / 1000, PST).date().isoformat()
                days.setdefault(d, []).append(v)
            rec = {d: [round(sum(v) / len(v), 1), round(max(v), 1), len(v)] for d, v in days.items()}
            cur = {r[0]: r[1:] for r in sites.get(k, [])}
            for d, (mean, peak, n) in rec.items():
                if n >= 20 or d not in cur:          # skip a partial day that would replace a full one
                    cur[d] = [mean, peak]
            cut = (datetime.now(PST).date() - timedelta(days=400)).isoformat()
            sites[k] = [[d] + cur[d] for d in sorted(cur) if d >= cut]
    meta["at"] = time.time()
    save("daily.json", {"sites": sites, "meta": meta})


def lag_hours(a, b):
    """Lag (h) that best lines up changes at upstream a with downstream b; None if no clear signal."""
    if len(a) < 500 or len(b) < 500:
        return None
    step = 15 * 60000

    def grid(pts):
        g = {}
        for t, v in pts:
            g[t - t % step] = v
        return g
    ga, gb = grid(a), grid(b)
    t0, t1 = max(min(ga), min(gb)), min(max(ga), max(gb))
    ts = list(range(t0 - t0 % step, t1, step))

    def diffs(g):
        out, last = [], None
        for t in ts:
            v = g.get(t, last)
            out.append(None if v is None or last is None else v - last)
            last = v if v is not None else last
        return out
    da, db = diffs(ga), diffs(gb)
    # smooth with a 2 h moving sum so 15-min noise doesn't dominate
    def smooth(x, w=8):
        out = []
        for i in range(len(x)):
            win = [v for v in x[max(0, i - w + 1):i + 1] if v is not None]
            out.append(sum(win) if win else None)
        return out
    da, db = smooth(da), smooth(db)
    best = (None, 0.0)
    for lag in range(0, 4 * 18 + 1):          # 0..18 h
        xs, ys = [], []
        for i in range(len(da) - lag):
            x, y = da[i], db[i + lag]
            if x is not None and y is not None:
                xs.append(x); ys.append(y)
        if len(xs) < 300:
            continue
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if sx == 0 or sy == 0:
            continue
        r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)
        if r > best[1]:
            best = (lag / 4, r)
    # need a real signal: good correlation and changes well above gauge noise
    amp = max((abs(v) for v in da if v is not None), default=0)
    if best[0] is None or best[1] < 0.55 or amp < 40:
        return None
    return {"hours": best[0], "r": round(best[1], 2)}


def update_travel():
    old = load("travel.json")
    if not due(old.get("meta", {}), "at", 24) and "pairs" in old:
        return
    keys = sorted({k for p in TRAVEL for k in p})
    try:
        data = usgs_iv([SITES[k] for k in keys], "P120D")
    except Exception as e:  # noqa: BLE001
        log(f"travel: {e}")
        return
    pairs = []
    for a, b in TRAVEL:
        res = lag_hours(data.get(SITES[a], []), data.get(SITES[b], []))
        pairs.append({"from": a, "to": b, **(res or {"hours": None})})
        log(f"travel {a}->{b}: {res}")
    save("travel.json", {"pairs": pairs, "meta": {"at": time.time(), "days": 120}})


def update_lake():
    """Lake Tahoe daily elevation for the last 400 days (USGS 10337000 gage height + 6,220 ft)."""
    old = load("lake.json")
    if not due(old.get("meta", {}), "at", 6) and old.get("pts"):
        return
    q = urllib.parse.urlencode({"format": "json", "sites": "10337000", "parameterCd": "00065", "period": "P400D", "siteStatus": "all"})
    j = json.loads(get(f"{USGS}/dv/?{q}", timeout=120))
    best = []
    for ts in j.get("value", {}).get("timeSeries", []):
        pts = []
        for blk in ts.get("values", []):
            for v in blk.get("value", []):
                try:
                    x = float(v["value"])
                except (TypeError, ValueError):
                    continue
                if x > -999:
                    pts.append([v["dateTime"][:10], round(x + 6220.0, 2)])
        if len(pts) > len(best):
            best = pts
    if len(best) < 30:
        log(f"lake: only {len(best)} days")
        return
    save("lake.json", {"pts": best, "meta": {"at": time.time(), "site": "10337000"}})
    log(f"lake: {len(best)} days, last {best[-1]}")


def main():
    ok = True
    for job in (update_cdec, update_normals, update_daily, update_travel, update_lake):
        try:
            job()
        except Exception as e:  # noqa: BLE001
            ok = False
            log(f"{job.__name__} failed: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
