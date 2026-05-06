"""Główna pętla agenta (ValuationAgent) — wysyła zapytania do GPT-4o,
odbiera decyzje o wywołaniu narzędzi, wykonuje je i przekazuje wyniki
z powrotem do modelu aż do wygenerowania finalnego raportu.
"""

import json
import statistics
import time
from typing import Optional

from openai import OpenAI, AzureOpenAI
from rich.console import Console
from rich.spinner import Spinner
from rich.live import Live

from config import (
    OPENAI_API_KEY,
    MODEL,
    MAX_ITERATIONS,
    DEFAULT_TERMINAL_GROWTH,
    USE_AZURE_OPENAI,
    HAS_LLM_CREDENTIALS,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_API_VERSION,
)
from agent.prompts import SYSTEM_PROMPT, TOOLS_DEFINITION
from tools.data_fetcher import get_financial_data, get_sector_peers, safe_get_value
from tools.dcf import run_dcf, sensitivity_analysis
from tools.ddm import run_ddm
from tools.multiples import run_multiples
from tools.sotp import run_sotp

console = Console(legacy_windows=False)


def _extract_positive_price(value: object) -> Optional[float]:
    """Zwraca dodatnia wartosc float albo None."""
    parsed = safe_get_value(value)
    if parsed is None or parsed <= 0:
        return None
    return float(parsed)


def _estimate_buyback_rate(financial_data: dict) -> Optional[float]:
    """Szacuje srednie roczne tempo redukcji liczby akcji."""
    balance_sheet = financial_data.get("balance_sheet", {}) or {}
    if not balance_sheet:
        return None

    shares_history: list[float] = []
    for date_key in sorted(balance_sheet.keys()):
        period = balance_sheet.get(date_key) or {}
        shares = safe_get_value(period.get("Ordinary Shares Number"))
        if shares is None:
            shares = safe_get_value(period.get("Share Issued"))
        if shares is not None and shares > 0:
            shares_history.append(float(shares))

    if len(shares_history) < 2:
        return None

    years = len(shares_history) - 1
    if years <= 0 or shares_history[0] <= 0:
        return None

    return max(0.0, (shares_history[0] - shares_history[-1]) / shares_history[0] / years)


def _assess_ddm_reliability(financial_data: dict, ddm_result: Optional[dict]) -> dict:
    """Ocena czy DDM jest metoda glowna czy tylko pomocnicza."""
    if not ddm_result:
        return {
            "reliable": False,
            "rola": "niedostepna",
            "powod": "Brak wyniku DDM.",
        }

    info = financial_data.get("info", {}) or {}
    dividend_yield = safe_get_value(info.get("dividendYield"))
    payout_ratio = safe_get_value(info.get("payoutRatio"))
    buyback_rate = _estimate_buyback_rate(financial_data) or 0.0

    reasons: list[str] = []
    reliable = True

    if ddm_result.get("ostrzezenie"):
        reliable = False
        reasons.append("DDM ma ostrzezenie stabilnosci (maly spread r-g).")

    if dividend_yield is not None and dividend_yield < 0.015 and buyback_rate > 0.01:
        reliable = False
        reasons.append(
            "Niska stopa dywidendy przy silnym buybacku - dywidenda nie jest glownym nosnikiem wartosci."
        )
    elif payout_ratio is not None and payout_ratio < 0.20 and buyback_rate > 0.01:
        reliable = False
        reasons.append(
            "Niski payout ratio przy silnym buybacku - DDM ma role pomocnicza."
        )

    if reliable:
        return {
            "reliable": True,
            "rola": "wspierajaca",
            "powod": "Profil dywidendowy pozwala traktowac DDM jako metode wspierajaca.",
        }

    return {
        "reliable": False,
        "rola": "pomocnicza",
        "powod": " ".join(reasons) if reasons else "DDM traktuj kierunkowo.",
    }


