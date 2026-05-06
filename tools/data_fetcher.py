"""Fetch financial statements and market data from yfinance with file caching."""


import math
from typing import Any, Optional

import pandas as pd
import yfinance as yf
from rich.console import Console

from data.cache import load_from_cache, save_to_cache, delete_cache
from data.validators import validate_financial_data, clean_financial_data

console = Console(legacy_windows=False)
_MARKET_CAP_LOOKUP_CACHE: dict[str, Optional[float]] = {}


def safe_get_value(data: Any) -> Optional[float]:
    """Bezpiecznie wyciąga skalarną wartość liczbową z obiektu yfinance.

    yfinance zwraca różne typy w zależności od wersji i tickera:
    - float/int — wartość skalarna (idealny przypadek)
    - numpy scalar — np.float64, np.int64
    - pd.Series — jednowierszowy wynik z DataFrame
    - pd.DataFrame — gdy MultiIndex zwróci podmacierz
    - None / NaN — brak danych

    Zwraca float lub None — nigdy nie rzuca wyjątku.
    """
    if data is None:
        return None

    # pandas NaN / numpy NaN
    try:
        if pd.isna(data):
            return None
    except (TypeError, ValueError):
        pass

    # pd.DataFrame → spróbuj wyciągnąć pierwszą wartość skalarną
    if isinstance(data, pd.DataFrame):
        if data.empty:
            return None
        data = data.iloc[0, 0] if data.size > 0 else None
        return safe_get_value(data)

    # pd.Series → pierwsza wartość
    if isinstance(data, pd.Series):
        if data.empty:
            return None
        data = data.iloc[0]
        return safe_get_value(data)

    # Konwersja na float
    try:
        val = float(data)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except (TypeError, ValueError):
        return None


def _dataframe_to_dict(df: pd.DataFrame) -> dict:
    """Konwertuje DataFrame z yfinance na słownik {kolumna: {wiersz: wartość}}.
    Zamienia daty na stringi i NaN na None — ułatwia serializację do JSON.

    Obsługuje MultiIndex w kolumnach/wierszach — yfinance czasem zwraca
    hierarchiczne indeksy, szczególnie dla spółek z mniejszych rynków.
    """
    if df is None or df.empty:
        return {}

    # Spłaszczenie MultiIndex w kolumnach (np. ('Financials', '2024-01-01'))
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [
            c[-1].isoformat() if hasattr(c[-1], "isoformat") else str(c[-1])
            for c in df.columns
        ]

    # Spłaszczenie MultiIndex w wierszach
    if isinstance(df.index, pd.MultiIndex):
        df = df.copy()
        df.index = [str(idx[-1]) for idx in df.index]

    result: dict = {}
    for col in df.columns:
        col_key = col.isoformat() if hasattr(col, "isoformat") else str(col)
        result[col_key] = {
            str(idx): safe_get_value(val)
            for idx, val in df[col].items()
        }
    return result


def _extract_info(info: dict) -> dict:
    """Wyciąga najważniejsze pola z obiektu stock.info.
    Zwraca uporządkowany słownik z danymi rynkowymi i profilowymi spółki.
    """
    keys = [
        # Profil spółki
        "symbol", "shortName", "longName", "sector", "industry", "country", "currency",
        # Cena i kapitalizacja
        "currentPrice", "regularMarketPrice", "previousClose", "marketCap", "enterpriseValue",
        # Mnożniki wyceny
        "trailingPE", "forwardPE", "priceToBook", "enterpriseToEbitda",
        "enterpriseToRevenue", "pegRatio",
        # Rentowność i marże
        "profitMargins", "operatingMargins", "grossMargins", "returnOnEquity",
        "returnOnAssets",
        # Dywidendy
        "dividendRate", "dividendYield", "payoutRatio",
        # Ryzyko i struktura kapitału
        "beta", "debtToEquity",
        # Liczba akcji
        "sharesOutstanding", "floatShares",
        # Przychody i zysk
        "totalRevenue", "revenueGrowth", "earningsGrowth",
        "totalCash", "totalDebt", "freeCashflow", "operatingCashflow",
    ]
    extracted = {k: info.get(k) for k in keys if info.get(k) is not None}
    # Ujednolicenie ceny rynkowej: część tickerów ma tylko regularMarketPrice.
    if extracted.get("currentPrice") is None and extracted.get("regularMarketPrice") is not None:
        extracted["currentPrice"] = extracted["regularMarketPrice"]
    return extracted


