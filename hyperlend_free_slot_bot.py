import os, time, math, requests, threading, asyncio
from decimal import Decimal, getcontext
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =========================
# Config / ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))  # Frecuencia de chequeo del monitor
FREE_SLOT_DELTA = float(os.getenv("FREE_SLOT_DELTA", "0.005"))  # 0.5% de holgura
FREE_SLOT_COOLDOWN_MIN = int(os.getenv("FREE_SLOT_COOLDOWN_MIN", "5"))  # anti-spam

API_BASE = os.getenv("HYPERLEND_API_BASE", "https://api.hyperlend.finance").rstrip("/")
CHAIN = "hyperEvm"
API_URL = f"{API_BASE}/data/markets"

# Lista por símbolo
WATCHLIST_SYMBOLS = {"beHYPE", "wstHYPE", "kHYPE"}

# Direcciones oficiales HyperEVM (checksummed)
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
    addr = res.get("underlyingAsset") or ""
    sym = (res.get("symbol") or "").strip()
    if sym in WATCHLIST_SYMBOLS:
        return True
    return addr in WATCHLIST_ADDR

def display_name(res):
    addr = res.get("underlyingAsset") or ""
    sym = (res.get("symbol") or "").strip()
    return WATCHLIST_ADDR.get(addr) or sym or (addr[:6] + "…" + addr[-4:])

def fetch_reserves():
    r = requests.get(API_URL, params={"chain": CHAIN}, timeout=15)
    r.raise_for_status()
    j = r.json()
    return j.get("reserves", [])

def compute_borrow_and_util(res):
    """
    - variableDebt(base units) = totalScaledVariableDebt * variableBorrowIndex / RAY
    - totalBorrow(base units)  = variableDebt + totalPrincipalStableDebt
    - totalBorrow(tokens)      = base units / 10**decimals
    - utilization              = totalBorrow(tokens) / borrowCap (si borrowCap>0)
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

# =========================
# Estado y Telegram
# =========================
bot = Bot(token=BOT_TOKEN)

# Estado para detectar transición de "capado" (>=100%) a "<100%-delta"
was_capped = {}            # addr -> bool
last_free_notify_ts = {}   # addr -> timestamp último aviso

def send(msg):
    """Enviar mensaje a CHAT_ID desde contexto síncrono (monitor en hilo)."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[WARN] Falta TELEGRAM_BOT_TOKEN o CHAT_ID. Mensaje:", msg)
        return
    try:
        asyncio.run(bot.send_message(chat_id=CHAT_ID, text=msg, disable_web_page_preview=True))
    except Exception as e:
        print("Telegram error:", e)

def format_status_lines(reserves):
    """Devuelve líneas de estado para los activos vigilados."""
    lines = []
    for res in reserves:
        if not should_track(res):
            continue
        name = display_name(res)
        addr = res.get("underlyingAsset")

        total_borrow, cap, util = compute_borrow_and_util(res)
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

def monitor_loop():
    send("✅ Bot HyperLend activo. Vigilo beHYPE, wstHYPE y kHYPE. Usa /status para ver estado. Aviso cuando se abra hueco (<100%).")
    while True:
        try:
            reserves = fetch_reserves()
            for res in reserves:
                if not should_track(res):
                    continue

                addr = res.get("underlyingAsset")
                name = display_name(res)

                total_borrow, cap, util = compute_borrow_and_util(res)
                if util is None or cap <= 0:
                    continue

                currently_capped = util >= 1.0
                prev_capped = was_capped.get(addr, False)
                was_capped[addr] = currently_capped

                # Transición de >=100% a <100%-delta  -> notificar
                if prev_capped and util <= (1.0 - FREE_SLOT_DELTA):
                    now_ts = time.time()
                    last_ts = last_free_notify_ts.get(addr, 0)
                    if now_ts - last_ts >= FREE_SLOT_COOLDOWN_MIN * 60:
                        pct = util * 100
                        send(
                            "🟢 Se abrió hueco para pedir prestado\n"
                            f"Activo: {name}\n"
                            f"Utilización: {pct:.2f}%\n"
                            f"Borrow: {human(total_borrow)}  |  Cap: {human(cap)}\n"
                            f"Addr: {addr}"
                        )
                        last_free_notify_ts[addr] = now_ts

        except Exception as e:
            print("Loop error:", e)

        time.sleep(POLL_SECONDS)

# =========================
# Handlers de Telegram (/status y /start)
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 Hola. Estoy vigilando beHYPE, wstHYPE y kHYPE en HyperLend.\n"
        "• Te aviso cuando bajen de 100% (con holgura).\n"
        "• Comandos:\n"
        "   /status — Ver estado actual (utilización / borrow / cap)\n"
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reserves = fetch_reserves()
        lines = format_status_lines(reserves)
        if not lines:
            await update.message.reply_text("No encuentro datos de los activos vigilados ahora mismo.")
            return
        header = "📊 *Estado actual (beHYPE, wstHYPE, kHYPE)*\n" \
                 f"Holgura para hueco: {FREE_SLOT_DELTA*100:.2f}%\n"
        await update.message.reply_text(header + "\n".join(lines), disable_web_page_preview=True, parse_mode=None)
    except Exception as e:
        await update.message.reply_text(f"Error al obtener el estado: {e}")

def start_bot_polling_in_thread():
    """Lanza el bot con polling (handlers /start y /status) en otro hilo."""
    if not BOT_TOKEN:
        print("[WARN] Falta TELEGRAM_BOT_TOKEN para polling.")
        return

    async def runner():
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("status", cmd_status))
        await app.initialize()
        # run_polling bloquea hasta cancelación; lo envolvemos en create_task y await
        await app.start()
        print("Telegram polling iniciado.")
        try:
            await app.updater.start_polling()
            # Mantener corriendo
            while True:
                await asyncio.sleep(3600)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    def thread_target():
        asyncio.run(runner())

    t = threading.Thread(target=thread_target, daemon=True)
    t.start()

# =========================
# Main
# =========================
if __name__ == "__main__":
    # Hilo para el polling de comandos
    start_bot_polling_in_thread()
    # Hilo para el monitor de huecos
    threading.Thread(target=monitor_loop, daemon=True).start()
    # Mantener vivo el proceso principal
    while True:
        time.sleep(3600)
