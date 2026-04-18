"""
RAVE SCANNER — CEX + DEX Pump Detector
Adapted for Railway deployment (no file I/O, env-var ready, crash-safe)
Detects 50%+ pumps with 8x volume across 7 exchanges + DEX Screener
"""

import ccxt
import time
import json
import requests
import os
from datetime import datetime

# ============================================================
#  CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8383111441:AAE8yM-CwnABcXAooiPWqPG9N__YLeApyi8")
CHAT_ID        = os.environ.get("CHAT_ID",        "5004814618")

EXCHANGES              = ['binance', 'bybit', 'okx', 'kucoin', 'mexc', 'gate', 'bitget']
MIN_24H_VOLUME_USD     = 50_000
PRICE_PUMP_THRESHOLD   = 0.50       # 50%+ pump since last scan
VOLUME_SPIKE_MULT      = 8          # 8x volume vs previous scan
FUNDING_NEG_THRESHOLD  = -0.0005
SCAN_INTERVAL_SEC      = 300        # 5 minutes

# ============================================================
#  IN-MEMORY STATE  (Railway has no persistent disk on free tier)
#  State resets on redeploy — acceptable for pump detection
# ============================================================
last_data   = {}   # {exchange_sym: {price, vol, time}}
alerted_set = set() # prevent duplicate alerts within a session

# ============================================================
#  TELEGRAM
# ============================================================
def send_telegram(msg: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    CHAT_ID,
            "text":       msg,
            "parse_mode": "HTML"
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  [TG ERROR] {e}")
        return False

# ============================================================
#  COINGECKO — circulating vs total supply
# ============================================================
def get_supply_ratio(base_symbol: str):
    """Returns (circulating, total) or (None, None) on failure."""
    try:
        search = requests.get(
            f"https://api.coingecko.com/api/v3/search?query={base_symbol}",
            timeout=10
        ).json()
        coins = search.get("coins", [])
        if not coins:
            return None, None
        coin_id = coins[0]["id"]
        data = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            timeout=10
        ).json()
        md   = data.get("market_data", {})
        circ = md.get("circulating_supply")
        total = md.get("total_supply") or md.get("max_supply")
        return circ, total
    except:
        return None, None

# ============================================================
#  FUNDING RATE CHECK
# ============================================================
def get_funding_rate(exchange_id: str, symbol: str) -> float:
    try:
        ex  = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        fut = symbol.replace("/USDT", "/USDT:USDT")
        r   = ex.fetch_funding_rate(fut)
        return r.get("fundingRate", 0) or 0
    except:
        return 0

# ============================================================
#  CHART PATTERN — flat weeks → explosive pump
# ============================================================
def is_flat_then_pump(exchange, symbol: str, current_price: float) -> bool:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, "4h", limit=100)
        if len(ohlcv) < 50:
            return False
        closes    = [c[4] for c in ohlcv]
        flat_part = closes[: int(len(closes) * 0.8)]
        if not flat_part or min(flat_part) == 0:
            return False
        if max(flat_part) / min(flat_part) > 1.25:
            return False   # not flat — too much movement in base
        pump = (current_price / flat_part[-1]) - 1
        return pump >= 0.50
    except:
        return False

# ============================================================
#  DEX SCREENER SCAN
# ============================================================
def scan_dexscreener() -> list:
    alerts = []
    try:
        resp  = requests.get(
            "https://api.dexscreener.com/latest/dex/search?q=",
            timeout=15
        ).json()
        pairs = resp.get("pairs", [])[:100]

        for pair in pairs:
            chain = pair.get("chainId", "")
            if chain not in ["solana", "ethereum", "base", "bsc"]:
                continue

            price_change = (
                pair.get("priceChange", {}).get("h1", 0) or
                pair.get("priceChange", {}).get("m5", 0) or 0
            )
            volume = pair.get("volume", {}).get("h1", 0) or 0

            if price_change < 50 or volume < MIN_24H_VOLUME_USD * 5:
                continue

            sym  = pair["baseToken"]["symbol"] + "/" + pair["quoteToken"]["symbol"]
            link = pair.get("url", f"https://dexscreener.com/{chain}/{pair['pairAddress']}")

            # Skip if already alerted this session
            dex_key = f"dex_{chain}_{pair.get('pairAddress','')}"
            if dex_key in alerted_set:
                continue
            alerted_set.add(dex_key)

            alerts.append(
                f"🚀 <b>DEX RUNNER — {chain.upper()}</b>\n"
                f"Pair: {sym}\n"
                f"Pump: +{price_change:.1f}%\n"
                f"Vol (1h): ${volume:,.0f}\n"
                f"Link: {link}\n\n"
                f"<b>Manual RAVE check:</b> supply · holders · team wallets"
            )
    except Exception as e:
        print(f"  [DEX ERROR] {e}")
    return alerts