def _fetch_from_yfinance(ticker: str) -> Optional[dict]:
    """Pobiera dane bezpośrednio z yfinance (bez cache).
    Wydzielona z get_financial_data, żeby logika cache była czytelna.
    """
    console.print(
        f"\n[bold cyan]⏳ Fetching yfinance data for [yellow]{ticker}[/yellow]...[/bold cyan]"
    )

    try:
        stock = yf.Ticker(ticker)

        # Dane profilowe i rynkowe (cena, P/E, sektor, waluta itp.)
        info = stock.info or {}
        if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
            console.print(
                f'[bold red]✗ No data returned for ticker "{ticker}".[/bold red]\n'
                "[dim]Check the symbol. For Warsaw (GPW) tickers append .WA "
                "(e.g. CDR.WA).[/dim]"
            )
            return None

        console.print("[dim]  ✓ Profile & market data[/dim]")

        financials = _dataframe_to_dict(stock.financials)
        console.print("[dim]  ✓ Income statement[/dim]")

        balance_sheet = _dataframe_to_dict(stock.balance_sheet)
        console.print("[dim]  ✓ Balance sheet[/dim]")

        cashflow = _dataframe_to_dict(stock.cashflow)
        console.print("[dim]  ✓ Cash flow statement[/dim]")

        # Historia dywidend
        dividends_series = stock.dividends
        if dividends_series is not None and not dividends_series.empty:
            dividends = {
                dt.isoformat(): float(val)
                for dt, val in dividends_series.items()
            }
        else:
            dividends = {}
        console.print("[dim]  ✓ Dividend history[/dim]")

        console.print(
            f"[bold green]✓ Loaded data for [yellow]{ticker}[/yellow] "
            f"({info.get('shortName', 'n/a')})[/bold green]"
        )

        # Konsensus analityków — ceny docelowe i rekomendacje z yfinance info
        analyst_data: dict = {}
        try:
            analyst_data["target_mean"]   = info.get("targetMeanPrice")
            analyst_data["target_high"]   = info.get("targetHighPrice")
            analyst_data["target_low"]    = info.get("targetLowPrice")
            analyst_data["target_median"] = info.get("targetMedianPrice")
            analyst_data["analyst_count"] = info.get("numberOfAnalystOpinions")
            # recommendationKey: "strong_buy", "buy", "hold", "sell", "strong_sell"
            analyst_data["recommendation"] = info.get("recommendationKey")

            if analyst_data.get("target_mean"):
                console.print(
                    f"[dim]  📊 Analyst consensus: "
                    f"{analyst_data['target_mean']:.2f} "
                    f"({analyst_data['analyst_count']} analysts, "
                    f"view: {analyst_data['recommendation']})[/dim]"
                )
        except Exception as e:
            console.print(f"[yellow]  ⚠ No analyst targets: {e}[/yellow]")

        return {
            "ticker": ticker,
            "info": _extract_info(info),
            "financials": financials,
            "balance_sheet": balance_sheet,
            "cashflow": cashflow,
            "dividends": dividends,
            "analyst_consensus": analyst_data,
        }

    except Exception as e:
        console.print(
            f'[bold red]✗ Error fetching "{ticker}": {e}[/bold red]\n'
            "[dim]Check your network connection and ticker symbol.[/dim]"
        )
        return None


def get_financial_data(
    ticker: str,
    force_refresh: bool = False,
) -> Optional[dict]:
    """Pobiera komplet danych finansowych spółki — najpierw z cache, potem z yfinance.

    Args:
        ticker: Symbol giełdowy spółki (np. "AAPL", "CDR.WA").
        force_refresh: Jeśli True, pomija cache i pobiera świeże dane z yfinance.
            Przydatne po wynikach kwartalnych lub dużych ruchach kursu.

    Returns:
        Słownik z kluczami: ticker, info, financials, balance_sheet, cashflow, dividends.
        None jeśli ticker nie istnieje lub dane są niedostępne.
    """
    # 1. Wymuś odświeżenie — usuń cache przed sprawdzeniem
    if force_refresh:
        delete_cache(ticker)

    # 2. Sprawdź cache — jeśli dane są świeże (< 48h), zwróć je od razu
    cached = load_from_cache(ticker)
    if cached is not None:
        return cached

    # 2. Cache pusty lub przeterminowany — pobierz z yfinance
    data = _fetch_from_yfinance(ticker)
    if data is None:
        return None

    # 3. Wyczyść dane — zamień NaN na None, usuń puste wiersze
    data = clean_financial_data(data)

    # 4. Zapisz wyczyszczone dane do cache na przyszłość
    save_to_cache(ticker, data)

    # 5. Walidacja — sprawdź kompletność danych i ostrzeż o brakach
    is_valid, missing = validate_financial_data(data)
    if not is_valid:
        console.print("[bold yellow]⚠ Incomplete data — missing fields:[/bold yellow]")
        for field in missing:
            console.print(f"[yellow]    • {field}[/yellow]")
        console.print(
            "[dim]  The agent will still try to value the company with available fields.[/dim]\n"
        )
    else:
        console.print("[dim]  ✓ All required fields present.[/dim]\n")

    return data


