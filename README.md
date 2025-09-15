# HyperLend Free Slot Bot 🤖

Bot de Telegram que monitoriza activos en HyperLend (HyperEVM) y envía alertas cuando
los borrow caps bajan de 100%. Incluye comando `/status`.

## 🚀 Deploy en Render

1. Haz fork de este repo o sube tu propio repo a GitHub/GitLab.
2. Ve a [Render](https://dashboard.render.com/) → "New +"
   → "Worker Service" → conecta tu repo.
3. En **Start Command** Render usará `Procfile` (`worker: python hyperlend_free_slot_bot.py`).
4. En el panel de Render, añade estas **Environment Variables**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - (Opcionales: `POLL_SECONDS`, `FREE_SLOT_DELTA`, `FREE_SLOT_COOLDOWN_MIN`)
5. Deploy y listo 🎉

## 🛠️ Variables principales
- `TELEGRAM_BOT_TOKEN`: Token de BotFather.
- `TELEGRAM_CHAT_ID`: Tu chat ID (ej. con @userinfobot).
- `POLL_SECONDS`: Frecuencia de chequeo (por defecto 30s).
- `FREE_SLOT_DELTA`: Holgura para considerar "hueco abierto".
- `FREE_SLOT_COOLDOWN_MIN`: Minutos entre avisos por activo.
