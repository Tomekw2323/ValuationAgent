"""CLI entrypoint — parse arguments, run the valuation agent, print the report.

Usage:
    python main.py AAPL
    python main.py CDR.WA --save
    python main.py PKN.WA --verbose --save
    python main.py MSFT --market USA -s -v
"""

import sys
import re
import argparse

# Force UTF-8 on stdout/stderr to avoid UnicodeEncodeError on Windows consoles
# using legacy code pages (cp1250, cp852, etc.).
# Must run before importing rich so Console attaches to UTF-8 streams.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console

console = Console(legacy_windows=False)


def detect_market(ticker: str) -> str:
    """Infer exchange bucket from ticker format.

    - Suffix `.WA` → Warsaw (GPW)
    - 1–5 uppercase letters, no dot → US-style ticker
    - Otherwise unknown
    """
    if ticker.upper().endswith(".WA"):
        return "GPW"
    if re.fullmatch(r"[A-Z]{1,5}", ticker.upper()):
        return "USA"
    return "UNKNOWN"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="GPT-4o agent for intrinsic valuation of listed equities",
        epilog=(
            "Examples:\n"
            "  python main.py AAPL              # Apple (NYSE)\n"
            "  python main.py CDR.WA --save   # CD Projekt (GPW) + save report\n"
            "  python main.py MSFT -s -v      # Microsoft, save + verbose logs\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "ticker",
        type=str,
        help="Ticker, e.g. AAPL (US) or CDR.WA (GPW)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Bypass cache and fetch fresh yfinance data. "
            "Use after earnings or large price moves."
        ),
    )
    parser.add_argument(
        "--save", "-s",
        action="store_true",
        help="Save report as .md under reports/",
    )
    parser.add_argument(
        "--market",
        type=str,
        choices=["GPW", "USA"],
        default=None,
        help="Market override (default: auto-detect from ticker)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logs for GPT-4o traffic",
    )

    return parser.parse_args()


def main() -> None:
    """Run one full valuation for the requested ticker."""
    args = parse_args()

    ticker = args.ticker.upper()

    if args.market:
        market = args.market
    else:
        market = detect_market(ticker)

    if market == "UNKNOWN":
        console.print(
            f'[bold yellow]Could not infer market for ticker "{ticker}".[/bold yellow]\n'
            "[dim]Use AAPL-style (US) or CDR.WA-style (GPW) tickers,\n"
            "or pass --market GPW / --market USA explicitly.[/dim]"
        )
        sys.exit(1)

    if market == "GPW" and not ticker.endswith(".WA"):
        ticker = f"{ticker}.WA"
        console.print(f"[dim]ℹ GPW — normalized ticker to: {ticker}[/dim]")

    console.print()
    console.print("[bold cyan]╔══════════════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║   Valuation Agent AI — Equity Valuation      ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════════════╝[/bold cyan]")
    console.print(f"[dim]  Ticker: {ticker} | Market: {market}[/dim]")

    from config import HAS_LLM_CREDENTIALS
    if not HAS_LLM_CREDENTIALS:
        console.print(
            "[bold red]Error: missing API configuration in .env[/bold red]\n"
            "[dim]Set OPENAI_API_KEY or the AZURE_OPENAI_* variables.[/dim]"
        )
        sys.exit(1)

    from agent.orchestrator import ValuationAgent
    from report.generator import display_report, save_report

    agent = ValuationAgent()

    report_text = agent.run(ticker, verbose=args.verbose, force_refresh=args.fresh)

    if report_text is None:
        console.print(
            f"\n[bold red]✗ Failed to produce a report for {ticker}.[/bold red]"
        )
        sys.exit(1)

    display_report(report_text, ticker)

    if args.save:
        filepath = save_report(report_text, ticker, agent.tool_call_log)
        console.print(f"[dim]  Markdown: {filepath}[/dim]")

        try:
            from report.pdf_generator import generate_pdf
            pdf_path = generate_pdf(report_text, ticker)
            console.print(f"[dim]  PDF: {pdf_path}[/dim]")
        except Exception as e:
            console.print(f"[yellow]  ⚠ PDF unavailable: {e}[/yellow]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"\n[bold red]✗ Unexpected error: {e}[/bold red]")
        sys.exit(1)
