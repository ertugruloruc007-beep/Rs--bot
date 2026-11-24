import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MANUAL = os.environ.get("MANUAL_RUN") == "true"   # sadece elle "Run workflow" ile calistirinca

BASE = "https://data-api.binance.vision/api/v3"

# ---- AYARLAR ----
RSI_LOW = 30                 # RSI bunun altina dusunce sinyal
RSI_HIGH = 70                # RSI bunun ustune cikinca sinyal
TOP_N = 100                  # 24s hacmine gore en buyuk kac coin taransin
MIN_VOL_24H = 20_000_000     # 24s hacmi (USDT) bunun altindaki coinler taranmaz
MAX_RECS = 8                 # bir mesajda en fazla kac coin onerilsin
LIVE_CANDLE = True           # True: mum kapanmadan, canli RSI ile uyar (daha erken, mum kapanisinda degisebilir)
STALE_MARGIN = 5             # RSI bu kadar puan geri donduyse eski sinyal gosterilmez
COOLDOWN_MIN = 60            # ayni coin/sinyal icin tekrar uyari beklemesi (dakika)
FOLLOW_UP_HOURS = 2          # kac saat sonra sonuc notu gonderilsin
STATE_FILE = "signals.json"
TZ_NAME = "Europe/Istanbul"  # saatler bu saat diliminde yazilir
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "BUSD",
           "BFUSD", "USD1", "USDE", "RLUSD", "XUSD", "USDS", "PYUSD", "AEUR",
           "USDD", "EURI", "EURC", "FRAX", "UST"}


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


def looks_stable(t):
    """Fiyati ~1$ civarinda ve 24s araligi %1'den dar olan coinleri stable sayar."""
    try:
        last = float(t["lastPrice"])
        hi = float(t["highPrice"])
        lo = float(t["lowPrice"])
        return 0.95 <= last <= 1.05 and (hi - lo) / last < 0.01
    except Exception:
        return False


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
    """Fiyati okunur yazar (1,55 gibi). Bilimsel gosterim kullanmaz."""
    if x >= 100:
        dec = 2
    elif x >= 1:
        dec = 4
    elif x >= 0.01:
        dec = 5
    elif x >= 0.0001:
        dec = 7
    else:
        dec = 9
    s = f"{x:.{dec}f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def pct(v, digits=1):
    s = f"{abs(v):.{digits}f}".replace(".", ",")
    return f"{'-' if v < 0 else '+'}%{s}"


def col(v):
    """Kar = yesil, zarar = kirmizi kare (Telegram yaziyi boyayamaz, emoji kullaniriz)."""
    if v > 0.05:
        return "🟩"
    if v < -0.05:
        return "🟥"
    return "⬜"


def fmt_usd(x):
    if x >= 1e9:
        return f"{x / 1e9:.1f} milyar$"
    return f"{x / 1e6:.0f} milyon$"


def hhmm(ms):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ_NAME)
    except Exception:
        tz = timezone(timedelta(hours=3))
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%H:%M")


