import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

BASE = "https://data-api.binance.vision/api/v3"
TOP_N = 300
MIN_VOL_24H = 5_000_000
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "BUSD"}

# ---- ONERI AYARLARI ----
MIN_SCORE = 3                # 5 uzerinden en az bu skoru alan coinler onerilir
MAX_RECS = 5                 # en fazla kac coin onerilsin
VOL_SPIKE = 1.5              # hacim, son 20 mum ortalamasinin en az bu kati olmali

# ---- ISLEM PLANI AYARLARI ----
ACCOUNT_USDT = 1000          # <-- Binance Futures hesabindaki toplam bakiyen (USDT). Burayi guncelle!
RISK_PCT = 2
LEVERAGE = 10
ATR_MULT = 2.0
MAX_MARGIN_PCT = 30
MAX_STOP_PCT = 8

# ---- TAKIP NOTU AYARLARI ----
STATE_FILE = "signals.json"
TZ_NAME = "Europe/Istanbul"  # saatler bu saat diliminde yazilir
FOLLOW_UP_HOURS = (1, 2)     # kac saat sonra sonuc notu gonderilsin


def rsi_series(closes, period=14):
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out = [None] * period
    out.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def ema_series(values, span):
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


def send_chunks(blocks):
    cur = ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3600 and cur:
            send(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        send(cur)


def fp(x):
    return f"{x:.6g}"


def get_klines(symbol, interval, limit):
    k = requests.get(
        BASE + "/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    ).json()
    return k[:-1]  # son (acik) mumu atla


def htf_trend(symbol):
    """1 saatlik trend: 1 yukari, -1 asagi, 0 yatay, None bilinmiyor"""
    try:
        k = get_klines(symbol, "1h", 120)
        closes = [float(c[4]) for c in k]
        e21 = ema_series(closes, 21)[-1]
        e50 = ema_series(closes, 50)[-1]
        if closes[-1] > e21 > e50:
            return 1
        if closes[-1] < e21 < e50:
            return -1
        return 0
    except Exception:
        return None


def eval_dir(d, i, closes, vols, rs, e9, e21, e50):
    """15dk icin 4 kosulu degerlendirir. d: 1 long, -1 short."""
    c = closes[i]
    r = rs[i]
    pts = {}
    # 1) EMA trend: EMA21, EMA50'nin uzerinde (long) ve fiyat EMA21 uzerinde
    pts["EMA trend"] = (e21[i] - e50[i]) * d > 0 and (c - e21[i]) * d > 0
    # 2) EMA9/EMA21 kesisimi son 3 mumda
    cross = False
    for j in range(i - 2, i + 1):
        if (e9[j] - e21[j]) * d > 0 and (e9[j - 1] - e21[j - 1]) * d <= 0:
            cross = True
    pts["EMA9/21 kesisim"] = cross and (e9[i] - e21[i]) * d > 0
    # 3) RSI: ya momentum bolgesinde ya da asiri bolgeden toparlaniyor
    if d == 1:
        mom = 50 <= r <= 70 and r > rs[i - 3]
        reb = (min(rs[i - 8:i + 1]) < 30 and 35 <= r <= 60
               and r > rs[i - 1] and c > e9[i])
    else:
        mom = 30 <= r <= 50 and r < rs[i - 3]
        reb = (max(rs[i - 8:i + 1]) > 70 and 40 <= r <= 65
               and r < rs[i - 1] and c < e9[i])
    pts["RSI"] = mom or reb
    # 4) Hacim
    avg = sum(vols[i - 20:i]) / 20
    ratio = vols[i] / avg if avg > 0 else 0
    pts["Hacim"] = ratio >= VOL_SPIKE
    # asiri uzamis hareketi kovalama
    if (d == 1 and r > 72) or (d == -1 and r < 28):
        pts = {k: False for k in pts}
    return pts, ratio


def plan_levels(d, entry, highs, lows, closes):
    dist = ATR_MULT * atr(highs, lows, closes)
    risk_amt = ACCOUNT_USDT * RISK_PCT / 100
    notional = risk_amt / dist * entry
    cap = ACCOUNT_USDT * LEVERAGE * MAX_MARGIN_PCT / 100
    risk_real = risk_amt
    if notional > cap:
        notional = cap
        risk_real = notional / entry * dist
    return {
        "stop": entry - d * dist,
        "tp1": entry + d * dist,
        "tp2": entry + d * 2 * dist,
        "stop_pct": dist / entry * 100,
        "risk": risk_real,
        "notional": notional,
        "margin": notional / LEVERAGE,
    }


def plan_lines(entry, lv):
    lines = [
        f"Giris ~{fp(entry)} | Stop {fp(lv['stop'])} (%{lv['stop_pct']:.1f})",
        f"Hedef1 {fp(lv['tp1'])} | Hedef2 {fp(lv['tp2'])}",
        f"Risk {lv['risk']:.0f}$ | Pozisyon {lv['notional']:.0f}$ | Marjin {lv['margin']:.0f}$",
    ]
    if lv["stop_pct"] > MAX_STOP_PCT:
        lines.append("Uyari: Stop cok uzak, ATLA.")
    return lines


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("signals", [])
    st.setdefault("stats", {})
    for h in FOLLOW_UP_HOURS:
        st["stats"].setdefault(str(h), {"n": 0, "win": 0, "sum": 0.0})
    return st


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def hhmm(ms):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ_NAME)
    except Exception:
        tz = timezone(timedelta(hours=3))
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%H:%M")


def follow_up(state, now_ms):
    """Suresi dolan onerilerin sonucunu hesaplar, mesaj bloklari dondurur."""
    items = {h: [] for h in FOLLOW_UP_HOURS}
    pnls = {h: [] for h in FOLLOW_UP_HOURS}
    for sig in state["signals"]:
        for h in FOLLOW_UP_HOURS:
            key = str(h)
            if key in sig["done"]:
                continue
            n_c = 4 * h
            end_ms = sig["t0"] + h * 3600 * 1000
            if now_ms < end_ms + 5000:
                continue
            if now_ms > end_ms + 6 * 3600 * 1000:   # cok gec kalmis, vazgec
                sig["done"].append(key)
                continue
            try:
                k = requests.get(
                    BASE + "/klines",
                    params={"symbol": sig["sym"], "interval": "15m",
                            "startTime": sig["t0"], "limit": n_c},
                    timeout=20,
                ).json()
                k = [c for c in k if c[6] < now_ms]   # sadece kapanmis mumlar
                if len(k) < n_c:
                    continue
                d, entry = sig["d"], sig["entry"]
                final = float(k[-1][4])
                hi = max(float(c[2]) for c in k)
                lo = min(float(c[3]) for c in k)
                price_chg = (final / entry - 1) * 100
                if d == 1:
                    pnl = price_chg
                    best = (hi / entry - 1) * 100
                    worst = (lo / entry - 1) * 100
                    stop_hit = lo <= sig["stop"]
                    t1 = hi >= sig["tp1"]
                    t2 = hi >= sig["tp2"]
                else:
                    pnl = -price_chg
                    best = (1 - lo / entry) * 100
                    worst = (1 - hi / entry) * 100
                    stop_hit = hi >= sig["stop"]
                    t1 = lo <= sig["tp1"]
                    t2 = lo <= sig["tp2"]
                side = "LONG" if d == 1 else "SHORT"
                text = (
                    f"{sig['sym'][:-4]}/USDT {side} ({hhmm(sig['t0'])} onerisi)\n"
                    f"{fp(entry)} -> {fp(final)} (fiyat %{price_chg:+.2f})\n"
                    f"Islem: %{pnl:+.2f} {'kazanc' if pnl > 0 else 'zarar'} "
                    f"| {LEVERAGE}x ile ~%{pnl * LEVERAGE:+.1f}\n"
                    f"Aralik: en iyi %{best:+.1f}, en kotu %{worst:+.1f}\n"
                    f"Stop {'DEGDI' if stop_hit else 'degmedi'} | "
                    f"Hedef1 {'degdi' if t1 else 'degmedi'} | "
                    f"Hedef2 {'degdi' if t2 else 'degmedi'}"
                )
                items[h].append(text)
                pnls[h].append(pnl)
                st = state["stats"][key]
                st["n"] += 1
                st["win"] += 1 if pnl > 0 else 0
                st["sum"] += pnl
                sig["done"].append(key)
            except Exception as e:
                print(sig["sym"], "takip hatasi:", e)

    blocks = []
    for h in FOLLOW_UP_HOURS:
        if not items[h]:
            continue
        p = pnls[h]
        wins = sum(1 for x in p if x > 0)
        st = state["stats"][str(h)]
        blocks.append(f"{h} SAAT SONRA - ONERI TAKIP NOTLARI")
        blocks += items[h]
        blocks.append(
            f"Bu grup: {len(p)} oneri, {wins} tanesi yonunde, ort %{sum(p) / len(p):+.2f}\n"
            f"Genel ({h}s): {st['n']} oneri, %{st['win'] / st['n'] * 100:.0f} yonunde, "
            f"ort %{st['sum'] / st['n']:+.2f}")
    if blocks:
        blocks.append("Fiyatlar 15dk mum kapanislaridir; komisyon ve kayma dahil degil.")
    return blocks


def main():
    state = load_state()
    now_ms = int(time.time() * 1000)
    fu_blocks = follow_up(state, now_ms)
    state["signals"] = [s for s in state["signals"]
                        if len(s["done"]) < len(FOLLOW_UP_HOURS)]
    save_state(state)

    tickers = requests.get(BASE + "/ticker/24hr", timeout=30).json()
    pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and t["symbol"][:-4] not in STABLES
        and float(t["quoteVolume"]) >= MIN_VOL_24H
    ]
    pairs.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    top = pairs[:TOP_N]

    recs = []
    for t in top:
        s = t["symbol"]
        try:
            k = get_klines(s, "15m", 150)
            if len(k) < 80:
                continue
            closes = [float(c[4]) for c in k]
            highs = [float(c[2]) for c in k]
            lows = [float(c[3]) for c in k]
            vols = [float(c[7]) for c in k]
            rs = rsi_series(closes)
            e9 = ema_series(closes, 9)
            e21 = ema_series(closes, 21)
            e50 = ema_series(closes, 50)
            i = len(closes) - 1

            best = None
            for d in (1, -1):
                pts, ratio = eval_dir(d, i, closes, vols, rs, e9, e21, e50)
                n = sum(pts.values())
                if n >= MIN_SCORE - 1 and (best is None or n > best[1]):
                    best = (d, n, pts, ratio)
            if best is None:
                continue
            d, n, pts, ratio = best

            h = htf_trend(s)
            h_ok = (h == d)
            total = n + (1 if h_ok else 0)
            if total < MIN_SCORE:
                continue

            # sadece YENI olusan sinyali bildir (onceki mumda da varsa atla)
            pts_prev, _ = eval_dir(d, i - 1, closes, vols, rs, e9, e21, e50)
            prev_total = sum(pts_prev.values()) + (1 if h_ok else 0)
            if prev_total >= MIN_SCORE:
                continue

            reasons = [name for name, ok in pts.items() if ok]
            if h_ok:
                reasons.append("1s trend")
            side = "LONG" if d == 1 else "SHORT"
            arrow = "yukari" if d == 1 else "asagi"
            head = f"{s[:-4]}/USDT  {side}  Skor {total}/5"
            body = [f"{' + '.join(reasons)}",
                    f"RSI {rs[i]:.0f} ({arrow}) | Hacim {ratio:.1f}x | 24s {float(t['priceChangePercent']):+.1f}%"]
            lv = plan_levels(d, closes[i], highs, lows, closes)
            body += plan_lines(closes[i], lv)
            sig = {"sym": s, "d": d, "entry": closes[i], "stop": lv["stop"],
                   "tp1": lv["tp1"], "tp2": lv["tp2"], "t0": int(k[-1][6]) + 1,
                   "done": []}
            recs.append((total, ratio, head + "\n" + "\n".join(body), sig))
        except Exception as e:
            print(s, "hata:", e)

    outgoing = []
    if recs:
        recs.sort(key=lambda x: (x[0], x[1]), reverse=True)
        btc = htf_trend("BTCUSDT")
        btc_txt = {1: "YUKARI", -1: "ASAGI", 0: "YATAY", None: "?"}[btc]
        shown = recs[:MAX_RECS]
        head = (f"15DK TARAMA - ONERILEN COINLER\n"
                f"BTC 1s trend: {btc_txt} | {len(recs)} oneri, en iyi {len(shown)}\n"
                f"Risk %{RISK_PCT} = {ACCOUNT_USDT * RISK_PCT / 100:.0f}$, {LEVERAGE}x")
        outgoing.append(head)
        outgoing += [f"{n}) {r[2]}" for n, r in enumerate(shown, 1)]
        outgoing.append(
            "Skor = teknik uyum sayisidir (EMA trend, EMA kesisim, RSI, hacim, 1s trend), "
            "kazanma olasiligi degil. Hedef1'de yarisini kapat, stopu girise cek. "
            "Girer girmez Stop Market emri gir. Ayni yonde en fazla 1-2 islem ac. "
            "Yatirim tavsiyesi degildir.")
        state["signals"] += [r[3] for r in shown]
    outgoing += fu_blocks
    if not recs and MANUAL:
        outgoing.insert(0, f"Test basarili. {len(top)} coin tarandi, su an yeni oneri yok.")
    if outgoing:
        send_chunks(outgoing)
    save_state(state)


if __name__ == "__main__":
    main()
