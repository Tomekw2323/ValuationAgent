"""Wycena metodą zdyskontowanych przepływów pieniężnych (DCF) — prognoza
wolnych przepływów pieniężnych, obliczenie wartości rezydualnej,
dyskontowanie do wartości bieżącej. Zawiera też analizę wrażliwości.

Obsługuje dwa tryby (automatyczny wybór lub wymuszenie przez use_two_stage):
- Standardowy DCF (single-stage): stały wzrost FCF przez 5 lat → wartość terminalna.
- Dwuetapowy DCF (two-stage): automatycznie włączany gdy historyczny wzrost FCF > 10%.
  Faza 1 (lata 1-5): wzrost = min((historyczny + growth_rate) / 2, 20%).
  Faza 2 (lata 6-10): wzrost spada liniowo od fazy 1 do terminal_growth.
  Wartość terminalna liczona od FCF z roku 10.

Dodatkowa korekta: skup akcji własnych (buyback) — jeśli spółka regularnie
redukuje liczbę akcji (>0.5% r/r), wycena na akcję jest korygowana w górę.
"""

import statistics
from typing import Optional

from rich.console import Console

from config import DEFAULT_WACC, DEFAULT_TERMINAL_GROWTH, DEFAULT_PROJECTION_YEARS
from tools.data_fetcher import safe_get_value

console = Console(legacy_windows=False)

# Parametry modelu CAPM do automatycznego obliczania WACC
RISK_FREE_RATE = 0.045       # stopa wolna od ryzyka (obligacje 10Y)
EQUITY_RISK_PREMIUM = 0.045  # premia za ryzyko rynkowe (rm - rf = 0.09 - 0.045)

# Próg historycznego wzrostu FCF powyżej którego włączamy dwuetapowy DCF
HIGH_GROWTH_THRESHOLD = 0.10

# Parametry fazy 2 dwuetapowego DCF
STAGE2_YEARS = 5             # faza 2 trwa kolejne 5 lat (łącznie 10)

# Maksymalny wzrost FCF w fazie 1 — ograniczenie chroniące przed absurdalnymi wycenami
MAX_STAGE1_GROWTH = 0.20

# Minimalny roczny skup akcji (w %) żeby korekta buyback była istotna
MIN_BUYBACK_RATE = 0.005

# Horyzont projekcji skupu akcji (lata) — do obliczenia korekty ceny
BUYBACK_PROJECTION_YEARS = 5


def _extract_fcf_from_period(period: dict) -> Optional[float]:
    """Wyciąga FCF z jednego okresu danych cashflow.

    Kolejność szukania:
    1. "Free Cash Flow" — standardowy klucz yfinance
    2. "FreeCashFlow" — alternatywny format (niektóre wersje yfinance)
    3. Obliczenie: Operating Cash Flow - abs(Capital Expenditure)
       Capex w yfinance bywa ujemny — abs() normalizuje to.

    Zwraca wartość float lub None.
    """
    fcf = safe_get_value(period.get("Free Cash Flow"))
    if fcf is not None:
        return fcf

    fcf = safe_get_value(period.get("FreeCashFlow"))
    if fcf is not None:
        return fcf

    op_cf = safe_get_value(period.get("Operating Cash Flow"))
    capex = safe_get_value(period.get("Capital Expenditure"))
    if op_cf is not None and capex is not None:
        return op_cf - abs(capex)

    return None


def _extract_historical_fcf(financial_data: dict) -> list[float]:
    """Wyciąga historyczne wartości Free Cash Flow posortowane chronologicznie
    (od najstarszego do najnowszego okresu).
    """
    cashflow = financial_data.get("cashflow", {})
    if not cashflow:
        return []

    sorted_dates = sorted(cashflow.keys())

    fcf_values: list[float] = []
    for date_key in sorted_dates:
        fcf = _extract_fcf_from_period(cashflow[date_key])
        if fcf is not None:
            fcf_values.append(fcf)

    return fcf_values


def _detect_historical_growth(financial_data: dict) -> float:
    """Wykrywa historyczny średni roczny wzrost FCF z ostatnich 3-4 lat.

    Bezpośrednio operuje na surowych danych cashflow — odporny na
    nieoczekiwane struktury danych zwracane przez yfinance.

    Strategia:
    1. Pobierz FCF z ostatnich 4 kolumn (lat) cashflow.
    2. Filtruj tylko dodatnie wartości FCF.
    3. Jeśli < 2 lata danych → fallback do info (revenueGrowth/earningsGrowth).
    4. Oblicz CAGR: (fcf_rok0 / fcf_rok3)^(1/3) - 1.
    5. Gdy CAGR FCF jest ujemny ale spółka rośnie (revenue/earnings > 10%),
       użyj średniej z FCF CAGR i revenue/earnings growth — FCF może
       czasowo spadać przez cykl capex mimo rosnących przychodów.
    6. Ogranicz wynik do zakresu [-5%, +25%].

    Returns:
        Szacowana roczna stopa wzrostu FCF.
    """
    info = financial_data.get("info", {})
    cashflow = financial_data.get("cashflow", {})

    # --- Krok 1: Wyciągnij FCF z ostatnich 4 lat ---
    fcf_growth: Optional[float] = None

    if cashflow:
        sorted_dates = sorted(cashflow.keys())
        recent_dates = sorted_dates[-4:]

        fcf_values: list[float] = []
        for dk in recent_dates:
            fcf = _extract_fcf_from_period(cashflow[dk])
            if fcf is not None:
                fcf_values.append(fcf)

        positive_fcf = [v for v in fcf_values if v > 0]

        if len(positive_fcf) >= 2:
            fcf_oldest = positive_fcf[0]
            fcf_newest = positive_fcf[-1]
            n_years = len(positive_fcf) - 1

            fcf_growth = (fcf_newest / fcf_oldest) ** (1.0 / n_years) - 1.0

    # --- Krok 2: Pobierz wzrost z info jako dodatkowy sygnał ---
    revenue_growth = safe_get_value(info.get("revenueGrowth"))
    earnings_growth = safe_get_value(info.get("earningsGrowth"))

    # Najlepszy dostępny wskaźnik wzrostu z info (yfinance TTM)
    info_growth: Optional[float] = None
    info_source = ""
    if earnings_growth is not None and earnings_growth > 0:
        info_growth = earnings_growth
        info_source = "earningsGrowth"
    elif revenue_growth is not None and revenue_growth > 0:
        info_growth = revenue_growth
        info_source = "revenueGrowth"

    # --- Krok 3: Ustal finalną stopę wzrostu ---
    if fcf_growth is not None and fcf_growth > 0:
        # FCF rośnie — używamy bezpośrednio
        growth = fcf_growth
    elif fcf_growth is not None and fcf_growth <= 0 and info_growth is not None and info_growth > HIGH_GROWTH_THRESHOLD:
        # FCF spada, ale przychody/zyski rosną > 10% — spółka wzrostowa
        # z tymczasowym spadkiem FCF (np. cykl inwestycyjny, wyższy capex).
        # Średnia ważona: 30% FCF CAGR + 70% info growth — zyski/przychody
        # lepiej odzwierciedlają potencjał generowania FCF w przyszłości.
        growth = 0.3 * fcf_growth + 0.7 * info_growth
    elif fcf_growth is not None:
        growth = fcf_growth
    elif info_growth is not None:
        growth = info_growth
    else:
        return 0.05

    clamped = max(-0.05, min(0.25, growth))
    return clamped


