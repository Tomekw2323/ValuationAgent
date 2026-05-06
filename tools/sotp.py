"""Wycena metodą Sum-of-the-Parts (SOTP) — dla konglomeratów z odrębnymi
segmentami biznesowymi, gdzie jeden mnożnik dla całej firmy jest mylący.

Przykład: Amazon (AWS ~15x, Reklamy ~8x, E-commerce ~0.5x) vs. jeden mnożnik
EV/Sales ~3x dla całości — SOTP daje dokładniejszy obraz wartości.

Funkcja run_sotp() działa tylko dla spółek zdefiniowanych w SOTP_COMPANIES.
Dla pozostałych zwraca None — wtedy agent używa standardowych metod.
"""

from typing import Optional

from rich.console import Console

console = Console(legacy_windows=False)

# Słownik konglomeratów z podziałem na segmenty.
# Dane przychodowe to estymacje za rok fiskalny 2024 (mld USD).
# Mnożniki EV/Sales odzwierciedlają profil marżowości i wzrostu każdego segmentu.
SOTP_COMPANIES: dict[str, dict] = {
    "AMZN": {
        "waluta": "USD",
        "segmenty": {
            "AWS": {
                "przychody_estymacja": 107.0,   # mld USD, ~17% wzrostu r/r
                # AWS: lider cloud z 30%+ marżą operacyjną,
                # Microsoft Azure wyceniany ~15x, AWS zasługuje na podobny mnożnik
                "ev_sales_mnoznik": 15.0,
                "opis": "Cloud computing — lider rynku, 30%+ marża operacyjna",
            },
            "Reklamy": {
                "przychody_estymacja": 47.0,    # mld USD, ~20% wzrostu r/r
                "ev_sales_mnoznik": 8.0,         # digital ads jak Google/Meta
                "opis": "Digital advertising — najwyższe marże ~50%",
            },
            "E-commerce": {
                "przychody_estymacja": 425.0,   # mld USD, rynek dojrzały
                "ev_sales_mnoznik": 0.5,         # retail: niskie marże ~3%
                "opis": "Online i physical retail — niskie marże, duża skala",
            },
        },
    },
    "GOOGL": {
        "waluta": "USD",
        "segmenty": {
            "Search & Reklamy": {
                "przychody_estymacja": 175.0,   # mld USD, dojrzały segment
                "ev_sales_mnoznik": 7.0,         # dojrzały, stabilny wzrost ~10%
                "opis": "Dominujące reklamy online — dojrzały segment",
            },
            "Google Cloud": {
                "przychody_estymacja": 36.0,    # mld USD, rosnący 28% r/r
                # Google Cloud: porównaj do Azure/AWS — rosnący, podobny profil marżowy
                "ev_sales_mnoznik": 12.0,
                "opis": "Cloud rosnący 28% rocznie — porównaj do Azure/AWS",
            },
            "YouTube": {
                "przychody_estymacja": 32.0,    # mld USD
                "ev_sales_mnoznik": 6.0,         # video platform z reklamami
                "opis": "Video platform z reklamami i subskrypcjami",
            },
            "Other Bets": {
                "przychody_estymacja": 2.0,     # mld USD (Waymo, DeepMind i in.)
                "ev_sales_mnoznik": 2.0,         # moonshots: wysokie ryzyko, opcja
                "opis": "Moonshots — Waymo, DeepMind commercial, wysokie ryzyko",
            },
        },
    },
    "GOOG": {
        # Alias — identyczna konfiguracja jak GOOGL (akcje klasy C bez prawa głosu)
        "waluta": "USD",
        "segmenty": {
            "Search & Reklamy": {
                "przychody_estymacja": 175.0,
                "ev_sales_mnoznik": 7.0,
                "opis": "Dominujące reklamy online — dojrzały segment",
            },
            "Google Cloud": {
                "przychody_estymacja": 36.0,
                "ev_sales_mnoznik": 12.0,
                "opis": "Cloud rosnący 28% rocznie — porównaj do Azure/AWS",
            },
            "YouTube": {
                "przychody_estymacja": 32.0,
                "ev_sales_mnoznik": 6.0,
                "opis": "Video platform z reklamami i subskrypcjami",
            },
            "Other Bets": {
                "przychody_estymacja": 2.0,
                "ev_sales_mnoznik": 2.0,
                "opis": "Moonshots — Waymo, DeepMind commercial, wysokie ryzyko",
            },
        },
    },
}


