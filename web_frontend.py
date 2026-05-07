"""Valuation JSON API for the Next.js frontend (and other clients).

Starts ThreadingHTTPServer with:
  POST /api/valuate
  GET  /api/health
  GET  /        — short note (UI is in frontend/)

Run:
    python web_frontend.py

Default: http://127.0.0.1:8000 — set WEB_HOST / WEB_PORT to override.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Wczytaj .env z katalogu projektu.
load_dotenv(BASE_DIR / ".env")
from config import HAS_LLM_CREDENTIALS, ACTIVE_LLM_PROVIDER


def _to_float(value: object, default: float) -> float:
    """Bezpieczna konwersja do float z fallbackiem."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class FrontendHandler(BaseHTTPRequestHandler):
    """Obsługa endpointów frontendowych."""

    server_version = "ValuationAgentWeb/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        """Bardziej zwięzłe logi żądań."""
        print(f"[web] {self.address_string()} - {format % args}")

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}

        try:
            length = int(raw_length)
        except ValueError:
            return {}

        if length <= 0:
            return {}

        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        if not raw_body.strip():
            return {}

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_text(
                "Valuation API only. Run the Next.js app: cd frontend && npm run dev "
                f"(http://127.0.0.1:{os.getenv('WEB_PORT', '8000')} — POST /api/valuate, GET /api/health).",
                status=HTTPStatus.OK,
            )
            return

        if path == "/api/health":
            self._send_json(
                {
                    "status": "ok",
                    "llm_configured": HAS_LLM_CREDENTIALS,
                    "provider": ACTIVE_LLM_PROVIDER,
                }
            )
            return

        self._send_json(
            {"error": f"Nieznany endpoint: {path}"},
            status=HTTPStatus.NOT_FOUND,
        )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path != "/api/valuate":
            self._send_json(
                {"error": f"Nieznany endpoint: {path}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        payload = self._read_json_body()
        ticker = str(payload.get("ticker", "")).strip().upper()

        if not ticker:
            self._send_json(
                {"error": "Pole 'ticker' jest wymagane."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        wacc_pct = _to_float(payload.get("wacc_pct"), 10.0)
        growth_pct = _to_float(payload.get("growth_pct"), 5.0)
        if not (5.0 <= wacc_pct <= 20.0):
            self._send_json(
                {"error": "WACC musi być w zakresie 5-20%."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if not (0.0 <= growth_pct <= 25.0):
            self._send_json(
                {"error": "Wzrost FCF musi być w zakresie 0-25%."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        wide_moat = bool(payload.get("wide_moat", False))
        buyback = bool(payload.get("buyback", True))
        force_refresh = bool(payload.get("fresh_data", False))

        try:
            from agent.orchestrator import ValuationAgent
        except Exception as exc:  # pragma: no cover - awaryjny fallback.
            self._send_json(
                {"error": f"Nie udało się załadować agenta: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        try:
            agent = ValuationAgent()

            preliminary = agent.run_preliminary(
                ticker=ticker,
                force_refresh=force_refresh,
            )
            if preliminary is None:
                self._send_json(
                    {
                        "error": (
                            f"Nie udało się pobrać danych lub przygotować wyceny dla {ticker}."
                        )
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            assumptions = {
                "wacc": wacc_pct / 100.0,
                "growth": growth_pct / 100.0,
                "wide_moat": wide_moat,
                "buyback": buyback,
                "cykliczna": (preliminary.get("cv_fcf") or 0.0) > 0.4,
                "peers": preliminary.get("peers") or [],
            }

            report = agent.run_final(
                ticker=ticker,
                assumptions=assumptions,
                wstepne=preliminary,
                force_refresh=force_refresh,
            )
            if not report:
                self._send_json(
                    {"error": "Agent nie wygenerował raportu."},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

            financial_data = (
                agent.financial_data_cache.get(ticker)
                or preliminary.get("_financial_data")
                or {}
            )
            info = financial_data.get("info") or {}

            market_price = (
                agent.last_market_price
                or info.get("currentPrice")
                or info.get("regularMarketPrice")
            )
            currency = (info.get("currency") or "USD").strip()

            dcf = agent.last_dcf_result or {}
            multiples = agent.last_multiples_result or {}
            sensitivity = agent.last_sensitivity_result or {}
            ddm = agent.last_ddm_result

            self._send_json(
                {
                    "ticker": ticker,
                    "currency": currency,
                    "market_price": market_price,
                    "report": report,
                    "dcf": {
                        "price": dcf.get("cena_z_korekta_buyback")
                        or dcf.get("cena_na_akcje"),
                        "raw_price": dcf.get("cena_na_akcje"),
                        "buyback_price": dcf.get("cena_z_korekta_buyback"),
                        "assumptions": dcf.get("zalozenia", {}),
                    },
                    "multiples": {
                        "median": multiples.get("mediana"),
                        "pe": (multiples.get("wycena_pe") or {}).get("cena_na_akcje"),
                        "ev_ebitda": (multiples.get("wycena_ev_ebitda") or {}).get(
                            "cena_na_akcje"
                        ),
                        "pbv": (multiples.get("wycena_pbv") or {}).get("cena_na_akcje"),
                        "ev_sales": (multiples.get("wycena_ev_sales") or {}).get(
                            "cena_na_akcje"
                        ),
                    },
                    "sensitivity": sensitivity.get("scenariusze") or {},
                    "ddm": ddm,
                    "used_assumptions": assumptions,
                }
            )
        except SystemExit:
            self._send_json(
                {
                    "error": (
                        "Missing API configuration. Set OPENAI_API_KEY or AZURE_OPENAI_* in .env.",
                    )
                },
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:  # pragma: no cover - awaryjny fallback.
            self._send_json(
                {"error": f"Błąd podczas wyceny: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def run_server() -> None:
    """Uruchamia serwer frontendowy."""
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), FrontendHandler)

    print(f"Valuation HTTP API listening on http://{host}:{port}")
    print("Next.js UI: cd frontend && npm run dev → http://127.0.0.1:3000")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run_server()
