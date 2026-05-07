# Valuation Agent — Next.js UI

Minimal **dark** dashboard (parity with the old static HTML). It does **not** embed business logic — it calls the Python **`web_frontend.py`** API.

## Setup

From repository root:

```bash
python web_frontend.py
```

From this folder:

```bash
npm ci
cp .env.example .env.local   # optional — set VALUATION_API_URL if API is not at http://127.0.0.1:8000
npm run dev
```

Open **http://127.0.0.1:3000**.

## Scripts

| Command | Description |
|---------|--------------|
| `npm run dev` | Development server (port 3000) |
| `npm run build` | Production bundle |
| `npm run start` | Serve production build |

## Env

| Variable | Default | Purpose |
|----------|---------|---------|
| `VALUATION_API_URL` | `http://127.0.0.1:8000` | Base URL of `web_frontend.py` |

Configured in **`.env.local`** (gitignored).

## Proxy

[`app/api/valuate/route.ts`](app/api/valuate/route.ts) forwards POST bodies to `{VALUATION_API_URL}/api/valuate`. If the Python server is down, you get **502** with a short diagnostic message.

More detail: **[../docs/HTTP_API.md](../docs/HTTP_API.md)**.