def _estimate_wacc_from_beta(financial_data: dict) -> float:
    """Szacuje WACC na podstawie modelu CAPM: WACC ~ Rf + beta * (Rm - Rf)."""
    beta = financial_data.get("info", {}).get("beta")

    if beta is None or beta <= 0:
        console.print(
            f"[yellow]  ⚠ Brak współczynnika beta — używam domyślnego WACC "
            f"= {DEFAULT_WACC:.1%}[/yellow]"
        )
        return DEFAULT_WACC

    wacc = RISK_FREE_RATE + beta * EQUITY_RISK_PREMIUM
    wacc = max(0.05, min(0.25, wacc))

    console.print(
        f"[dim]  📐 WACC z CAPM: Rf({RISK_FREE_RATE:.1%}) + β({beta:.2f}) "
        f"× ERP({EQUITY_RISK_PREMIUM:.1%}) = {wacc:.2%}[/dim]"
    )
    return wacc



def _is_financial_company(financial_data: dict) -> bool:
    """Wykrywa czy spółka działa w sektorze finansowym.

    Dla banków, ubezpieczycieli i firm zarządzających aktywami model DCF
    jest metodologicznie nieodpowiedni — dług operacyjny (depozyty) miesza
    się z długiem finansowym, FCF jest trudny do zdefiniowania, a kapitał
    regulacyjny ogranicza swobodę dystrybucji gotówki.

    Sprawdza sektor i branżę z yfinance info (case-insensitive).

    Returns:
        True jeśli wykryto spółkę finansową, False w pozostałych przypadkach.
    """
    info = financial_data.get("info", {})
    sektor = (info.get("sector") or "").lower()
    branza = (info.get("industry") or "").lower()
    polaczone = sektor + " " + branza

    # Słowa kluczowe identyfikujące spółki finansowe
    slowa_kluczowe = [
        "bank",
        "insurance",
        "financial services",
        "asset management",
        "credit services",
        "mortgage",
    ]

    return any(slowo in polaczone for slowo in slowa_kluczowe)


def _normalize_fcf(fcf_values: list) -> tuple[float, str]:
    """Normalizuje bazę FCF dla spółek cyklicznych z nieregularnym FCF.

    Wybiera odpowiednią bazę FCF (ostatni rok / średnia / mediana)
    w zależności od współczynnika zmienności (CV) obliczonego na podstawie
    wyłącznie dodatnich wartości FCF — ujemne lata zaburzają CV.

    Args:
        fcf_values: Lista historycznych wartości FCF (od najstarszego do najnowszego).

    Returns:
        Krotka (baza_fcf, opis_normalizacji).
    """
    # Przypadek brzegowy: brak historii do normalizacji — zwróć ostatni rok
    if len(fcf_values) < 2:
        return (fcf_values[0], "ostatni rok, brak historii")

    # Filtruj tylko dodatnie wartości FCF do obliczenia CV
    # Ujemne lata (straty) zaburzają współczynnik zmienności
    positive_values = [v for v in fcf_values if v > 0]

    # Jeśli mniej niż 2 dodatnie lata — brak podstawy do obliczenia CV
    if len(positive_values) < 2:
        return (fcf_values[-1], "ostatni rok, brak historii")

    mean_fcf = statistics.mean(positive_values)

    # Zabezpieczenie przed dzieleniem przez zero
    if mean_fcf == 0:
        return (fcf_values[-1], "ostatni rok, brak historii")

    std_fcf = statistics.stdev(positive_values)
    cv = std_fcf / mean_fcf

    if cv < 0.3:
        # Niskie CV — stabilny FCF, używamy ostatniego roku jako reprezentatywnego
        return (fcf_values[-1], "ostatni rok, stabilny FCF")
    elif cv < 0.6:
        # Umiarkowane CV — średnia wieloletnia wygładza wahania
        mean_all = statistics.mean(fcf_values)
        return (mean_all, f"średnia {len(fcf_values)}-letnia, CV={cv:.2f}")
    else:
        # Wysokie CV — spółka cykliczna, mediana odporna na ekstrema
        median_all = statistics.median(fcf_values)
        return (median_all, f"mediana {len(fcf_values)}-letnia, CV={cv:.2f} - spółka cykliczna")


def _normalize_negative_fcf(fcf_history: list[float]) -> Optional[float]:
    """Gdy ostatni FCF jest ujemny, oblicza znormalizowany FCF
    jako średnią z lat w których FCF był dodatni.

    Zwraca None gdy WSZYSTKIE lata mają ujemny FCF.
    """
    positive_years = [v for v in fcf_history if v > 0]

    if not positive_years:
        console.print(
            "[bold red]  ✗ FCF ujemny we wszystkich dostępnych latach — "
            "wycena DCF niedostępna dla tej spółki.[/bold red]"
        )
        return None

    normalized = sum(positive_years) / len(positive_years)
    console.print(
        f"[yellow]  ⚠ Ostatni FCF ujemny — używam znormalizowanego FCF "
        f"(średnia z {len(positive_years)} dodatnich lat): "
        f"{normalized:,.0f}[/yellow]"
    )
    return normalized


def _project_fcf_single_stage(
    base_fcf: float,
    growth_rate: float,
    wacc: float,
    years: int,
) -> tuple[list[float], list[float]]:
    """Prognoza FCF jednoetapowa — stały wzrost przez cały okres."""
    projected: list[float] = []
    pv_list: list[float] = []
    current = base_fcf

    for t in range(1, years + 1):
        current = current * (1 + growth_rate)
        pv = current / ((1 + wacc) ** t)
        projected.append(round(current, 2))
        pv_list.append(round(pv, 2))

    return projected, pv_list


def _project_fcf_two_stage(
    base_fcf: float,
    stage1_growth: float,
    wacc: float,
    terminal_growth: float,
    stage1_years: int = DEFAULT_PROJECTION_YEARS,
    stage2_years: int = STAGE2_YEARS,
) -> tuple[list[float], list[float], list[float]]:
    """Prognoza FCF dwuetapowa — wyższy wzrost w fazie 1, malejący w fazie 2.

    Faza 1 (lata 1-5): wzrost = stage1_growth (historyczny, max 20%).
    Faza 2 (lata 6-10): wzrost spada liniowo od stage1_growth do terminal_growth.
    Po fazie 2: wartość terminalna liczona z terminal_growth (Gordon Growth).

    Przykład: stage1_growth=15%, terminal_growth=2.5%, stage2_years=5
    → Faza 2: [12.5%, 10.0%, 7.5%, 5.0%, 2.5%]

    Returns:
        (projected_fcf, pv_fcf_list, growth_schedule) — prognozy, PV, stopy wzrostu per rok.
    """
    projected: list[float] = []
    pv_list: list[float] = []
    growth_schedule: list[float] = []
    current = base_fcf

    total_years = stage1_years + stage2_years

    # Interpolacja liniowa w fazie 2: od stage1_growth do terminal_growth
    # w (stage2_years + 1) krokach — ostatni rok fazy 2 = terminal_growth
    fade_step = (
        (stage1_growth - terminal_growth) / (stage2_years + 1)
        if stage2_years > 0
        else 0
    )

    for t in range(1, total_years + 1):
        if t <= stage1_years:
            g = stage1_growth
        else:
            fade_year = t - stage1_years
            g = stage1_growth - fade_step * fade_year
            g = max(g, terminal_growth)

        current = current * (1 + g)
        pv = current / ((1 + wacc) ** t)

        projected.append(round(current, 2))
        pv_list.append(round(pv, 2))
        growth_schedule.append(round(g, 4))

    return projected, pv_list, growth_schedule


