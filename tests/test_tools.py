"""Testy narzędzi wyceny — sprawdzanie poprawności pobierania danych,
obliczeń DCF, mnożników i cache'u. Uruchom: python -m tests.test_tools
"""

import sys
import os
import time

# Wymuś UTF-8 na stdout/stderr — testy zawierają znaki box-drawing i emoji,
# które potrafią wywołać UnicodeEncodeError na domyślnej stronie kodowej Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Dodaj katalog projektu do ścieżki importów
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console

console = Console(legacy_windows=False)

# Liczniki wyników
passed = 0
failed = 0
warnings = 0


def report(test_name: str, success: bool, detail: str = "") -> None:
    """Wyświetla wynik pojedynczego testu i aktualizuje liczniki."""
    global passed, failed
    if success:
        passed += 1
        console.print(f"  [bold green]PASS[/bold green]: {test_name}  {detail}")
    else:
        failed += 1
        console.print(f"  [bold red]FAIL[/bold red]: {test_name}  {detail}")


def warn(test_name: str, detail: str) -> None:
    """Wyświetla ostrzeżenie (test nie jest jednoznacznie PASS/FAIL)."""
    global warnings
    warnings += 1
    console.print(f"  [bold yellow]WARN[/bold yellow]: {test_name}  {detail}")


# ────────────────────────────────────────────────────────────────────────────
# Test 1: Pobieranie danych finansowych
# ────────────────────────────────────────────────────────────────────────────

def test_data_fetcher() -> None:
    console.print("\n[bold]1. Test data_fetcher (AAPL)[/bold]")

    from tools.data_fetcher import get_financial_data

    data = get_financial_data("AAPL")

    # Wynik nie powinien być None
    if data is None:
        report("get_financial_data zwraca dane", False, "zwrócono None")
        return
    report("get_financial_data zwraca dane", True)

    # Sprawdź wymagane klucze
    required_keys = ["info", "financials", "balance_sheet", "cashflow", "dividends"]
    missing = [k for k in required_keys if k not in data]
    report(
        "zawiera wymagane klucze",
        len(missing) == 0,
        f"brakuje: {missing}" if missing else f"klucze: {list(data.keys())}",
    )

    # Sprawdź czy info zawiera cenę
    info = data.get("info", {})
    has_price = info.get("currentPrice") is not None
    report(
        "info zawiera currentPrice",
        has_price,
        f"cena: {info.get('currentPrice')} {info.get('currency', '')}" if has_price else "",
    )

    # Sprawdź czy financials nie są puste
    financials = data.get("financials", {})
    report(
        "financials nie jest pusty",
        len(financials) > 0,
        f"okresów: {len(financials)}",
    )


# ────────────────────────────────────────────────────────────────────────────
# Test 2: Wycena DCF
# ────────────────────────────────────────────────────────────────────────────

def test_dcf() -> None:
    console.print("\n[bold]2. Test DCF (AAPL)[/bold]")

    from tools.data_fetcher import get_financial_data
    from tools.dcf import run_dcf

    data = get_financial_data("AAPL")
    if data is None:
        report("DCF — dane wejściowe", False, "brak danych AAPL")
        return

    result = run_dcf(data)

    # Wynik powinien zawierać klucz cena_na_akcje
    has_price = "cena_na_akcje" in result
    report("DCF zwraca cena_na_akcje", has_price)

    price = result.get("cena_na_akcje")

    # Cena powinna być liczbą > 0
    is_positive = isinstance(price, (int, float)) and price > 0
    report(
        "cena > 0",
        is_positive,
        f"cena: {price}" if price else "",
    )

    # Cena Apple powinna być w rozsądnym zakresie (50-1000 USD)
    in_range = isinstance(price, (int, float)) and 50 < price < 1000
    report(
        "cena w zakresie 50–1000 USD",
        in_range,
        f"{price:.2f} USD" if isinstance(price, (int, float)) else "",
    )

    # Sprawdź czy założenia są zapisane
    assumptions = result.get("zalozenia", {})
    report(
        "założenia zapisane (WACC, growth, years)",
        all(k in assumptions for k in ["wacc", "growth_rate", "years"]),
        f"WACC={assumptions.get('wacc')}, g={assumptions.get('growth_rate')}, "
        f"lata={assumptions.get('years')}",
    )

    # Sprawdź prognozy FCF
    forecasts = result.get("prognozy_fcf", [])
    report(
        "prognozy FCF wygenerowane",
        len(forecasts) > 0,
        f"lat: {len(forecasts)}, wartości: {[f'{v:,.0f}' for v in forecasts[:3]]}...",
    )


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Wycena mnożnikowa
# ────────────────────────────────────────────────────────────────────────────

def test_multiples() -> None:
    console.print("\n[bold]3. Test multiples (AAPL)[/bold]")

    from tools.data_fetcher import get_financial_data
    from tools.multiples import run_multiples

    data = get_financial_data("AAPL")
    if data is None:
        report("Multiples — dane wejściowe", False, "brak danych AAPL")
        return

    result = run_multiples(data)

    # Mediana powinna istnieć i być > 0
    median_val = result.get("mediana")
    report(
        "mediana > 0",
        isinstance(median_val, (int, float)) and median_val > 0,
        f"mediana: {median_val:.2f}" if isinstance(median_val, (int, float)) else f"wartość: {median_val}",
    )

    # Sprawdź poszczególne metody
    for method_key, label in [
        ("wycena_pe", "P/E"),
        ("wycena_ev_ebitda", "EV/EBITDA"),
        ("wycena_pbv", "P/BV"),
    ]:
        sub = result.get(method_key)
        if sub is not None:
            p = sub.get("cena_na_akcje")
            report(f"wycena {label}", p is not None and p > 0, f"{p:.2f}" if p else "")
        else:
            warn(f"wycena {label}", "brak danych — pomijam")

    # Sprawdź źródła mnożników
    multiples_info = result.get("uzyte_mnozniki", {})
    report(
        "źródła mnożników zapisane",
        len(multiples_info) > 0,
        ", ".join(f"{k}={v.get('wartosc')}x ({v.get('zrodlo')})" for k, v in multiples_info.items()),
    )