# Peers dla spółek z GPW — europejskie i regionalne odpowiedniki.
# Amerykańskie spółki mają wyższe mnożniki (większa płynność, inne standardy
# rachunkowości, niższe ryzyko polityczne), więc porównywanie z nimi zawyżałoby wycenę.
GPW_SECTOR_PEERS: dict[str, list[str]] = {
    "Energy": ["MOL.BD", "OMV.VI", "REPSOL.MC", "ENI.MI", "LOTOS.WA"],
    "Banking": ["PKO.WA", "PEO.WA", "SPL.WA", "ING.WA", "MBK.WA"],
    "Retail": ["LPP.WA", "CCC.WA", "ALE.WA", "EUR.WA", "KER.PA"],
    # Branża gamingowa wydzielona osobno — CDR.WA i 11B.WA to game dev,
    # nie typowe spółki IT. Porównanie z EA/TTWO daje trafniejsze mnożniki.
    "Electronic Gaming & Multimedia": ["CDR.WA", "11B.WA", "UBI.PA", "EA", "TTWO", "NTES"],
    "Technology": ["PKP.WA", "ATC.WA", "TEN.WA", "SAP.DE", "CAP.PA"],
    "Chemicals": ["PCC.WA", "ZCH.WA", "GRX.WA", "BASF.DE", "AKZA.AS"],
    "Utilities": ["PGE.WA", "TPE.WA", "ENA.WA", "CEZ.PR", "EOAN.DE"],
    "Telecom": ["OPL.WA", "PLY.WA", "PLTEL.WA", "DTE.DE", "ORAN.PA"],
    "Real Estate": ["GTC.WA", "ECH.WA", "PHN.WA", "WDP.BR", "ARGAN.PA"],
    "Food": ["MASPEX.WA", "INDYK.WA", "KSW.WA", "ULVR.L", "NESN.SW"],
    "Media": ["TVN.WA", "ATD.WA", "AGORA.WA", "AXEL.DE", "PRS.L"],
    "Construction": ["BDX.WA", "ERBUD.WA", "PBG.WA", "STX.WA", "STRABAG.VI"],
}

# Peers GPW pogrupowane po branży (industry) — dokładniejsze niż po sektorze.
# Używane jako pierwszy wybór gdy industry jest znane i pasuje do klucza.
GPW_INDUSTRY_PEERS: dict[str, list[str]] = {
    "Apparel Manufacturing": ["LPP.WA", "CCC.WA", "ZAR.MC", "HM-B.ST", "ITX.MC"],
    "Electronic Gaming & Multimedia": ["CDR.WA", "11B.WA", "UBI.PA", "EA", "TTWO"],
    "Banks - Regional": ["PKO.WA", "PEO.WA", "SPL.WA", "ING.WA", "MBK.WA"],
    "Insurance": ["PZU.WA", "AXA.PA", "ALV.DE", "MUV2.DE", "ZURN.SW"],
    "Insurance - Property & Casualty": ["PZU.WA", "AXA.PA", "ALV.DE", "MUV2.DE", "ZURN.SW"],
    "Insurance - Life": ["PZU.WA", "AXA.PA", "ALV.DE", "MUV2.DE", "ZURN.SW"],
    "Oil & Gas Integrated": ["PKN.WA", "MOL.BD", "OMV.VI", "ENI.MI", "REP.MC"],
    "Utilities - Regulated Electric": ["PGE.WA", "TPE.WA", "ENA.WA", "CEZ.PR", "EOAN.DE"],
    "Specialty Chemicals": ["PCC.WA", "ZCH.WA", "BASF.DE", "AKZA.AS", "DSM.AS"],
    "Chemicals": ["PCC.WA", "ZCH.WA", "BASF.DE", "AKZA.AS", "DSM.AS"],
    "Internet Retail": ["ALE.WA", "AMZN", "BABA", "EBAY", "SHOP"],
    "Copper": ["KGHM.WA", "FCX", "SCCO", "TECK", "BHP"],
    "Steel": ["CMC.WA", "NUE", "STLD", "MT.AS", "SSAB-A.ST"],
    "Food Distribution": ["DINO.WA", "SYY", "UNFI", "PFGC", "CHEF"],
    "Telecom Services": ["OPL.WA", "DTE.DE", "ORAN.PA", "TEF.MC", "TELIA.ST"],
}

