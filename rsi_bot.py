import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

BASE = "https://data-api.binance.vision/api/v3"
RSI_LOW = 25
RSI_HIGH = 75
TOP_N = 300
MIN_VOL_24H = 5_000_000      # 24s hacmi bunun altindaki coinler taranmaz
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "BUSD"}

# ---- ISLEM PLANI AYARLARI ----
ACCOUNT_USDT = 1000          # <-- Binance Futures hesabindaki toplam bakiyen (USDT). Burayi guncelle!
RISK_PCT = 2                 # islem basina risk yuzdesi
LEVERAGE = 10
ATR_MULT = 2.0               # stop mesafesi = 2 x ATR (15dk)
MAX_MARGIN_PCT = 30          # tek islemde hesabin en fazla %30'u marjin olsun
MAX_STOP_PCT = 8             # stop bundan uzaksa "atla" uyarisi
FEE = 0.0005                 # taker komisyonu (tek yon)
MAX_PLANS = 3                # detayli plan yazilacak en guclu sinyal sayisi
MARKET_WARN_COUNT = 5        # bu kadar coinde ayni anda ayni yonde sinyal = piyasa uyarisi


def rsi(closes, period=14):
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


def ema(values, span):
    k = 2 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


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


def fmt_usd(x):
    if x >= 1e9:
        return f"{x / 1e9:.2f}B$"
    if x >= 1e6:
        return f"{x / 1e6:.1f}M$"
    return f"{x / 1e3:.0f}K$"


def fp(x):
    return f"{x:.6g}"


def hourly_rsi(symbol):
    try:
        k = requests.get(
            BASE + "/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 100},
            timeout=20,
        ).json()
        closes = [float(c[4]) for c in k[:-1]]
        return f"{rsi(closes)[-1]:.1f}"
    except Exception:
        return "?"


def trend_4h(symbol):
    """1: yukari, -1: asagi, 0: yatay, None: bilinmiyor"""
    try:
        k = requests.get(
            BASE + "/klines",
            params={"symbol": symbol, "interval": "4h", "limit": 300},
            timeout=20,
        ).json()
        closes = [float(c[4]) for c in k[:-1]]
        if len(closes) < 210:
            return None
        e50, e200 = ema(closes, 50), ema(closes, 200)
        if e50 > e200 and closes[-1] > e200:
            return 1
        if e50 < e200 and closes[-1] < e200:
            return -1
        return 0
    except Exception:
        return None


def side_block(entry, dist, d, trend):
    stop = entry - d * dist
    tp1 = entry + d * dist
    tp2 = entry + d * 2 * dist
    side = "LONG" if d == 1 else "SHORT"
    if trend == d:
        tag = "trend yonunde"
    elif trend == -d:
        tag = "trend tersi, riskli"
    else:
        tag = "trend belirsiz"
    return (f"{side} ({tag})\n"
            f"Giris ~{fp(entry)} | Stop {fp(stop)}\n"
            f"Hedef1 {fp(tp1)} | Hedef2 {fp(tp2)}")


def plan_text(sym, d, closes, highs, lows, rsi_val, h_rsi, trend, qvol, ratio):
    """d: RSI yonu (1 = dipte, -1 = tepede). Iki yonlu plan uretir."""
    entry = closes[-1]
    dist = ATR_MULT * atr(highs, lows, closes)
    stop_pct = dist / entry * 100

    risk_amt = ACCOUNT_USDT * RISK_PCT / 100
    notional = risk_amt / dist * entry
    cap = ACCOUNT_USDT * LEVERAGE * MAX_MARGIN_PCT / 100
    risk_real = risk_amt
    if notional > cap:
        notional = cap
        risk_real = notional / entry * dist
    margin = notional / LEVERAGE
    fee = notional * FEE * 2

    tname = {1: "YUKARI", -1: "ASAGI", 0: "YATAY", None: "?"}[trend]
    # trend yonundeki plan once yazilir
    order = [d, -d] if trend != -d else [-d, d]

    lines = [
        f"{sym[:-4]}/USDT | RSI {rsi_val:.1f} (15dk) {h_rsi} (1s) | 4s trend: {tname}",
        f"Hacim {ratio:.1f}x | 24s hacim {fmt_usd(qvol)} | Stop mesafesi %{stop_pct:.1f}",
        f"Risk {risk_real:.0f}$ | Pozisyon {notional:.0f}$ | Marjin ({LEVERAGE}x) {margin:.0f}$ | Komisyon ~{fee:.1f}$",
        "(Boyut iki yon icin de aynidir)",
        "",
        side_block(entry, dist, order[0], trend),
        "",
        side_block(entry, dist, order[1], trend),
    ]
    if risk_real < risk_amt * 0.999:
        lines.append(f"Uyari: Marjin sinirina takildi, risk %{risk_real / ACCOUNT_USDT * 100:.1f}'e dustu.")
    if stop_pct > MAX_STOP_PCT:
        lines.append("Uyari: Stop cok uzak, 10x icin uygun degil. ATLA.")
    if risk_real > 0 and fee / risk_real > 0.25:
        lines.append("Uyari: Komisyon riskin buyuk kismi.")
    return "\n".join(lines)


