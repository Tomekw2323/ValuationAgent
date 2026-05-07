# Frontends and entry points

There are **three** user-facing ways to run valuations. They differ in **orchestration** and **UX**, not in the underlying **Python tools** (`tools/`, yfinance cache).

## 1. CLI — `main.py`

- **Flow:** `ValuationAgent.run()` — full **GPT-4o tool-use loop** (fetch data → DCF / multiples / DDM / SOTP / sensitivity as the model chooses) → single long **Markdown** report.
- **Best for:** Scripting, logs, reproducible terminal output, **`--save`** for `reports/*.md` (+ PDF when available).

## 2. Streamlit — `app.py`

- **Flow:** `get_financial_data` → `run_preliminary` → **`run_final`** with sidebar **WACC** and **FCF growth** sliders. No wide-moat / fresh-cache toggles in the default sidebar (can be extended in code).
- **Best for:** Quick interactive exploration with **Plotly** DCF scenarios and multiples table built in Streamlit.

## 3. Next.js — `frontend/` + `web_frontend.py`

- **Flow:** Browser → Next **Route Handler** `POST /api/valuate` → proxy to Python `web_frontend.py` → same **`run_preliminary` + `run_final`** as Streamlit (with wide moat / buyback / cache refresh checkboxes matching the legacy static HTML UX).
- **Best for:** A **minimal dark UI** tuned for portfolio demos; runs on **`http://127.0.0.1:3000`** with API on **`http://127.0.0.1:8000`** by default.

## Static `web/` folder

The former single-file `web/index.html` was **replaced** by Next.js. The [`web/`](../web/) directory is reserved for optional assets or notes — see [`web/README.md`](../web/README.md).

## Which should I run?

| Need | Use |
|------|-----|
| Full autonomous tool loop from the model | `python main.py TICKER` |
| Charts + sliders in Python only | `streamlit run app.py` |
| Browser stack (Node + Python API) | `python web_frontend.py` + `cd frontend && npm run dev` |