# Fallback dla spółek GPW gdy sektor nie pasuje do żadnego klucza
GPW_FALLBACK_PEERS: list[str] = ["PKO.WA", "PKN.WA", "PZU.WA"]

# Spółki hybrydowe — nie pasują do standardowych kategorii branżowych.
# yfinance przypisuje je do sektora bazowego (np. Tesla → Auto Manufacturers),
# ale rynek wycenia je inaczej niż tradycyjnych graczy w tym sektorze.
# Używamy dedykowanych peers zamiast fallbacku sektorowego.
HYBRID_COMPANY_PEERS: dict[str, dict] = {
    "TSLA": {
        "peers": ["RIVN", "NIO", "LCID", "LI", "XPEV"],
        "opis": "Pure-play EV manufacturers",
        "uwaga": (
            "Tesla jest wyceniana jako spółka technologiczna, nie tradycyjny "
            "producent samochodów. Porównanie do Toyota/Ford zaniżałoby wycenę. "
            "Użyto pure-play EV peers."
        ),
    },
    "AMZN": {
        "peers": ["BABA", "JD", "EBAY", "SHOP", "MELI"],
        "opis": "E-commerce platforms",
        "uwaga": (
            "Amazon łączy 3 segmenty: e-commerce, AWS i reklamy. "
            "Sum-of-parts byłby dokładniejszy — użyto peers e-commerce "
            "jako przybliżenie głównego segmentu przychodowego."
        ),
    },
    "GOOGL": {
        "peers": ["META", "MSFT", "SNAP", "PINS", "TTD"],
        "opis": "Digital advertising + cloud",
        "uwaga": (
            "Alphabet łączy reklamy (~80% przychodów), cloud (Google Cloud) "
            "i moonshots. SOTP jest dokładniejszy niż jeden mnożnik. "
            "Używam peers digital advertising jako przybliżenie."
        ),
    },
    "GOOG": {
        "peers": ["META", "MSFT", "SNAP", "PINS", "TTD"],
        "opis": "Digital advertising + cloud",
        "uwaga": (
            "Alphabet łączy wyszukiwarkę (reklamy), Google Cloud i moonshots. "
            "Mnożniki reklamowe są najbliższym przybliżeniem."
        ),
    },
    "META": {
        "peers": ["GOOGL", "SNAP", "PINS", "RDDT", "ZM"],
        "opis": "Social media platforms",
        "uwaga": (
            "Meta dominuje social media z silnymi efektami sieciowymi. "
            "Inwestycje w Reality Labs (metaverse) obniżają krótkoterminowe marże."
        ),
    },
    "NFLX": {
        "peers": ["DIS", "WBD", "PARA", "AMCX", "FUBO"],
        "opis": "Streaming entertainment",
        "uwaga": "Netflix jako pure-play streaming z globalnym zasięgiem.",
    },
    "BRK-B": {
        "peers": ["MKL", "FFH", "L", "LUK", "HRG"],
        "opis": "Diversified holding companies",
        "uwaga": (
            "Berkshire Hathaway to konglomerat — standardowe mnożniki P/E "
            "i EV/EBITDA są mylące. Preferuj wycenę przez wartość aktywów netto "
            "(NAV) lub P/BV relative to ROE."
        ),
    },
    "BRK-A": {
        "peers": ["MKL", "FFH", "L", "LUK", "HRG"],
        "opis": "Diversified holding companies",
        "uwaga": (
            "Berkshire Hathaway to konglomerat — standardowe mnożniki P/E "
            "i EV/EBITDA są mylące. Preferuj wycenę przez wartość aktywów netto "
            "(NAV) lub P/BV relative to ROE."
        ),
    },
}