def _build_valuation_guardrails(
    financial_data: dict,
    dcf_result: dict,
    multiples_result: dict,
    ddm_result: Optional[dict],
    sensitivity_result: dict,
) -> dict:
    """Buduje twarde guardrails do finalnego raportu."""
    dcf_price = _extract_positive_price(
        (dcf_result or {}).get("cena_z_korekta_buyback")
        or (dcf_result or {}).get("cena_na_akcje")
    )
    multiples_price = _extract_positive_price((multiples_result or {}).get("mediana"))
    ddm_price = _extract_positive_price((ddm_result or {}).get("wycena"))

    peers_count = int((multiples_result or {}).get("liczba_peers") or 0)
    peers_note = (multiples_result or {}).get("jakosc_peers_uwaga")
    multiples_quality = "wysoka"
    if peers_count < 2:
        multiples_quality = "niska"
    elif peers_count < 3 or peers_note:
        multiples_quality = "srednia"

    ddm_assessment = _assess_ddm_reliability(financial_data, ddm_result)

    reliable_methods: list[dict] = []
    supplemental_methods: list[dict] = []

    if dcf_price is not None:
        reliable_methods.append({"metoda": "DCF", "cena": round(dcf_price, 2)})

    if multiples_price is not None:
        if multiples_quality in ("wysoka", "srednia"):
            reliable_methods.append({
                "metoda": "Mnozniki",
                "cena": round(multiples_price, 2),
                "jakosc": multiples_quality,
            })
        else:
            supplemental_methods.append({
                "metoda": "Mnozniki",
                "cena": round(multiples_price, 2),
                "powod": "Niska jakosc peers (<2 porownywalne spolki).",
            })

    if ddm_price is not None:
        target_bucket = reliable_methods if ddm_assessment["reliable"] else supplemental_methods
        target_bucket.append({
            "metoda": "DDM",
            "cena": round(ddm_price, 2),
            "powod": ddm_assessment["powod"],
        })

    range_low: Optional[float] = None
    range_mid: Optional[float] = None
    range_high: Optional[float] = None
    range_source = "brak"

    reliable_prices = [m["cena"] for m in reliable_methods if m.get("cena")]
    if len(reliable_prices) >= 2:
        range_low = round(min(reliable_prices), 2)
        range_mid = round(float(statistics.median(reliable_prices)), 2)
        range_high = round(max(reliable_prices), 2)
        range_source = "metody_reliable"
    elif len(reliable_prices) == 1:
        base_price = reliable_prices[0]
        scenarios = (sensitivity_result or {}).get("scenariusze") or {}
        pess = _extract_positive_price(scenarios.get("pesymistyczny"))
        opt = _extract_positive_price(scenarios.get("optymistyczny"))
        range_low = round(pess if pess is not None else base_price, 2)
        range_mid = round(base_price, 2)
        range_high = round(opt if opt is not None else base_price, 2)
        range_source = "jedna_metoda_plus_sensitivity"

    score = 0
    confidence_reasons: list[str] = []
    dcf_reliability = ((dcf_result or {}).get("ocena_wiarygodnosci") or {}).get("wiarygodnosc")
    if dcf_reliability == "wysoka":
        score += 2
    elif dcf_reliability == "srednia":
        score += 1
    else:
        confidence_reasons.append("Niska wiarygodnosc DCF.")

    if multiples_quality == "wysoka":
        score += 2
    elif multiples_quality == "srednia":
        score += 1
        confidence_reasons.append("Mnozniki na sredniej jakosci peers.")
    else:
        confidence_reasons.append("Mnozniki na niskiej jakosci peers.")

    if len(reliable_methods) >= 2:
        score += 1
    else:
        confidence_reasons.append("Tylko jedna metoda reliable.")

    if ddm_assessment["reliable"]:
        score += 1

    if score >= 5:
        confidence_level = "wysoka"
    elif score >= 3:
        confidence_level = "srednia"
    else:
        confidence_level = "niska"

    return {
        "metody_reliable": reliable_methods,
        "metody_pomocnicze": supplemental_methods,
        "mnozniki_jakosc": {
            "poziom": multiples_quality,
            "liczba_peers": peers_count,
            "uwaga": peers_note,
        },
        "ddm_ocena": ddm_assessment,
        "zakres_wartosci_godziwej_reliable": {
            "low": range_low,
            "median": range_mid,
            "high": range_high,
            "zrodlo": range_source,
            "uwaga": "Zakres liczony tylko z metod modelowych (bez ceny rynkowej).",
        },
        "confidence": {
            "score_0_6": score,
            "poziom": confidence_level,
            "powody": confidence_reasons,
        },
    }