# ============================================================
#  CEX SCAN — one exchange
# ============================================================
def scan_exchange(ex_id: str) -> int:
    alerts_sent = 0
    try:
        ex      = getattr(ccxt, ex_id)({"enableRateLimit": True})
        tickers = ex.fetch_tickers()
    except Exception as e:
        print(f"  [{ex_id}] fetch_tickers failed: {e}")
        return 0

    for sym, ticker in tickers.items():
        if not sym.endswith("/USDT"):
            continue

        price = ticker.get("last") or 0
        vol   = ticker.get("quoteVolume") or 0

        if price <= 0 or vol < MIN_24H_VOLUME_USD:
            continue

        key = f"{ex_id}_{sym}"
        old = last_data.get(key, {})
        old_price = old.get("price")

        # Update state
        last_data[key] = {"price": price, "vol": vol, "time": time.time()}

        if not old_price or old_price <= 0:
            continue

        pct_change = (price / old_price) - 1
        vol_mult   = vol / max(old.get("vol", 1), 1)

        if pct_change < PRICE_PUMP_THRESHOLD or vol_mult < VOLUME_SPIKE_MULT:
            continue

        # --- RAVE scoring ---
        alert_key = f"{ex_id}_{sym}_{round(price, 6)}"
        if alert_key in alerted_set:
            continue

        base        = sym.split("/")[0]
        score       = 0
        conditions  = []

        # 1. Supply check
        circ, total = get_supply_ratio(base)
        if circ and total and total > 0:
            ratio = circ / total
            if ratio < 0.30:
                conditions.append(f"✅ Supply: {ratio*100:.1f}% circulating (low → bullish)")
                score += 1
            else:
                conditions.append(f"❌ Supply: {ratio*100:.1f}% circulating (high)")
        else:
            conditions.append("⚠️ Supply: data unavailable — check manually")

        # 2. Funding rate
        ref_ex  = "binance" if ex_id != "bybit" else "bybit"
        funding = get_funding_rate(ref_ex, sym)
        if funding <= FUNDING_NEG_THRESHOLD:
            conditions.append(f"✅ Funding: {funding*10000:.2f} bp (negative = bullish squeeze)")
            score += 1
        else:
            conditions.append(f"❌ Funding: {funding*10000:.2f} bp (not negative)")

        # 3. Chart pattern
        chart_ok = is_flat_then_pump(ex, sym, price)
        if chart_ok:
            conditions.append("✅ Chart: flat base → explosive breakout")
            score += 1
        else:
            conditions.append("❌ Chart: no flat→pump pattern")

        # 4. Pump itself (always true at this point)
        conditions.append(f"✅ Pump: +{pct_change*100:.1f}% on {vol_mult:.1f}x volume")

        # Rank
        rank = {3: "💎 DIAMOND RAVE", 2: "🏆 PLATINUM", 1: "🥇 GOLD", 0: "🥈 SILVER"}.get(score, "🥈 SILVER")

        msg = (
            f"<b>{rank} — RUNNER DETECTED</b>\n\n"
            f"Exchange: {ex_id.upper()}\n"
            f"Pair: {sym}\n"
            f"Price: ${price:,.6f}\n\n"
            + "\n".join(conditions) +
            f"\n\n🔍 Top holders: https://www.coingecko.com/en/coins/{base.lower()}\n"
            f"🔍 Team wallets: Etherscan / Solscan → search {base}\n"
            f"📈 Trade: https://www.{ex_id}.com/trade/{sym.replace('/','_')}"
        )

        if send_telegram(msg):
            alerted_set.add(alert_key)
            alerts_sent += 1
            print(f"  ✅ ALERT → {rank} | {sym} | {ex_id}")

    return alerts_sent

# ============================================================
#  MAIN LOOP
# ============================================================
def main():
    print("=" * 55)
    print("  RAVE SCANNER — CEX + DEX  |  Railway Edition")
    print("=" * 55)

    send_telegram(
        "🟢 <b>RAVE Scanner started!</b>\n"
        f"Exchanges: {', '.join(e.upper() for e in EXCHANGES)}\n"
        "DEX: Solana · ETH · Base · BSC\n"
        f"Pump threshold: {int(PRICE_PUMP_THRESHOLD*100)}% + {VOLUME_SPIKE_MULT}x volume\n"
        "Scanning every 5 min 👀"
    )

    scan_count = 0

    while True:
        scan_count += 1
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n{'='*55}")
        print(f"  SCAN #{scan_count}  |  {now}")
        print(f"{'='*55}")

        total_alerts = 0

        # CEX scan
        for ex_id in EXCHANGES:
            print(f"\n[CEX] {ex_id.upper()}")
            try:
                n = scan_exchange(ex_id)
                total_alerts += n
            except Exception as e:
                print(f"  [ERR] {ex_id}: {e}")
            time.sleep(2)   # gentle between exchanges

        # DEX scan
        print("\n[DEX] DexScreener")
        try:
            dex_alerts = scan_dexscreener()
            for alert in dex_alerts:
                send_telegram(alert)
                total_alerts += 1
        except Exception as e:
            print(f"  [DEX ERR] {e}")

        print(f"\n[DONE] Scan #{scan_count} complete. {total_alerts} alerts sent.")
        print(f"[SLEEP] Next scan in {SCAN_INTERVAL_SEC // 60} min...\n")
        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("\nStopped by user.")
            send_telegram("🔴 RAVE Scanner stopped.")
            break
        except Exception as e:
            print(f"[CRASH] {e} — restarting in 60s")
            time.sleep(60)
