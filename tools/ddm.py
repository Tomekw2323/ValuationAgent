"""Model dywidendowy (DDM — Dividend Discount Model) — wycena spółek
wypłacających dywidendy na podstawie modelu Gordona (Gordon Growth Model).

Wymaga co najmniej 4 lat historii dywidend do wiarygodnego oszacowania
tempa wzrostu. Zwraca None gdy spółka nie płaci dywidend lub dane
są niewystarczające.
"""

from typing import Optional

import pandas as pd
from rich.console import Console

console = Console(legacy_windows=False)

# Stopa wolna od ryzyka i premia za ryzyko do modelu CAPM
# Używane do obliczenia wymaganej stopy zwrotu gdy nie podano jej wprost
_RISK_FREE_RATE = 0.045
_EQUITY_RISK_PREMIUM = 0.05

# Limity bezpieczeństwa dla wzrostu dywidendy i wymaganej stopy zwrotu
_MAX_DIVIDEND_GROWTH = 0.15
_MIN_REQUIRED_RETURN = 0.06
_MAX_REQUIRED_RETURN = 0.20

# Minimalna historia dywidend (w latach) wymagana do obliczeń
_MIN_DIVIDEND_YEARS = 4

# Minimalny spread między wymaganą stopą zwrotu a wzrostem dywidendy.
# Gdy (r - g) < MIN_SPREAD, model Gordona staje się ekstremalnie wrażliwy
# na małe zmiany założeń — wycena rośnie/spada o setki procent przy zmianie g o 0.5%.
_MIN_SPREAD = 0.02