# Peers US pogrupowane po branży (industry) — precyzyjne dopasowanie.
# Tesla (Auto Manufacturers) → TM, F, GM zamiast Amazon czy Home Depot.
# Amazon (Internet Retail) → BABA, JD, EBAY zamiast Tesli czy McDonald's.
US_INDUSTRY_PEERS: dict[str, list[str]] = {
    # Motoryzacja
    "Auto Manufacturers": ["TM", "F", "GM", "STLA", "HMC"],
    "Auto Parts": ["MGA", "BWA", "APTV", "LEA", "ALV"],
    # Internet i technologia
    "Internet Retail": ["BABA", "JD", "EBAY", "ETSY", "SHOP"],
    "Internet Content & Information": ["GOOGL", "META", "SNAP", "PINS", "IAC"],
    "Software - Application": ["MSFT", "CRM", "NOW", "WDAY", "ADSK"],
    "Software - Infrastructure": ["ORCL", "IBM", "CSCO", "PANW", "CRWD"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "QCOM", "AVGO"],
    "Consumer Electronics": ["AAPL", "SONO", "GPRO", "HEAR", "VOXX"],
    "Electronic Gaming & Multimedia": ["EA", "TTWO", "RBLX", "U", "NTES"],
    # Żywność i napoje
    "Beverages - Non-Alcoholic": ["KO", "PEP", "MNST", "CELH", "FIZZ"],
    "Beverages - Alcoholic": ["BUD", "TAP", "SAM", "STZ", "MGPI"],
    "Packaged Foods": ["GIS", "K", "CPB", "SJM", "MKC"],
    "Restaurants": ["MCD", "YUM", "QSR", "DPZ", "SBUX"],
    # Handel detaliczny i odzież
    "Apparel Manufacturing": ["LULU", "PVH", "HBI", "RL", "VFC"],
    "Apparel Retail": ["GPS", "ANF", "AEO", "URBN", "ROST"],
    "Specialty Retail": ["HD", "LOW", "TGT", "COST", "WMT"],
    "Luxury Goods": ["TPR", "CPRI", "PVH", "RL", "MOV"],
    "Department Stores": ["M", "KSS", "JWN", "DDS", "BIG"],
    "Discount Stores": ["WMT", "TGT", "COST", "BJ", "DLTR"],
    # Ochrona zdrowia
    "Drug Manufacturers - General": ["JNJ", "PFE", "MRK", "LLY", "ABBV"],
    "Drug Manufacturers - Specialty & Generic": ["BIIB", "REGN", "VRTX", "ALXN", "BMRN"],
    "Medical Devices": ["MDT", "ABT", "BSX", "SYK", "ZBH"],
    "Health Care Plans": ["UNH", "CVS", "CI", "HUM", "MOH"],
    "Biotechnology": ["AMGN", "GILD", "BIIB", "REGN", "VRTX"],
    # Energia
    "Oil & Gas Integrated": ["XOM", "CVX", "COP", "EOG", "PXD"],
    "Oil & Gas E&P": ["DVN", "FANG", "MRO", "APA", "OXY"],
    "Oil & Gas Refining & Marketing": ["PSX", "VLO", "MPC", "DK", "DINO"],
    # Finanse
    "Banks - Diversified": ["JPM", "BAC", "WFC", "C", "USB"],
    "Banks - Regional": ["PNC", "TFC", "RF", "FITB", "HBAN"],
    "Insurance - Life": ["MET", "PRU", "LNC", "AFL", "GL"],
    "Insurance - Property & Casualty": ["BRK-B", "PGR", "TRV", "ALL", "CB"],
    "Asset Management": ["BLK", "BX", "KKR", "APO", "ARES"],
    "Capital Markets": ["GS", "MS", "JPM", "C", "BAC"],
    # Nieruchomości
    "REIT - Retail": ["SPG", "O", "NNN", "BRX", "KIM"],
    "REIT - Office": ["BXP", "VNO", "SLG", "HIW", "PDM"],
    "REIT - Industrial": ["PLD", "DRE", "REXR", "FR", "EGP"],
    "REIT - Residential": ["EQR", "AVB", "ESS", "MAA", "UDR"],
    # Utilities
    "Utilities - Regulated Electric": ["NEE", "DUK", "SO", "AEP", "EXC"],
    "Utilities - Renewable": ["ENPH", "SEDG", "RUN", "NOVA", "ARRY"],
    "Utilities - Regulated Gas": ["SRE", "NI", "ATO", "OGS", "SWX"],
    # Przemysł
    "Aerospace & Defense": ["BA", "LMT", "RTX", "NOC", "GD"],
    "Industrial Distribution": ["GWW", "MSM", "FAST", "WSO", "AIT"],
    "Specialty Industrial Machinery": ["EMR", "ROK", "AME", "PH", "ITW"],
    "Farm & Heavy Construction Machinery": ["CAT", "DE", "AGCO", "CNH", "TEX"],
    # Materiały
    "Specialty Chemicals": ["LIN", "APD", "ECL", "SHW", "PPG"],
    "Gold": ["NEM", "GOLD", "AEM", "KGC", "AGI"],
    "Copper": ["FCX", "SCCO", "TECK", "HBM", "CS"],
    "Steel": ["NUE", "STLD", "RS", "X", "CMC"],
}