def run_sotp(
    financial_data: dict,
    ticker: Optional[str] = None,
) -> Optional[dict]:
    """Wycena metodą Sum-of-the-Parts dla konglomeratów.

    Każdy segment wyceniany jest oddzielnym mnożnikiem EV/Sales dopasowanym
    do jego profilu marżowości i tempa wzrostu. Enterprise Value całej spółki
    = suma EV poszczególnych segmentów.

    Args:
        financial_data: Słownik z danymi finansowymi (z get_financial_data).
        ticker: Opcjonalne jawne podanie tickera — gdy None, pobiera z financial_data.

    Returns:
        Słownik z wynikami SOTP lub None gdy spółka nie jest w SOTP_COMPANIES.
    """
    # Ticker z argumentu ma pierwszeństwo — pozwala wywołać run_sotp(fd, "AMZN")
    raw_ticker = ticker or financial_data.get("ticker", "")

    # Normalizacja: usuwamy suffiksy giełdowe i przestawiamy na wielkie litery
    ticker_clean = raw_ticker.upper().split(".")[0]

    if ticker_clean not in SOTP_COMPANIES:
        return None

    config = SOTP_COMPANIES[ticker_clean]
    segmenty_config = config["segmenty"]
    waluta = config.get("waluta", "USD")

    info = financial_data.get("info", {})
    shares = info.get("sharesOutstanding", 0) or 0
    total_debt = info.get("totalDebt", 0) or 0
    total_cash = info.get("totalCash", 0) or 0

    # Dług netto = dług − gotówka (konwertujemy z USD na miliardy dla czytelności)
    dlugnetto_mld = (total_debt - total_cash) / 1e9

    console.print(
        f"\n[bold cyan]🏗️  Sum-of-the-Parts dla "
        f"[yellow]{ticker_clean}[/yellow]...[/bold cyan]"
    )

    if shares <= 0:
        console.print(
            "[yellow]  ⚠ SOTP: brak danych o liczbie akcji — pomijam.[/yellow]"
        )
        return None

    # --- Wycena każdego segmentu ---
    segmenty_wyniki: dict[str, dict] = {}
    total_ev_mld = 0.0

    for nazwa, seg in segmenty_config.items():
        przychody = seg["przychody_estymacja"]   # mld
        mnoznik = seg["ev_sales_mnoznik"]
        ev_seg = przychody * mnoznik              # mld

        segmenty_wyniki[nazwa] = {
            "przychody_mld": przychody,
            "ev_sales_mnoznik": mnoznik,
            "ev_mld": round(ev_seg, 1),
            "ev": round(ev_seg, 1),   # alias dla wygody (ev_mld i ev to to samo)
            "opis": seg["opis"],
        }
        total_ev_mld += ev_seg

        console.print(
            f"[dim]  {nazwa:>20}: {przychody:>6.0f} mld × {mnoznik}x "
            f"= {ev_seg:>7.0f} mld {waluta} — {seg['opis']}[/dim]"
        )

    # --- Equity Value = Total EV − dług netto ---
    equity_mld = total_ev_mld - dlugnetto_mld

    if shares <= 0:
        console.print("[yellow]  ⚠ SOTP: nieprawidłowa liczba akcji.[/yellow]")
        return None

    # Konwersja: equity w mld / akcje = cena w USD (skale się znoszą)
    cena_na_akcje = (equity_mld * 1e9) / shares

    console.print(f"[dim]  {'─' * 50}[/dim]")
    console.print(f"[dim]  {'Total EV':>20}: {total_ev_mld:>7.0f} mld {waluta}[/dim]")
    console.print(
        f"[dim]  {'Dług netto':>20}: {dlugnetto_mld:>7.0f} mld {waluta} "
        f"(dług={total_debt/1e9:.0f}, gotówka={total_cash/1e9:.0f})[/dim]"
    )
    console.print(
        f"[dim]  {'Equity Value':>20}: {equity_mld:>7.0f} mld {waluta}[/dim]"
    )
    console.print(
        f"\n[bold green]  ✓ Cena SOTP: {cena_na_akcje:.2f} {waluta}[/bold green]"
    )

    current_price = info.get("currentPrice")
    if current_price and current_price > 0:
        upside = (cena_na_akcje - current_price) / current_price * 100
        color = "green" if upside > 0 else "red"
        console.print(f"[{color}]  Potencjał SOTP: {upside:+.1f}%[/{color}]")

    return {
        "ticker": ticker_clean,
        "metoda": "Sum-of-the-Parts (SOTP)",
        "cena_na_akcje": round(cena_na_akcje, 2),
        "total_ev_mld": round(total_ev_mld, 1),
        "dlugnetto_mld": round(dlugnetto_mld, 1),
        "equity_mld": round(equity_mld, 1),
        "segmenty": segmenty_wyniki,
        "waluta": waluta,
        "cena_rynkowa": current_price,
        "uwaga": (
            "Wycena oparta na estymowanych przychodach segmentów 2024. "
            "Mnożniki EV/Sales dobrane do profilu marżowości każdego segmentu. "
            "AWS 15x — poziom zbliżony do Azure (Microsoft). "
            "Google Cloud 12x — rosnący segment, niższy od AWS ze względu na "
            "mniejszą skalę i niższe marże. "
            "Dane segmentowe są przybliżeniem — spółki nie publikują pełnego "
            "podziału marż per segment."
        ),
    }