def run_ddm(
    financial_data: dict,
    required_return: Optional[float] = None,
) -> Optional[dict]:
    """Wycena metodą DDM (Gordon Growth Model).

    Algorytm:
    1. Pobiera historię dywidend z financial_data["dividends"] (pandas Series).
    2. Grupuje wypłaty po roku kalendarzowym — wiele wypłat rocznie → suma roczna.
    3. Oblicza CAGR dywidendy z ostatnich min(5, dostępne) lat.
    4. Oblicza D1 = ostatnia roczna dywidenda * (1 + wzrost) i dzieli przez (r - g).
    5. Zwraca None gdy brak danych, zbyt mało historii lub model jest niestabilny.

    Args:
        financial_data: Słownik z danymi finansowymi (z get_financial_data).
        required_return: Wymagana stopa zwrotu. None → oblicz z CAPM (Rf + β*ERP).

    Returns:
        Słownik z wynikami lub None gdy DDM jest niedostępny / niestabilny.
    """
    ticker = financial_data.get("ticker", "?")
    info = financial_data.get("info", {})

    console.print(
        f"\n[bold cyan]💰 Wycena DDM dla [yellow]{ticker}[/yellow]...[/bold cyan]"
    )

    # --- Krok 1: Pobierz historię dywidend ---
    dividends_raw = financial_data.get("dividends")

    # Sprawdź czy dywidendy w ogóle istnieją i mają sensowną długość
    if not dividends_raw:
        console.print("[dim]  DDM pominięty — brak dywidend.[/dim]")
        return None

    # data_fetcher serializuje dywidendy jako dict {iso_date: float}.
    # Konwertujemy z powrotem na pd.Series z DatetimeIndex do dalszych obliczeń.
    if isinstance(dividends_raw, dict):
        try:
            # utc=True normalizuje mieszane strefy czasowe (yfinance zwraca
            # różne formaty dat w historii dywidend — np. tz-aware i tz-naive)
            dividends = pd.Series(
                list(dividends_raw.values()),
                index=pd.to_datetime(list(dividends_raw.keys()), utc=True),
            )
        except Exception as e:
            console.print(f"[yellow]  ⚠ DDM: nie można skonwertować dywidend: {e}[/yellow]")
            return None
    else:
        # Jeśli już pd.Series (np. wersja bez cache) — używamy bezpośrednio
        dividends = dividends_raw

    if len(dividends) == 0:
        console.print("[dim]  DDM pominięty — brak dywidend.[/dim]")
        return None

    # --- Krok 2: Grupuj dywidendy po roku kalendarzowym i oblicz sumy roczne ---
    # KO i podobne spółki płacą kwartalnie — 4 wpisy na rok → suma roczna
    try:
        # Wyciągnij rok ze znacznika czasu (obsługa timezone-aware i naive)
        years = dividends.index.map(
            lambda x: x.year if hasattr(x, "year") else int(str(x)[:4])
        )
        # Suma dywidend w każdym roku kalendarzowym
        annual_dividends = dividends.groupby(years).sum()
    except Exception as e:
        console.print(f"[yellow]  ⚠ DDM: błąd grupowania dywidend po roku: {e}[/yellow]")
        return None

    # Sortuj rosnąco — najstarszy rok pierwszy, najnowszy ostatni
    annual_dividends = annual_dividends.sort_index()

    # Odfiltruj bieżący niepełny rok kalendarzowy.
    # yfinance zwraca dane mid-year — bieżący rok ma tylko część wypłat
    # (np. w kwietniu 2025 KO ma tylko Q1). Taki rok zaniżałby CAGR i D_last.
    # Przyjmujemy, że rok jest "pełny" gdy ma ≥ 2 wypłaty dywidend.
    current_year = pd.Timestamp.now().year
    payments_per_year = dividends.groupby(dividends.index.year).count()
    complete_years = annual_dividends[
        annual_dividends.index.map(
            lambda y: (y < current_year) or (payments_per_year.get(y, 0) >= 4)
        )
    ]

    # Minimalna liczba pełnych lat wymaganych do wiarygodnego obliczenia CAGR
    if len(complete_years) < _MIN_DIVIDEND_YEARS:
        console.print(
            f"[dim]  DDM pominięty — za mało pełnych lat dywidendowych "
            f"({len(complete_years)} lat, wymagane {_MIN_DIVIDEND_YEARS}).[/dim]"
        )
        return None

    # --- Krok 3: Wybierz ostatnie max 5 pełnych lat i oblicz CAGR dywidendy ---
    n_years = min(5, len(complete_years))
    recent_divs = complete_years.iloc[-n_years:]

    div_pierwszy = float(recent_divs.iloc[0])
    div_ostatni = float(recent_divs.iloc[-1])

    if div_pierwszy <= 0 or div_ostatni <= 0:
        console.print(
            "[yellow]  ⚠ DDM: zerowa lub ujemna dywidenda w historii — pomijam.[/yellow]"
        )
        return None

    # CAGR dywidendy: (D_n / D_0)^(1/(n-1)) - 1
    n_periods = n_years - 1
    wzrost_dywidendy = (div_ostatni / div_pierwszy) ** (1.0 / n_periods) - 1.0

    # Ogranicz wzrost do zakresu 0%–15% — Gordon Growth Model traci stabilność
    # przy wzroście zbliżonym do stopy dyskontowej
    wzrost_dywidendy = max(0.0, min(_MAX_DIVIDEND_GROWTH, wzrost_dywidendy))

    # --- Krok 4: Ostatnia roczna dywidenda — suma z ostatnich 12 miesięcy ---
    # Używamy sumy ostatnich 12 miesięcy z surowych danych (nie ostatniego roku
    # kalendarzowego), bo bieżący rok może być niepełny.
    cutoff = dividends.index.max() - pd.DateOffset(months=12)
    ostatnia_dywidenda_roczna = float(dividends[dividends.index >= cutoff].sum())

    if ostatnia_dywidenda_roczna <= 0:
        # Fallback: ostatni pełny rok kalendarzowy
        ostatnia_dywidenda_roczna = float(complete_years.iloc[-1])

    # --- Krok 5: Wymagana stopa zwrotu (r) ---
    if required_return is None:
        # Model CAPM: r = Rf + β * ERP
        beta = info.get("beta") or 1.0
        beta = max(0.5, beta)  # beta < 0.5 daje nierealistycznie niskie r
        required_return = _RISK_FREE_RATE + beta * _EQUITY_RISK_PREMIUM
        # Ogranicz do rozsądnego zakresu
        required_return = max(_MIN_REQUIRED_RETURN, min(_MAX_REQUIRED_RETURN, required_return))
        console.print(
            f"[dim]  📐 CAPM: Rf({_RISK_FREE_RATE:.1%}) + β({beta:.2f}) × "
            f"ERP({_EQUITY_RISK_PREMIUM:.1%}) = r={required_return:.2%}[/dim]"
        )

    # --- Krok 6: Sprawdź stabilność modelu Gordona i zastosuj korekty spreadu ---
    # Model Gordona traci stabilność gdy g zbliża się do r.
    # Obsługujemy trzy przypadki:

    spread = required_return - wzrost_dywidendy
    ostrzezenie: Optional[str] = None

    if wzrost_dywidendy > required_return + 0.03:
        # Przypadek A: wzrost wyraźnie powyżej r (> 3pp) — model faktycznie niestabilny.
        # Spółka o takim profilu wymagałaby wieloetapowego DDM, nie Gordona.
        console.print(
            f"[yellow]  ⚠ DDM niestabilny — wzrost dywidendy ({wzrost_dywidendy:.2%}) "
            f"przekracza wymaganą stopę o ponad 3pp "
            f"({required_return:.2%}). Pomijam DDM.[/yellow]"
        )
        return None

    elif wzrost_dywidendy > required_return:
        # Przypadek B: wzrost nieznacznie powyżej r (0–3pp) — np. PepsiCo.
        # Podnosimy r do g + MIN_SPREAD zamiast odrzucać model całkowicie.
        adjusted_return = wzrost_dywidendy + _MIN_SPREAD
        ostrzezenie = (
            f"Wzrost dywidendy ({wzrost_dywidendy*100:.1f}%) przekracza "
            f"wymaganą stopę ({required_return*100:.1f}%). "
            f"Dostosowano wymaganą stopę do "
            f"{adjusted_return*100:.1f}% dla stabilności modelu."
        )
        console.print(f"[yellow]  ⚠ DDM: {ostrzezenie}[/yellow]")
        required_return = adjusted_return
        spread = _MIN_SPREAD

    elif spread < _MIN_SPREAD:
        # Przypadek C: spread zbyt mały (< 2pp), np. ABBV — wycena wrażliwa na założenia.
        # Wymuszamy minimalny spread przez obniżenie efektywnego wzrostu.
        adjusted_growth = required_return - _MIN_SPREAD
        ostrzezenie = (
            f"Wzrost dywidendy ({wzrost_dywidendy*100:.1f}%) bliski wymaganej "
            f"stopie ({required_return*100:.1f}%). "
            f"Zastosowano konserwatywny spread 2% → "
            f"efektywny wzrost: {adjusted_growth*100:.1f}%."
        )
        console.print(f"[yellow]  ⚠ DDM: {ostrzezenie}[/yellow]")
        wzrost_dywidendy = adjusted_growth
        spread = _MIN_SPREAD

    # --- Krok 7: Gordon Growth Model: P = D1 / (r - g) ---
    # D1 = prognozowana dywidenda za następny rok
    d1 = ostatnia_dywidenda_roczna * (1 + wzrost_dywidendy)
    wycena = d1 / spread

    currency = info.get("currency", "")
    current_price = info.get("currentPrice")

    console.print(f"[bold green]  ✓ Wycena DDM zakończona[/bold green]")
    console.print(
        f"[dim]  Ostatnia dywidenda roczna: {ostatnia_dywidenda_roczna:.4f} {currency}[/dim]"
    )
    console.print(
        f"[dim]  Wzrost dywidendy: {wzrost_dywidendy:.2%}, "
        f"wymagana stopa zwrotu: {required_return:.2%}[/dim]"
    )
    console.print(f"[dim]  Wycena DDM: {wycena:.2f} {currency}[/dim]")

    # Ostrzeżenie gdy spread jest mały — wynik DDM jest wtedy kierunkowy, nie precyzyjny
    if spread < 0.03:
        console.print(
            f"[yellow]  ⚠ DDM: mały spread ({spread*100:.1f}%) "
            f"— wycena wrażliwa na założenia[/yellow]"
        )

    if current_price and current_price > 0:
        upside = (wycena - current_price) / current_price * 100
        color = "green" if upside > 0 else "red"
        console.print(f"[{color}]  Potencjał DDM: {upside:+.1f}%[/{color}]")

    result = {
        "wycena": round(wycena, 2),
        "ostatnia_dywidenda_roczna": round(ostatnia_dywidenda_roczna, 4),
        "d1": round(d1, 4),
        "wzrost_dywidendy_pct": round(wzrost_dywidendy * 100, 2),
        "required_return_pct": round(required_return * 100, 2),
        "lat_historii": n_years,
        "metoda": "Gordon Growth Model",
        "waluta": currency,
        "cena_rynkowa": current_price,
    }

    # Dodaj ostrzeżenie jeśli spread był zbyt mały i wymagał korekty
    if ostrzezenie:
        result["ostrzezenie"] = ostrzezenie

    return result