def get_klines(symbol, interval, limit, drop_open=True):
    k = requests.get(
        BASE + "/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    ).json()
    return k[:-1] if drop_open else k   # drop_open: son (acik) mumu atla


def btc_trend():
    """BTC 1 saatlik trend: 1 yukari, -1 asagi, 0 yatay"""
    try:
        k = get_klines("BTCUSDT", "1h", 120)
        closes = [float(c[4]) for c in k]
        e21 = ema_series(closes, 21)[-1]
        e50 = ema_series(closes, 50)[-1]
        c = closes[-1]
        if e21 > e50 and c > e50:
            return 1
        if e21 < e50 and c < e50:
            return -1
        return 0
    except Exception:
        return 0


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    if st.get("version") != 2:          # eski surum kayitlari temizlenir
        st = {"version": 2}
    st.setdefault("signals", [])
    st.setdefault("sent", {})
    st.setdefault("stats", {"n": 0, "win": 0, "sum": 0.0})
    return st


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def follow_up(state, now_ms):
    """Uyariden 2 saat sonra sonucu hesaplar, mesaj bloklari dondurur."""
    items = []
    gains = []
    keep = []
    for sig in state["signals"]:
        end_ms = sig["t0"] + FOLLOW_UP_HOURS * 3600 * 1000
        if now_ms < end_ms + 5000:
            keep.append(sig)
            continue
        if now_ms > end_ms + 6 * 3600 * 1000:      # cok gec kalmis, vazgec
            continue
        start = -(-sig["t0"] // 900000) * 900000   # uyaridan sonraki ilk 15dk mum baslangici
        try:
            k = requests.get(
                BASE + "/klines",
                params={"symbol": sig["sym"], "interval": "15m",
                        "startTime": start, "limit": 10},
                timeout=20,
            ).json()
            k = [c for c in k if c[6] <= end_ms and c[6] < now_ms]   # kapanmis ve 2 saat icinde
            if len(k) < 6:
                keep.append(sig)
                continue
            entry, d = sig["entry"], sig["d"]
            final = float(k[-1][4])
            end_label = hhmm(int(k[-1][6]) + 1)
            hi = max(float(c[2]) for c in k)
            lo = min(float(c[3]) for c in k)
            chg = (final / entry - 1) * 100
            up = (hi / entry - 1) * 100
            down = (lo / entry - 1) * 100
            side = {1: "LONG", -1: "SHORT", 0: "izleme"}[d]
            if d != 0:
                gain = chg * d
                head = f"{col(gain)} {sig['sym'][:-4]}/USDT ({hhmm(sig['t0'])}) {side} önerisi"
                result = f"Önerilen yönde sonuç: {col(gain)} {pct(gain, 2)}"
                gains.append(gain)
                st = state["stats"]
                st["n"] += 1
                st["win"] += 1 if gain > 0 else 0
                st["sum"] += gain
            else:
                head = f"⬜ {sig['sym'][:-4]}/USDT ({hhmm(sig['t0'])}) izleme"
                result = "Yön önerilmediği için kâr/zarar hesaplanmadı"
            items.append(
                f"{head}\n"
                f"{fp(entry)} → {fp(final)} ({end_label})\n"
                f"Fiyat 2 saatte {pct(chg, 2)} {'yükseldi' if chg > 0 else 'düştü'}\n"
                f"2 saat içinde en yüksek {pct(up)}, en düşük {pct(down)}\n"
                f"{result}"
            )
        except Exception as e:
            print(sig["sym"], "takip hatasi:", e)
            keep.append(sig)
    state["signals"] = keep

    blocks = []
    if items:
        blocks.append(f"⏱ {FOLLOW_UP_HOURS} SAAT SONRA - SONUÇLAR")
        blocks += items
        if gains:
            wins = sum(1 for g in gains if g > 0)
            st = state["stats"]
            blocks.append(
                f"Özet: {len(gains)} öneri, {wins} tanesi önerilen yönde kazandırdı. "
                f"Ortalama: {col(sum(gains) / len(gains))} {pct(sum(gains) / len(gains), 2)}\n"
                f"Genel: {st['n']} öneri, %{st['win'] / st['n'] * 100:.0f} kazandırdı, "
                f"ortalama {col(st['sum'] / st['n'])} {pct(st['sum'] / st['n'], 2)}")
        blocks.append("Fiyat, uyarı anındaki fiyattan hesaplanır. Yüzdeler kaldıraçsızdır; komisyon dahil değil.")
    return blocks


def main():
    state = load_state()
    now_ms = int(time.time() * 1000)
    fu_blocks = follow_up(state, now_ms)

    tickers = requests.get(BASE + "/ticker/24hr", timeout=30).json()
    pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and t["symbol"][:-4] not in STABLES
        and float(t["quoteVolume"]) >= MIN_VOL_24H
        and not looks_stable(t)
    ]
    pairs.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    top = pairs[:TOP_N]

    btc = btc_trend()
    d = btc                                  # yon BTC trendine gore
    sent = state["sent"]
    hits = []
    for t in top:
        s = t["symbol"]
        try:
            k = get_klines(s, "15m", 100, drop_open=not LIVE_CANDLE)
            if len(k) < 50:
                continue
            closes = [float(c[4]) for c in k]
            rs = rsi_series(closes)
            i = len(closes) - 1
            kind = None
            for j in (i, i - 1):               # son 2 mum (atlanan calismayi kacirmamak icin)
                if rs[j] is None or rs[j - 1] is None:
                    continue
                if rs[j] < RSI_LOW <= rs[j - 1]:
                    kind = "low"
                    break
                if rs[j] > RSI_HIGH >= rs[j - 1]:
                    kind = "high"
                    break
            if kind is None:
                continue
            # RSI sinyalden sonra geri donduyse (eski sinyal) gosterme
            if kind == "low" and rs[i] >= RSI_LOW + STALE_MARGIN:
                continue
            if kind == "high" and rs[i] <= RSI_HIGH - STALE_MARGIN:
                continue
            skey = f"{s}|{kind}"
            last = sent.get(skey)
            if last and now_ms - last < COOLDOWN_MIN * 60 * 1000:
                continue
            hits.append({"sym": s, "kind": kind, "rsi": rs[i], "price": float(t["lastPrice"]),
                         "vol": float(t["quoteVolume"]), "open_ms": int(k[j][0]),
                         "live": LIVE_CANDLE and j == i, "skey": skey})
        except Exception as e:
            print(s, "hata:", e)

    outgoing = []
    if hits:
        hits.sort(key=lambda h: h["vol"], reverse=True)
        shown = hits[:MAX_RECS]
        btc_txt = {1: "YUKARI", -1: "AŞAĞI", 0: "YATAY"}[btc]
        dir_txt = {1: "LONG", -1: "SHORT", 0: "yön yok (sadece izleme)"}[btc]
        emoji = {1: "🟢", -1: "🔴", 0: "⚪"}[btc]
        side = {1: "LONG", -1: "SHORT", 0: "İZLE"}[btc]
        outgoing.append(f"📊 RSI TARAMASI (15 dk, hacimli coinler)\n"
                        f"BTC 1 saatlik trend: {btc_txt} → öneri yönü: {dir_txt}")
        for h in shown:
            event = "30 altına düştü" if h["kind"] == "low" else "70 üstüne çıktı"
            live_txt = " ⚡ canlı, mum henüz kapanmadı" if h["live"] else ""
            outgoing.append(
                f"{emoji} {h['sym'][:-4]}/USDT - {side}\n"
                f"RSI şu an {h['rsi']:.0f} | {hhmm(h['open_ms'])} mumunda {event}{live_txt}\n"
                f"Fiyat: {fp(h['price'])} | 24s hacim: {fmt_usd(h['vol'])}")
            sent[h["skey"]] = now_ms
            state["signals"].append({"sym": h["sym"], "d": d, "entry": h["price"],
                                     "t0": now_ms})
        outgoing.append(
            "Yön, BTC'nin 1 saatlik trendine göre belirlenir. "
            f"{FOLLOW_UP_HOURS} saat sonra sonuç paylaşılacak. Yatırım tavsiyesi değildir.")
    outgoing += fu_blocks
    # eski kayitlari temizle
    state["sent"] = {k: v for k, v in sent.items() if now_ms - v < 6 * 3600 * 1000}
    if not hits and MANUAL:
        outgoing.insert(0, f"Test başarılı. {len(top)} coin tarandı, şu an yeni sinyal yok.")
    if outgoing:
        send_chunks(outgoing)
    save_state(state)


if __name__ == "__main__":
    main()
