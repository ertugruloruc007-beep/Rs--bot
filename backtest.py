import os
import time

import numpy as np
import pandas as pd
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
RISK_PCT = float(os.environ.get("RISK_PCT") or 1)

BASE = "https://data-api.binance.vision/api/v3"
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
    "LTCUSDT", "NEARUSDT",
]

TEST_DAYS = 180       # iki yariya bolunur: eski 90 gun / yeni 90 gun
WARMUP_DAYS = 45
START_EQUITY = 1000.0
LEVERAGE = 10
MAX_POS = 3
FEE = 0.0005
SLIP = 0.0002
DAILY_LOSS = 0.03
MAX_STOP_PCT = 0.08
ADX_MIN = 18


def send(text):
    print(text)
    if not TOKEN or not CHAT_ID:
        return
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


def fetch(symbol, start_ms, end_ms):
    rows = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get(
            BASE + "/klines",
            params={"symbol": symbol, "interval": "1h",
                    "startTime": cur, "limit": 1000},
            timeout=30,
        )
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        rows += data
        cur = data[-1][0] + 1
        if len(data) < 1000:
            break
        time.sleep(0.1)
    df = pd.DataFrame([x[:6] for x in rows],
                      columns=["t", "open", "high", "low", "close", "volume"])
    df = df.astype(float)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.drop_duplicates("t").set_index("t").sort_index()
    return df.iloc[:-1]


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def trend_frames(df):
    """4 saatlik trend (tamamlanmis mumlar, ileri bakis yok)."""
    h = df.resample("4h").agg({"open": "first", "high": "max", "low": "min",
                               "close": "last", "volume": "sum"}).dropna()
    ema50 = h["close"].ewm(span=50, adjust=False).mean()
    ema200 = h["close"].ewm(span=200, adjust=False).mean()
    a = adx(h)
    out = pd.DataFrame(index=h.index)
    out["up"] = ((ema50 > ema200) & (h["close"] > ema200) & (a >= ADX_MIN)).astype(float)
    out["down"] = ((ema50 < ema200) & (h["close"] < ema200) & (a >= ADX_MIN)).astype(float)
    out["loose_up"] = (h["close"] > ema200).astype(float)
    out["loose_down"] = (h["close"] < ema200).astype(float)
    out = out.shift(1)
    return out.reindex(df.index, method="ffill").fillna(0) > 0


def base_frame(df):
    f = df.copy()
    f["rsi"] = rsi(f["close"])
    f["atr"] = atr(f)
    f["vr"] = f["volume"] / f["volume"].rolling(20).mean().shift(1)
    f["ema21"] = f["close"].ewm(span=21, adjust=False).mean()
    for n in (20, 55):
        f[f"hh{n}"] = f["high"].rolling(n).max().shift(1)
        f[f"ll{n}"] = f["low"].rolling(n).min().shift(1)
    return f


# ---- stratejiler: (uzun_sinyal, kisa_sinyal) dondurur ----
def sig_brk(n):
    def fn(f, up, dn):
        l = up & (f["close"] > f[f"hh{n}"]) & (f["vr"] >= 1.0)
        s = dn & (f["close"] < f[f"ll{n}"]) & (f["vr"] >= 1.0)
        return l, s
    return fn


def sig_pull(f, up, dn):
    tl = (f["low"] <= f["ema21"]).astype(float).rolling(3).max() > 0
    ts = (f["high"] >= f["ema21"]).astype(float).rolling(3).max() > 0
    l = (up & tl & (f["close"] > f["ema21"]) & (f["close"] > f["open"])
         & (f["rsi"] > 35) & (f["rsi"] < 55))
    s = (dn & ts & (f["close"] < f["ema21"]) & (f["close"] < f["open"])
         & (f["rsi"] < 65) & (f["rsi"] > 45))
    return l, s


def sig_rsi(f, up, dn):
    l = (up & (f["rsi"].rolling(3).min() < 30) & (f["rsi"] > f["rsi"].shift(1))
         & (f["close"] > f["open"]))
    s = (dn & (f["rsi"].rolling(3).max() > 70) & (f["rsi"] < f["rsi"].shift(1))
         & (f["close"] < f["open"]))
    return l, s


