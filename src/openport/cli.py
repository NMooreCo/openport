"""OpenPort command line."""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import config, tokenfactory

app = typer.Typer(
    help="OpenPort: move your app from closed LLM APIs onto NVIDIA Nemotron, and prove it still works.",
    no_args_is_help=True,
)
console = Console()


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(1)


def _per_million(price: float | None) -> str:
    return f"${price * 1_000_000:.2f}" if price is not None else "?"


@app.command()
def models() -> None:
    """List NVIDIA models on Token Factory and the Nemotron model picked for each tier."""
    try:
        nvidia = tokenfactory.nvidia_models()
    except config.MissingKeyError as error:
        _fail(str(error))

    table = Table(title="NVIDIA models on Nebius Token Factory")
    for column in ("Model", "Context", "$ / 1M input", "$ / 1M output"):
        table.add_column(column)
    for model in sorted(nvidia, key=lambda m: m.id):
        table.add_row(model.id, str(model.context_length or "?"), _per_million(model.prompt_price), _per_million(model.completion_price))
    console.print(table)

    try:
        for tier, model in tokenfactory.tiers().items():
            console.print(f"  {tier:>5} -> [bold]{model}[/bold]")
    except RuntimeError as error:
        console.print(f"[yellow]{error}[/yellow]")


@app.command()
def smoke() -> None:
    """Check what each Nemotron tier supports: JSON modes, tool calling, reasoning on and off."""
    from .smoke import run_model_checks, run_tavily_check

    try:
        checks = run_model_checks()
    except (config.MissingKeyError, RuntimeError) as error:
        _fail(str(error))
    tavily = run_tavily_check()

    tiers = list(dict.fromkeys(check.tier for check in checks))
    table = Table(title="Token Factory smoke tests")
    table.add_column("Check")
    for tier in tiers:
        model = next(check.model for check in checks if check.tier == tier)
        table.add_column(f"{tier}\n[dim]{model}[/dim]")
    for name in dict.fromkeys(check.name for check in checks):
        row = [name]
        for tier in tiers:
            check = next(c for c in checks if c.tier == tier and c.name == name)
            row.append(f"[green]pass[/green] {check.seconds}s" if check.ok else f"[red]FAIL[/red] {check.seconds}s")
        table.add_row(*row)
    console.print(table)

    for check in checks:
        style = "dim" if check.ok else "red"
        console.print(f"[{style}]{check.tier:>5} | {check.name:<16} | {check.detail}[/{style}]")
    if tavily:
        console.print(f"Tavily search: {'[green]pass[/green]' if tavily.ok else '[red]FAIL[/red]'} ({tavily.detail})")
    else:
        console.print("[yellow]Tavily: skipped (TAVILY_API_KEY not set)[/yellow]")

    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RUNS_DIR / f"smoke-{datetime.now():%Y%m%d-%H%M%S}.json"
    records = [asdict(check) for check in checks] + ([asdict(tavily)] if tavily else [])
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"Saved {path}")


