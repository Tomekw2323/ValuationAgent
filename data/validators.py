"""Walidacja i czyszczenie danych finansowych — sprawdzanie kompletności,
uzupełnianie brakujących wartości, konwersja walut, normalizacja formatów.
"""

import math
from copy import deepcopy


# Wymagane pola w danych z rachunku zysków i strat
REQUIRED_FINANCIALS_KEYS: list[str] = [
    "Total Revenue",
    "EBIT",
    "Net Income",
]

# Wymagane pola w bilansie
REQUIRED_BALANCE_SHEET_KEYS: list[str] = [
    "Total Assets",
    "Total Debt",
]

# Wymagane pola w przepływach pieniężnych
REQUIRED_CASHFLOW_KEYS: list[str] = [
    "Free Cash Flow",
    "Operating Cash Flow",
    "Capital Expenditure",
]

# Wymagane pola w danych profilowych (stock.info)
REQUIRED_INFO_KEYS: list[str] = [
    "currentPrice",
    "marketCap",
    "sharesOutstanding",
]


def _check_section(data: dict, section_name: str, required_keys: list[str]) -> list[str]:
    """Sprawdza czy sekcja danych zawiera wymagane pola.

    Dla danych tabelarycznych (financials, balance_sheet, cashflow) szukamy klucza
    wśród nazw wierszy w dowolnej kolumnie (okresie sprawozdawczym).
    Dla danych płaskich (info) sprawdzamy bezpośrednio obecność klucza.
    """
    missing: list[str] = []
    section = data.get(section_name, {})

    if not section:
        return [f"{section_name} → brak danych"]

    # Dane tabelaryczne: {kolumna/data: {wiersz: wartość}}
    if section_name in ("financials", "balance_sheet", "cashflow"):
        all_row_keys: set[str] = set()
        for col_data in section.values():
            if isinstance(col_data, dict):
                all_row_keys.update(col_data.keys())

        for key in required_keys:
            if key not in all_row_keys:
                missing.append(f"{section_name} → {key}")

    # Dane płaskie: {klucz: wartość}
    else:
        for key in required_keys:
            if key not in section:
                missing.append(f"{section_name} → {key}")

    return missing


def validate_financial_data(data: dict) -> tuple[bool, list[str]]:
    """Sprawdza kompletność pobranych danych finansowych.

    Args:
        data: Słownik zwrócony przez get_financial_data().

    Returns:
        Krotka (is_valid, missing_fields):
        - (True, []) — dane kompletne, gotowe do analizy.
        - (False, ["lista brakujących pól"]) — brakuje wymaganych danych.
    """
    if not data:
        return False, ["Brak danych — słownik jest pusty"]

    missing: list[str] = []

    # Sprawdź każdą sekcję wobec jej wymaganych pól
    missing.extend(_check_section(data, "info", REQUIRED_INFO_KEYS))
    missing.extend(_check_section(data, "financials", REQUIRED_FINANCIALS_KEYS))
    missing.extend(_check_section(data, "balance_sheet", REQUIRED_BALANCE_SHEET_KEYS))
    missing.extend(_check_section(data, "cashflow", REQUIRED_CASHFLOW_KEYS))

    is_valid = len(missing) == 0
    return is_valid, missing


def _is_nan(value) -> bool:
    """Sprawdza czy wartość to NaN (float NaN, numpy NaN, string 'nan')."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.lower() == "nan":
        return True
    return False


def _clean_section(section: dict) -> dict:
    """Czyści sekcję tabelaryczną: zamienia NaN/inf na None,
    usuwa wiersze gdzie wszystkie wartości w każdym okresie są None.

    yfinance zwraca NaN dla brakujących danych — potrzebujemy None
    żeby JSON.dumps działał i żeby narzędzia wyceny poprawnie
    interpretowały brak danych (zamiast dostawać float('nan')).
    """
    if not section:
        return section

    cleaned: dict = {}
    for col_key, col_data in section.items():
        if not isinstance(col_data, dict):
            cleaned[col_key] = col_data
            continue

        cleaned[col_key] = {
            row_key: None if _is_nan(val) else val
            for row_key, val in col_data.items()
        }

    # Zbierz wiersze gdzie WSZYSTKIE wartości to None → do usunięcia
    all_row_keys: set[str] = set()
    for col_data in cleaned.values():
        if isinstance(col_data, dict):
            all_row_keys.update(col_data.keys())

    rows_to_remove: set[str] = set()
    for row_key in all_row_keys:
        all_none = all(
            col_data.get(row_key) is None
            for col_data in cleaned.values()
            if isinstance(col_data, dict)
        )
        if all_none:
            rows_to_remove.add(row_key)

    # Usuń puste wiersze
    if rows_to_remove:
        for col_key in cleaned:
            if isinstance(cleaned[col_key], dict):
                cleaned[col_key] = {
                    k: v for k, v in cleaned[col_key].items()
                    if k not in rows_to_remove
                }

    return cleaned


def clean_financial_data(data: dict) -> dict:
    """Czyści cały słownik danych finansowych:
    - Zamienia NaN na None w sekcjach tabelarycznych i info
    - Usuwa wiersze, w których wszystkie wartości to None
    - Zwraca kopię — oryginał nie jest modyfikowany

    Args:
        data: Surowy słownik z get_financial_data / yfinance.

    Returns:
        Wyczyszczona kopia danych.
    """
    if not data:
        return data

    result = deepcopy(data)

    # Wyczyść sekcje tabelaryczne (financials, balance_sheet, cashflow)
    for section_key in ("financials", "balance_sheet", "cashflow"):
        if section_key in result:
            result[section_key] = _clean_section(result[section_key])

    # Wyczyść sekcję info — zamień NaN na None w płaskim słowniku
    if "info" in result and isinstance(result["info"], dict):
        result["info"] = {
            k: (None if _is_nan(v) else v)
            for k, v in result["info"].items()
        }

    # Wyczyść dywidendy — zamień NaN na None
    if "dividends" in result and isinstance(result["dividends"], dict):
        result["dividends"] = {
            k: (None if _is_nan(v) else v)
            for k, v in result["dividends"].items()
        }

    return result
