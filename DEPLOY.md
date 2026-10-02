# Putting Meeting Agent online (free)

The public app lives in `web/`. Each person signs in with Google, pastes their own free Vexa keys,
and sees only their own meetings and report cards. The old single-user scripts (`agent.py`,
`dashboard.py`, …) still work for local use.

## Try it on your Mac first

```
source venv/bin/activate
pip install -r requirements-web.txt
cp .env.example .env.web      # optional reference; your existing .env keeps working
python -m web.app
```

Open http://localhost:5050 → **Continue on this computer** → Settings → paste your Vexa keys.
To bring over old report cards: `python import_local.py`.

Locally the report writer uses Ollama (llama3.1) unless `GEMINI_API_KEY` is set.

## Go live

You'll create five free accounts. Keep every key private; paste them only into Render.

1. **Database (Neon)**: neon.tech → New project → copy the *connection string* → this is `DATABASE_URL`.
2. **Report writer (Gemini)**: aistudio.google.com → Get API key → `GEMINI_API_KEY`.
3. **Email (Brevo)**: brevo.com → SMTP & API → SMTP tab → `SMTP_HOST=smtp-relay.brevo.com`, `SMTP_USER`,
   `SMTP_PASS` (the SMTP key). Add and verify a sender email → `MAIL_FROM`.
4. **Hosting (Render)**: render.com → New → **Blueprint** → pick this repo. Render reads `render.yaml`.
   Fill in the empty values it asks for. Your address will be like `https://meeting-agent.onrender.com`
   → set `PUBLIC_URL` to it.
5. **Google sign-in**: console.cloud.google.com → APIs & Services → OAuth consent screen (External) →
   Credentials → Create OAuth client ID → Web application. Authorized redirect URI:
   `https://YOUR-APP.onrender.com/auth/callback` (and `http://localhost:5050/auth/callback` for testing).
   Put the ID and secret in `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
6. **Every-minute check**: cron-job.org → new job, every 1 minute, URL
   `https://YOUR-APP.onrender.com/cron/tick?key=YOUR_CRON_SECRET` (copy `CRON_SECRET` from Render).
   This writes report cards, sends Telegram questions, and keeps the free server awake.
7. **Telegram**: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`. Each user connects from Settings.

While the Google consent screen is in "Testing", only test users you add can sign in. Click
**Publish app** when you're ready for anyone.

## How it works

- Sending a bot, scheduling, and calendars call Vexa with the signed-in user's keys.
- `/cron/tick` (or a background thread when run locally) checks each user's finished meetings,
  writes report cards with Gemini, saves them, and emails them.
- Vexa keys are encrypted with `SECRET_KEY`. Don't change `SECRET_KEY` after launch.
