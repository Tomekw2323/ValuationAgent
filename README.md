# Valuation Agent

GPT-4o–orchestrated equity research demo: **DCF**, **trading multiples**, **dividend discount (DDM)**, optional **sum-of-parts (SOTP)** for select names, and analyst context from **yfinance**. Works for **US-listed** tickers and **Warsaw Stock Exchange (GPW)** names (`.WA` suffix).

English narrative reports, Streamlit UI, CLI, static HTML server, and Markdown / PDF export — useful as a portfolio piece or teaching example, **not** production investment advice.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Highlights

| Capability | Notes |
|------------|--------|
| **Agent loop** | GPT-4o chooses tools (up to 10 iterations), then synthesizes an English Markdown report. |
| **DCF** | Single- and two-stage models, CAPM-derived WACC hints, per-share fallback, buyback uplift, optional wide-moat terminal bump. |
| **Multiples** | P/E, EV/EBITDA, P/BV, EV/Sales where relevant; IQR peer cleaning; GPW liquidity / country-risk discount vs. Western peers. |
| **DDM** | Gordon growth with stability guardrails when \(g \approx r\). |
| **SOTP** | Built-in segment view for **AMZN** and **GOOGL** / **GOOG**. |
| **Data** | yfinance with a 48h JSON file cache under `cache/` (gitignored). |

Internal tool payloads still use **Polish JSON keys** (`cena_na_akcje`, `mediana`, …) for historical continuity; prompts tell the model to read those fields and write the **final report in English**.

---

## Quick start

```bash
cd valuation_agent
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate      # Linux / macOS
pip install -r requirements.txt
copy .env.example .env           # Windows: copy; Unix: cp
# Fill OPENAI_* or AZURE_OPENAI_* in .env
streamlit run app.py
```

**CLI** (writes to terminal; `--save` → `reports/` as Markdown + PDF when ReportLab succeeds):

```bash
python main.py AAPL
python main.py PKN.WA --fresh
python main.py CDR.WA --save --verbose
```

**Static HTTP UI** (`web/index.html` + `web_frontend.py`):

```bash
python web_frontend.py
# http://127.0.0.1:8000   (WEB_HOST / WEB_PORT optional)
```

---

## Repository layout

```
valuation_agent/
  agent/           # GPT-4o client, prompts, orchestration loop
  tools/           # data_fetcher, dcf, multiples, ddm, sotp
  report/          # terminal display, Markdown save, PDF
  data/            # cache + validation helpers
  web/             # optional static frontend
  app.py           # Streamlit dashboard
  main.py          # CLI
  web_frontend.py  # Threaded HTTPServer wrapper
  config.py        # env-driven OpenAI vs Azure settings
```

---

## Configuration

Supported providers (via `config.py`):

- **OpenAI**: `OPENAI_API_KEY`, optional `OPENAI_MODEL` (defaults to `gpt-4o`).
- **Azure OpenAI**: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`.

Never commit `.env`. Example variables live in [.env.example](.env.example).

---

## Cost ballpark

Expect on the order of **6–8** chat completions per full CLI run (one tool trajectory + synthesis). Typical token load is loosely **~8k–12k** input and **~1.2k–1.8k** output tokens — on the order of **a few cents per ticker** at public GPT-4o list pricing (verify current rates). Hitting `MAX_ITERATIONS` will cost more — watch logs.

---

## Limitations (read before trusting any number)

1. **yfinance is free and imperfect** — delayed quotes, filings lag, sparse GPW fields, occasional silent gaps. Always verify filings and exchange data.
2. **Heuristic GPW discount** — sector overlays are rules of thumb, not econometric estimates.
3. **Banks / insurers** — modeled FCF is often misleading; the agent is steered toward book / dividend-aware methods when `ostrzezenie_bank` is set.
4. **Gaming & cyclicals** — FCF spikes around releases or cycles; the agent is instructed to lean on multiples and label DCF uncertainty.
5. **SOTP segment splits** — static assumptions for demo companies; stale after corporate actions.

---

## Development

```bash
pytest tests/ -q
```

---

## License

MIT — see [LICENSE](LICENSE).

---

## Disclaimer

This software is provided **for education and experimentation only**. It is **not** investment, tax, or legal advice. Past model output is not indicative of future results. **You are responsible** for compliance with market rules and API terms where you deploy it.
