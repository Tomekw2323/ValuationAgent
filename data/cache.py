"""Cache danych finansowych — zapisywanie i wczytywanie wyników zapytań
do plików JSON, zarządzanie czasem ważności cache.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console

from config import CACHE_DIR

console = Console(legacy_windows=False)


def _cache_path(ticker: str):
    """Zwraca ścieżkę do pliku cache dla danego tickera."""
    # Zamiana kropki na podkreślenie w nazwie pliku (np. CDR.WA → CDR_WA.json)
    safe_name = ticker.replace(".", "_").upper()
    return CACHE_DIR / f"{safe_name}.json"


def save_to_cache(ticker: str, data: dict) -> None:
    """Zapisuje dane finansowe do pliku JSON w folderze cache.

    Args:
        ticker: Symbol giełdowy spółki (np. "AAPL", "CDR.WA").
        data: Słownik z danymi finansowymi do zapisania.
    """
    # Utwórz folder cache jeśli nie istnieje
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Dodaj timestamp zapisu (UTC) — służy do sprawdzania świeżości cache
    payload = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "data": data,
    }

    path = _cache_path(ticker)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    console.print(f"[dim]  💾 Dane zapisane w cache: {path.name}[/dim]")


def delete_cache(ticker: str) -> bool:
    """Usuwa plik cache dla danego tickera.

    Args:
        ticker: Symbol giełdowy spółki.

    Returns:
        True jeśli plik istniał i został usunięty, False jeśli plik nie istniał.
    """
    path = _cache_path(ticker)
    if path.exists():
        path.unlink()
        console.print(f"[dim]  🔄 Cache usunięto dla {ticker} (--fresh)[/dim]")
        return True
    return False


def load_from_cache(ticker: str, max_age_hours: int = 48) -> Optional[dict]:
    """Wczytuje dane z cache jeśli plik istnieje i nie jest przeterminowany.

    Args:
        ticker: Symbol giełdowy spółki.
        max_age_hours: Maksymalny wiek cache w godzinach (domyślnie 48h).

    Returns:
        Słownik z danymi finansowymi lub None (brak cache / za stary).
    """
    path = _cache_path(ticker)

    # Sprawdź czy plik cache istnieje
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        # Sprawdź wiek cache
        cached_at = datetime.fromisoformat(payload["cached_at"])
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600

        if age_hours > max_age_hours:
            console.print(
                f"[yellow]  ⚠ Cache dla {ticker} ma {age_hours:.0f}h "
                f"— automatyczne odświeżenie (>{max_age_hours}h).[/yellow]"
            )
            return None

        console.print(
            f"[bold cyan]📦 Używam danych z cache dla [yellow]{ticker}[/yellow] "
            f"(wiek: {age_hours:.1f}h)[/bold cyan]"
        )

        # Informacja gdy dane mają 24–48h — jeszcze ważne, ale warto rozważyć odświeżenie
        if age_hours > 24:
            console.print(
                f"[dim]  ℹ Dane mają {age_hours:.0f}h. Użyj --fresh aby pobrać "
                f"aktualne dane po wynikach kwartalnych lub dużych ruchach kursu.[/dim]"
            )

        return payload["data"]

    except (json.JSONDecodeError, KeyError):
        # Uszkodzony plik cache — ignoruj, dane zostaną pobrane na nowo
        console.print(f"[dim]  ⚠ Uszkodzony plik cache dla {ticker} — pomijam.[/dim]")
        return None
