"""Konfiguracja projektu — ładowanie zmiennych środowiskowych, stałe globalne,
parametry domyślne dla modeli wyceny (np. stopa dyskontowa, stopa wzrostu).
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Ścieżka bazowa projektu (katalog w którym leży config.py)
BASE_DIR = Path(__file__).resolve().parent

# Załaduj zmienne środowiskowe z pliku .env
load_dotenv(BASE_DIR / ".env")

# Klucz API OpenAI (publiczne API OpenAI)
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# Konfiguracja Azure OpenAI (Foundry / Azure AI Services)
AZURE_OPENAI_API_KEY: str = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT: str = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_OPENAI_API_VERSION: str = os.getenv(
    "AZURE_OPENAI_API_VERSION",
    "2024-12-01-preview",
)
AZURE_OPENAI_DEPLOYMENT: str = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")

# Nazwa modelu OpenAI używanego przez agenta (dla publicznego API OpenAI)
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

# Tryb dostawcy LLM:
# - Azure OpenAI, jeśli pełna konfiguracja AZURE_* jest obecna
# - inaczej publiczne OpenAI, jeśli jest OPENAI_API_KEY
USE_AZURE_OPENAI: bool = bool(
    AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT
)
HAS_LLM_CREDENTIALS: bool = bool(OPENAI_API_KEY) or USE_AZURE_OPENAI

if USE_AZURE_OPENAI:
    ACTIVE_LLM_PROVIDER: str = "azure"
    MODEL: str = AZURE_OPENAI_DEPLOYMENT
elif OPENAI_API_KEY:
    ACTIVE_LLM_PROVIDER = "openai"
    MODEL = OPENAI_MODEL
else:
    ACTIVE_LLM_PROVIDER = "none"
    MODEL = OPENAI_MODEL

# Maksymalna liczba iteracji pętli agenta (zabezpieczenie przed nieskończonym cyklem)
MAX_ITERATIONS: int = 10

# Folder na cache danych finansowych (względem BASE_DIR)
CACHE_DIR: Path = BASE_DIR / "cache"

# --- Domyślne parametry modeli wyceny ---

# WACC (Weighted Average Cost of Capital) — średni ważony koszt kapitału, stopa dyskontowa dla DCF
DEFAULT_WACC: float = 0.10

# Stopa wzrostu w okresie terminalnym (po okresie szczegółowej prognozy)
DEFAULT_TERMINAL_GROWTH: float = 0.025

# Liczba lat szczegółowej prognozy przepływów pieniężnych w modelu DCF
DEFAULT_PROJECTION_YEARS: int = 5