def calculate_buyback_adjusted_price(
    financial_data: dict,
    dcf_price: float,
) -> dict:
    """Oblicza korektę wyceny DCF o program skupu akcji własnych (buyback).

    Spółki prowadzące regularne buybacki (np. Apple) zmniejszają liczbę akcji
    w obiegu, co podnosi wartość na akcję nawet bez wzrostu łącznego EV.
    Klasyczny DCF tego nie uwzględnia — ta funkcja dodaje tę korektę.

    Args:
        financial_data: Słownik z danymi finansowymi (z get_financial_data).
        dcf_price: Cena na akcję z modelu DCF (przed korektą).

    Returns:
        Słownik z ceną przed i po korekcie, tempem skupu, % korekty.
    """
    balance_sheet = financial_data.get("balance_sheet", {})
    currency = financial_data.get("info", {}).get("currency", "")

    if not balance_sheet:
        return {"cena_bez_korekty": dcf_price, "buyback_istotny": False}

    # Wyciągnij liczbę akcji z kolejnych lat (chronologicznie)
    sorted_dates = sorted(balance_sheet.keys())
    shares_history: list[tuple[str, float]] = []

    for date_key in sorted_dates:
        period = balance_sheet[date_key]
        shares = safe_get_value(period.get("Ordinary Shares Number"))
        if shares is None:
            shares = safe_get_value(period.get("Share Issued"))
        if shares is not None and shares > 0:
            shares_history.append((date_key, shares))

    # Potrzebujemy co najmniej 2 punktów (najstarszy i najnowszy) do obliczenia trendu
    if len(shares_history) < 2:
        return {"cena_bez_korekty": dcf_price, "buyback_istotny": False}

    # Weź do 3 lat historii (lub ile jest dostępne)
    recent = shares_history[-4:]  # max 4 rekordy → 3 lata zmian
    shares_oldest = recent[0][1]
    shares_newest = recent[-1][1]
    n_years = len(recent) - 1

    if shares_oldest <= 0 or n_years <= 0:
        return {"cena_bez_korekty": dcf_price, "buyback_istotny": False}

    # Roczne tempo skupu akcji (dodatnie = spółka skupuje = redukuje liczbę akcji)
    buyback_rate = (shares_oldest - shares_newest) / shares_oldest / n_years

    if buyback_rate < MIN_BUYBACK_RATE:
        return {
            "cena_bez_korekty": dcf_price,
            "buyback_istotny": False,
            "tempo_skupu_roczne": round(buyback_rate * 100, 2),
        }

    # Prognoza liczby akcji za 5 lat przy utrzymaniu obecnego tempa
    shares_future = shares_newest * ((1 - buyback_rate) ** BUYBACK_PROJECTION_YEARS)
    adjustment_factor = shares_newest / shares_future
    adjusted_price = dcf_price * adjustment_factor
    correction_pct = (adjustment_factor - 1) * 100

    return {
        "cena_bez_korekty": round(dcf_price, 2),
        "cena_z_korekta_buyback": round(adjusted_price, 2),
        "buyback_istotny": True,
        "tempo_skupu_roczne": round(buyback_rate * 100, 2),
        "korekta_pct": round(correction_pct, 1),
        "akcje_obecne": round(shares_newest),
        "akcje_prognoza_5lat": round(shares_future),
        "waluta": currency,
        "uwaga": (
            f"Korekta o skup akcji własnych +{correction_pct:.1f}% "
            f"(tempo skupu: {buyback_rate:.2%} r/r, "
            f"horyzont: {BUYBACK_PROJECTION_YEARS} lat)"
        ),
    }


def assess_dcf_reliability(financial_data: dict) -> dict:
    """Ocenia wiarygodność wyceny DCF na podstawie danych finansowych.

    Sprawdza 4 przypadki mogące zniekształcić wycenę DCF:
    1. Ujemny kapitał własny (masowe skupy akcji / dywidendy)
    2. Wysoki dług netto względem FCF (>5x)
    3. Nieregularny FCF — wysoki współczynnik zmienności (>50%)
    4. Ujemny lub bliski zeru FCF (spółka wzrostowa / w fazie inwestycji)

    Returns:
        Słownik z polami: wiarygodnosc, ostrzezenia, rekomendacja.
    """
    info = financial_data.get("info", {})
    balance_sheet = financial_data.get("balance_sheet", {})
    ostrzezenia: list[str] = []

    # --- 1. Ujemny kapitał własny ---
    equity_value = None
    if balance_sheet:
        # Weź najnowszy okres (ostatnia kolumna chronologicznie)
        sorted_dates = sorted(balance_sheet.keys())
        latest_bs = balance_sheet[sorted_dates[-1]] if sorted_dates else {}
        equity_value = safe_get_value(latest_bs.get("Total Stockholder Equity"))
        if equity_value is None:
            equity_value = safe_get_value(latest_bs.get("Stockholders Equity"))

    if equity_value is not None and equity_value < 0:
        eq_mld = equity_value / 1e9
        ostrzezenia.append(
            f"Ujemny kapital wlasny ({eq_mld:.1f} mld) — DCF moze byc silnie "
            f"znieksztalcony. Wycena mnoznikowa jest bardziej wiarygodna."
        )

    # --- 2. Wysoki dług netto względem FCF ---
    fcf_history = _extract_historical_fcf(financial_data)
    total_debt = info.get("totalDebt", 0) or 0
    total_cash = info.get("totalCash", 0) or 0
    net_debt = total_debt - total_cash
    last_fcf = fcf_history[-1] if fcf_history else None

    if last_fcf is not None and last_fcf > 0 and net_debt > 0:
        debt_fcf_ratio = net_debt / last_fcf
        if debt_fcf_ratio > 5:
            ostrzezenia.append(
                f"Wysoki dlug netto ({debt_fcf_ratio:.1f}x FCF) — Equity Value "
                f"po odjeciu dlugu moze byc bardzo niska lub ujemna."
            )

    # --- 3. Nieregularny FCF (cykliczność) ---
    if len(fcf_history) >= 3:
        mean_fcf = statistics.mean(fcf_history)
        if mean_fcf != 0:
            std_fcf = statistics.stdev(fcf_history)
            cv = abs(std_fcf / mean_fcf)
            if cv > 0.5:
                ostrzezenia.append(
                    f"Wysokie odchylenie FCF ({cv * 100:.0f}%) — spolka cykliczna lub "
                    f"z nieregularnymi wynikami. Prognoza wzrostu moze byc "
                    f"zawodna. Sprawdz czy to efekt jednorazowy."
                )

    # --- 4. Ujemny lub bliski zeru FCF ---
    if last_fcf is not None:
        if last_fcf < 0:
            ostrzezenia.append(
                "Ujemny FCF w ostatnim roku — DCF niedostepny lub oparty "
                "na znormalizowanych danych historycznych."
            )
        else:
            market_cap = info.get("marketCap", 0) or 0
            if market_cap > 0 and last_fcf < 0.01 * market_cap:
                fcf_yield = (last_fcf / market_cap) * 100
                ostrzezenia.append(
                    f"FCF yield bardzo niski ({fcf_yield:.2f}%) — spolka wzrostowa lub "
                    f"w fazie inwestycji. DCF moze niedoszacowywac wartosci."
                )

    # --- Ocena wiarygodności ---
    n = len(ostrzezenia)
    if n == 0:
        wiarygodnosc = "wysoka"
        rekomendacja = "DCF wiarygodny"
    elif n == 1:
        wiarygodnosc = "srednia"
        rekomendacja = "Preferuj mnozniki"
    else:
        wiarygodnosc = "niska"
        rekomendacja = (
            "DCF i mnozniki niezalecane — brak danych"
            if not fcf_history
            else "Preferuj mnozniki"
        )

    return {
        "wiarygodnosc": wiarygodnosc,
        "ostrzezenia": ostrzezenia,
        "rekomendacja": rekomendacja,
    }