@app.command()
def baseline(
    target: Path = typer.Argument(..., help="Target app folder, or its openport.evals.yaml"),
    tiers: str = typer.Option("nano,super,ultra", help="Comma-separated Nemotron tiers to try"),
    concurrency: int = typer.Option(8, help="Cases to run in parallel"),
    limit: int | None = typer.Option(None, help="Only run the first N cases of each call site"),
    judge: bool = typer.Option(True, help="Score free-text outputs with a Nemotron Ultra judge"),
) -> None:
    """Measure a plain swap: only the base URL and model name change. This is the number OpenPort has to beat."""
    from .baseline import run_baseline

    tier_list = [t.strip() for t in tiers.split(",") if t.strip()]
    unknown = set(tier_list) - set(config.TIERS)
    if unknown:
        _fail(f"Unknown tiers: {', '.join(sorted(unknown))}")

    def progress(result):
        errors = ", ".join(f"{name} x{count}" for name, count in result.error_types().most_common(3))
        console.print(
            f"  {result.site_id:<10} {result.tier:<6} {result.passed}/{len(result.cases)} passed"
            + (f"  [red]crashes: {errors}[/red]" if errors else "")
        )

    try:
        results, path = run_baseline(target, tier_list, concurrency=concurrency, limit=limit, use_judge=judge, on_result=progress)
    except (config.MissingKeyError, RuntimeError, FileNotFoundError) as error:
        _fail(str(error))

    table = Table(title="Plain-swap baseline (base URL + model name only)")
    table.add_column("Call site")
    for tier in tier_list:
        table.add_column(tier)
    for site_id in dict.fromkeys(r.site_id for r in results):
        row = [site_id]
        for tier in tier_list:
            r = next(r for r in results if r.site_id == site_id and r.tier == tier)
            color = "green" if r.pass_rate >= 0.9 else "yellow" if r.pass_rate >= 0.6 else "red"
            row.append(f"[{color}]{r.pass_rate:.0%}[/{color}] ({r.passed}/{len(r.cases)})")
        table.add_row(*row)
    totals = ["[bold]all calls[/bold]"]
    for tier in tier_list:
        tier_results = [r for r in results if r.tier == tier]
        passed = sum(r.passed for r in tier_results)
        total = sum(len(r.cases) for r in tier_results)
        cost = sum(r.cost_usd or 0 for r in tier_results)
        totals.append(f"[bold]{passed / total:.0%}[/bold] ({passed}/{total}) ${cost:.4f}")
    table.add_row(*totals)
    console.print(table)
    console.print(f"Saved {path}")


@app.command()
def scan(
    target: str = typer.Argument(..., help="A local folder, or a GitHub repo (URL or owner/name)"),
    include_tests: bool = typer.Option(False, help="Also list calls found in test files"),
) -> None:
    """Find every OpenAI and Anthropic SDK call in a repo, and what each one needs to run on Token Factory."""
    from .scan import scan_repo
    from .workspace import fetch_repo, is_remote

    if is_remote(target):
        root, fresh = fetch_repo(target)
        console.print(f"{'Cloned' if fresh else 'Using the earlier clone of'} {target} in {root} (the code is parsed, never run)")
    else:
        root = Path(target)
        if not root.is_dir():
            _fail(f"No such folder: {target}")

    report = scan_repo(root)
    sites = report.portable(include_tests)
    colors = {"auto": "green", "translate": "yellow", "review": "red"}

    table = Table(title=f"LLM calls in {root.resolve().name}")
    for column in ("Call site", "Where", "SDK / API", "Model", "Output", "Port"):
        table.add_column(column)
    for f in sites:
        color = colors[f.portability]
        table.add_row(
            f.id, f"{f.file}:{f.line}", f"{f.sdk} {f.api}", f.model or f.model_expr or "?", f.output,
            f"[{color}]{f.portability}[/{color}]",
        )
    console.print(table)
    for f in sites:
        if f.notes:
            console.print(f"[bold]{f.id}[/bold]: " + "; ".join(f.notes))

    summary = report.summary(include_tests)
    ports = summary["by_portability"]
    console.print(
        f"\n{summary['call_sites']} LLM calls in {report.files_scanned} files: "
        f"{ports.get('auto', 0)} swap as-is, {ports.get('translate', 0)} need translating, "
        f"{ports.get('review', 0)} need review."
        + (f" ({summary['in_tests']} more in test files; --include-tests to list them.)" if summary["in_tests"] and not include_tests else "")
    )
    if report.skipped:
        console.print(f"[yellow]Couldn't parse {len(report.skipped)} files: {', '.join(report.skipped[:5])}[/yellow]")

    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RUNS_DIR / f"scan-{root.resolve().name}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"Saved {path}")


if __name__ == "__main__":
    app()