# ────────────────────────────────────────────────────────────────────────────
# Test 4: Cache (zapis i odczyt)
# ────────────────────────────────────────────────────────────────────────────

def test_cache() -> None:
    console.print("\n[bold]4. Test cache (MSFT)[/bold]")

    from tools.data_fetcher import get_financial_data
    from data.cache import save_to_cache, load_from_cache

    data = get_financial_data("MSFT")
    if data is None:
        report("Cache — dane wejściowe", False, "brak danych MSFT")
        return

    # Zapisz do cache
    save_to_cache("MSFT_TEST", data)
    report("save_to_cache", True, "zapisano MSFT_TEST")

    # Odczytaj z cache
    loaded = load_from_cache("MSFT_TEST", max_age_hours=1)
    report("load_from_cache", loaded is not None, "dane odczytane" if loaded else "None")

    if loaded is not None:
        # Porównaj klucze główne
        original_keys = set(data.keys())
        loaded_keys = set(loaded.keys())
        keys_match = original_keys == loaded_keys
        report(
            "klucze identyczne",
            keys_match,
            f"oryg={sorted(original_keys)}, cache={sorted(loaded_keys)}",
        )

        # Porównaj ticker
        report(
            "ticker identyczny",
            data.get("ticker") == loaded.get("ticker"),
            f"{data.get('ticker')} == {loaded.get('ticker')}",
        )

    # Test przeterminowanego cache
    expired = load_from_cache("MSFT_TEST", max_age_hours=0)
    report("cache max_age_hours=0 → None", expired is None)

    # Sprzątanie — usunięcie pliku testowego
    from config import CACHE_DIR
    test_file = CACHE_DIR / "MSFT_TEST.json"
    if test_file.exists():
        test_file.unlink()
        console.print("[dim]    🗑 Usunięto plik testowy cache[/dim]")


# ────────────────────────────────────────────────────────────────────────────
# Test 5: Dane z GPW (PKN Orlen)
# ────────────────────────────────────────────────────────────────────────────

def test_gpw() -> None:
    console.print("\n[bold]5. Test GPW — PKN.WA (PKN Orlen)[/bold]")

    from tools.data_fetcher import get_financial_data

    data = get_financial_data("PKN.WA")

    if data is None:
        warn(
            "GPW PKN.WA",
            "Nie udało się pobrać danych — yfinance może nie obsługiwać tego tickera. "
            "To znane ograniczenie dla rynku GPW.",
        )
        return

    report("get_financial_data(PKN.WA) zwraca dane", True)

    info = data.get("info", {})

    # Sprawdź walutę (powinna być PLN)
    currency = info.get("currency", "")
    report(
        "waluta = PLN",
        currency == "PLN",
        f"waluta: {currency}" if currency else "brak waluty",
    )

    # Sprawdź czy cena jest dostępna
    price = info.get("currentPrice")
    report(
        "cena dostępna",
        price is not None,
        f"{price} {currency}" if price else "",
    )

    # Sprawdź kompletność danych
    from data.validators import validate_financial_data
    is_valid, missing = validate_financial_data(data)
    if is_valid:
        report("dane kompletne", True)
    else:
        warn(
            "dane niekompletne",
            f"brakujące pola: {missing[:5]}{'...' if len(missing) > 5 else ''}",
        )


# ────────────────────────────────────────────────────────────────────────────
# Runner — uruchom wszystkie testy i wyświetl podsumowanie
# ────────────────────────────────────────────────────────────────────────────

def run_all() -> None:
    console.print("\n[bold cyan]═══ Testy narzędzi wyceny ═══[/bold cyan]")

    start = time.time()

    all_tests = [
        test_data_fetcher,
        test_dcf,
        test_multiples,
        test_cache,
        test_gpw,
    ]

    for test_fn in all_tests:
        try:
            test_fn()
        except Exception as e:
            report(test_fn.__name__, False, f"WYJĄTEK: {e}")

    elapsed = time.time() - start

    # Podsumowanie
    total = passed + failed
    console.print("\n[bold cyan]═══ Podsumowanie ═══[/bold cyan]")
    console.print(f"  Testy:       {total}")
    console.print(f"  [green]PASS:        {passed}[/green]")
    console.print(f"  [red]FAIL:        {failed}[/red]")
    console.print(f"  [yellow]WARN:        {warnings}[/yellow]")
    console.print(f"  Czas:        {elapsed:.1f}s")

    if failed == 0:
        console.print("\n[bold green]✓ Wszystkie testy przeszły pomyślnie![/bold green]\n")
    else:
        console.print(f"\n[bold red]✗ {failed} testów nie przeszło.[/bold red]\n")
        sys.exit(1)


if __name__ == "__main__":
    run_all()
