# Telegram layer (telegram.py)
- Send alerts + receive commands only. Never trades.
- Commands accepted ONLY from chat IDs in TELEGRAM_CHAT_IDS (env owners) + runtime-added users; everyone else ignored.
- getUpdates offset persisted in state.json so commands aren't replayed after restart.
- One shared requests.Session keeps TLS to api.telegram.org open (skips 200-500ms handshake); broadcasts fan out via ThreadPoolExecutor(8).
- Token from TELEGRAM_BOT_TOKEN env — never hardcode, never print.