STRATS = {
    "BRK20 (20 mum kirilim, takip eden stop)":
        (sig_brk(20), dict(mode="trail", atr_mult=2.5, trail=2.5, max_hold=240)),
    "BRK55 (55 mum kirilim, takip eden stop)":
        (sig_brk(55), dict(mode="trail", atr_mult=3.0, trail=3.0, max_hold=360)),
    "PULL (trend icinde EMA21 geri cekilme)":
        (sig_pull, dict(mode="fixed", atr_mult=2.0, tp1=1.0, tp2=3.0, max_hold=72)),
    "RSI1S (1 saatlik RSI dip/tepe)":
        (sig_rsi, dict(mode="fixed", atr_mult=2.0, tp1=1.0, tp2=2.0, max_hold=48)),
}


def build(f, tr, btc_tr, fn):
    up = tr["up"] & btc_tr["loose_up"].reindex(f.index, fill_value=False)
    dn = tr["down"] & btc_tr["loose_down"].reindex(f.index, fill_value=False)
    l, s = fn(f, up, dn)
    d = pd.Series(0, index=f.index)
    d[l] = 1
    d[s] = -1
    g = f[["open", "high", "low", "close"]].copy()
    g["dir"] = d.shift(1).fillna(0)          # giris bir sonraki mum acilisinda
    g["sig_atr"] = f["atr"].shift(1)
    g["sig_vr"] = f["vr"].shift(1).fillna(0)
    return g


def simulate(feats, idx, p):
    n = len(idx)
    A = {}
    for s, f in feats.items():
        g = f.reindex(idx)
        A[s] = {k: g[k].to_numpy(dtype=float)
                for k in ["open", "high", "low", "close", "dir", "sig_atr", "sig_vr"]}

    equity = START_EQUITY
    peak = equity
    max_dd = 0.0
    open_pos = {}
    trades = []
    cur_day = None
    day_start = equity

    def realize(pos, qty, price, slip):
        nonlocal equity, peak, max_dd
        px = price * (1 - SLIP * pos["d"]) if slip else price
        gross = qty * pos["d"] * (px - pos["entry"])
        fee = qty * px * FEE
        equity += gross - fee
        pos["pnl"] += gross - fee
        pos["left"] -= qty
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    def finalize(sym, pos):
        trades.append({"sym": sym, "d": pos["d"], "pnl": pos["pnl"],
                       "r": pos["pnl"] / pos["risk"]})
        del open_pos[sym]

    for i in range(n):
        ts = idx[i]
        if ts.date() != cur_day:
            cur_day = ts.date()
            day_start = equity
        blocked = equity <= day_start * (1 - DAILY_LOSS)

        if len(open_pos) < MAX_POS and not blocked:
            cands = []
            for s, a in A.items():
                if s in open_pos:
                    continue
                d = a["dir"][i]
                if d == 0 or np.isnan(d) or np.isnan(a["sig_atr"][i]) or np.isnan(a["open"][i]):
                    continue
                cands.append((a["sig_vr"][i], s, int(d)))
            cands.sort(reverse=True)
            for _, s, d in cands:
                if len(open_pos) >= MAX_POS:
                    break
                a = A[s]
                entry = a["open"][i] * (1 + SLIP * d)
                atr_v = a["sig_atr"][i]
                dist = p["atr_mult"] * atr_v
                if dist <= 0 or dist / entry > MAX_STOP_PCT:
                    continue
                risk_amt = equity * RISK_PCT / 100
                size = risk_amt / dist
                cap = equity * LEVERAGE / MAX_POS
                if size * entry > cap:
                    size = cap / entry
                if size <= 0:
                    continue
                fee_in = size * entry * FEE
                equity -= fee_in
                pos = {"d": d, "entry": entry, "stop": entry - d * dist,
                       "size": size, "left": size, "pnl": -fee_in, "risk": risk_amt,
                       "bars": 0, "tp1_done": False, "atr": atr_v, "ref": entry}
                if p["mode"] == "fixed":
                    pos["tp1"] = entry + d * p["tp1"] * dist
                    pos["tp2"] = entry + d * p["tp2"] * dist
                open_pos[s] = pos

        for s in list(open_pos):
            a = A[s]
            h, l, c = a["high"][i], a["low"][i], a["close"][i]
            if np.isnan(h):
                continue
            pos = open_pos[s]
            d = pos["d"]
            pos["bars"] += 1
            hit_stop = (l <= pos["stop"]) if d == 1 else (h >= pos["stop"])
            done = False
            if p["mode"] == "trail":
                if hit_stop:
                    realize(pos, pos["left"], pos["stop"], True)
                    done = True
                else:
                    if d == 1:
                        pos["ref"] = max(pos["ref"], h)
                        pos["stop"] = max(pos["stop"], pos["ref"] - p["trail"] * pos["atr"])
                    else:
                        pos["ref"] = min(pos["ref"], l)
                        pos["stop"] = min(pos["stop"], pos["ref"] + p["trail"] * pos["atr"])
            else:
                hit2 = (h >= pos["tp2"]) if d == 1 else (l <= pos["tp2"])
                if not pos["tp1_done"]:
                    if hit_stop:
                        realize(pos, pos["left"], pos["stop"], True)
                        done = True
                    else:
                        hit1 = (h >= pos["tp1"]) if d == 1 else (l <= pos["tp1"])
                        if hit1:
                            realize(pos, pos["size"] * 0.5, pos["tp1"], False)
                            pos["tp1_done"] = True
                            pos["stop"] = pos["entry"]
                            if hit2:
                                realize(pos, pos["left"], pos["tp2"], False)
                                done = True
                else:
                    if hit_stop:
                        realize(pos, pos["left"], pos["stop"], True)
                        done = True
                    elif hit2:
                        realize(pos, pos["left"], pos["tp2"], False)
                        done = True
            if not done and pos["bars"] >= p["max_hold"]:
                realize(pos, pos["left"], c, True)
                done = True
            if done:
                finalize(s, pos)

    for s in list(open_pos):
        pos = open_pos[s]
        c = A[s]["close"][-1]
        if not np.isnan(c):
            realize(pos, pos["left"], c, True)
        finalize(s, pos)

    return equity, max_dd, trades


