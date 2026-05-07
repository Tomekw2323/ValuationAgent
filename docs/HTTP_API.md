# HTTP API (`web_frontend.py`)

Lightweight **JSON** server based on `ThreadingHTTPServer`. Load environment from the project root `.env` (same as other entry points).

## Run

```bash
python web_frontend.py
```

- **Default bind:** `127.0.0.1:8000`
- **Overrides:** `WEB_HOST`, `WEB_PORT`

## Endpoints

### `GET /`

Plain-text hint: UI is provided by **Next.js** (`cd frontend && npm run dev`). The Python process only exposes JSON routes below.

### `GET /api/health`

**200** — JSON:

```json
{
  "status": "ok",
  "llm_configured": true,
  "provider": "openai"
}
```

`llm_configured` reflects whether `config.HAS_LLM_CREDENTIALS` is satisfied. `provider` is an internal label (e.g. `openai`, `azure`, `none`).

### `POST /api/valuate`

Runs the same pipeline as the Next.js UI: `ValuationAgent.run_preliminary()` then `run_final()` with user assumptions.

**Request body** (`application/json`):

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `ticker` | string | yes | Uppercased trim; GPW typically `FOO.WA` |
| `wacc_pct` | number | no | Default `10`; must be **5–20** |
| `growth_pct` | number | no | Default `5`; **0–25** |
| `wide_moat` | bool | no | Widens terminal growth via agent |
| `buyback` | bool | no | Default `true` |
| `fresh_data` | bool | no | If `true`, bypasses file cache |

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/api/valuate \
  -H "Content-Type: application/json" \
  -d '{"ticker":"AAPL","wacc_pct":10,"growth_pct":5,"wide_moat":false,"buyback":true,"fresh_data":false}'
```

**Success 200** — JSON (subset):

- `ticker`, `currency`, `market_price`
- `report` — Markdown string from the model
- `dcf.price` — preferred buyback-adjusted figure when present
- `multiples.median` — peer-derived median implied price where computed
- `sensitivity` — keys `pesymistyczny`, `bazowy`, `optymistyczny`
- `ddm` — may be `null` or contain `wycena` among other legacy fields

**Errors** — JSON `{"error": "..."}` with **4xx/5xx** as appropriate.

## Next.js proxy

The Next app posts to **`/api/valuate` on origin :3000**; [`frontend/app/api/valuate/route.ts`](../frontend/app/api/valuate/route.ts) forwards the body server-side to **`VALUATION_API_URL`** (default `http://127.0.0.1:8000`). No browser CORS configuration is required against the Python origin.
