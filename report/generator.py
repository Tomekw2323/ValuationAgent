"""Format and persist valuation reports (Rich terminal + Markdown export)."""

from datetime import datetime

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from config import BASE_DIR

console = Console(legacy_windows=False)

REPORTS_DIR = BASE_DIR / "reports"


def display_report(report_text: str, ticker: str) -> None:
    """Render the GPT-4o Markdown report with Rich styling."""
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    console.print()
    console.print(Rule(style="cyan"))
    console.print(
        Panel(
            f"[bold white]VALUATION REPORT: [yellow]{ticker.upper()}[/yellow][/bold white]\n"
            f"[dim]Generated: {today}[/dim]",
            style="cyan",
            padding=(1, 4),
        )
    )
    console.print(Rule(style="cyan"))
    console.print()

    md = Markdown(report_text)
    console.print(md)

    console.print()
    console.print(Rule(style="cyan"))
    console.print(
        "[dim]Valuation Agent AI · yfinance data · GPT-4o orchestration[/dim]",
        justify="center",
    )
    console.print(Rule(style="cyan"))
    console.print()


def save_report(report_text: str, ticker: str, tool_log: list) -> str:
    """Persist Markdown plus tool-call appendix under reports/."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    safe_ticker = ticker.replace(".", "_").upper()
    filename = f"{safe_ticker}_{today}.md"
    filepath = REPORTS_DIR / filename

    total_calls = len(tool_log)
    successful = sum(1 for t in tool_log if t.get("success"))
    failed = total_calls - successful

    lines: list[str] = []

    lines.append("---")
    lines.append(f"ticker: {ticker}")
    lines.append(f"date: {today}")
    lines.append(f"tool_calls: {total_calls}")
    lines.append(f"tool_calls_ok: {successful}")
    lines.append(f"tool_calls_fail: {failed}")
    lines.append("---")
    lines.append("")

    lines.append(report_text)
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Tool calls")
    lines.append("")

    if tool_log:
        lines.append("| # | Tool | Arguments | OK |")
        lines.append("|---|------|-----------|----|")
        for i, entry in enumerate(tool_log, 1):
            tool_name = entry.get("tool", "?")
            args = entry.get("args", {})
            status = "✓" if entry.get("success") else "✗"

            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            if len(args_str) > 60:
                args_str = args_str[:57] + "..."

            lines.append(f"| {i} | `{tool_name}` | {args_str} | {status} |")
    else:
        lines.append("*No tool calls recorded.*")

    lines.append("")
    lines.append(f"*Saved {today} — Valuation Agent AI*")

    filepath.write_text("\n".join(lines), encoding="utf-8")

    console.print(
        f"\n[bold green]💾 Report saved: [white]{filepath}[/white][/bold green]"
    )

    return str(filepath)
