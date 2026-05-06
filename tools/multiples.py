"""Wycena przez mnożniki rynkowe — obliczanie i interpretacja wskaźników
P/E, EV/EBITDA, P/BV na tle sektora i historycznych średnich.
"""

from typing import Optional
from statistics import median

from rich.console import Console
from tools.data_fetcher import safe_get_value

console = Console(legacy_windows=False)

# Domyślne mnożniki sektorowe (fallback gdy brak danych z peers)
DEFAULT_PE = 15.0
DEFAULT_EV_EBITDA = 10.0
DEFAULT_PBV = 1.5
# Gaming trade'uje 4-10x przychodów — mediana branżowa jako fallback
DEFAULT_EV_SALES_GAMING = 5.0
DEFAULT_EV_SALES = 2.0  # fallback dla innych sektorów

# Branże dla których EV/Sales jest szczególnie istotny (brak zysku między premierami)
_INDUSTRIES_EV_SALES_PRIMARY = {"Electronic Gaming & Multimedia"}

# ─────────────────────────────────────────────────────────────────────────────
# Korekta dyskontowa dla spółek GPW porównywanych z zachodnimi peers.
#
# Polskie spółki systematycznie handlują poniżej zachodnich odpowiedników
# z trzech powodów:
#   1. Premia za ryzyko kraju (country risk premium) ~2–3% — Polska to
#      rynek wschodzący (EM) w indeksach MSCI/FTSE.
#   2. Niższa płynność GPW: mniejsza baza inwestorów, węższe spready,
#      niższy free-float → inwestorzy instytucjonalni wymagają dyskonta.
#   3. Różnice w standardach rachunkowości i jakości ładu korporacyjnego.
#
# Discount dobierany sektorowo — sektory o wyższej ekspozycji na ryzyko
# geopolityczne lub bardziej zdominowane przez globalne spółki US (tech, surowce)
# mają wyższy discount niż np. banki GPW porównywane z bankami europejskimi.
# ─────────────────────────────────────────────────────────────────────────────
GPW_DISCOUNT_BY_SECTOR: dict[str, float] = {
    "Energy": 0.20,             # 20% discount vs US/EU energy (np. PKN.WA vs XOM)
    "Basic Materials": 0.25,    # 25% discount (KGHM vs FCX, AA — US copper/aluminium)
    "Utilities": 0.15,          # PGE vs NEE, EDP — niższy discount bo bardziej lokalne
    "Consumer Defensive": 0.15,
    "Consumer Cyclical": 0.20,
    "Industrials": 0.20,
    "Technology": 0.25,         # CDR vs EA, TTWO — gaming/tech premium USA
    "Financial Services": 0.10, # Banki GPW vs europejskie — bliższe porównanie
    "Healthcare": 0.20,
    "Real Estate": 0.15,
}

# Sufiks tickerów z rynków wschodzących CEE — nie stosujemy dyskonta
# gdy peer jest z podobnego rynku (np. Czechy, Węgry)
_CEE_SUFFIXES = {".WA", ".HU", ".PR", ".RO", ".BU"}


def _is_western_peer(peer_ticker: str) -> bool:
    """Zwraca True gdy peer pochodzi z rynku zachodniego (USA/Europa Zachodnia).

    Zachodnie peers to spółki bez sufiksu CEE — handlują z wyższymi mnożnikami
    ze względu na większą płynność i niższe ryzyko kraju.
    """
    upper = peer_ticker.upper()
    return not any(upper.endswith(sfx) for sfx in _CEE_SUFFIXES)