def stats(equity, max_dd, trades):
    n = len(trades)
    ret = (equity / START_EQUITY - 1) * 100
    if n == 0:
        return {"ret": 0.0, "pf": 0.0, "n": 0, "win": 0.0, "dd": 0.0}
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    return {"ret": ret, "pf": gp / gl if gl > 0 else 9.99, "n": n,
            "win": sum(1 for t in trades if t["pnl"] > 0) / n * 100,
            "dd": max_dd * 100}


def line(label, st, btc_chg):
    return (f"{label}: {st['ret']:+.0f}% | PF {st['pf']:.2f} | {st['n']} islem | "
            f"%{st['win']:.0f} kazanma | DD %{st['dd']:.0f} (BTC {btc_chg:+.0f}%)")


def main():
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (TEST_DAYS + WARMUP_DAYS) * 86400 * 1000
    data = {}
    for s in SYMBOLS:
        try:
            df = fetch(s, start_ms, end_ms)
            if len(df) > 2500:
                data[s] = df
            else:
                print(s, "yetersiz veri:", len(df))
        except Exception as e:
            print(s, "hata:", e)
    if "BTCUSDT" not in data:
        send("Backtest hatasi: BTC verisi alinamadi.")
        return

    btc = data["BTCUSDT"]
    btc_tr = trend_frames(btc)
    bases = {s: base_frame(df) for s, df in data.items()}
    trs = {s: trend_frames(df) for s, df in data.items()}

    full_idx = btc.index[btc.index >= btc.index[-1] - pd.Timedelta(days=TEST_DAYS)]
    mid = len(full_idx) // 2
    halves = [("Eski 90g", full_idx[:mid]), ("Yeni 90g", full_idx[mid:])]
    btc_chg = [(btc["close"].reindex(ix).iloc[-1] / btc["close"].reindex(ix).iloc[0] - 1) * 100
               for _, ix in halves]

    out = [f"STRATEJI KARSILASTIRMA (1s grafik, 10x, risk %{RISK_PCT:g}, {len(data)} coin)\n"]
    for name, (fn, params) in STRATS.items():
        feats = {s: build(bases[s], trs[s], btc_tr, fn) for s in data}
        res = []
        for (label, ix), bc in zip(halves, btc_chg):
            eq, dd, tr = simulate(feats, ix, params)
            res.append((label, stats(eq, dd, tr), bc))
        ok = all(st["ret"] > 0 and st["pf"] >= 1.15 and st["n"] >= 20 for _, st, _ in res)
        verdict = "UMUTLU (iki donemde de kazandi)" if ok else "ELENDI"
        out.append(name)
        for label, st, bc in res:
            out.append(line(label, st, bc))
        out.append(f"Karar: {verdict}\n")
    out.append("Not: Spot verisi, fonlama ucreti dahil degil. Parametreler "
               "optimize edilmedi, standart degerler kullanildi. Gecmis sonuc "
               "gelecegi garanti etmez.")
    send("\n".join(out))


if __name__ == "__main__":
    main()
