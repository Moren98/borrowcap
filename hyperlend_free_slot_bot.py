import os, re, time, math, requests, threading, asyncio
from decimal import Decimal, getcontext
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from bs4 import BeautifulSoup

# =========================
# Config / ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# Frecuencias
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))                 # HypurrFi loop
HL_REFRESH_SECONDS = int(os.getenv("HL_REFRESH_SECONDS", "600"))    # HyperLend: 10 min por defecto

# Umbrales/holguras
FREE_SLOT_DELTA = float(os.getenv("FREE_SLOT_DELTA", "0.005"))  # 0.5% para considerar “holgura” bajo 100%
FREE_SLOT_COOLDOWN_MIN = int(os.getenv("FREE_SLOT_COOLDOWN_MIN", "5"))  # anti-spam HyperLend
MIN_FREE_TOKENS = float(os.getenv("MIN_FREE_TOKENS", "5"))  # mínimo de beHYPE libres para avisar en HypurrFi

# HyperLend API
API_BASE = os.getenv("HYPERLEND_API_BASE", "https://api.hyperlend.finance").rstrip("/")
CHAIN = "hyperEvm"
API_URL = f"{API_BASE}/data/markets"
API_URL_RATES = f"{API_BASE}/data/markets/rates"
STALE_SECS = int(os.getenv("STALE_SECS", "300"))  # cache “aceptable” si la API falla (5 min)

# HypurrFi (pooled market)
HYPURR_CHAIN_ID = os.getenv("HYPURR_CHAIN_ID", "999").strip()
BEHYPE_ADDR = os.getenv("ASSET_ADDR", "0xd8fc8f0b03eba61f64d08b0bef69d80916e5dda9").strip()
HYPURR_URL = f"https://app.hypurr.fi/markets/pooled/{HYPURR_CHAIN_ID}/{BEHYPE_ADDR}"

# Lista por símbolo (HyperLend)
WATCHLIST_SYMBOLS = {"beHYPE", "wstHYPE", "kHYPE"}
WATCHLIST_ADDR = {
    "0x94e8396e0869c9F2200760aF0621aFd240E1CF38": "wstHYPE",
    "0xfD739d4e423301CE9385c1fb8850539D657C296D": "kHYPE",
    "0xd8fc8f0b03eba61f64d08b0bef69d80916e5dda9": "beHYPE",
}

# =========================
# Constantes y helpers
# =========================
getcontext().prec = 50
RAY = Decimal(10) ** 27