def _remove_outliers(
    labeled_values: list[tuple[str, float]],
    label: str,
) -> list[float]:
    """Usuwa wartości odstające ze zbioru mnożników metodą IQR (Interquartile Range).

    Algorytm Tukey'a: dolna granica = Q1 - 1.5×IQR, górna = Q3 + 1.5×IQR.
    Wartości poza granicami traktowane jako outliery i usuwane.

    Jeśli po filtracji zostałoby < 2 wartości, zwraca oryginalną listę —
    przy małej próbie usuwanie outlierów bardziej szkodzi niż pomaga.

    Args:
        labeled_values: Lista par (ticker, wartość) — ticker służy tylko do logów.
        label: Nazwa mnożnika do wyświetlenia w logu (np. "P/E", "EV/EBITDA").

    Returns:
        Lista wartości po usunięciu outlierów (bez tickerów).
    """
    if len(labeled_values) < 4:
        # Zbyt mała próba — IQR dałby zbyt agresywne cięcie
        return [v for _, v in labeled_values]

    values = [v for _, v in labeled_values]
    values_sorted = sorted(values)
    n = len(values_sorted)

    # Kwantyle Q1 i Q3 — metoda interpolacji liniowej
    q1 = values_sorted[n // 4]
    q3 = values_sorted[(3 * n) // 4]
    iqr = q3 - q1

    if iqr == 0:
        # Wszystkie wartości identyczne lub brak rozrzutu — nic nie usuwamy
        return values

    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr

    filtered: list[float] = []
    for ticker_name, val in labeled_values:
        if lower <= val <= upper:
            filtered.append(val)
        else:
            console.print(
                f"[dim]    ✂ Usunięto outlier {label}: {ticker_name} = {val:.2f}x "
                f"(poza IQR [{lower:.2f}, {upper:.2f}])[/dim]"
            )

    # Zabezpieczenie: jeśli zbyt wiele outlierów, wróć do oryginału
    if len(filtered) < 2:
        console.print(
            f"[dim]    ↩ Za mało danych po filtracji {label} — przywracam oryginalne wartości.[/dim]"
        )
        return values

    return filtered


def _get_latest_value(section: dict, key: str) -> Optional[float]:
    """Wyciąga najnowszą wartość danego wiersza z sekcji tabelarycznej
    (financials / balance_sheet / cashflow). Kolumny posortowane malejąco
    po dacie — bierzemy najnowszą.

    Używa safe_get_value() jako zabezpieczenie — yfinance może zwrócić
    DataFrame/Series zamiast skalara w niektórych konfiguracjach.
    """
    if not section:
        return None

    from tools.data_fetcher import safe_get_value

    # Kolumny to daty ISO — sortujemy malejąco, pierwsza = najnowsza
    for date_key in sorted(section.keys(), reverse=True):
        raw = section[date_key].get(key)
        val = safe_get_value(raw)
        if val is not None:
            return val
    return None


def _estimate_buyback_rate(financial_data: dict) -> Optional[float]:
    """Szacuje średnie roczne tempo redukcji liczby akcji (buyback)."""
    balance_sheet = financial_data.get("balance_sheet", {}) or {}
    if not balance_sheet:
        return None

    shares_history: list[float] = []
    for date_key in sorted(balance_sheet.keys()):
        period = balance_sheet.get(date_key) or {}
        shares = _get_latest_value({date_key: period}, "Ordinary Shares Number")
        if shares is None:
            shares = _get_latest_value({date_key: period}, "Share Issued")
        if shares is not None and shares > 0:
            shares_history.append(float(shares))

    if len(shares_history) < 2:
        return None

    shares_oldest = shares_history[0]
    shares_newest = shares_history[-1]
    years = len(shares_history) - 1
    if shares_oldest <= 0 or years <= 0:
        return None

    return max(0.0, (shares_oldest - shares_newest) / shares_oldest / years)


def _is_intangible_heavy_industry(industry: str) -> bool:
    """Heurystyka branż, gdzie P/BV bywa słabym miernikiem wartości."""
    i = (industry or "").lower()
    keywords = (
        "software",
        "internet",
        "semiconductor",
        "consumer electronics",
        "electronic gaming",
        "communication",
        "biotechnology",
        "pharmaceutical",
    )
    return any(k in i for k in keywords)


def _evaluate_pbv_reliability(
    financial_data: dict,
    pbv_result: Optional[dict],
) -> tuple[bool, Optional[str]]:
    """Ocena, czy P/BV powinno wejść do mediany końcowej."""
    if pbv_result is None:
        return False, "Brak dodatniej wartości księgowej lub brak danych P/BV."

    info = financial_data.get("info", {}) or {}
    industry = info.get("industry", "")
    own_pbv = info.get("priceToBook")
    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    bvps = pbv_result.get("bvps")
    buyback_rate = _estimate_buyback_rate(financial_data)

    # Gdy wartość księgowa na akcję jest bardzo niska względem ceny rynkowej,
    # P/BV zwykle zaniża wycenę spółek asset-light / buyback-heavy.
    if current_price and bvps and current_price > 0 and bvps > 0:
        ratio_price_to_bvps = current_price / bvps
        if ratio_price_to_bvps > 8 and _is_intangible_heavy_industry(industry):
            return (
                False,
                (
                    f"Niski BVPS względem ceny (P/BV impl. {ratio_price_to_bvps:.1f}x) "
                    f"w branży asset-light ({industry})."
                ),
            )

    if own_pbv is not None and own_pbv > 10 and _is_intangible_heavy_industry(industry):
        return (
            False,
            f"Wysokie priceToBook ({own_pbv:.1f}x) w branży asset-light ({industry}).",
        )

    if buyback_rate is not None and buyback_rate > 0.01 and _is_intangible_heavy_industry(industry):
        return (
            False,
            (
                f"Silny buyback ({buyback_rate * 100:.2f}% r/r) obniża equity book, "
                "przez co P/BV jest zaniżające."
            ),
        )

    return True, None


def _evaluate_ev_sales_relevance(
    financial_data: dict,
    ev_sales_result: Optional[dict],
    pe_result: Optional[dict],
    ev_ebitda_result: Optional[dict],
) -> tuple[bool, Optional[str]]:
    """Ocena czy EV/Sales powinno wejść do mediany końcowej.

    EV/Sales jest najlepsze dla firm wzrostowych lub nisko-zyskownych.
    Dla dojrzałych, zyskownych spółek traktujemy je jako wskaźnik pomocniczy,
    bo może nadawać zbyt dużą wagę marżowo różnym peerom.
    """
    if ev_sales_result is None:
        return False, "Brak wyniku EV/Sales."

    info = financial_data.get("info", {}) or {}
    industry = (info.get("industry") or "").strip()
    profit_margin = safe_get_value(info.get("profitMargins"))
    operating_margin = safe_get_value(info.get("operatingMargins"))
    revenue_growth = safe_get_value(info.get("revenueGrowth"))
    earnings_growth = safe_get_value(info.get("earningsGrowth"))
    trailing_pe = safe_get_value(info.get("trailingPE"))
    forward_pe = safe_get_value(info.get("forwardPE"))
    market_cap = safe_get_value(info.get("marketCap"))

    if industry in _INDUSTRIES_EV_SALES_PRIMARY:
        return True, f"Branza {industry} - EV/Sales jest mnoznikiem pierwszorzednym."

    # Jeśli klasyczne mnożniki nie działają, EV/Sales staje się kluczowe.
    if pe_result is None or ev_ebitda_result is None:
        return True, "Brak stabilnego P/E lub EV/EBITDA."

    if (profit_margin is not None and profit_margin <= 0) or (
        operating_margin is not None and operating_margin <= 0
    ):
        return True, "Niska/ujemna rentownosc - EV/Sales lepiej oddaje skale biznesu."

    if (trailing_pe is None or trailing_pe <= 0) and (forward_pe is None or forward_pe <= 0):
        return True, "Brak dodatniego P/E."

    # Dla dużych, stabilnie rentownych megacapów EV/Sales zwykle ma charakter
    # pomocniczy i bywa zdominowany przez różnice marżowe między peerami.
    if (
        market_cap is not None
        and market_cap >= 300_000_000_000
        and (profit_margin is not None and profit_margin > 0.15)
        and (operating_margin is not None and operating_margin > 0.15)
        and ((trailing_pe is not None and trailing_pe > 0) or (forward_pe is not None and forward_pe > 0))
    ):
        return False, "Megacap o stabilnej rentowności - EV/Sales traktuj pomocniczo."

    high_growth = False
    if revenue_growth is not None and revenue_growth >= 0.20:
        high_growth = True
    if earnings_growth is not None and earnings_growth >= 0.20:
        high_growth = True
    if high_growth:
        return True, "Wysoki wzrost (>20%) - EV/Sales istotny jako mnoznik wzrostowy."

    return False, "Dojrzaly profil rentownosci - EV/Sales tylko informacyjnie."


def _compute_peer_multiples(peers_data: list[dict]) -> dict[str, Optional[float]]:
    """Oblicza mediany mnożników z danych spółek porównywalnych.

    Szuka kluczy trailingPE / forwardPE, enterpriseToEbitda, priceToBook,
    enterpriseToRevenue (EV/Sales) w sekcji info każdego peera.

    Przed obliczeniem mediany usuwa outliery metodą IQR, żeby pojedyncze
    spółki z ekstremalnym mnożnikiem (np. Pfizer po krachu, Celsius po hype)
    nie przesuwały mediany sektorowej o setki USD.
    """
    # Zbieramy pary (ticker, wartość) — ticker służy tylko do logów outlierów
    pe_pairs: list[tuple[str, float]] = []
    ev_ebitda_pairs: list[tuple[str, float]] = []
    pbv_pairs: list[tuple[str, float]] = []
    ev_sales_pairs: list[tuple[str, float]] = []

    for peer in peers_data:
        info = peer.get("info", {})
        peer_ticker = peer.get("ticker", "?")

        # P/E — preferujemy forward, fallback na trailing
        pe = info.get("forwardPE") or info.get("trailingPE")
        if pe is not None and 0 < pe < 200:
            pe_pairs.append((peer_ticker, float(pe)))

        ev_eb = info.get("enterpriseToEbitda")
        if ev_eb is not None and 0 < ev_eb < 100:
            ev_ebitda_pairs.append((peer_ticker, float(ev_eb)))

        pbv = info.get("priceToBook")
        if pbv is not None and 0 < pbv < 50:
            pbv_pairs.append((peer_ticker, float(pbv)))

        # EV/Sales (enterpriseToRevenue) — kluczowy dla gaming i spółek wzrostowych
        ev_sales = info.get("enterpriseToRevenue")
        if ev_sales is not None and 0 < ev_sales < 50:
            ev_sales_pairs.append((peer_ticker, float(ev_sales)))

    # Usuń outliery metodą IQR przed obliczeniem mediany sektorowej
    pe_clean = _remove_outliers(pe_pairs, "P/E")
    ev_ebitda_clean = _remove_outliers(ev_ebitda_pairs, "EV/EBITDA")
    pbv_clean = _remove_outliers(pbv_pairs, "P/BV")
    ev_sales_clean = _remove_outliers(ev_sales_pairs, "EV/Sales")

    return {
        "pe": median(pe_clean) if pe_clean else None,
        "ev_ebitda": median(ev_ebitda_clean) if ev_ebitda_clean else None,
        "pbv": median(pbv_clean) if pbv_clean else None,
        "ev_sales": median(ev_sales_clean) if ev_sales_clean else None,
    }


def _valuation_pe(
    financial_data: dict,
    sector_pe: float,
    shares: float,
) -> Optional[dict]:
    """Wycena mnożnikiem P/E: cena = EPS × P/E sektora."""
    info = financial_data.get("info", {})
    financials = financial_data.get("financials", {})

    # Próba 1: zysk netto z rachunku zysków i strat ÷ liczba akcji
    net_income = _get_latest_value(financials, "Net Income")
    if net_income is not None and shares > 0:
        eps = net_income / shares
    else:
        eps = None

    # Próba 2: oblicz EPS z trailing P/E i ceny (P/E = cena/EPS → EPS = cena/P/E)
    if eps is None:
        trailing_pe = info.get("trailingPE")
        current_price = info.get("currentPrice")
        if trailing_pe and current_price and trailing_pe > 0:
            eps = current_price / trailing_pe

    if eps is None or eps <= 0:
        console.print("[yellow]    ⚠ P/E: brak dodatniego EPS — pomijam.[/yellow]")
        return None

    fair_price = eps * sector_pe

    return {
        "mnoznik": "P/E",
        "wartosc_mnoznika": round(sector_pe, 2),
        "eps": round(eps, 2),
        "cena_na_akcje": round(fair_price, 2),
    }


def _valuation_ev_ebitda(
    financial_data: dict,
    sector_ev_ebitda: float,
    shares: float,
) -> Optional[dict]:
    """Wycena mnożnikiem EV/EBITDA: EV = EBITDA × mnożnik → equity = EV - dług + gotówka."""
    info = financial_data.get("info", {})
    financials = financial_data.get("financials", {})

    # EBITDA = EBIT + Depreciation & Amortization
    ebit = _get_latest_value(financials, "EBIT")
    da = _get_latest_value(financials, "Reconciled Depreciation")

    if ebit is not None and da is not None:
        ebitda = ebit + abs(da)
    elif ebit is not None:
        # Przybliżenie: EBITDA ≈ EBIT × 1.15 (typowa korekta o D&A)
        ebitda = ebit * 1.15
        console.print(
            "[dim]    ℹ Brak D&A — przybliżam EBITDA ≈ EBIT × 1.15[/dim]"
        )
    else:
        console.print(
            "[yellow]    ⚠ EV/EBITDA: brak danych EBIT — pomijam.[/yellow]"
        )
        return None

    if ebitda <= 0:
        console.print(
            "[yellow]    ⚠ EV/EBITDA: ujemna EBITDA — pomijam.[/yellow]"
        )
        return None

    enterprise_value = ebitda * sector_ev_ebitda

    # Equity Value = EV - dług + gotówka
    total_debt = info.get("totalDebt", 0) or 0
    total_cash = info.get("totalCash", 0) or 0
    equity_value = enterprise_value - total_debt + total_cash

    if shares > 0:
        fair_price = equity_value / shares
    else:
        return None

    return {
        "mnoznik": "EV/EBITDA",
        "wartosc_mnoznika": round(sector_ev_ebitda, 2),
        "ebitda": round(ebitda, 2),
        "enterprise_value": round(enterprise_value, 2),
        "cena_na_akcje": round(fair_price, 2),
    }


def _valuation_pbv(
    financial_data: dict,
    sector_pbv: float,
    shares: float,
) -> Optional[dict]:
    """Wycena mnożnikiem P/BV: cena = wartość księgowa na akcję × P/BV sektora."""
    info = financial_data.get("info", {})
    balance_sheet = financial_data.get("balance_sheet", {})

    # Wartość księgowa = Total Assets - Total Liabilities (lub Stockholders Equity)
    equity_book = _get_latest_value(balance_sheet, "Stockholders Equity")

    if equity_book is None:
        total_assets = _get_latest_value(balance_sheet, "Total Assets")
        total_liab = _get_latest_value(balance_sheet, "Total Liabilities Net Minority Interest")
        if total_assets is not None and total_liab is not None:
            equity_book = total_assets - total_liab

    if equity_book is None or equity_book <= 0 or shares <= 0:
        console.print(
            "[yellow]    ⚠ P/BV: brak dodatniej wartości księgowej — pomijam.[/yellow]"
        )
        return None

    bvps = equity_book / shares  # Book Value Per Share
    fair_price = bvps * sector_pbv

    return {
        "mnoznik": "P/BV",
        "wartosc_mnoznika": round(sector_pbv, 2),
        "bvps": round(bvps, 2),
        "cena_na_akcje": round(fair_price, 2),
    }


def _valuation_ev_sales(
    financial_data: dict,
    sector_ev_sales: float,
    shares: float,
) -> Optional[dict]:
    """Wycena mnożnikiem EV/Sales: EV = Revenue × mnożnik → equity = EV - dług + gotówka.

    Szczególnie przydatna dla spółek gamingowych i wzrostowych, gdzie zysk
    lub EBITDA są niestabilne lub ujemne między ważnymi premierami.
    """
    info = financial_data.get("info", {})
    financials = financial_data.get("financials", {})

    # Przychody z rachunku zysków i strat — szukamy Total Revenue
    revenue = _get_latest_value(financials, "Total Revenue")

    # Fallback na dane z info (yfinance ttm)
    if revenue is None:
        revenue_raw = info.get("totalRevenue")
        if revenue_raw is not None:
            try:
                revenue = float(revenue_raw)
            except (TypeError, ValueError):
                revenue = None

    if revenue is None or revenue <= 0:
        console.print("[yellow]    ⚠ EV/Sales: brak danych o przychodach — pomijam.[/yellow]")
        return None

    # Enterprise Value na podstawie przychodu i mnożnika sektora
    enterprise_value = revenue * sector_ev_sales

    # Equity Value = EV - dług netto (odejmujemy dług, dodajemy gotówkę)
    total_debt = info.get("totalDebt", 0) or 0
    total_cash = info.get("totalCash", 0) or 0
    equity_value = enterprise_value - total_debt + total_cash

    if shares <= 0:
        return None

    fair_price = equity_value / shares

    # Wartość ujemna przy bardzo wysokim zadłużeniu — pomijamy
    if fair_price <= 0:
        console.print("[yellow]    ⚠ EV/Sales: ujemna cena po odliczeniu długu — pomijam.[/yellow]")
        return None

    return {
        "mnoznik": "EV/Sales",
        "wartosc_mnoznika": round(sector_ev_sales, 2),
        "revenue": round(revenue, 2),
        "enterprise_value": round(enterprise_value, 2),
        "cena_na_akcje": round(fair_price, 2),
    }


def run_multiples(
    financial_data: dict,
    peers_data: Optional[list[dict]] = None,
) -> dict:
    """Wycena porównawcza przez mnożniki rynkowe (P/E, EV/EBITDA, P/BV, EV/Sales).

    Oblicza wartość godziwą akcji czterema metodami i zwraca medianę.
    EV/Sales szczególnie istotny dla spółek gamingowych i wzrostowych,
    gdzie FCF jest niestabilny między premierami gier.

    Args:
        financial_data: Słownik z danymi finansowymi (z get_financial_data).
        peers_data: Lista słowników z danymi spółek porównywalnych (opcjonalnie).

    Returns:
        Słownik z wynikami czterech wycen mnożnikowych i ich medianą.
    """
    ticker = financial_data.get("ticker", "?")
    info = financial_data.get("info", {})
    currency = info.get("currency", "")
    current_price = info.get("currentPrice")
    shares = info.get("sharesOutstanding", 0) or 0
    industry = info.get("industry", "")

    console.print(
        f"\n[bold cyan]📊 Wycena mnożnikowa dla [yellow]{ticker}[/yellow]...[/bold cyan]"
    )

    # Ustal mnożniki sektorowe — z peers lub fallback
    peers_quality_note: Optional[str] = None
    peers_sample_size = len(peers_data) if peers_data else 0

    if peers_data:
        if peers_sample_size >= 2:
            console.print(
                f"[dim]  Obliczam mediany mnożników z {peers_sample_size} "
                f"spółek porównywalnych...[/dim]"
            )
            if peers_sample_size < 3:
                peers_quality_note = (
                    "Niska liczebność próby peers (<3). "
                    "Wyniki mnożnikowe traktuj orientacyjnie."
                )
                console.print(
                    "[yellow]  ⚠ Niska liczebność peers (<3) — "
                    "mediany mogą być niestabilne.[/yellow]"
                )
            peer_multiples = _compute_peer_multiples(peers_data)
        else:
            peers_quality_note = (
                "Za mało porównywalnych peers (<2). "
                "Użyto fallbacków mnożnikowych."
            )
            console.print(
                "[yellow]  ⚠ Za mało peers do wiarygodnej mediany (<2) — "
                "używam fallbacków sektorowych/domyslnych.[/yellow]"
            )
            peer_multiples = {"pe": None, "ev_ebitda": None, "pbv": None, "ev_sales": None}
    else:
        peers_quality_note = "Brak danych peers — użyto fallbacków mnożnikowych."
        peer_multiples = {"pe": None, "ev_ebitda": None, "pbv": None, "ev_sales": None}

    # Dla P/E: peers → info.forwardPE → fallback
    sector_pe = peer_multiples["pe"]
    pe_source = "mediana peers"
    if sector_pe is None:
        sector_pe = info.get("forwardPE")
        pe_source = "forwardPE spółki"
    if sector_pe is None or sector_pe <= 0:
        sector_pe = DEFAULT_PE
        pe_source = f"domyślny ({DEFAULT_PE}x)"

    sector_ev_ebitda = peer_multiples["ev_ebitda"] or DEFAULT_EV_EBITDA
    ev_ebitda_source = "mediana peers" if peer_multiples["ev_ebitda"] else f"domyślny ({DEFAULT_EV_EBITDA}x)"

    sector_pbv = peer_multiples["pbv"] or DEFAULT_PBV
    pbv_source = "mediana peers" if peer_multiples["pbv"] else f"domyślny ({DEFAULT_PBV}x)"

    # EV/Sales — dla gamingu fallback 5x (branżowa mediana 4–10x),
    # dla pozostałych sektorów fallback 2x lub mediana peers
    if peer_multiples.get("ev_sales") is not None:
        sector_ev_sales = peer_multiples["ev_sales"]
        ev_sales_source = "mediana peers"
    elif industry in _INDUSTRIES_EV_SALES_PRIMARY:
        sector_ev_sales = DEFAULT_EV_SALES_GAMING
        ev_sales_source = f"domyślny gaming ({DEFAULT_EV_SALES_GAMING}x, zakres 4–10x)"
    else:
        sector_ev_sales = DEFAULT_EV_SALES
        ev_sales_source = f"domyślny ({DEFAULT_EV_SALES}x)"

    console.print(
        f"[dim]  Mnożniki: P/E={sector_pe:.1f}x ({pe_source}), "
        f"EV/EBITDA={sector_ev_ebitda:.1f}x ({ev_ebitda_source}), "
        f"P/BV={sector_pbv:.1f}x ({pbv_source}), "
        f"EV/Sales={sector_ev_sales:.1f}x ({ev_sales_source})[/dim]"
    )

    # Wykryj spółkę gamingową — P/BV nieodpowiedni dla firm IP-driven
    # z niską wartością księgową (majątek = IP, nie środki trwałe)
    gaming_industries = [
        "Electronic Gaming & Multimedia", "Electronic Games",
        "Games", "Video Games",
    ]
    is_gaming = any(g.lower() in industry.lower() for g in gaming_industries)

    # Oblicz wyceny czterema metodami
    result_pe = _valuation_pe(financial_data, sector_pe, shares)
    result_ev = _valuation_ev_ebitda(financial_data, sector_ev_ebitda, shares)
    result_pbv = _valuation_pbv(financial_data, sector_pbv, shares)
    result_ev_sales = _valuation_ev_sales(financial_data, sector_ev_sales, shares)

    # Ustal które wyniki wchodzą do mediany.
    # Dla gaming: tylko P/E i EV/EBITDA — najbardziej wiarygodne mnożniki.
    # P/BV wykluczone: wartość księgowa nie odzwierciedla wartości IP
    #   (prawa do gier, marka, silnik to aktywa pozabilansowe).
    # EV/Sales wykluczone z mediany: pełni rolę informacyjną/uzupełniającą —
    #   duże wahania między premierami sprawiają, że ~2x revenue daje bardzo
    #   konserwatywny wynik nieodpowiedni dla głównej wyceny.
    # DDM wykluczone: gaming wypłaca symboliczne dywidendy niepowiązane z FCF.
    pbv_reliable, pbv_reason = _evaluate_pbv_reliability(financial_data, result_pbv)
    if result_pbv is not None and not pbv_reliable:
        console.print(
            f"[yellow]  ⚠ P/BV wykluczone z mediany: {pbv_reason}[/yellow]"
        )
    ev_sales_relevant, ev_sales_reason = _evaluate_ev_sales_relevance(
        financial_data=financial_data,
        ev_sales_result=result_ev_sales,
        pe_result=result_pe,
        ev_ebitda_result=result_ev,
    )
    if result_ev_sales is not None and not ev_sales_relevant:
        console.print(
            f"[yellow]  ⚠ EV/Sales wykluczone z mediany: {ev_sales_reason}[/yellow]"
        )

    if is_gaming:
        results_do_mediany = [result_pe, result_ev]
        wykluczone_metody = ["P/BV z mediany", "EV/Sales z mediany", "DDM"]
        console.print(
            "[dim]  Gaming: mediana = P/E + EV/EBITDA. "
            "P/BV i EV/Sales wyświetlane informacyjnie.[/dim]"
        )
    else:
        results_do_mediany = [result_pe, result_ev]
        wykluczone_metody: list[str] = []
        if ev_sales_relevant:
            results_do_mediany.append(result_ev_sales)
        elif result_ev_sales is not None:
            wykluczone_metody.append("EV/Sales z mediany")
        if pbv_reliable:
            results_do_mediany.append(result_pbv)
        elif result_pbv is not None:
            wykluczone_metody.append("P/BV z mediany")

    valid_prices = [
        r["cena_na_akcje"] for r in results_do_mediany
        if r is not None and r.get("cena_na_akcje") is not None and r["cena_na_akcje"] > 0
    ]

    if valid_prices:
        median_price = round(median(valid_prices), 2)
    else:
        median_price = None
        console.print(
            "[bold red]  ✗ Żadna z metod mnożnikowych nie dała wyniku.[/bold red]"
        )

    # Podsumowanie w terminalu — wyświetl wszystkie cztery, oznacz wykluczone
    console.print(f"\n[bold green]  ✓ Wycena mnożnikowa zakończona[/bold green]")
    all_results = [result_pe, result_ev, result_pbv, result_ev_sales]
    for r in all_results:
        if r is not None:
            wykluczona = (
                (is_gaming and r["mnoznik"] in ("P/BV", "EV/Sales"))
                or (r["mnoznik"] == "P/BV" and not pbv_reliable)
                or (r["mnoznik"] == "EV/Sales" and not is_gaming and not ev_sales_relevant)
            )
            suffix = " [informacyjnie]" if wykluczona else ""
            console.print(
                f"[dim]    {r['mnoznik']:>10}: {r['cena_na_akcje']:>10,.2f} "
                f"{currency}{suffix}[/dim]"
            )

    if median_price is not None:
        label = "mediana*" if is_gaming else "mediana"
        console.print(
            f"[dim]    {label:>10}: {median_price:>10,.2f} {currency}[/dim]"
        )
        if is_gaming:
            console.print(
                "[dim]    * mediana P/E + EV/EBITDA — gaming (P/BV i EV/Sales informacyjnie)[/dim]"
            )
        if current_price is not None:
            upside = ((median_price - current_price) / current_price) * 100
            direction = "wzrost" if upside > 0 else "spadek"
            color = "green" if upside > 0 else "red"
            console.print(
                f"[{color}]    Potencjał:       {upside:+.1f}% ({direction})[/{color}]"
            )

    result: dict = {
        "ticker": ticker,
        "metoda": "Wycena mnożnikowa (porównawcza)",
        "cena_rynkowa": current_price,
        "waluta": currency,
        "liczba_peers": peers_sample_size,
        "jakosc_peers_uwaga": peers_quality_note,
        "jakosc_metod": {
            "pbv_reliable": pbv_reliable,
            "pbv_reason": pbv_reason,
            "ev_sales_relevant": ev_sales_relevant,
            "ev_sales_reason": ev_sales_reason,
        },
        "wycena_pe": result_pe,
        "wycena_ev_ebitda": result_ev,
        "wycena_pbv": result_pbv,
        "wycena_ev_sales": result_ev_sales,
        "mediana": median_price,
        "uzyte_mnozniki": {
            "pe": {"wartosc": round(sector_pe, 2), "zrodlo": pe_source},
            "ev_ebitda": {"wartosc": round(sector_ev_ebitda, 2), "zrodlo": ev_ebitda_source},
            "pbv": {"wartosc": round(sector_pbv, 2), "zrodlo": pbv_source},
            "ev_sales": {"wartosc": round(sector_ev_sales, 2), "zrodlo": ev_sales_source},
        },
    }

    if wykluczone_metody:
        result["wykluczone_metody"] = wykluczone_metody

    if is_gaming:
        result["uwaga_gaming"] = (
            "Spółka gamingowa — mediana oparta tylko na P/E i EV/EBITDA. "
            "P/BV (niska wartość IP) i EV/Sales (duże wahania między premierami) "
            "wyświetlane informacyjnie. DDM pominięte."
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Korekta dyskontowa GPW — stosowana gdy spółka pochodzi z GPW (.WA)
    # i większość peers to spółki zachodnie (USA / Europa Zachodnia).
    #
    # Uzasadnienie: zachodnie spółki handlują z premią wynikającą z wyższej
    # płynności rynku, niższego ryzyka kraju i szerszej bazy inwestorów.
    # Bezpośrednie porównanie mnożników zawyża wycenę polskich spółek.
    # ─────────────────────────────────────────────────────────────────────────
    is_gpw = ticker.upper().endswith(".WA")

    if is_gpw:
        # Wyznacz udział zachodnich peers.
        # Gdy brak peers_data — używamy domyślnych mnożników sektorowych
        # (DEFAULT_PE=15, DEFAULT_EV_EBITDA=10, DEFAULT_PBV=1.5), które są
        # kalibrowane na zachodnie rynki → traktujemy jak 100% zachodnich peers.
        if peers_data:
            peers_tickers = [p.get("ticker", "") for p in peers_data]
            western_peers = [pt for pt in peers_tickers if _is_western_peer(pt)]
            pct_western = len(western_peers) / max(len(peers_tickers), 1)
        else:
            # Brak peers → fallback do globalnych (zachodnich) wartości domyślnych
            pct_western = 1.0

        if pct_western > 0.5:
            sektor = info.get("sector", "")
            discount = GPW_DISCOUNT_BY_SECTOR.get(sektor, 0.15)

            # Zapamiętaj wyceny przed dyskontem — agent użyje ich w raporcie
            mediana_przed = result.get("mediana")

            # Zastosuj discount do każdej indywidualnej wyceny mnożnikowej
            for key in ("wycena_pe", "wycena_ev_ebitda", "wycena_pbv", "wycena_ev_sales"):
                blok = result.get(key)
                if isinstance(blok, dict) and blok.get("cena_na_akcje") is not None:
                    cena_przed = blok["cena_na_akcje"]
                    blok["cena_na_akcje"] = round(cena_przed * (1 - discount), 2)
                    blok["cena_przed_dyskontem_gpw"] = round(cena_przed, 2)

            # Przelicz medianę na podstawie już zdyskontowanych cen
            valid_after = [
                result[k]["cena_na_akcje"]
                for k in ("wycena_pe", "wycena_ev_ebitda", "wycena_pbv", "wycena_ev_sales")
                if isinstance(result.get(k), dict)
                and result[k].get("cena_na_akcje") is not None
                and result[k]["cena_na_akcje"] > 0
                # Dla gamingu liczymy medianę tylko z wiarygodnych metod
                and not (is_gaming and result[k].get("mnoznik") in ("P/BV", "EV/Sales"))
                and not (result[k].get("mnoznik") == "P/BV" and not pbv_reliable)
                and not (result[k].get("mnoznik") == "EV/Sales" and not is_gaming and not ev_sales_relevant)
            ]
            result["mediana"] = round(median(valid_after), 2) if valid_after else None

            # Metadane o korekcie — używane przez agenta w raporcie
            result["gpw_discount_applied"] = discount
            result["gpw_discount_pct"] = f"{discount * 100:.0f}%"
            result["gpw_mediana_przed_dyskontem"] = mediana_przed
            result["gpw_western_peers_pct"] = f"{pct_western * 100:.0f}%"

            console.print(
                f"  [bold cyan]🇵🇱 GPW discount {discount * 100:.0f}% zastosowany "
                f"(peers: {pct_western * 100:.0f}% zachodnich — "
                f"sektor: {sektor or 'nieznany'})[/bold cyan]"
            )
            if mediana_przed is not None and result["mediana"] is not None:
                console.print(
                    f"[dim]    Mediana: {mediana_przed:,.2f} → "
                    f"{result['mediana']:,.2f} {currency} "
                    f"(po korekcie {discount * 100:.0f}%)[/dim]"
                )

    return result