def _dcf_per_share(
    fcf_base: float,
    shares: float,
    wacc: float,
    growth_rate: float,
    terminal_growth: float,
    years: int,
    hist_growth: float,
) -> dict:
    """Alternatywna wycena DCF operująca bezpośrednio na FCF per akcję.

    Używana gdy standardowy DCF (EV → Equity Value) jest zawodny —
    tj. gdy spółka ma ujemny kapitał własny lub bardzo wysoki dług netto.
    Omija problem odejmowania długu od EV: dyskontuje FCF/akcję wprost,
    bez pośredniej konwersji EV → Equity Value.

    Stosuje ten sam wybór modelu (single-stage / two-stage) co run_dcf().

    Args:
        fcf_base: Znormalizowany bazowy FCF całej spółki (w tej samej walucie co shares).
        shares: Liczba akcji w obiegu.
        wacc: Stopa dyskontowa.
        growth_rate: Roczna stopa wzrostu FCF (faza bazowa).
        terminal_growth: Perpetualny wzrost terminalny.
        years: Liczba lat projekcji fazy 1.
        hist_growth: Historyczny wzrost FCF — decyduje o wyborze modelu.

    Returns:
        Słownik z kluczami: cena_na_akcje, metoda, fcf_per_share_bazowy.
    """
    # Baza FCF na jedną akcję
    fcf_per_share = fcf_base / shares

    # Wybór modelu spójny z run_dcf — ten sam próg HIGH_GROWTH_THRESHOLD
    is_high_growth = hist_growth > HIGH_GROWTH_THRESHOLD

    if is_high_growth:
        # Model dwuetapowy: faza 1 = wyższy wzrost, faza 2 = liniowe spowolnienie
        stage1_growth = min(
            (hist_growth + growth_rate) / 2,
            MAX_STAGE1_GROWTH,
        )
        projected, pv_list, _ = _project_fcf_two_stage(
            fcf_per_share, stage1_growth, wacc, terminal_growth,
            stage1_years=years, stage2_years=STAGE2_YEARS,
        )
        total_years = years + STAGE2_YEARS
        sub_model = "two-stage"
    else:
        # Model jednostopniowy: stały wzrost przez cały okres projekcji
        projected, pv_list = _project_fcf_single_stage(
            fcf_per_share, growth_rate, wacc, years,
        )
        total_years = years
        sub_model = "single-stage"

    pv_fcf_total = sum(pv_list)

    # Wartość terminalna per akcję zdyskontowana do dnia dzisiejszego
    last_fcf_ps = projected[-1]
    terminal_value_ps = last_fcf_ps * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal_ps = terminal_value_ps / ((1 + wacc) ** total_years)

    # Cena na akcję = suma PV przyszłych FCF/akcję + PV wartości terminalnej/akcję
    price_per_share = pv_fcf_total + pv_terminal_ps

    return {
        "cena_na_akcje": round(price_per_share, 2),
        "metoda": "per_akcje",
        "sub_model": sub_model,
        "fcf_per_share_bazowy": round(fcf_per_share, 4),
        "pv_fcf_per_share": round(pv_fcf_total, 4),
        "pv_terminal_per_share": round(pv_terminal_ps, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Znane spółki z szeroką przewagą konkurencyjną (wide moat) wg Morningstar.
# Baza używana w _detect_wide_moat() — warto rozszerzyć o kolejne spółki.
# ─────────────────────────────────────────────────────────────────────────────
_KNOWN_WIDE_MOAT: frozenset[str] = frozenset({
    # Silne marki konsumenckie
    "KO", "PEP", "MCD", "SBUX", "NKE",
    # Platformy technologiczne (efekty sieciowe)
    "MSFT", "AAPL", "GOOGL", "GOOG", "META",
    # Sieci płatnicze (niskie koszty przełączenia + efekty sieciowe)
    "V", "MA", "PYPL",
    # Konsumenckie dobra podstawowe — silne marki globalne
    "JNJ", "PG", "CL", "UL",
    # Przemysł z trwałą przewagą skali / regulacyjną
    "BRK-B", "BRK-A", "JPM",
    # GPW — liderzy z rozpoznawalną marką w Polsce/regionie
    "LPP.WA", "CDR.WA",
})

# Bonus do terminal growth stosowany gdy wide_moat=True
WIDE_MOAT_TG_BONUS = 0.005  # +0.5pp — trwałość przewagi uzasadnia wyższy wzrost


def _detect_wide_moat(financial_data: dict) -> tuple[bool, str]:
    """Heurystyczne wykrywanie szerokiej przewagi konkurencyjnej (wide moat).

    Łączy dwa podejścia:
    1. Lista znanych spółek wide moat (_KNOWN_WIDE_MOAT) — pewna klasyfikacja.
    2. Wskaźniki finansowe — wysoka marża brutto, ROE i ROA sugerują moat
       nawet dla spółek spoza listy.

    Morningstar definiuje wide moat jako: silna marka, efekty sieciowe,
    niskie koszty przełączenia (switching costs), skala lub patenty —
    przewaga utrzymująca się co najmniej 20 lat.

    Args:
        financial_data: Słownik z danymi finansowymi (z get_financial_data).

    Returns:
        Krotka (is_moat: bool, uzasadnienie: str).
    """
    info = financial_data.get("info", {})
    ticker = (info.get("symbol") or financial_data.get("ticker", "")).upper()

    # Sprawdź listę znanych spółek wide moat
    if ticker in _KNOWN_WIDE_MOAT:
        return True, f"Znana spółka wide moat ({ticker})"

    # Heurystyki finansowe — minimum 2 spełnione sygnały → wide moat
    gross_margin = info.get("grossMargins", 0) or 0
    roe = info.get("returnOnEquity", 0) or 0
    roa = info.get("returnOnAssets", 0) or 0  # proxy dla ROIC

    sygnaly: list[str] = []

    # Wysoka marża brutto > 50% — często efekt silnej marki lub IP
    if gross_margin > 0.50:
        sygnaly.append(f"wysoka marża brutto {gross_margin * 100:.0f}%")

    # Wysokie ROE > 20% — spółka generuje ponadprzeciętny zwrot z kapitału
    if roe > 0.20:
        sygnaly.append(f"wysokie ROE {roe * 100:.0f}%")

    # Wysokie ROA > 15% — efektywne wykorzystanie aktywów (silna pozycja rynkowa)
    if roa > 0.15:
        sygnaly.append(f"wysokie ROA {roa * 100:.0f}%")

    if len(sygnaly) >= 2:
        return True, f"Wskaźniki sugerują moat: {', '.join(sygnaly)}"

    return False, "Brak sygnałów wide moat"


def run_dcf(
    financial_data: dict,
    wacc: Optional[float] = None,
    growth_rate: Optional[float] = None,
    terminal_growth: Optional[float] = None,
    years: int = DEFAULT_PROJECTION_YEARS,
    show_warning: bool = True,
    show_reliability_warning: bool = True,
    use_two_stage: Optional[bool] = None,
    apply_buyback: bool = True,
    wide_moat: bool = False,
) -> dict:
    """Przeprowadza wycenę DCF (Discounted Cash Flow).

    Automatycznie wybiera model (gdy use_two_stage=None):
    - Standardowy (single-stage): gdy historyczny wzrost FCF <= 10%.
    - Dwuetapowy (two-stage): gdy historyczny wzrost FCF > 10%.
      Faza 1 (lata 1-5): wzrost = (historyczny + growth_rate) / 2, max 20%.
      Faza 2 (lata 6-10): wzrost spada liniowo do terminal_growth.

    W modelu two-stage parametr growth_rate wpływa na fazę 1 jako modyfikator:
    stage1_rate = (hist_growth + growth_rate) / 2. Dzięki temu scenariusze
    wrażliwości z różnym growth_rate dają zróżnicowane wyceny.

    Args:
        financial_data: Słownik z danymi finansowymi (z get_financial_data).
        wacc: Stopa dyskontowa. None → oblicz z CAPM na podstawie beta.
        growth_rate: Roczna stopa wzrostu FCF. None → estymuj z historii.
            W modelu two-stage: używany jako modyfikator fazy 1.
        terminal_growth: Stopa wzrostu w okresie terminalnym. None → 2.5%.
        years: Liczba lat prognozy fazy 1 (domyślnie 5).
        show_warning: Czy wyświetlać ostrzeżenie o dużej rozbieżności z rynkiem.
            False w sensitivity_analysis, żeby nie spamować komunikatami.
        show_reliability_warning: Czy wyświetlać ostrzeżenie o niskiej wiarygodności
            DCF (spółka finansowa, ujemny equity itp.). False w sensitivity_analysis
            — ostrzeżenie pojawia się tylko raz przy głównym wywołaniu run_dcf().
        use_two_stage: Wymuszenie modelu DCF:
            None  → automatyczny wybór na podstawie historycznego wzrostu FCF.
            True  → zawsze model dwustopniowy.
            False → zawsze model jednostopniowy.
        apply_buyback: Czy stosować korektę o skup akcji własnych.
            False w sensitivity_analysis — macierz wrażliwości pokazuje
            ceny BEZ buybacku dla spójności porównania scenariuszy.
        wide_moat: Czy zastosować korektę wide moat (+0.5pp do terminal growth).
            Uzasadnienie: spółki z trwałą przewagą konkurencyjną (silna marka,
            efekty sieciowe, switching costs) mogą utrzymać wyższy wzrost
            w nieskończoności. Morningstar stosuje analogiczne podejście.

    Returns:
        Słownik z wynikami wyceny DCF lub komunikatem o błędzie.
    """
    ticker = financial_data.get("ticker", "?")
    info = financial_data.get("info", {})

    # Ocena wiarygodności DCF — zanim zaczniemy liczyć
    ocena = assess_dcf_reliability(financial_data)

    # Wykryj spółkę finansową (bank, ubezpieczyciel, asset management).
    # DCF jest metodologicznie nieodpowiedni dla tych podmiotów — dług operacyjny
    # (depozyty klientów) nie daje się oddzielić od długu finansowego, a FCF
    # jest trudny do zdefiniowania. Nadpisujemy ocenę wiarygodności na "niska".
    jest_spolka_finansowa = _is_financial_company(financial_data)
    if jest_spolka_finansowa:
        sektor = info.get("sector", "?")
        branza = info.get("industry", "?")
        # Ostrzeżenie tylko przy głównym wywołaniu — nie powtarzamy 9x w sensitivity
        if show_reliability_warning:
            console.print(
                f"\n[bold red]  ⚠ UWAGA: Spółka finansowa ({sektor} / {branza}) — "
                f"DCF może być niewiarygodny. Zalecane: DDM i mnożniki P/BV, P/E.[/bold red]"
            )
        # Nadpisz ocenę — dla banków/ubezpieczycieli DCF jest zawsze niska wiarygodność
        ocena["wiarygodnosc"] = "niska"
        ocena["rekomendacja"] = (
            "Użyj DDM i mnożników P/BV, P/E — DCF nieodpowiedni dla banków"
        )
        ocena["ostrzezenia"].insert(
            0,
            f"Spółka finansowa ({sektor} / {branza}) — DCF metodologicznie nieodpowiedni.",
        )
    elif ocena["wiarygodnosc"] == "niska" and show_reliability_warning:
        # Ostrzeżenie o niskiej wiarygodności tylko przy głównym wywołaniu
        console.print(
            f"\n[bold red]  UWAGA: Wiarygodnosc DCF: NISKA — "
            f"{', '.join(ocena['ostrzezenia'])}[/bold red]"
        )

    console.print(
        f"\n[bold cyan]📊 Wycena DCF dla [yellow]{ticker}[/yellow]...[/bold cyan]"
    )

    # --- Krok 1: Wyciągnij historyczne FCF ---
    fcf_history = _extract_historical_fcf(financial_data)

    if not fcf_history:
        console.print(
            "[bold red]  ✗ Brak danych o Free Cash Flow — "
            "nie można przeprowadzić wyceny DCF.[/bold red]"
        )
        return {
            "error": "Brak danych o Free Cash Flow — wycena DCF niemożliwa.",
            "ticker": ticker,
            "ocena_wiarygodnosci": ocena,
        }

    # Normalizacja bazy FCF — dobór ostatniego roku / średniej / mediany
    # w zależności od cykliczności spółki (współczynnik zmienności CV)
    base_fcf, normalizacja_opis = _normalize_fcf(fcf_history)

    # Obsługa ujemnego FCF po normalizacji (np. gdy wszystkie lata są ujemne)
    if base_fcf <= 0:
        normalized = _normalize_negative_fcf(fcf_history)
        if normalized is None:
            return {
                "error": (
                    "DCF niedostępny — spółka nie generuje dodatnich "
                    "przepływów pieniężnych w żadnym z dostępnych lat."
                ),
                "ticker": ticker,
                "fcf_historyczne": [round(v, 2) for v in fcf_history],
                "ocena_wiarygodnosci": ocena,
            }
        base_fcf = normalized
        normalizacja_opis = "znormalizowany FCF (średnia z dodatnich lat)"

    # Wyświetl informację o wybranej bazie FCF
    console.print(
        f"[dim]  Baza FCF: {base_fcf / 1e9:.2f} mld ({normalizacja_opis})[/dim]"
    )

    # --- Krok 2: Ustal WACC ---
    if wacc is None:
        wacc = _estimate_wacc_from_beta(financial_data)

    # --- Krok 3: Wykryj historyczny wzrost FCF (nowa logika) ---
    detected_growth = _detect_historical_growth(financial_data)
    is_high_growth = detected_growth > HIGH_GROWTH_THRESHOLD

    # Stopa wzrostu do projekcji: podana przez użytkownika lub wykryta z historii
    if growth_rate is None:
        growth_rate = detected_growth

    # --- Krok 4: Ustal wzrost terminalny ---
    if terminal_growth is None:
        terminal_growth = DEFAULT_TERMINAL_GROWTH

    if terminal_growth >= wacc:
        terminal_growth = wacc * 0.4
        console.print(
            f"[yellow]  ⚠ Wzrost terminalny >= WACC — skorygowano do "
            f"{terminal_growth:.2%}.[/yellow]"
        )

    # --- Krok 4b: Korekta wide moat — +0.5pp do terminal growth ---
    # Spółki z szeroką przewagą konkurencyjną (wide moat) mogą utrzymywać
    # wyższy wzrost w nieskończoności dzięki barierom wejścia.
    # Korekta stosowana tylko gdy jawnie przekazano wide_moat=True.
    tg_bez_moat = terminal_growth  # zapamiętaj oryginalną wartość do wyniku
    if wide_moat:
        adjusted_tg = terminal_growth + WIDE_MOAT_TG_BONUS
        # Zabezpieczenie: wide moat nie może sprawić że TG >= WACC
        if adjusted_tg >= wacc:
            console.print(
                f"[yellow]  ⚠ Wide moat: skorygowany TG ({adjusted_tg:.2%}) "
                f">= WACC ({wacc:.2%}) — pomijam korektę.[/yellow]"
            )
        else:
            console.print(
                f"  [bold magenta]🏰 Wide moat adjustment: terminal growth "
                f"{terminal_growth * 100:.1f}% → "
                f"{adjusted_tg * 100:.1f}% (+{WIDE_MOAT_TG_BONUS * 100:.1f}pp)"
                f"[/bold magenta]"
            )
            terminal_growth = adjusted_tg

    # --- Krok 5-6: Prognoza i dyskontowanie FCF ---
    growth_schedule: Optional[list[float]] = None

    # Wybór modelu: automatyczny (historyczny wzrost > 10%) lub wymuszony parametrem
    if use_two_stage is None:
        apply_two_stage = is_high_growth
    elif use_two_stage is True:
        apply_two_stage = True
    else:
        apply_two_stage = False

    if apply_two_stage:
        # Faza 1: średnia z historycznego wzrostu i przekazanego growth_rate —
        # dzięki temu scenariusze wrażliwości (różne growth_rate) dają różne wyceny,
        # a jednocześnie historyczny trend ma wpływ na projekcję.
        # Cap na MAX_STAGE1_GROWTH chroni przed absurdalnymi wycenami.
        stage1_growth = min(
            (detected_growth + growth_rate) / 2,
            MAX_STAGE1_GROWTH,
        )
        stage2_end = terminal_growth

        projected_fcf, pv_fcf_list, growth_schedule = _project_fcf_two_stage(
            base_fcf, stage1_growth, wacc, terminal_growth,
            stage1_years=years, stage2_years=STAGE2_YEARS,
        )
        total_projection_years = years + STAGE2_YEARS
        model_type = "two-stage DCF"

        console.print(
            f"[dim]  📐 model=two-stage DCF | "
            f"Faza1: {stage1_growth:.1%} (lata 1-{years}) -> "
            f"Faza2: {stage1_growth:.1%}->{stage2_end:.1%} "
            f"(lata {years+1}-{total_projection_years})[/dim]"
        )
    else:
        projected_fcf, pv_fcf_list = _project_fcf_single_stage(
            base_fcf, growth_rate, wacc, years,
        )
        total_projection_years = years
        model_type = "single-stage DCF"

        console.print(f"[dim]  📐 model=single-stage DCF[/dim]")

    console.print(
        f"[dim]  Zalozenia: WACC={wacc:.2%}, wzrost FCF={growth_rate:.2%}, "
        f"wzrost terminalny={terminal_growth:.2%}[/dim]"
    )

    pv_fcf_total = sum(pv_fcf_list)

    # --- Krok 7-8: Wartość terminalna (na końcu ostatniego roku prognozy) ---
    last_projected_fcf = projected_fcf[-1]
    terminal_value = last_projected_fcf * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1 + wacc) ** total_projection_years)

    # --- Krok 9: Enterprise Value ---
    enterprise_value = pv_fcf_total + pv_terminal

    # --- Krok 10: Equity Value = EV - dług netto + gotówka ---
    total_debt = info.get("totalDebt", 0) or 0
    total_cash = info.get("totalCash", 0) or 0
    equity_value = enterprise_value - total_debt + total_cash

    # --- Krok 11: Cena na akcję ---
    shares = info.get("sharesOutstanding", 0) or 0
    if shares > 0:
        price_per_share = equity_value / shares
    else:
        price_per_share = None
        console.print(
            "[yellow]  ⚠ Brak danych o liczbie akcji — "
            "nie można obliczyć ceny na akcję.[/yellow]"
        )

    current_price = info.get("currentPrice")
    currency = info.get("currency", "")

    # --- Krok 11b: Wybór metody DCF — standard vs. per_akcje ---
    # Standardowy DCF (EV → Equity Value) zawodzi gdy:
    #   a) spółka ma ujemny kapitał własny (np. masowe buybacki jak Apple/KO)
    #   b) dług netto stanowi > 40% EV — odejmowanie długu daje mylący wynik
    # W takim przypadku wyceniamy bezpośrednio FCF na akcję, omijając dług.

    # Pobierz kapitał własny z bilansu (najnowszy okres).
    # yfinance zmienia nazwy kluczy między wersjami — sprawdzamy wszystkie warianty.
    kapital_wlasny: Optional[float] = None
    balance_sheet = financial_data.get("balance_sheet", {})
    _EQUITY_KEYS = [
        "Stockholders Equity",          # yfinance >= 0.2.x (najczęstszy)
        "Total Stockholder Equity",     # starsze wersje yfinance
        "Common Stock Equity",          # alternatywny klucz w niektórych raportach
        "Total Equity Gross Minority Interest",  # yfinance z minority interest
    ]
    if balance_sheet:
        sorted_bs = sorted(balance_sheet.keys())
        latest_bs = balance_sheet[sorted_bs[-1]] if sorted_bs else {}
        for _key in _EQUITY_KEYS:
            kapital_wlasny = safe_get_value(latest_bs.get(_key))
            if kapital_wlasny is not None:
                break

    # Klucz debug — widoczny w wyniku testu, usuwalny w produkcji
    _kapital_wlasny_debug = (
        f"{kapital_wlasny / 1e9:.2f} mld" if kapital_wlasny is not None else "brak danych"
    )

    # Stosunek długu netto do EV (wskaźnik ryzyka zawodności metody EV→Equity)
    dlug_netto = total_debt - total_cash
    dlugnetto_do_ev: Optional[float] = None
    if enterprise_value > 0:
        dlugnetto_do_ev = dlug_netto / enterprise_value

    # Warunek przełączenia na metodę per_akcje
    uzyj_per_akcje = (
        (kapital_wlasny is not None and kapital_wlasny <= 0)
        or (dlugnetto_do_ev is not None and dlugnetto_do_ev > 0.40)
    )

    metoda_dcf: str
    powod_metody: Optional[str] = None

    if uzyj_per_akcje and shares > 0:
        # Uruchom alternatywną wycenę per_akcje
        per_share_result = _dcf_per_share(
            fcf_base=base_fcf,
            shares=shares,
            wacc=wacc,
            growth_rate=growth_rate,
            terminal_growth=terminal_growth,
            years=years,
            hist_growth=detected_growth,
        )
        price_per_share = per_share_result["cena_na_akcje"]
        metoda_dcf = "per_akcje"

        # Zbuduj opis powodu dla raportu
        powody: list[str] = []
        if kapital_wlasny is not None and kapital_wlasny <= 0:
            powody.append(f"ujemny kapitał własny ({kapital_wlasny / 1e9:.1f} mld)")
        if dlugnetto_do_ev is not None and dlugnetto_do_ev > 0.40:
            powody.append(f"dług netto = {dlugnetto_do_ev:.0%} EV")
        powod_metody = "; ".join(powody)

        console.print(
            f"[bold yellow]  ⚡ Metoda DCF per_akcje (zamiast EV→Equity): "
            f"{powod_metody}[/bold yellow]"
        )
        console.print(
            f"[dim]  FCF/akcję bazowy: {per_share_result['fcf_per_share_bazowy']:,.4f} "
            f"{currency}[/dim]"
        )
    else:
        metoda_dcf = "standard_ev_equity"

    # --- Podsumowanie i interpretacja rozbieżności z rynkiem ---
    console.print(f"\n[bold green]  ✓ Wycena DCF zakończona[/bold green]")

    # Komunikat interpretacyjny — kluczowa logika dla dużych rozbieżności
    valuation_caveat: Optional[str] = None

    if price_per_share is not None and current_price is not None and current_price > 0:
        upside = ((price_per_share - current_price) / current_price) * 100
        direction = "wzrost" if upside > 0 else "spadek"
        color = "green" if upside > 0 else "red"
        console.print(
            f"[dim]    Wartość godziwa: {price_per_share:,.2f} {currency}[/dim]"
        )
        console.print(
            f"[dim]    Cena rynkowa:    {current_price:,.2f} {currency}[/dim]"
        )
        console.print(
            f"[{color}]    Potencjał:       {upside:+.1f}% ({direction})[/{color}]"
        )

        # Gdy DCF daje wycenę >40% poniżej ceny rynkowej, rynek prawdopodobnie
        # wycenia wzrost lub wartości niematerialne (marka, IP, efekty sieciowe)
        # których DCF oparty na historycznych FCF nie uwzględnia.
        # Ostrzeżenie wyświetlane tylko przy show_warning=True (główne wywołanie),
        # a nie przy każdym z 9+ scenariuszy w sensitivity_analysis.
        if upside < -40:
            valuation_caveat = (
                "Wycena DCF jest znacząco niższa od ceny rynkowej "
                f"({upside:+.1f}%). Może to oznaczać, że rynek wycenia "
                "przyszły wzrost, wartości niematerialne (marka, własność "
                "intelektualna, efekty sieciowe) lub potencjał transformacji "
                "biznesowej, których model DCF oparty na historycznych "
                "przepływach pieniężnych nie uwzględnia. Nie należy "
                "automatycznie traktować spółki jako przewartościowanej."
            )
            if show_warning:
                console.print(
                    f"\n[bold yellow]  ⚠ {valuation_caveat}[/bold yellow]"
                )

    # --- Krok 12: Korekta o skup akcji własnych (buyback) ---
    # Wyłączana w sensitivity_analysis — macierz wrażliwości bez buybacku
    buyback_data: Optional[dict] = None
    if apply_buyback and price_per_share is not None:
        buyback_data = calculate_buyback_adjusted_price(financial_data, price_per_share)

        if buyback_data.get("buyback_istotny"):
            adj_price = buyback_data["cena_z_korekta_buyback"]
            corr_pct = buyback_data["korekta_pct"]
            console.print(
                f"[bold cyan]  🔄 Korekta o buyback: +{corr_pct:.1f}% "
                f"-> cena skorygowana: {adj_price:,.2f} {currency}[/bold cyan]"
            )

    result = {
        "ticker": ticker,
        "metoda": f"DCF ({model_type})",
        "metoda_dcf": metoda_dcf,
        "kapital_wlasny_debug": _kapital_wlasny_debug,
        "cena_na_akcje": round(price_per_share, 2) if price_per_share else None,
        "cena_rynkowa": current_price,
        "waluta": currency,
        "enterprise_value": round(enterprise_value, 2),
        "equity_value": round(equity_value, 2),
        "pv_fcf": round(pv_fcf_total, 2),
        "pv_terminal": round(pv_terminal, 2),
        "udzial_terminal_w_ev": round(pv_terminal / enterprise_value * 100, 1) if enterprise_value else None,
        "zalozenia": {
            "wacc": round(wacc, 4),
            "growth_rate": round(growth_rate, 4),
            "terminal_growth": round(terminal_growth, 4),
            "years": total_projection_years,
            "stage1_years": years,
            "base_fcf": round(base_fcf, 2),
            "normalizacja_fcf": normalizacja_opis,
            "model": model_type,
            "is_high_growth": is_high_growth,
        },
        "prognozy_fcf": projected_fcf,
        "pv_fcf_per_year": pv_fcf_list,
        "fcf_historyczne": [round(v, 2) for v in fcf_history],
    }

    if powod_metody is not None:
        result["powod_metody_dcf"] = powod_metody

    if growth_schedule is not None:
        result["growth_schedule"] = growth_schedule

    if valuation_caveat is not None:
        result["zastrzezenie_wyceny"] = valuation_caveat

    if buyback_data is not None and buyback_data.get("buyback_istotny"):
        result["buyback"] = buyback_data
        result["cena_z_korekta_buyback"] = buyback_data["cena_z_korekta_buyback"]

    result["ocena_wiarygodnosci"] = ocena

    # Oznacz w wyniku że wykryto spółkę finansową — agent użyje tego do raportu
    if jest_spolka_finansowa:
        result["ostrzezenie_bank"] = True

    # --- Wide moat: metadane o zastosowanej korekcie ---
    if wide_moat and terminal_growth > tg_bez_moat:
        # Korekta była zastosowana (nie zablokowana przez warunek TG >= WACC)
        result["wide_moat_applied"] = True
        result["tg_bez_moat"] = round(tg_bez_moat, 4)
        result["tg_z_moatem"] = round(terminal_growth, 4)

    # --- Automatyczne wykrywanie wide moat (informacyjnie, bez zmiany wyniku) ---
    # Gdy użytkownik nie przekazał wide_moat=True, sprawdź czy spółka ma cechy
    # wide moat i zasugeruj korektę. Nie zmienia wyceny — to wskazówka dla agenta.
    if not wide_moat:
        detected_moat, moat_reason = _detect_wide_moat(financial_data)
        if detected_moat:
            console.print(
                f"  [bold blue]💡 Wykryto potencjalny wide moat: {moat_reason}[/bold blue]"
            )
            console.print(
                "     Użyj parametru wide_moat=True lub zaznacz "
                "w aplikacji aby zastosować korektę terminal growth."
            )
            result["wide_moat_detected"] = True
            result["wide_moat_reason"] = moat_reason

    return result