def human(n, dec=2):
    try:
        n = float(n)
        if n == 0:
            return "0"
        units = ["", "K", "M", "B", "T"]
        idx = min(len(units) - 1, int(math.log10(abs(n)) // 3))
        return f"{n/(1000**idx):,.{dec}f}{units[idx]}"
    except Exception:
        return str(n)

def should_track(res):
    addr = (res.get("underlyingAsset") or "").strip()
    sym = (res.get("symbol") or "").strip()
    if sym in WATCHLIST_SYMBOLS:
        return True
    return addr in WATCHLIST_ADDR

def display_name(res):
    addr = (res.get("underlyingAsset") or "").strip()
    sym = (res.get("symbol") or "").strip()
    return WATCHLIST_ADDR.get(addr) or sym or (addr[:6] + "…" + addr[-4:])

# =========================
# Telegram
# =========================
bot = Bot(token=BOT_TOKEN)

def send(msg):
    """Enviar mensaje a CHAT_ID desde contexto síncrono (hilos de monitor)."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[WARN] Falta TELEGRAM_BOT_TOKEN o CHAT_ID. Mensaje:", msg)
        return
    try:
        asyncio.run(bot.send_message(chat_id=CHAT_ID, text=msg, disable_web_page_preview=True))
    except Exception as e:
        print("Telegram error:", e)

# =========================
# HyperLend: fetch con reintentos + cache + refresco cada 10 min
# =========================
HL_LAST_RESERVES = None
HL_LAST_TS = 0

def _get_json_with_retries(url, params, retries=5, timeout=15):
    backoff = 0.5
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if 500 <= r.status_code < 600:
                raise requests.HTTPError(f"{r.status_code} {r.text}")
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == retries - 1:
                raise
            time.sleep(backoff)
            backoff *= 2

def hl_fetch_reserves():
    """Devuelve (reserves, stale:bool). Actualiza cache si éxito."""
    global HL_LAST_RESERVES, HL_LAST_TS
    try:
        j = _get_json_with_retries(API_URL, {"chain": CHAIN})
        reserves = j.get("reserves", [])
        HL_LAST_RESERVES, HL_LAST_TS = reserves, time.time()
        return reserves, False
    except Exception:
        # Pruébalo para diagnóstico (no usamos respuesta)
        try:
            _ = _get_json_with_retries(API_URL_RATES, {"chain": CHAIN}, retries=3, timeout=10)
            print("[warn] /data/markets falla pero /rates responde; usando cache si existe.")
        except Exception:
            print("[warn] HyperLend API caída (markets/rates); usando cache si existe.")

        if HL_LAST_RESERVES and (time.time() - HL_LAST_TS) <= STALE_SECS:
            return HL_LAST_RESERVES, True
        raise

def hl_compute_borrow_and_util(res):
    """
    - variableDebt(base units) = totalScaledVariableDebt * variableBorrowIndex / RAY
    - totalBorrow(tokens) = (variableDebt + totalPrincipalStableDebt) / 10**decimals
    - utilization = totalBorrow(tokens) / borrowCap (si borrowCap>0)
    """
    decimals = int(res.get("decimals", "18"))
    borrow_cap_tokens = Decimal(res.get("borrowCap", "0"))
    scaled_var = Decimal(res.get("totalScaledVariableDebt", "0"))
    var_index = Decimal(res.get("variableBorrowIndex", str(RAY)))
    var_debt_base = (scaled_var * var_index) / RAY
    stable_principal_base = Decimal(res.get("totalPrincipalStableDebt", "0"))
    total_borrow_base = var_debt_base + stable_principal_base
    divisor = Decimal(10) ** decimals
    total_borrow_tokens = total_borrow_base / divisor
    util = None
    if borrow_cap_tokens > 0:
        util = float(total_borrow_tokens / borrow_cap_tokens)
    return float(total_borrow_tokens), float(borrow_cap_tokens), util

def hl_format_status_lines(reserves):
    lines = []
    for res in reserves or []:
        if not should_track(res):
            continue
        name = display_name(res)
        addr = (res.get("underlyingAsset") or "").strip()
        total_borrow, cap, util = hl_compute_borrow_and_util(res)
        if util is None or cap <= 0:
            lines.append(f"• {name}: sin datos de cap")
            continue
        pct = util * 100
        flag = "🟥" if util >= 1.0 else ("🟨" if util >= (1.0 - FREE_SLOT_DELTA) else "🟩")
        lines.append(
            f"{flag} {name} — {pct:.2f}%  |  Borrow {human(total_borrow)} / Cap {human(cap)}\n"
            f"   {addr}"
        )
    return lines

# Estado para avisos de HyperLend (solo en el refresco de 10 min)
hl_was_capped = {}          # addr -> bool
hl_last_free_notify_ts = {} # addr -> timestamp

def hyperlend_refresher_loop():
    """Pulso cada HL_REFRESH_SECONDS; evita consulta continua a la API de HyperLend."""
    send("✅ Monitor HyperLend activo (pulso cada 10 min).")
    while True:
        try:
            reserves, stale = hl_fetch_reserves()
            if stale:
                print("[warn] HyperLend usando cache del último pulso (API 500/timeout)")

            # Notificaciones solo en este pulso
            for res in reserves or []:
                if not should_track(res):
                    continue
                addr = (res.get("underlyingAsset") or "").strip()
                name = display_name(res)
                total_borrow, cap, util = hl_compute_borrow_and_util(res)
                if util is None or cap <= 0:
                    continue

                currently_capped = util >= 1.0
                prev_capped = hl_was_capped.get(addr, False)
                hl_was_capped[addr] = currently_capped

                if prev_capped and util <= (1.0 - FREE_SLOT_DELTA):
                    now_ts = time.time()
                    last_ts = hl_last_free_notify_ts.get(addr, 0)
                    if now_ts - last_ts >= FREE_SLOT_COOLDOWN_MIN * 60:
                        pct = util * 100
                        send(
                            "🟢 [HyperLend] Se abrió hueco para pedir prestado\n"
                            f"Activo: {name}\n"
                            f"Utilización: {pct:.2f}%\n"
                            f"Borrow: {human(total_borrow)}  |  Cap: {human(cap)}\n"
                            f"Addr: {addr}"
                        )
                        hl_last_free_notify_ts[addr] = now_ts

        except Exception as e:
            print("HyperLend loop error:", e)

        time.sleep(HL_REFRESH_SECONDS)

# =========================
# HypurrFi: scraping de la página del activo (beHYPE)
# =========================
def parse_money_or_units(txt):
    # acepta 200K / 3.2M / 12345.67  (quitamos comas)
    txt = txt.replace(",", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)([KMBT]?)\b", txt)
    if not m:
        return None
    val = float(m.group(1))
    suf = m.group(2)
    mult = {"":1, "K":1e3, "M":1e6, "B":1e9, "T":1e12}.get(suf, 1)
    return val * mult

def hypurr_fetch_status():
    """
    Devuelve: (borrowed_tokens, cap_tokens, utilization, is_capped, url)
    Lee la página del activo en pooled markets.
    """
    url = HYPURR_URL
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    is_capped = ("Cannot be borrowed" in html) or ("Borrow cap reached" in html)

    borrowed = None
    cap = None
    # Buscar “Total borrowed …” y “Borrow cap …”
    for label in ["Total borrowed", "Total Borrows", "Total borrowed "]:
        if label in text:
            seg = text.split(label, 1)[1][:160]
            borrowed = parse_money_or_units(seg)
            break
    for label in ["Borrow cap", "Borrow Cap", "Borrow cap "]:
        if label in text:
            seg = text.split(label, 1)[1][:160]
            cap = parse_money_or_units(seg)
            break

    util = None
    if borrowed is not None and cap and cap > 0:
        util = borrowed / cap
    return borrowed, cap, util, is_capped, url

# Estado HypurrFi
hypurr_last_free_ts = 0
hypurr_last_state_capped = None  # None/True/False

def hypurr_monitor_loop():
    send("✅ Monitor HypurrFi activo (beHYPE).")
    global hypurr_last_free_ts, hypurr_last_state_capped
    while True:
        try:
            borrowed, cap, util, capped, url = hypurr_fetch_status()
            if util is None or cap is None:
                print("[warn] HypurrFi: no pude calcular utilización; reintento luego")
            else:
                available = max(0.0, (cap or 0) - (borrowed or 0))
                now = time.time()
                # Avisar solo al pasar de capado a libre con holgura Y con mínimo disponible
                if hypurr_last_state_capped is True and util <= (1.0 - FREE_SLOT_DELTA) and available >= MIN_FREE_TOKENS:
                    # anti-spam
                    if now - hypurr_last_free_ts >= 60:  # 1 min de respiro por si flapea
                        send(
                            "🟢 [HypurrFi] Se abrió hueco para pedir prestado beHYPE\n"
                            f"Utilización: {util*100:.2f}%  |  Borrow {human(borrowed)} / Cap {human(cap)}  |  Disponible ≈ {available:,.2f}\n"
                            f"{url}"
                        )
                        hypurr_last_free_ts = now
                hypurr_last_state_capped = (util is not None and util >= 1.0)
        except Exception as e:
            print("HypurrFi loop error:", e)

        time.sleep(POLL_SECONDS)

# =========================
# /start y /status (muestra HyperLend + HypurrFi)
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Monitoreo de *borrow cap*:\n"
        "• HyperLend: pulso cada 10 min (menos llamadas a la API).\n"
        "• HypurrFi (beHYPE): chequeo continuo.\n"
        "• /status: ver estado de ambos."
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = []

    # HypurrFi (consulta en vivo)
    try:
        borrowed, cap, util, capped, url = hypurr_fetch_status()
        if util is None:
            parts.append(f"📊 HypurrFi (beHYPE): no pude leer cap/borrow ahora mismo.\n{url}")
        else:
            available = max(0.0, (cap or 0) - (borrowed or 0))
            flag = "🟥" if util >= 1.0 else ("🟨" if util >= (1.0 - FREE_SLOT_DELTA) else "🟩")
            parts.append(
                f"📊 HypurrFi — {flag} beHYPE {util*100:.2f}% | Borrow {human(borrowed)} / Cap {human(cap)} | Disponible ≈ {available:,.2f}\n"
                f"(Aviso si disponible ≥ {MIN_FREE_TOKENS:,.2f} y util ≤ {(1.0 - FREE_SLOT_DELTA)*100:.2f}%)\n"
                f"{url}"
            )
    except Exception as e:
        parts.append(f"📊 HypurrFi: error al obtener estado: {e}")

    # HyperLend (usamos lo último del refresco; si no hay cache, intentamos leer una vez)
    try:
        reserves = HL_LAST_RESERVES
        stale_note = ""
        if not reserves or (time.time() - HL_LAST_TS) > HL_REFRESH_SECONDS * 2:
            # Si no hay datos aún (por ejemplo, bot recién iniciado), intentar una lectura única
            try:
                reserves, stale = hl_fetch_reserves()
                if stale:
                    stale_note = " (cache)"
            except Exception as e:
                parts.append(f"📊 HyperLend: sin datos aún (la API podría estar caída).")
                reserves = None
        age_min = int((time.time() - HL_LAST_TS) / 60) if HL_LAST_TS else None
        if reserves:
            lines = hl_format_status_lines(reserves)
            header = "📊 HyperLend"
            if age_min is not None:
                header += f" — último pulso hace {age_min} min"
            if stale_note:
                header += stale_note
            parts.append(header + "\n" + ("\n".join(lines) if lines else "Sin datos de los vigilados."))
    except Exception as e:
        parts.append(f"📊 HyperLend: error al preparar estado: {e}")

    await update.message.reply_text("\n\n".join(parts), disable_web_page_preview=True)

# =========================
# Lanzadores
# =========================
def start_polling_thread():
    if not BOT_TOKEN:
        print("[WARN] Falta TELEGRAM_BOT_TOKEN para polling.")
        return

    def runner():
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("status", cmd_status))
        print("Telegram polling iniciado.")
        app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=5)

    t = threading.Thread(target=runner, daemon=True)
    t.start()

# =========================
# Main
# =========================
if __name__ == "__main__":
    start_polling_thread()
    # HyperLend: refresco lento (cada 10 min)
    threading.Thread(target=hyperlend_refresher_loop, daemon=True).start()
    # HypurrFi: monitor continuo
    threading.Thread(target=hypurr_monitor_loop, daemon=True).start()
    # Mantener vivo
    while True:
        time.sleep(3600)
