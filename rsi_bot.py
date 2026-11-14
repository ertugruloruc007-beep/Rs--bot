import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

BASE = "https://data-api.binance.vision/api/v3"
RSI_LOW = 25
RSI_HIGH = 75
TOP_N = 300
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "BUSD"}


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


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


def fmt_usd(x):
    if x >= 1e9:
        return f"{x / 1e9:.2f}B$"
    if x >= 1e6:
        return f"{x / 1e6:.1f}M$"
    return f"{x / 1e3:.0f}K$"


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


def main():
    tickers = requests.get(BASE + "/ticker/24hr", timeout=30).json()
    pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and t["symbol"][:-4] not in STABLES
    ]
    pairs.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    top = pairs[:TOP_N]

    low_hits = []
    high_hits = []
    for t in top:
        s = t["symbol"]
        try:
            k = requests.get(
                BASE + "/klines",
                params={"symbol": s, "interval": "15m", "limit": 100},
                timeout=20,
            ).json()
            k = k[:-1]  # son (açık) mumu atla
            closes = [float(c[4]) for c in k]
            vols = [float(c[7]) for c in k]
            r = rsi(closes)

            is_low = r[-1] < RSI_LOW and r[-2] >= RSI_LOW
            is_high = r[-1] > RSI_HIGH and r[-2] <= RSI_HIGH
            if not (is_low or is_high):
                continue

            avg_vol = sum(vols[-21:-1]) / 20
            ratio = vols[-1] / avg_vol if avg_vol > 0 else 0
            line = (
                f"{s[:-4]}/USDT\n"
                f"RSI: {r[-1]:.1f} | Fiyat: {closes[-1]}\n"
                f"24s değişim: {float(t['priceChangePercent']):+.1f}% | "
                f"24s hacim: {fmt_usd(float(t['quoteVolume']))}\n"
                f"Hacim: {ratio:.1f}x | 1s RSI: {hourly_rsi(s)}"
            )
            if is_low:
                low_hits.append(line)
            else:
                high_hits.append(line)
        except Exception as e:
            print(s, "hata:", e)

    parts = []
    if low_hits:
        parts.append("RSI 25 altına düştü (15dk):\n\n" + "\n\n".join(low_hits))
    if high_hits:
        parts.append("RSI 75 üstüne çıktı (15dk):\n\n" + "\n\n".join(high_hits))

    if parts:
        send("\n\n-----\n\n".join(parts))
    elif MANUAL:
        send(f"Test başarılı. {len(top)} coin tarandı, şu an sinyal yok.")


main()