def sensitivity_analysis(
    financial_data: dict,
    base_wacc: float,
    base_growth: float,
) -> dict:
    """Analiza wrażliwości DCF — macierz 3x3 wycen dla wariantów WACC i wzrostu.

    Testuje 3 poziomy WACC (base +/- 1pp) x 3 poziomy wzrostu (base +/- 2pp)
    i przypisuje nazwy scenariuszy: pesymistyczny, bazowy, optymistyczny.

    Model (single-stage / two-stage) dobierany automatycznie na podstawie
    historycznego wzrostu FCF — spójnie z główną wyceną run_dcf().
    """
    ticker = financial_data.get("ticker", "?")

    console.print(
        f"\n[bold cyan]🔬 Analiza wrażliwości DCF dla "
        f"[yellow]{ticker}[/yellow]...[/bold cyan]"
    )

    # Wykryj model DCF spójnie z główną wyceną
    hist_growth = _detect_historical_growth(financial_data)
    if hist_growth > HIGH_GROWTH_THRESHOLD:
        use_two_stage = True
        console.print(
            "[dim]  Analiza wrażliwości: model two-stage DCF "
            "(spójnie z główną wyceną)[/dim]"
        )
    else:
        use_two_stage = False
        console.print("[dim]  Analiza wrażliwości: model single-stage DCF[/dim]")

    # 3 warianty WACC (base ± 1pp) i 3 warianty wzrostu (base ± 2pp)
    waccs = [
        round(base_wacc - 0.01, 4),
        round(base_wacc, 4),
        round(base_wacc + 0.01, 4),
    ]
    growths = [
        round(base_growth - 0.02, 4),
        round(base_growth, 4),
        round(base_growth + 0.02, 4),
    ]

    wacc_labels = ["niski (optymist.)", "bazowy", "wysoki (pesymist.)"]
    growth_labels = ["niski (pesymist.)", "bazowy", "wysoki (optymist.)"]

    # Deterministyczna pętla — dokładnie 9 unikalnych kombinacji, bez pomijania
    results: dict[tuple[float, float], Optional[float]] = {}

    for w in waccs:
        for g in growths:
            res = run_dcf(
                financial_data,
                wacc=w,
                growth_rate=g,
                show_warning=False,
                show_reliability_warning=False,  # ostrzeżenie już było przy głównym DCF
                use_two_stage=use_two_stage,
                apply_buyback=False,
            )
            results[(round(w, 4), round(g, 4))] = res.get("cena_na_akcje")

    # Zbuduj macierz z etykietami z wyników już obliczonych powyżej
    matrix: dict[str, dict[str, Optional[float]]] = {}
    for w, wl in zip(waccs, wacc_labels):
        wacc_key = f"WACC {w:.2%} ({wl})"
        matrix[wacc_key] = {}
        for g, gl in zip(growths, growth_labels):
            growth_key = f"Wzrost {g:.2%} ({gl})"
            matrix[wacc_key][growth_key] = results[(round(w, 4), round(g, 4))]

    # Scenariusze nazwane — odczytane z tej samej macierzy (bez dodatkowych wywołań)
    scenarios: dict[str, Optional[float]] = {
        "pesymistyczny": results[(round(waccs[2], 4), round(growths[0], 4))],
        "bazowy":        results[(round(waccs[1], 4), round(growths[1], 4))],
        "optymistyczny": results[(round(waccs[0], 4), round(growths[2], 4))],
    }

    # Korekta buyback obliczana osobno — informacja dla raportu,
    # bez wpływu na macierz wrażliwości (ceny w macierzy są bez buybacku).
    # Wywołanie run_dcf z apply_buyback=False żeby nie drukować "Korekta o buyback",
    # a potem obliczamy buyback_pct bezpośrednio z calculate_buyback_adjusted_price.
    buyback_pct: Optional[float] = None
    base_result = run_dcf(
        financial_data, wacc=base_wacc, growth_rate=base_growth,
        show_warning=False, show_reliability_warning=False,
        use_two_stage=use_two_stage, apply_buyback=False,
    )
    base_price = base_result.get("cena_na_akcje")
    if base_price is not None:
        buyback_info = calculate_buyback_adjusted_price(financial_data, base_price)
        if buyback_info.get("buyback_istotny"):
            buyback_pct = buyback_info.get("korekta_pct")

    console.print("[bold green]  ✓ Analiza wrażliwości zakończona[/bold green]")

    currency = financial_data.get("info", {}).get("currency", "")
    for name, price in scenarios.items():
        if price is not None:
            console.print(f"[dim]    {name:>16}: {price:>10,.2f} {currency}[/dim]")

    result_dict: dict = {
        "ticker": ticker,
        "metoda": "Analiza wrażliwości DCF",
        "model": "two-stage DCF" if use_two_stage else "single-stage DCF",
        "opis": (
            "Macierz 3x3 cen na akcje przy roznych WACC i stopach wzrostu FCF "
            f"(model {'two-stage' if use_two_stage else 'single-stage'}, bez korekty buyback)"
        ),
        "scenariusze": scenarios,
        "macierz": matrix,
        "parametry": {
            "base_wacc": base_wacc,
            "base_growth": base_growth,
            "wacc_range": waccs,
            "growth_range": growths,
        },
    }

    if buyback_pct is not None:
        result_dict["buyback_adjustment_pct"] = buyback_pct

    return result_dict