def _sanitize_analyst_consensus(financial_data: dict) -> dict:
    """Normalizuje konsensus analityków do bezpiecznego formatu raportowego."""
    raw = (financial_data.get("analyst_consensus") or {}).copy()

    target_mean = _extract_positive_price(raw.get("target_mean"))
    target_high = _extract_positive_price(raw.get("target_high"))
    target_low = _extract_positive_price(raw.get("target_low"))
    target_median = _extract_positive_price(raw.get("target_median"))
    analyst_count_raw = safe_get_value(raw.get("analyst_count"))
    analyst_count = int(analyst_count_raw) if analyst_count_raw and analyst_count_raw > 0 else None
    recommendation = raw.get("recommendation")

    normalized = {
        "target_mean": target_mean,
        "target_high": target_high,
        "target_low": target_low,
        "target_median": target_median,
        "analyst_count": analyst_count,
        "recommendation": recommendation,
        "source": "yfinance.info",
    }
    normalized["has_data"] = any(
        normalized.get(k) is not None
        for k in ("target_mean", "target_high", "target_low", "target_median")
    )
    return normalized


class ValuationAgent:
    """Agent AI do wyceny spółek giełdowych.

    Orkiestruje komunikację między GPT-4o a lokalnymi narzędziami wyceny.
    GPT-4o decyduje które narzędzia wywołać — agent je wykonuje i zwraca
    wyniki do modelu, aż ten wygeneruje finalny raport.
    """

    def __init__(self) -> None:
        # Walidacja konfiguracji LLM — wymagane OpenAI albo Azure OpenAI
        if not HAS_LLM_CREDENTIALS:
            console.print(
                "[bold red]✗ Brak konfiguracji klucza API![/bold red]\n"
                "[dim]Ustaw OPENAI_API_KEY lub komplet AZURE_OPENAI_* w pliku .env.[/dim]"
            )
            raise SystemExit(1)

        # Klient LLM — publiczne OpenAI albo Azure OpenAI
        self.provider = "azure" if USE_AZURE_OPENAI else "openai"
        if USE_AZURE_OPENAI:
            self.client = AzureOpenAI(
                api_key=AZURE_OPENAI_API_KEY,
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
                api_version=AZURE_OPENAI_API_VERSION,
            )
        else:
            self.client = OpenAI(api_key=OPENAI_API_KEY)

        # Flaga wymuszenia odświeżenia cache — ustawiana przez run() na podstawie
        # argumentu --fresh z CLI lub checkboxa w app.py
        self.force_refresh: bool = False

        # Cache danych finansowych w pamięci (na czas jednej sesji agenta).
        # Klucz: ticker, wartość: słownik z danymi z get_financial_data.
        self.financial_data_cache: dict[str, dict] = {}

        # Dziennik wywołań narzędzi — do debugowania i raportowania
        self.tool_call_log: list[dict] = []

        # Ostatnie wyniki narzędzi — do odczytu przez frontend (app.py)
        self.last_dcf_result: Optional[dict] = None
        self.last_ddm_result: Optional[dict] = None
        self.last_multiples_result: Optional[dict] = None
        self.last_sensitivity_result: Optional[dict] = None
        self.last_sotp_result: Optional[dict] = None
        self.last_market_price: Optional[float] = None

    def _ensure_financial_data(self, ticker: str) -> Optional[dict]:
        """Zwraca dane finansowe z cache sesji lub pobiera je jeśli brak.
        Gwarantuje, że narzędzia wyceny zawsze mają dane do pracy.

        Gdy self.force_refresh=True, omija cache sesji i plikowy — pobiera świeże dane.
        """
        # Pomiń cache sesji gdy force_refresh — możliwe że GPT-4o wywołuje narzędzie
        # dla peers, których dane zostały już wstępnie pobrane i chcemy je też odświeżyć
        if not self.force_refresh and ticker in self.financial_data_cache:
            return self.financial_data_cache[ticker]

        # Pobierz z yfinance (lub cache plikowego, z uwzględnieniem force_refresh)
        data = get_financial_data(ticker, force_refresh=self.force_refresh)
        if data is not None:
            self.financial_data_cache[ticker] = data
        return data

    def execute_tool(self, tool_name: str, tool_args: dict) -> str:
        """Dispatcher narzędzi — wywołuje właściwą funkcję na podstawie nazwy.

        Args:
            tool_name: Nazwa narzędzia (z odpowiedzi GPT-4o).
            tool_args: Argumenty narzędzia (sparsowane z JSON).

        Returns:
            Wynik narzędzia jako string JSON (lub komunikat o błędzie).
        """
        ticker = tool_args.get("ticker", "")

        try:
            # --- get_financial_data ---
            if tool_name == "get_financial_data":
                result = get_financial_data(ticker, force_refresh=self.force_refresh)

                if result is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return json.dumps(
                        {"error": f"Could not fetch data for {ticker}."},
                        ensure_ascii=False,
                    )

                self.financial_data_cache[ticker] = result
                # Zapamiętaj cenę rynkową do odczytu przez frontend
                self.last_market_price = (
                    result.get("info", {}).get("currentPrice")
                    or result.get("info", {}).get("regularMarketPrice")
                )

                # Wyświetl potencjał wg konsensusu analityków (jeśli dostępny)
                consensus = result.get("analyst_consensus", {})
                cena_rynkowa = self.last_market_price or 0
                if consensus.get("target_mean") and cena_rynkowa > 0:
                    potencjal = (consensus["target_mean"] / cena_rynkowa - 1) * 100
                    console.print(
                        f"[dim]  📈 Analyst consensus implied move: {potencjal:+.1f}%[/dim]"
                    )

                self._log_tool_call(tool_name, tool_args, success=True)
                return json.dumps(result, ensure_ascii=False, indent=2, default=str)

            # --- run_dcf ---
            if tool_name == "run_dcf":
                financial_data = self._ensure_financial_data(ticker)
                if financial_data is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return self._no_data_message(ticker)

                result = run_dcf(
                    financial_data,
                    wacc=tool_args.get("wacc"),
                    growth_rate=tool_args.get("growth_rate"),
                    terminal_growth=tool_args.get("terminal_growth"),
                    years=tool_args.get("years", 5),
                )
                if "error" not in result:
                    self.last_dcf_result = result
                self._log_tool_call(tool_name, tool_args, success="error" not in result)
                return json.dumps(result, ensure_ascii=False, indent=2, default=str)

            # --- run_multiples ---
            if tool_name == "run_multiples":
                financial_data = self._ensure_financial_data(ticker)
                if financial_data is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return self._no_data_message(ticker)

                # Pobierz peers z uwzględnieniem branży (industry-first) —
                # przekazujemy financial_data żeby uniknąć ponownego zapytania yfinance
                peers_tickers = get_sector_peers(ticker, financial_data=financial_data)
                peers_data: list[dict] = []
                for peer_ticker in peers_tickers:
                    peer = self._ensure_financial_data(peer_ticker)
                    if peer is not None:
                        peers_data.append(peer)

                result = run_multiples(financial_data, peers_data=peers_data or None)
                self.last_multiples_result = result
                self._log_tool_call(tool_name, tool_args, success=True)
                return json.dumps(result, ensure_ascii=False, indent=2, default=str)

            # --- run_ddm ---
            if tool_name == "run_ddm":
                financial_data = self._ensure_financial_data(ticker)
                if financial_data is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return self._no_data_message(ticker)

                required_return = tool_args.get("required_return")
                result = run_ddm(financial_data, required_return=required_return)

                # run_ddm zwraca None gdy brak dywidend lub model niestabilny
                if result is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return json.dumps(
                        {"info": (
                            f"DDM not available for {ticker} — insufficient "
                            "dividend history or unstable model (g ≥ r)."
                        )},
                        ensure_ascii=False,
                    )

                self.last_ddm_result = result
                self._log_tool_call(tool_name, tool_args, success=True)
                return json.dumps(result, ensure_ascii=False, indent=2, default=str)

            # --- sensitivity_analysis ---
            if tool_name == "sensitivity_analysis":
                financial_data = self._ensure_financial_data(ticker)
                if financial_data is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return self._no_data_message(ticker)

                base_wacc = tool_args.get("base_wacc", 0.10)
                base_growth = tool_args.get("base_growth", 0.05)

                result = sensitivity_analysis(financial_data, base_wacc, base_growth)
                self.last_sensitivity_result = result
                self._log_tool_call(tool_name, tool_args, success=True)
                return json.dumps(result, ensure_ascii=False, indent=2, default=str)

            # --- run_sotp ---
            if tool_name == "run_sotp":
                financial_data = self._ensure_financial_data(ticker)
                if financial_data is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return self._no_data_message(ticker)

                result = run_sotp(financial_data)

                if result is None:
                    self._log_tool_call(tool_name, tool_args, success=False)
                    return json.dumps(
                        {"info": (
                            f"SOTP not available for {ticker} — company is not "
                            "in SOTP_COMPANIES. Use standard methods."
                        )},
                        ensure_ascii=False,
                    )

                self.last_sotp_result = result
                self._log_tool_call(tool_name, tool_args, success=True)
                return json.dumps(result, ensure_ascii=False, indent=2, default=str)

            # --- Nieznane narzędzie ---
            self._log_tool_call(tool_name, tool_args, success=False)
            return json.dumps(
                {"error": f"Unknown tool: {tool_name}"},
                ensure_ascii=False,
            )

        except Exception as e:
            # Przechwycenie dowolnego błędu — agent nie powinien się wysypać
            self._log_tool_call(tool_name, tool_args, success=False)
            console.print(
                f"[bold red]✗ Tool error {tool_name}: {e}[/bold red]"
            )
            return json.dumps(
                {"error": f"Error while running {tool_name}: {str(e)}"},
                ensure_ascii=False,
            )

    def run(self, ticker: str, verbose: bool = False, force_refresh: bool = False) -> Optional[str]:
        """Główna pętla agenta — komunikacja z GPT-4o w cyklu tool-use.

        1. Wysyła wiadomość początkową do GPT-4o z tickerem.
        2. GPT-4o odpowiada wywołaniami narzędzi lub tekstem raportu.
        3. Agent wykonuje narzędzia i zwraca wyniki do modelu.
        4. Cykl powtarza się aż GPT-4o wygeneruje finalny tekst (bez tool calls).

        Args:
            ticker: Symbol giełdowy spółki do wyceny.
            verbose: Jeśli True, wyświetla szczegółowe logi komunikacji.

        Returns:
            Tekst raportu wyceny (Markdown) lub None w razie błędu.
        """
        # Zapisz flagę odświeżenia — execute_tool() i _ensure_financial_data()
        # odczytają ją przy każdym wywołaniu get_financial_data
        self.force_refresh = force_refresh

        console.print(
            f"\n[bold cyan]🤖 Starting valuation agent for "
            f"[yellow]{ticker}[/yellow]...[/bold cyan]\n"
        )
        if force_refresh:
            console.print(
                "[yellow]  🔄 --fresh: skipping on-disk cache — "
                "fetching live yfinance data.[/yellow]"
            )

        # Wstaw dzisiejszą datę do system promptu — model nie zna aktualnej daty
        from datetime import date
        today = date.today().strftime("%d.%m.%Y")
        system_prompt = SYSTEM_PROMPT.replace("{today}", today)

        # Historia konwersacji z GPT-4o
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Run a full intrinsic-value analysis for ticker: {ticker}. "
                    f"Use all applicable valuation methods and write the "
                    f"report in English."
                ),
            },
        ]

        # Pętla agenta — max iteracji jako zabezpieczenie
        for iteration in range(1, MAX_ITERATIONS + 1):
            if verbose:
                console.print(
                    f"[dim]  ─── Iteration {iteration}/{MAX_ITERATIONS} "
                    f"(messages: {len(messages)}) ───[/dim]"
                )

            # Zapytanie do GPT-4o z retry na rate-limit (429)
            response = None
            for _attempt in range(3):
                try:
                    with Live(
                        Spinner("dots", text="[dim]GPT-4o thinking...[/dim]"),
                        console=console,
                        transient=True,
                    ):
                        response = self.client.chat.completions.create(
                            model=MODEL,
                            messages=messages,
                            tools=TOOLS_DEFINITION,
                            tool_choice="auto",
                        )
                    break
                except Exception as e:
                    err_str = str(e)
                    if "rate_limit" in err_str or "429" in err_str:
                        wait = (_attempt + 1) * 10
                        console.print(
                            f"[yellow]  ⏳ Rate limit — waiting {wait}s "
                            f"(attempt {_attempt + 1}/3)...[/yellow]"
                        )
                        time.sleep(wait)
                        if _attempt == 2:
                            console.print(
                                f"[bold red]✗ Rate limit after 3 attempts: {e}[/bold red]"
                            )
                            return None
                    else:
                        console.print(
                            f"[bold red]✗ OpenAI API error: {e}[/bold red]"
                        )
                        return None

            if response is None:
                return None

            choice = response.choices[0]
            message = choice.message

            # Dodaj odpowiedź asystenta do historii
            messages.append(message.model_dump())

            # Sprawdź czy GPT-4o chce wywołać narzędzia
            if message.tool_calls:
                if verbose:
                    names = [tc.function.name for tc in message.tool_calls]
                    console.print(
                        f"[dim]  🔧 GPT-4o calling: {', '.join(names)}[/dim]"
                    )

                # Wykonaj każde żądane narzędzie i dodaj wynik do historii
                for tool_call in message.tool_calls:
                    func_name = tool_call.function.name
                    try:
                        func_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        func_args = {}

                    console.print(
                        f"[cyan]  ▶ {func_name}("
                        f"{', '.join(f'{k}={v!r}' for k, v in func_args.items())})"
                        f"[/cyan]"
                    )

                    # Wykonaj narzędzie
                    result_str = self.execute_tool(func_name, func_args)

                    # Wynik wraca do GPT-4o jako wiadomość tool
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    })

                # Kontynuuj pętlę — GPT-4o zdecyduje co dalej
                continue

            # GPT-4o nie wywołuje narzędzi → zwrócił finalny tekst raportu
            report_text = message.content
            if report_text:
                console.print(
                    f"\n[bold green]✓ Agent finished after "
                    f"{iteration} iterations "
                    f"({len(self.tool_call_log)} tool calls).[/bold green]"
                )
                return report_text

            # Brak treści i brak tool calls — nieoczekiwany stan
            console.print(
                "[yellow]  ⚠ GPT-4o returned an empty reply — requesting continuation...[/yellow]"
            )
            messages.append({
                "role": "user",
                "content": "Continue the analysis and produce the report.",
            })

        # Przekroczono limit iteracji
        console.print(
            f"[bold red]✗ Exceeded {MAX_ITERATIONS} iteration limit. "
            f"Agent did not finish.[/bold red]"
        )
        return None

    def run_preliminary(
        self, ticker: str, force_refresh: bool = False
    ) -> Optional[dict]:
        """Etap 1: pobiera dane i uruchamia obliczenia wyceny
        (DCF, mnożniki, DDM, analiza wrażliwości) bez generowania
        narracyjnego raportu przez GPT-4o.

        Zwraca słownik z wynikami i założeniami agenta lub None w razie błędu.
        Wyniki pod kluczami zaczynającymi się od '_' to surowe dane
        przekazywane do run_final() — użytkownik ich nie widzi.
        """
        self.force_refresh = force_refresh

        console.print(
            f"\n[bold cyan]📊 Preliminary analysis for "
            f"[yellow]{ticker}[/yellow]...[/bold cyan]\n"
        )

        # Pobierz dane finansowe z yfinance / cache
        financial_data = self._ensure_financial_data(ticker)
        if financial_data is None:
            return None

        info = financial_data.get("info") or {}
        self.last_market_price = (
            info.get("currentPrice") or info.get("regularMarketPrice")
        )

        # DCF — agent oblicza WACC z CAPM i growth z historii FCF
        dcf_result = run_dcf(financial_data)
        if "error" not in dcf_result:
            self.last_dcf_result = dcf_result

        # Wyciągnij założenia użyte przez DCF do wyświetlenia w edytorze
        zalozenia = dcf_result.get("zalozenia") or {}
        wacc_uzyte = zalozenia.get("wacc", 0.10)
        growth_uzyte = zalozenia.get("growth_rate", 0.05)

        # Oblicz współczynnik zmienności FCF (cv_fcf) — im wyższy tym bardziej
        # cykliczna spółka; próg 0.4 sugeruje użycie mediany zamiast ostatniego FCF
        fcf_historia = dcf_result.get("fcf_historyczne") or []
        cv_fcf = 0.0
        if len(fcf_historia) >= 3:
            try:
                mean_fcf = statistics.mean(fcf_historia)
                if mean_fcf != 0:
                    cv_fcf = statistics.stdev(fcf_historia) / abs(mean_fcf)
            except statistics.StatisticsError:
                cv_fcf = 0.0

        # Pobierz peers i uruchom wycenę mnożnikową
        peers_tickers = get_sector_peers(ticker, financial_data=financial_data)
        peers_data: list[dict] = []
        for peer_ticker in peers_tickers:
            peer = self._ensure_financial_data(peer_ticker)
            if peer is not None:
                peers_data.append(peer)

        multiples_result = run_multiples(
            financial_data, peers_data=peers_data or None
        )
        self.last_multiples_result = multiples_result

        # DDM — tylko jeśli spółka wypłaca dywidendy
        ddm_result = run_ddm(financial_data)
        if ddm_result is not None:
            self.last_ddm_result = ddm_result

        # Analiza wrażliwości z parametrami agenta
        sens_result = sensitivity_analysis(financial_data, wacc_uzyte, growth_uzyte)
        self.last_sensitivity_result = sens_result

        # Ceny wstępne do podglądu na ekranie edycji założeń
        dcf_cena = (
            dcf_result.get("cena_z_korekta_buyback")
            or dcf_result.get("cena_na_akcje")
        )
        mnozniki_mediana = multiples_result.get("mediana")
        waluta = (info.get("currency") or "USD").strip()

        return {
            # Założenia agenta — wyświetlane i edytowalne przez użytkownika
            "wacc_uzyte": wacc_uzyte,
            "growth_uzyte": growth_uzyte,
            "peers": list(peers_tickers),
            "cv_fcf": cv_fcf,
            # Wstępne ceny do podglądu
            "dcf_cena": dcf_cena,
            "mnozniki_mediana": mnozniki_mediana,
            "cena_rynkowa": self.last_market_price,
            "waluta": waluta,
            # Surowe wyniki przekazywane do run_final (nie wyświetlane)
            "_financial_data": financial_data,
            "_dcf_result": dcf_result,
            "_multiples_result": multiples_result,
            "_ddm_result": ddm_result,
            "_sensitivity_result": sens_result,
            "_peers_tickers": list(peers_tickers),
        }

    def run_final(
        self,
        ticker: str,
        assumptions: dict,
        wstepne: Optional[dict] = None,
        force_refresh: bool = False,
    ) -> Optional[str]:
        """Etap 2: przelicza wycenę z zatwierdzonymi założeniami użytkownika
        i generuje narracyjny raport przez GPT-4o (jedno wywołanie — bez pętli
        tool-use, wyniki obliczeń przekazywane bezpośrednio w prompcie).

        Args:
            ticker: Symbol giełdowy spółki.
            assumptions: Zatwierdzone założenia użytkownika
                         (wacc, growth, wide_moat, cykliczna, buyback, peers).
            wstepne: Wyniki z run_preliminary() — pozwala uniknąć ponownego
                     pobierania danych i obliczania mnożników gdy peers niezmienione.
            force_refresh: Czy pominąć cache yfinance.

        Returns:
            Tekst raportu wyceny w Markdown lub None w razie błędu.
        """
        self.force_refresh = force_refresh

        # Odtwórz lub pobierz dane finansowe
        if wstepne and "_financial_data" in wstepne:
            financial_data = wstepne["_financial_data"]
            self.financial_data_cache[ticker] = financial_data
        else:
            financial_data = self._ensure_financial_data(ticker)

        if financial_data is None:
            return None

        info = financial_data.get("info") or {}
        self.last_market_price = (
            info.get("currentPrice") or info.get("regularMarketPrice")
        )

        # Parametry zatwierdzone przez użytkownika
        wacc = assumptions.get("wacc", 0.10)
        growth = assumptions.get("growth", 0.05)
        wide_moat = assumptions.get("wide_moat", False)
        buyback = assumptions.get("buyback", True)
        user_peers = [p for p in (assumptions.get("peers") or []) if p]

        # Wide moat → terminal growth wyższe o 0.5pp (silna przewaga konkurencyjna
        # uzasadnia nieco wyższy wzrost w okresie terminalnym)
        terminal_growth = DEFAULT_TERMINAL_GROWTH + (0.005 if wide_moat else 0.0)

        # Przelicz DCF z zatwierdzonymi przez użytkownika parametrami
        console.print(
            f"[cyan]  ▶ run_dcf(wacc={wacc:.1%}, growth={growth:.1%}, "
            f"terminal={terminal_growth:.1%}, buyback={buyback})[/cyan]"
        )
        dcf_result = run_dcf(
            financial_data,
            wacc=wacc,
            growth_rate=growth,
            terminal_growth=terminal_growth,
            apply_buyback=buyback,
        )
        if "error" not in dcf_result:
            self.last_dcf_result = dcf_result

        # Przelicz mnożniki — tylko jeśli peers zostały zmienione przez użytkownika
        prev_peers = set((wstepne or {}).get("_peers_tickers") or [])
        peers_changed = set(user_peers) != prev_peers

        if peers_changed and user_peers:
            console.print(
                f"[cyan]  ▶ run_multiples (peers edited: {user_peers})[/cyan]"
            )
            peers_data: list[dict] = []
            for pt in user_peers:
                peer = self._ensure_financial_data(pt)
                if peer is not None:
                    peers_data.append(peer)
            multiples_result = run_multiples(
                financial_data, peers_data=peers_data or None
            )
        elif wstepne and "_multiples_result" in wstepne:
            # Peers niezmienione — użyj wyników z etapu 1
            multiples_result = wstepne["_multiples_result"]
            console.print("[dim]  ✓ Multiples unchanged — reusing stage-1 results[/dim]")
        else:
            multiples_result = run_multiples(financial_data)
        self.last_multiples_result = multiples_result

        # DDM z cache etapu 1 lub ponowne obliczenie
        if wstepne and "_ddm_result" in wstepne:
            ddm_result = wstepne["_ddm_result"]
            if ddm_result is not None:
                self.last_ddm_result = ddm_result
        else:
            ddm_result = run_ddm(financial_data)
            if ddm_result is not None:
                self.last_ddm_result = ddm_result

        # Analiza wrażliwości z nowymi (zatwierdzonymi) parametrami
        sens_result = sensitivity_analysis(financial_data, wacc, growth)
        self.last_sensitivity_result = sens_result

        # Guardrails do raportu finalnego:
        # - zakres fair value liczony tylko z metod modelowych
        # - ocena wiarygodnosci metod i confidence score
        valuation_guardrails = _build_valuation_guardrails(
            financial_data=financial_data,
            dcf_result=dcf_result,
            multiples_result=multiples_result,
            ddm_result=ddm_result,
            sensitivity_result=sens_result,
        )
        analyst_consensus = _sanitize_analyst_consensus(financial_data)

        # Generuj narracyjny raport przez GPT-4o — jedno wywołanie bez tool-use,
        # wszystkie wyniki obliczeń przekazane bezpośrednio w treści wiadomości
        from datetime import date
        today = date.today().strftime("%d.%m.%Y")
        system_prompt = SYSTEM_PROMPT.replace("{today}", today)

        summary = {
            "ticker": ticker,
            "zatwierdzone_zalozenia": {
                "wacc": wacc,
                "growth_fcf": growth,
                "terminal_growth": terminal_growth,
                "wide_moat": wide_moat,
                "buyback": buyback,
                "peers_uzyte": user_peers,
            },
            "dcf": dcf_result,
            "multiples": multiples_result,
            "ddm": ddm_result,
            "sensitivity": sens_result,
            "cena_rynkowa": self.last_market_price,
            "konsensus_analitykow": analyst_consensus,
            "podsumowanie_metod": valuation_guardrails,
        }

        user_msg = (
            f"Generate the full valuation report for {ticker}. "
            f"The user approved assumptions: "
            f"WACC={wacc:.1%}, FCF growth={growth:.1%}, "
            f"terminal growth={terminal_growth:.1%}"
            f"{', wide moat (durable competitive advantage)' if wide_moat else ''}"
            f"{', cyclical company (median FCF used)' if assumptions.get('cykliczna') else ''}"
            f", peers: {', '.join(user_peers) if user_peers else 'default peer set'}. "
            f"Write the report in English.\n\n"
            f"CRITICAL REPORT RULES:\n"
            f"1) Compute low–median–high ONLY from "
            f"`podsumowanie_metod.zakres_wartosci_godziwej_reliable`.\n"
            f"2) Never use market price as part of the fair value range.\n"
            f"3) Describe methods listed under `metody_pomocnicze` as secondary — "
            f"do not fold them into the primary range.\n"
            f"4) Build the 'Analyst consensus' section ONLY from "
            f"`konsensus_analitykow` — do not invent missing fields "
            f"(e.g. implied vote counts by recommendation).\n"
            f"5) Add a 'Report confidence assessment' section from "
            f"`podsumowanie_metod.confidence`.\n\n"
            f"Calculation results:\n"
            f"{json.dumps(summary, ensure_ascii=False, indent=2, default=str)}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        console.print("\n[bold cyan]🤖 Generating final report...[/bold cyan]")
        try:
            with Live(
                Spinner("dots", text="[dim]GPT-4o drafting report...[/dim]"),
                console=console,
                transient=True,
            ):
                response = self.client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    # Bez tools — chcemy bezpośrednio tekst raportu
                )
            report_text = response.choices[0].message.content
            if report_text:
                console.print(
                    "[bold green]✓ Final report generated.[/bold green]"
                )
                return report_text
            return None
        except Exception as e:
            console.print(
                f"[bold red]✗ Report generation failed: {e}[/bold red]"
            )
            return None

    def _log_tool_call(self, tool_name: str, tool_args: dict, success: bool) -> None:
        """Zapisuje wywołanie narzędzia do dziennika."""
        self.tool_call_log.append({
            "tool": tool_name,
            "args": tool_args,
            "success": success,
        })

    @staticmethod
    def _no_data_message(ticker: str) -> str:
        """Zwraca ustandaryzowany komunikat o braku danych finansowych."""
        return json.dumps(
            {
                "error": (
                    f"No financial data for {ticker}. "
                    f"Call get_financial_data first."
                )
            },
            ensure_ascii=False,
        )
