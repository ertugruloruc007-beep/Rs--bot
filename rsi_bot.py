import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

BASE = "https://data-api.binance.vision/api/v3"
RSI_LEVEL = 30
TOP_N = 100
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


def main():
    tickers = requests.get(BASE + "/ticker/24hr", timeout=30).json()
    pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and t["symbol"][:-4] not in STABLES
    ]
    pairs.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    symbols = [t["symbol"] for t in pairs[:TOP_N]]

    hits = []
    for s in symbols:
        try:
            k = requests.get(
                BASE + "/klines",
                params={"symbol": s, "interval": "15m", "limit": 100},
                timeout=20,
            ).json()
            closes = [float(c[4]) for c in k[:-1]]  # son (açık) mumu atla
            r = rsi(closes)
            if r[-1] < RSI_LEVEL and r[-2] >= RSI_LEVEL:
                hits.append(f"{s[:-4]}/USDT  RSI: {r[-1]:.1f}  Fiyat: {closes[-1]}")
        except Exception as e:
            print(s, "hata:", e)

    if hits:
        send("RSI 30 altına düştü (15dk):\n\n" + "\n".join(hits))
    elif MANUAL:
        send(f"Test başarılı. {len(symbols)} coin tarandı, şu an sinyal yok.")


main()