# Peers dla spółek z NYSE/NASDAQ — największe amerykańskie spółki per sektor.
# Używane jako fallback gdy brak dopasowania po branży w US_INDUSTRY_PEERS.
US_SECTOR_PEERS: dict[str, list[str]] = {
    "Technology": ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "ADBE", "CRM", "INTC", "AMD", "ORCL"],
    # Gaming/multimedia jako osobna branża — trafniejsze porównanie dla game dev
    "Electronic Gaming & Multimedia": ["EA", "TTWO", "RBLX", "U", "NTES"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "NKE", "MCD", "SBUX", "TGT", "LOW"],
    "Consumer Defensive": ["PG", "KO", "PEP", "WMT", "COST", "CL", "MDLZ", "PM"],
    "Financial Services": ["JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "AXP"],
    "Healthcare": ["JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO", "ABT"],
    "Industrials": ["BA", "HON", "UPS", "CAT", "GE", "RTX", "DE", "LMT"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO"],
    "Basic Materials": ["LIN", "APD", "SHW", "ECL", "DD", "NEM", "FCX", "NUE"],
    "Real Estate": ["AMT", "PLD", "CCI", "SPG", "EQIX", "PSA", "O", "DLR"],
    "Utilities": ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL"],
}


def _match_gpw_sector(yf_sector: str) -> Optional[str]:
    """Dopasowuje sektor z yfinance do klucza w GPW_SECTOR_PEERS.

    yfinance zwraca angielskie nazwy sektorów (np. "Energy", "Financial Services"),
    ale GPW_SECTOR_PEERS ma uproszczone klucze ("Banking" zamiast "Financial Services").
    Dopasowanie case-insensitive + mapowanie popularnych wariantów.
    """
    # Bezpośrednie dopasowanie (case-insensitive)
    for key in GPW_SECTOR_PEERS:
        if key.lower() == yf_sector.lower():
            return key

    # Mapowanie wariantów nazw sektorów z yfinance na klucze GPW
    sector_aliases: dict[str, str] = {
        "financial services": "Banking",
        "financials": "Banking",
        "consumer cyclical": "Retail",
        "consumer defensive": "Food",
        "communication services": "Telecom",
        "basic materials": "Chemicals",
        "industrials": "Construction",
    }

    return sector_aliases.get(yf_sector.lower())


def _extract_market_cap(info: dict) -> Optional[float]:
    """Zwraca dodatnią kapitalizację rynkową z info lub None."""
    market_cap = safe_get_value((info or {}).get("marketCap"))
    if market_cap is None or market_cap <= 0:
        return None
    return float(market_cap)


def _peek_market_cap(ticker: str) -> Optional[float]:
    """Lekko pobiera kapitalizację rynkową peera (bez pełnych sprawozdań).

    Używane wyłącznie do sanity-check doboru peers, żeby unikać porównań
    megacapów do mikrocapów w tej samej branży.
    """
    cache_key = ticker.upper()
    if cache_key in _MARKET_CAP_LOOKUP_CACHE:
        return _MARKET_CAP_LOOKUP_CACHE[cache_key]

    try:
        info = yf.Ticker(ticker).info or {}
        market_cap = _extract_market_cap(info)
        _MARKET_CAP_LOOKUP_CACHE[cache_key] = market_cap
        return market_cap
    except Exception:
        _MARKET_CAP_LOOKUP_CACHE[cache_key] = None
        return None


def _scale_gate_for_market_cap(base_market_cap: float) -> tuple[float, float]:
    """Zwraca akceptowalny zakres relacji market cap peer/base.

    Dla bardzo dużych spółek wymuszamy ciaśniejszy dolny próg, żeby uniknąć
    zbyt małych peerów (klasyczny błąd: AAPL vs mikrocapy consumer electronics).
    """
    if base_market_cap >= 200_000_000_000:   # >= 200 mld USD
        return 0.12, 12.0
    if base_market_cap >= 20_000_000_000:    # >= 20 mld USD
        return 0.06, 15.0
    return 0.02, 20.0


def _filter_peers_by_scale(
    base_ticker: str,
    base_market_cap: Optional[float],
    candidates: list[str],
) -> tuple[list[str], dict]:
    """Filtruje kandydatów peers po porównywalnej skali kapitalizacji."""
    if base_market_cap is None:
        return list(candidates), {
            "applied": False,
            "reason": "Brak marketCap spółki bazowej.",
        }

    min_ratio, max_ratio = _scale_gate_for_market_cap(base_market_cap)
    is_megacap = base_market_cap >= 200_000_000_000
    accepted: list[str] = []
    rejected: list[str] = []
    unknown_cap: list[str] = []
    unknown_rejected: list[str] = []
    base_clean = base_ticker.split(".")[0].upper()

    for cand in candidates:
        if cand.split(".")[0].upper() == base_clean:
            continue
        peer_cap = _peek_market_cap(cand)
        if peer_cap is None:
            # Dla megacapów brak market cap peera zwykle oznacza słabą jakość
            # porównania (często delisting/niska płynność) — odrzucamy.
            if is_megacap:
                unknown_rejected.append(cand)
            else:
                unknown_cap.append(cand)
            continue

        ratio = peer_cap / base_market_cap
        if min_ratio <= ratio <= max_ratio:
            accepted.append(cand)
        else:
            rejected.append(cand)

    filtered = accepted + unknown_cap
    return filtered, {
        "applied": True,
        "base_market_cap": base_market_cap,
        "min_ratio": min_ratio,
        "max_ratio": max_ratio,
        "accepted": accepted,
        "unknown_cap": unknown_cap,
        "unknown_rejected": unknown_rejected,
        "rejected": rejected,
    }


def get_sector_peers(
    ticker: str,
    financial_data: Optional[dict] = None,
    limit: int = 5,
) -> list[str]:
    """Pobiera listę spółek porównywalnych z tego samego sektora i rynku.

    Strategia dopasowania (od najbardziej do najmniej precyzyjnego):
    1. HYBRID_COMPANY_PEERS — spółki hybrydowe z niestandardowym pozycjonowaniem
       (np. Tesla → pure-play EV zamiast Toyota/Ford, Amazon → e-commerce zamiast retail).
    2. Dopasowanie po branży (industry) — np. "Electronic Gaming & Multimedia"
       daje bardziej trafne peers niż ogólny sektor "Technology".
    3. Fallback na sektor (sector) gdy brak klucza branżowego.

    Dla spółek GPW (.WA) dobiera europejskie/regionalne peers — nie US,
    bo US-owe mnożniki są zawyżone (wyższa płynność, inne standardy rachunkowości).

    Args:
        ticker: Symbol giełdowy spółki bazowej.
        financial_data: Opcjonalne dane finansowe z get_financial_data —
            jeśli podane, używa info z nich zamiast ponownego zapytania yfinance.
        limit: Maksymalna liczba zwracanych tickerów (domyślnie 5).

    Returns:
        Lista tickerów spółek porównywalnych (może być pusta).
    """
    console.print(
        f"[dim]  🔍 Szukam spółek porównywalnych dla {ticker}...[/dim]"
    )

    is_gpw = ticker.upper().endswith(".WA")

    # --- Warstwa 0: HYBRID_COMPANY_PEERS — spółki z niestandardowym pozycjonowaniem ---
    # Normalizacja tickera: usuwamy suffiksy giełdowe (.WA, .PA itp.) i wielkie litery.
    # Sprawdzamy PRZED pobraniem info z yfinance — hybrydowy lookup jest deterministyczny.
    base_ticker_clean = ticker.upper().split(".")[0]
    if base_ticker_clean in HYBRID_COMPANY_PEERS:
        config = HYBRID_COMPANY_PEERS[base_ticker_clean]
        console.print(
            f"[yellow]  ⚠ Hybryda: {config['uwaga']}[/yellow]"
        )
        peers = config["peers"][:limit]
        console.print(
            f"[dim]  ✓ {len(peers)} peers (hybrid): {', '.join(peers)}[/dim]"
        )
        console.print(f"[dim]    Źródło: HYBRID_COMPANY_PEERS ({config['opis']})[/dim]")
        return peers

    try:
        # Użyj info z przekazanych danych jeśli dostępne — oszczędza wywołanie API
        if financial_data is not None:
            info = financial_data.get("info", {})
        else:
            stock = yf.Ticker(ticker)
            info = stock.info or {}

        sector = info.get("sector")
        industry = info.get("industry")
        base_market_cap = _extract_market_cap(info)

        if not sector:
            console.print(
                f"[yellow]  ⚠ Brak informacji o sektorze dla {ticker} "
                f"— nie mogę znaleźć spółek porównywalnych.[/yellow]"
            )
            return []

        # Ticker bazowy bez suffixu rynkowego (np. CDR.WA → CDR, PKN.WA → PKN)
        base_ticker = ticker.split(".")[0].upper()

        if is_gpw:
            # Spółki GPW: peers europejskie/regionalne.
            # Priorytet: branża (industry) → sektor (sector) → fallback GPW.
            # Branże jak gaming mają własne listy peers trafniejsze od ogólnego IT.
            candidates = None
            source = None

            if industry and industry in GPW_INDUSTRY_PEERS:
                # Najdokładniejsze dopasowanie — po branży (industry)
                candidates = GPW_INDUSTRY_PEERS[industry]
                source = f'GPW peers: branża "{industry}" (industry-level)'
            elif industry and industry in GPW_SECTOR_PEERS:
                # Branża pasuje do klucza w GPW_SECTOR_PEERS (np. "Electronic Gaming & Multimedia")
                candidates = GPW_SECTOR_PEERS[industry]
                source = f'GPW peers: branża "{industry}" (sector-level)'
            else:
                matched_key = _match_gpw_sector(sector)
                if matched_key:
                    candidates = GPW_SECTOR_PEERS[matched_key]
                    source = f'GPW peers: sektor "{matched_key}"'

            if candidates is None:
                # Sektor i branża nierozpoznane — fallback na 3 największe spółki GPW
                candidates = GPW_FALLBACK_PEERS
                source = "GPW fallback (3 największe spółki)"
                console.print(
                    f'[dim]  ℹ Sektor "{sector}" / branża "{industry}" '
                    f"nie ma mapowania GPW — używam fallback peers.[/dim]"
                )
        else:
            # Spółki USA: priorytet branża (industry) → sektor (sector)
            if industry and industry in US_INDUSTRY_PEERS:
                candidates = US_INDUSTRY_PEERS[industry]
                source = f'US peers: branża "{industry}" (industry-level)'
            else:
                candidates = US_SECTOR_PEERS.get(sector, [])
                source = f'US peers: sektor "{sector}"'

            # Sanity-check skali: jeśli peers branżowe są wyraźnie mniejsze
            # od spółki bazowej, przełącz się na sektorowe large-caps.
            scale_filtered, scale_meta = _filter_peers_by_scale(
                base_ticker=ticker,
                base_market_cap=base_market_cap,
                candidates=candidates,
            )

            min_required = 2
            if len(scale_filtered) >= min_required:
                rejected_total = len(scale_meta.get("rejected") or []) + len(
                    scale_meta.get("unknown_rejected") or []
                )
                if scale_meta.get("applied") and rejected_total:
                    console.print(
                        f"[dim]  🔎 Filtr skali peers: odrzucono "
                        f"{rejected_total} tickerów jako "
                        f"nieporównywalne wielkością.[/dim]"
                    )
                candidates = scale_filtered
            elif industry and industry in US_INDUSTRY_PEERS and sector in US_SECTOR_PEERS:
                sector_candidates = US_SECTOR_PEERS.get(sector, [])
                sector_filtered, sector_meta = _filter_peers_by_scale(
                    base_ticker=ticker,
                    base_market_cap=base_market_cap,
                    candidates=sector_candidates,
                )
                if len(sector_filtered) >= min_required:
                    candidates = sector_filtered
                    source = f'US peers: sektor "{sector}" (fallback po filtrze skali)'
                    console.print(
                        "[yellow]  ⚠ Branżowe peers były nieporównywalne skalą — "
                        "używam peers sektorowych (large-cap fallback).[/yellow]"
                    )
                    rejected_sector_total = len(sector_meta.get("rejected") or []) + len(
                        sector_meta.get("unknown_rejected") or []
                    )
                    if rejected_sector_total:
                        console.print(
                            f"[dim]    Odrzucono {rejected_sector_total} "
                            "tickerów sektorowych po filtrze skali.[/dim]"
                        )
                else:
                    # Ostateczny fallback: zachowaj oryginalną listę branżową,
                    # ale sygnalizuj niską jakość peers.
                    console.print(
                        "[yellow]  ⚠ Ostrzeżenie: peers są słabo porównywalne "
                        "(duża różnica skali kapitalizacji).[/yellow]"
                    )

        # Odfiltruj samą spółkę (porównanie po tickerze bez suffixu)
        peers = [
            t for t in candidates
            if t.split(".")[0].upper() != base_ticker
        ][:limit]

        if peers:
            market_label = "GPW/Europa" if is_gpw else "USA"
            console.print(
                f"[dim]  ✓ {len(peers)} peers ({market_label}): "
                f"{', '.join(peers)}[/dim]"
            )
            console.print(f"[dim]    Źródło: {source}[/dim]")
            if industry:
                console.print(f"[dim]    Branża: {industry}[/dim]")
        else:
            console.print(
                f'[yellow]  ⚠ Brak peers dla sektora "{sector}" '
                f"— lista porównawcza będzie pusta.[/yellow]"
            )

        return peers

    except Exception as e:
        console.print(
            f"[yellow]  ⚠ Nie udało się pobrać spółek porównywalnych: {e}[/yellow]"
        )
        return []
