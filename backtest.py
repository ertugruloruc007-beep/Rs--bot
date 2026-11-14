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

TEST_DAYS = 90
WARMUP_DAYS = 15
START_EQUITY = 1000.0
LEVERAGE = 10
MAX_POS = 3
FEE = 0.0005          # her islem tarafi icin %0.05 (taker)
SLIP = 0.0002         # %0.02 kayma (giris ve stop cikislarinda)
ATR_MULT = 1.5        # stop mesafesi = 1.5 x ATR
TP1_R = 1.0           # pozisyonun yarisi 1R'de kapanir, stop girise cekilir
TP2_R = 2.5           # kalan yari 2.5R'de kapanir
MAX_HOLD = 96         # en fazla 96 mum (24 saat)
DAILY_LOSS = 0.03     # gunluk %3 zarara ulasinca o gun yeni islem yok
RSI_LONG = 35
RSI_SHORT = 65
ADX_MIN = 20
MAX_STOP_PCT = 0.08   # stop %8'den uzaksa islemi atla (10x'te tasfiye riski)


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
            params={"symbol": symbol, "interval": "15m",
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
    return df.iloc[:-1]  # son (kapanmamis) mumu at


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
    """1 saatlik trend. Tamamlanmis mumlar kullanilir (shift ile ileri bakis yok)."""
    h = df.resample("1h").agg({"open": "first", "high": "max", "low": "min",
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
    out = out.reindex(df.index, method="ffill").fillna(0) > 0
    return out


def build(df, btc_tr):
    tr = trend_frames(df)
    btc_up = btc_tr["loose_up"].reindex(df.index, fill_value=False)
    btc_dn = btc_tr["loose_down"].reindex(df.index, fill_value=False)
    f = df.copy()
    f["rsi"] = rsi(f["close"])
    f["atr"] = atr(f)
    f["vr"] = f["volume"] / f["volume"].rolling(20).mean().shift(1)
    rmin = f["rsi"].rolling(3).min()
    rmax = f["rsi"].rolling(3).max()
    long_sig = (tr["up"] & btc_up & (rmin < RSI_LONG) & (f["rsi"] > f["rsi"].shift(1))
                & (f["close"] > f["open"]) & (f["vr"] >= 1.0))
    short_sig = (tr["down"] & btc_dn & (rmax > RSI_SHORT) & (f["rsi"] < f["rsi"].shift(1))
                 & (f["close"] < f["open"]) & (f["vr"] >= 1.0))
    direction = pd.Series(0, index=f.index)
    direction[long_sig] = 1
    direction[short_sig] = -1
    # sinyal mum kapanisinda olusur, giris bir sonraki mumun acilisinda
    f["dir"] = direction.shift(1).fillna(0)
    f["sig_atr"] = f["atr"].shift(1)
    f["sig_vr"] = f["vr"].shift(1)
    return f


def simulate(feats, idx):
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

    def finalize(sym, pos, ts):
        trades.append({"sym": sym, "d": pos["d"], "pnl": pos["pnl"],
                       "r": pos["pnl"] / pos["risk"], "bars": pos["bars"]})
        del open_pos[sym]

    for i in range(n):
        ts = idx[i]
        if ts.date() != cur_day:
            cur_day = ts.date()
            day_start = equity
        blocked = equity <= day_start * (1 - DAILY_LOSS)

        # 1) yeni girisler (mumun acilisinda)
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
                dist = ATR_MULT * a["sig_atr"][i]
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
                open_pos[s] = {
                    "d": d, "entry": entry, "stop": entry - d * dist,
                    "tp1": entry + d * TP1_R * dist, "tp2": entry + d * TP2_R * dist,
                    "size": size, "left": size, "pnl": -fee_in, "risk": risk_amt,
                    "bars": 0, "tp1_done": False,
                }

        # 2) acik pozisyonlari bu mumda yonet
        for s in list(open_pos):
            a = A[s]
            h, l, c = a["high"][i], a["low"][i], a["close"][i]
            if np.isnan(h):
                continue
            pos = open_pos[s]
            d = pos["d"]
            pos["bars"] += 1
            hit_stop = (l <= pos["stop"]) if d == 1 else (h >= pos["stop"])
            hit2 = (h >= pos["tp2"]) if d == 1 else (l <= pos["tp2"])
            done = False
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
            if not done and pos["bars"] >= MAX_HOLD:
                realize(pos, pos["left"], c, True)
                done = True
            if done:
                finalize(s, pos, ts)

    # donem sonunda acik kalanlari son fiyattan kapat
    for s in list(open_pos):
        pos = open_pos[s]
        c = A[s]["close"][-1]
        if not np.isnan(c):
            realize(pos, pos["left"], c, True)
        finalize(s, pos, idx[-1])

    return equity, max_dd, trades


def report(equity, max_dd, trades, days):
    n = len(trades)
    if n == 0:
        return "Backtest: hic islem uretilmedi."
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    avg_r = sum(t["r"] for t in trades) / n
    longs = [t for t in trades if t["d"] == 1]
    shorts = [t for t in trades if t["d"] == -1]
    by_sym = {}
    for t in trades:
        by_sym[t["sym"]] = by_sym.get(t["sym"], 0) + t["pnl"]
    ranked = sorted(by_sym.items(), key=lambda x: x[1], reverse=True)
    best = ", ".join(f"{s[:-4]} {p:+.0f}$" for s, p in ranked[:3])
    worst = ", ".join(f"{s[:-4]} {p:+.0f}$" for s, p in ranked[-3:])
    ret = (equity / START_EQUITY - 1) * 100

    def side(ts):
        if not ts:
            return "0 islem"
        w = sum(1 for t in ts if t["pnl"] > 0)
        return f"{len(ts)} islem, %{w / len(ts) * 100:.0f} kazanma, {sum(t['pnl'] for t in ts):+.0f}$"

    return (
        f"BACKTEST SONUCU (son {days} gun, 15dk, 10x, risk %{RISK_PCT:g})\n"
        f"Baslangic: {START_EQUITY:.0f}$ -> Bitis: {equity:.0f}$ ({ret:+.1f}%)\n"
        f"En buyuk dusus (drawdown): %{max_dd * 100:.1f}\n\n"
        f"Islem sayisi: {n} (gunde ~{n / days:.1f})\n"
        f"Kazanma orani: %{len(wins) / n * 100:.0f}\n"
        f"Profit factor: {pf:.2f}\n"
        f"Ortalama islem: {avg_r:+.2f}R\n\n"
        f"LONG: {side(longs)}\n"
        f"SHORT: {side(shorts)}\n\n"
        f"En iyi: {best}\n"
        f"En kotu: {worst}\n\n"
        f"Not: Binance spot verisi kullanildi, fonlama ucreti dahil degil. "
        f"Gecmis sonuc gelecegi garanti etmez."
    )


def main():
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (TEST_DAYS + WARMUP_DAYS) * 86400 * 1000
    data = {}
    for s in SYMBOLS:
        try:
            df = fetch(s, start_ms, end_ms)
            if len(df) > 3000:
                data[s] = df
            else:
                print(s, "yetersiz veri:", len(df))
        except Exception as e:
            print(s, "hata:", e)
    if "BTCUSDT" not in data:
        send("Backtest hatasi: BTC verisi alinamadi.")
        return

    btc_tr = trend_frames(data["BTCUSDT"])
    feats = {s: build(df, btc_tr) for s, df in data.items()}

    btc_idx = data["BTCUSDT"].index
    idx = btc_idx[btc_idx >= btc_idx[-1] - pd.Timedelta(days=TEST_DAYS)]

    equity, max_dd, trades = simulate(feats, idx)
    send(report(equity, max_dd, trades, TEST_DAYS))


if __name__ == "__main__":
    main()