def main():
    tickers = requests.get(BASE + "/ticker/24hr", timeout=30).json()
    pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and t["symbol"][:-4] not in STABLES
        and float(t["quoteVolume"]) >= MIN_VOL_24H
    ]
    pairs.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    top = pairs[:TOP_N]

    hits = []
    for t in top:
        s = t["symbol"]
        try:
            k = requests.get(
                BASE + "/klines",
                params={"symbol": s, "interval": "15m", "limit": 100},
                timeout=20,
            ).json()
            k = k[:-1]  # son (acik) mumu atla
            closes = [float(c[4]) for c in k]
            highs = [float(c[2]) for c in k]
            lows = [float(c[3]) for c in k]
            vols = [float(c[7]) for c in k]
            r = rsi(closes)

            is_low = r[-1] < RSI_LOW and r[-2] >= RSI_LOW
            is_high = r[-1] > RSI_HIGH and r[-2] <= RSI_HIGH
            if not (is_low or is_high):
                continue

            d = 1 if is_low else -1
            avg_vol = sum(vols[-21:-1]) / 20
            ratio = vols[-1] / avg_vol if avg_vol > 0 else 0
            trend = trend_4h(s)
            text = plan_text(s, d, closes, highs, lows, r[-1], hourly_rsi(s),
                             trend, float(t["quoteVolume"]), ratio)
            aligned = 1 if trend == d else 0
            hits.append({"aligned": aligned, "ratio": ratio, "text": text,
                         "d": d, "rsi": r[-1], "trend": trend, "sym": s})
        except Exception as e:
            print(s, "hata:", e)

    if hits:
        hits.sort(key=lambda x: (x["aligned"], x["ratio"]), reverse=True)
        lows_n = sum(1 for h in hits if h["d"] == 1)
        highs_n = len(hits) - lows_n
        risk_each = ACCOUNT_USDT * RISK_PCT / 100
        shown = hits[:MAX_PLANS]

        head = (f"RSI SINYALI - ISLEM PLANLARI (15dk, risk %{RISK_PCT} = "
                f"{risk_each:.0f}$, {LEVERAGE}x)\n"
                f"{len(hits)} sinyal var, en guclu {len(shown)} tanesi detayli.")
        blocks = [head]
        if max(lows_n, highs_n) >= MARKET_WARN_COUNT:
            kind = "DIBINE VURDU (sert dusus)" if lows_n >= highs_n else "TEPESINE CIKTI (sert yukselis)"
            blocks.append(
                f"PIYASA UYARISI: {max(lows_n, highs_n)} coinde ayni anda RSI {kind}. "
                f"Coinler birlikte hareket eder. Ayni yonde en fazla 1-2 islem ac; "
                f"{len(shown)} plana birden girersen risk birlesir "
                f"({len(shown) * risk_each:.0f}$).")
        blocks += [h["text"] for h in shown]
        rest = hits[MAX_PLANS:]
        if rest:
            items = [f"{h['sym'][:-4]} {h['rsi']:.0f} {'dip' if h['d'] == 1 else 'tepe'}"
                     for h in rest[:20]]
            more = f" (+{len(rest) - 20} daha)" if len(rest) > 20 else ""
            blocks.append("Diger sinyaller (detaysiz): " + ", ".join(items) + more)
        blocks.append(
            "Hedef1'de pozisyonun yarisini kapat ve stopu girise cek, Hedef2'de "
            "kalani kapat. Girer girmez Stop Market emrini borsaya gir.\n"
            "Not: Asiri RSI'da trend yonunde girmek dipte satmak / tepede almak "
            "demektir, ani tepki stopa yakalar; tercihen tepkiden sonra girisi bekle. "
            "Bu bir yatirim tavsiyesi degil, hesap araci. RSI tek basina kar kaniti degil.")
        send_chunks(blocks)
    elif MANUAL:
        send(f"Test basarili. {len(top)} coin tarandi, su an sinyal yok.")


if __name__ == "__main__":
    main()
