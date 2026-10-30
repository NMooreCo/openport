"""OpenPort command line."""

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape as _plain  # repo text: app/[locale]/page.tsx isn't markup
from rich.table import Table

from . import config, tokenfactory

# Windows consoles default to cp1252, which can't encode characters that turn up in model output
# (non-breaking hyphens, smart quotes, box drawing); writing them would crash a run mid-stream.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

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
            console.print(f"  {tier:>9} -> [bold]{model}[/bold]")
    except RuntimeError as error:
        console.print(f"[yellow]{error}[/yellow]")


@app.command()
def smoke() -> None:
    """Check what each Nemotron tier supports: JSON modes, tool calling, reasoning on and off, images, sampling settings and the Responses API."""
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
        console.print(f"[{style}]{check.tier:>9} | {check.name:<17} | {check.detail}[/{style}]")
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
    tiers: str = typer.Option("nano,super,ultra", help="Comma-separated Nemotron tiers to try (also lightning; calls that send images use vision)"),
    concurrency: int = typer.Option(8, help="Cases to run in parallel"),
    limit: int | None = typer.Option(None, help="Only run the first N cases of each call site"),
    judge: bool = typer.Option(True, help="Score free-text outputs with a Nemotron Ultra judge"),
) -> None:
    """Measure a plain swap: only the base URL and model name change. This is the number OpenPort has to beat."""
    from .baseline import run_baseline

    tier_list = [t.strip() for t in tiers.split(",") if t.strip()]
    unknown = set(tier_list) - set(config.TIERS) - set(config.EXTRA_TIERS)
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
    include_tests: bool = typer.Option(False, help="Also list calls found in tests and in documentation's code samples"),
) -> None:
    """Find every OpenAI and Anthropic call in a repo (SDK or plain HTTP), and what each one needs to run on Token Factory."""
    from .scan import scan_repo

    root = _target_root(target)
    report = scan_repo(root)
    sites = report.portable(include_tests)
    colors = {"auto": "green", "translate": "yellow", "review": "red"}

    table = Table(title=f"LLM calls in {root.resolve().name}")
    for column in ("Call site", "Where", "SDK / API", "Model", "Output", "Port"):
        table.add_column(column, overflow="fold", no_wrap=column == "Call site")  # IDs whole: --assign needs them
    for f in sites:
        color = colors[f.portability]
        table.add_row(
            _plain(f.id), _plain(f"{f.file}:{f.line}"), f"{f.sdk} {f.api}", _plain(f.model or f.model_expr or "?"), f.output,
            f"[{color}]{f.portability}[/{color}]",
        )
    console.print(table)
    for f in sites:
        if f.notes:
            console.print(f"[bold]{_plain(f.id)}[/bold]: " + _plain("; ".join(f.notes)))

    summary = report.summary(include_tests)
    ports = summary["by_portability"]
    console.print(
        f"\n{summary['call_sites']} LLM calls in {report.files_scanned} files: "
        f"{ports.get('auto', 0)} swap as-is, {ports.get('translate', 0)} need translating, "
        f"{ports.get('review', 0)} need review."
        + (f" ({summary['in_tests']} more in tests and docs; --include-tests to list them.)" if summary["in_tests"] and not include_tests else "")
    )
    if report.skipped:
        console.print(f"[yellow]Couldn't parse {len(report.skipped)} files: {', '.join(report.skipped[:5])}[/yellow]")

    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RUNS_DIR / f"scan-{root.resolve().name}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"Saved {path}")


@app.command()
def capture(
    target: str = typer.Argument(..., help="A local folder, or a GitHub repo (URL or owner/name)"),
    logs: list[str] | None = typer.Option(None, "--logs", help="Recorded calls for a site: ID=PATH to a JSONL of {input, output} lines (repeatable)"),
    generate: int = typer.Option(0, help="With no logs, have Super invent N inputs per call and label them (needs NEBIUS_API_KEY)"),
    label_with: str = typer.Option("ultra", "--label-with", help="Label generated cases with 'ultra' (Nemotron Ultra) or 'original' (run the app through its original model; needs that provider's key)"),
    out: Path | None = typer.Option(None, help="Where to write the eval spec (default: runs/captures/<name>)"),
    include_tests: bool = typer.Option(False, help="Also capture calls found in test files"),
) -> None:
    """Draft a starter eval spec from a repo: a call site, grader and dataset per LLM call, ready for `openport tune`."""
    from .capture import capture_repo, write_capture
    from .scan import scan_repo

    if label_with not in ("ultra", "original"):
        _fail("--label-with must be 'ultra' or 'original'")

    root = _target_root(target)
    report = scan_repo(root)

    log_map: dict[str, Path] = {}
    for item in logs or []:
        site_id, sep, path = item.partition("=")
        if not sep or not path:
            _fail(f"--logs wants ID=PATH, got {item!r}")
        file = Path(path)
        if not file.is_file():
            _fail(f"No such logs file: {path}")
        log_map[site_id] = file
    unknown = set(log_map) - {f.id for f in report.findings}
    if unknown:
        _fail(f"No call site called {', '.join(sorted(unknown))}. Run `openport scan` to list them.")

    def progress(site):
        console.print(f"  {site.spec['id']:<26} {len(site.cases)} case(s) from {site.source}")

    try:
        result = capture_repo(root, report, logs=log_map, generate=generate, label=label_with, include_tests=include_tests, on_site=progress)
    except (config.MissingKeyError, RuntimeError) as error:
        _fail(str(error))
    if not result.sites and not result.skipped:
        _fail("No LLM calls found to capture.")

    out_dir = out or config.RUNS_DIR / "captures" / root.resolve().name
    paths = write_capture(result, out_dir)

    table = Table(title=f"Captured eval spec for {result.target}")
    for column in ("Call site", "SDK / output", "Grader", "Cases", "From"):
        table.add_column(column)
    for site in result.sites:
        grader = site.spec["grader"]
        detail = grader["type"] + (f" ({len(grader['fields'])})" if grader["type"] == "fields" else "")
        color = "green" if site.cases else "yellow"
        table.add_row(
            _plain(site.spec["id"]), f"{site.finding.sdk} {site.finding.output}", detail,
            f"[{color}]{len(site.cases)}[/{color}]", site.source,
        )
    console.print(table)
    if result.helper_cost_usd is not None:
        console.print(f"OpenPort's own cost: ${result.helper_cost_usd:.4f}")
    if result.skipped:
        console.print(f"[yellow]Skipped {len(result.skipped)} call(s) the harness can't run yet (see the notes).[/yellow]")
    console.print(f"Spec: {paths['spec']}")
    console.print(f"Notes: {paths['notes']}")
    console.print("Review the spec, then run: openport tune " + str(Path(out_dir)))


@app.command()
def tune(
    target: Path = typer.Argument(..., help="Target app folder, or its openport.evals.yaml"),
    sites: str | None = typer.Option(None, help="Comma-separated eval call-site IDs (default: all)"),
    target_rate: float = typer.Option(0.9, "--target", help="Pass rate a call must reach on both tuning and held-out cases"),
    rounds: int = typer.Option(3, help="Attempts per tier before moving up"),
    tiers: str = typer.Option("nano,super,ultra", help="Comma-separated tiers, cheapest first (also lightning; calls that send images use vision)"),
    concurrency: int = typer.Option(8, help="Cases to run in parallel"),
    limit: int | None = typer.Option(None, help="Only use the first N cases of each call site"),
    judge: bool = typer.Option(True, help="Score free-text outputs with a Nemotron Ultra judge"),
    research: bool = typer.Option(True, help="Look up NVIDIA's guidance for each model with Tavily before proposing a fix (needs TAVILY_API_KEY)"),
    model: list[str] | None = typer.Option(None, "--model", help="Add or override a tier: NAME=MODEL_ID or NAME=MODEL_ID@BASE_URL (repeatable), e.g. a fine-tuned student; list it in --tiers"),
    jobs: bool = typer.Option(False, "--jobs", help="Tune each call site in its own Nebius Serverless Job, all at once (needs the nebius CLI)"),
    job_image: str | None = typer.Option(None, envvar="OPENPORT_JOB_IMAGE", help="With --jobs: the container image to run (built from the Dockerfile)"),
    job_platform: str | None = typer.Option(None, envvar="OPENPORT_JOB_PLATFORM", help="With --jobs: a CPU platform, from `nebius compute platform list`"),
    job_preset: str | None = typer.Option(None, envvar="OPENPORT_JOB_PRESET", help="With --jobs: a preset of that platform"),
    job_secret: list[str] | None = typer.Option(None, "--job-secret", help="With --jobs: ENV=MYSTERYBOX_SECRET for each key the jobs need (repeatable; or OPENPORT_JOB_SECRETS=ENV=SECRET,...)"),
    job_registry_secret: str | None = typer.Option(None, envvar="OPENPORT_JOB_REGISTRY_SECRET", help="With --jobs: a MysteryBox secret with registry credentials, if the image is private"),
) -> None:
    """Find the cheapest Nemotron model, and the smallest prompt or setting change, that makes each call pass."""
    from .tune import tune_spec

    extra: dict[str, tuple[str, str | None]] = {}
    for item in model or []:
        name, sep, value = item.partition("=")
        model_id, _, base_url = value.partition("@")
        if not sep or not name.strip() or not model_id.strip():
            _fail(f"--model wants NAME=MODEL_ID[@BASE_URL], got {item!r}")
        extra[name.strip()] = (model_id.strip(), base_url.strip() or None)
    tier_list = tuple(t.strip() for t in tiers.split(",") if t.strip())
    if set(tier_list) - set(config.TIERS) - set(config.EXTRA_TIERS) - set(extra):
        _fail(f"Unknown tiers in {tiers!r}; add a tier with --model NAME=MODEL_ID")

    if jobs:
        tune_flags = ["--target", str(target_rate), "--rounds", str(rounds), "--tiers", ",".join(tier_list), "--concurrency", str(concurrency)]
        tune_flags += (["--limit", str(limit)] if limit else []) + ([] if judge else ["--no-judge"]) + ([] if research else ["--no-research"])
        tune_flags += [flag for item in model or [] for flag in ("--model", item)]
        secrets = job_secret or [s for s in os.getenv("OPENPORT_JOB_SECRETS", "").split(",") if s.strip()]
        _tune_on_jobs(target, sites, tune_flags, job_image, job_platform, job_preset, secrets, job_registry_secret)
        return

    def progress(site, attempt):
        held = "" if attempt.holdout_pass is None else f", held-out {attempt.holdout_pass:.0%}"
        mark = " [green]passes[/green]" if attempt.meets(target_rate) else ""
        console.print(
            f"  {site.id:<10} {attempt.tier:<6} round {attempt.round}  {attempt.variant.describe():<44} "
            f"tuning {attempt.tune_pass:.0%}{held}{mark}"
        )

    def found_guidance(site, guidance):
        console.print(f"  {site.id:<10} [cyan]research[/cyan] {guidance.query} -> {len(guidance.sources)} NVIDIA page(s)")

    try:
        results, totals, report_path, plan_path = tune_spec(
            target, site_ids=sites.split(",") if sites else None, target=target_rate, rounds=rounds, tiers=tier_list,
            concurrency=concurrency, limit=limit, use_judge=judge, use_research=research,
            on_attempt=progress, on_research=found_guidance, extra_models=extra,
        )
    except (config.MissingKeyError, RuntimeError, FileNotFoundError) as error:
        _fail(str(error))

    table = Table(title=f"Tuning results (target {target_rate:.0%} on tuning and held-out cases)")
    for column in ("Call site", "Plain Nano", "Chosen", "Pass rate", "Held-out", "Change", "Eval cost"):
        table.add_column(column)
    for r in results:
        base = f"{r.baseline.pass_rate:.0%}" if r.baseline else "?"
        if r.chosen:
            c = r.chosen
            held = "-" if c.holdout_pass is None else f"{c.holdout_pass:.0%}"
            table.add_row(r.site_id, base, f"[green]{c.tier}[/green]", f"{c.pass_rate:.0%}", held, c.variant.describe(), f"${r.cost_usd:.4f}")
        else:
            best = r.best
            table.add_row(r.site_id, base, "[yellow]none[/yellow]", f"best {best.pass_rate:.0%}" if best else "?", "-", "-", f"${r.cost_usd:.4f}")
    console.print(table)
    eval_cost = sum(r.cost_usd for r in results)
    console.print(f"OpenPort's own cost: ${eval_cost + totals['helper_cost_usd']:.4f} (evals ${eval_cost:.4f}, engineer + judge ${totals['helper_cost_usd']:.4f})")
    console.print(f"Report: {report_path}\nPlan for `openport port --plan`: {plan_path}")


def _tune_on_jobs(target: Path, sites: str | None, tune_flags: list[str], image, platform, preset, secret_specs: list[str], registry_secret) -> None:
    from .jobs import JobSettings, nebius_cli, tune_on_jobs

    if shutil.which(nebius_cli()[0]) is None:
        _fail("--jobs needs the nebius CLI, signed in to your Nebius AI Cloud project: https://docs.nebius.com/cli/install "
              "(on Windows, install it in WSL and set OPENPORT_NEBIUS_CLI='wsl -e /home/<you>/.nebius/bin/nebius')")
    secrets: dict[str, str] = {}
    for item in secret_specs:
        env, sep, selector = item.partition("=")
        if not sep or not env.strip() or not selector.strip():
            _fail(f"--job-secret wants ENV=MYSTERYBOX_SECRET, got {item!r}")
        secrets[env.strip()] = selector.strip()
    settings = JobSettings(image=image or "", platform=platform or "", preset=preset or "", secrets=secrets, registry_secret=registry_secret)

    def event(kind, job):
        if kind == "submitted":
            console.print(f"  {job.site:<10} [cyan]submitted[/cyan] {job.job_id}")
        else:
            color = "green" if job.state == "COMPLETED" else "red" if job.state in ("FAILED", "ERROR", "CANCELLED") else "yellow"
            console.print(f"  {job.site:<10} [{color}]{job.state.lower()}[/{color}] {job.detail}")

    try:
        jobs, report_path, plan_path = tune_on_jobs(target, settings, site_ids=sites.split(",") if sites else None, tune_flags=tune_flags, on_event=event)
    except subprocess.CalledProcessError as error:
        _fail(f"nebius CLI failed ({' '.join(error.cmd[:4])}): {(error.stderr or '').strip()[:500]}")
    except (ValueError, RuntimeError, TimeoutError, FileNotFoundError) as error:
        _fail(str(error))

    report = json.loads(report_path.read_text(encoding="utf-8"))
    table = Table(title=f"Tuning on Nebius Serverless Jobs ({len(jobs)} jobs in parallel)")
    for column in ("Call site", "Job", "State", "Chosen", "Pass rate", "Held-out"):
        table.add_column(column)
    for job in jobs:
        result = next((r for r in report["results"] if r["site_id"] == job.site), None)
        chosen = (result or {}).get("chosen")
        if chosen:
            held = "-" if chosen["holdout_pass"] is None else f"{chosen['holdout_pass']:.0%}"
            row = [f"[green]{chosen['tier']}[/green]", f"{chosen['passed'] / chosen['total']:.0%}", held]
        else:
            row = ["[yellow]none[/yellow]" if result else "-", "-", "-"]
        table.add_row(job.site, job.job_id, job.state.lower(), *row)
    console.print(table)
    eval_cost = sum(a.get("cost_usd") or 0 for r in report["results"] for a in r["attempts"])
    console.print(f"OpenPort's own cost: ${eval_cost + report['helper_cost_usd']:.4f} (evals ${eval_cost:.4f}, engineer + judge ${report['helper_cost_usd']:.4f})")
    console.print(f"Report: {report_path}\nPlan for `openport port --plan`: {plan_path}")


@app.command()
def distill(
    target: Path = typer.Argument(..., help="Target app folder, or its openport.evals.yaml"),
    site: str = typer.Option(..., "--site", help="Eval call-site ID to build a training set for"),
    examples: int = typer.Option(200, help="How many new inputs to invent (fewer are kept: only answers that pass the call's checks)"),
    teacher: str = typer.Option("ultra", help="Tier that writes the answers"),
    tune_report: Path | None = typer.Option(None, "--tune", help="Tuning report whose best settings the teacher uses (default: the latest)"),
    concurrency: int = typer.Option(8, help="Inputs to answer in parallel"),
) -> None:
    """Build a fine-tuning set for one call from Nemotron Ultra's answers to new inputs, keeping only answers that pass."""
    from . import report as reports
    from .distill import distill_site
    from .evals.spec import load_spec

    if teacher not in config.TIERS:
        _fail(f"Unknown tier {teacher!r}")
    try:
        spec = load_spec(target)
        report_path = tune_report or reports.latest_tune_report(spec.target)
        result, path = distill_site(
            target, site, examples=examples, teacher_tier=teacher, tune_report=reports.load_tune_report(report_path),
            concurrency=concurrency, on_progress=lambda stage, count: console.print(f"  Super invented {count} new inputs"),
        )
    except (config.MissingKeyError, RuntimeError, FileNotFoundError, KeyError, ValueError) as error:
        _fail(str(error))
    console.print(f"Teacher: {result.teacher} ({_describe_variant(result.variant)}), using {report_path or 'no tuning report'}")
    console.print(f"Kept {result.kept} of {result.invented} answers" + (f"; rejected: {', '.join(f'{r} x{n}' for r, n in result.rejected.items())}" if result.rejected else ""))
    if result.cost_usd is not None:
        console.print(f"Cost: ${result.cost_usd:.4f}")
    console.print(f"Training set: {path}")


@app.command()
def finetune(
    dataset: Path | None = typer.Argument(None, help="A training set from `openport distill` (omit with --job)"),
    base: str = typer.Option("meta-llama/Llama-3.1-8B-Instruct", help="Base model for the LoRA fine-tune"),
    suffix: str = typer.Option("openport", help="Suffix for the fine-tuned model's name"),
    wait: bool = typer.Option(True, help="Follow the job until it finishes, then download the adapter"),
    job: str | None = typer.Option(None, "--job", help="Follow an existing job instead of starting one"),
) -> None:
    """Fine-tune a smaller model on Token Factory from a distilled training set (LoRA), and download the adapter."""
    from openai import APIError

    from . import finetune as ft

    tune = None
    try:
        if job:
            saved = config.RUNS_DIR / "finetunes" / f"{job}.json"
            tune = ft.FineTune(**json.loads(saved.read_text(encoding="utf-8"))) if saved.exists() else ft.FineTune(job, base, "unknown", "", None, {})
        else:
            if dataset is None:
                _fail("Give a training set, or --job to follow an existing job")
            tune = ft.start(dataset, base=base, suffix=suffix)
            console.print(f"Started fine-tuning job {tune.job_id}: {base} (LoRA), {tune.examples['train']} training and "
                          f"{tune.examples['validation']} validation examples")
        ft.save(tune)
        if not wait:
            console.print(f"Follow it later: openport finetune --job {tune.job_id}")
            return

        def status(state, messages):
            for message in messages:
                console.print(f"  {message}")
            console.print(f"  [dim]status: {state}[/dim]")

        tune = ft.follow(tune, on_status=status)
        ft.save(tune)
        if tune.status == "succeeded":
            tune = ft.download_adapter(tune)
            ft.save(tune)
    except (config.MissingKeyError, ValueError, RuntimeError, TimeoutError) as error:
        _fail(str(error))
    except APIError as error:
        if tune and tune.status == "succeeded":
            hint = f"\nThe fine-tune succeeded; download its adapter again with: openport finetune --job {tune.job_id}"
        elif tune:
            hint = f"\nThe job carries on; follow it again with: openport finetune --job {tune.job_id}"
        else:
            hint = ""
        _fail(f"Token Factory: {error}{hint}")
    if tune.status != "succeeded":
        _fail(f"Fine-tuning {tune.status}: {tune.error or 'see the events above'}")
    console.print(f"[green]Done.[/green] Model: {tune.fine_tuned_model or '(see the job)'}; trained on {tune.trained_tokens or '?'} tokens")
    for path in tune.adapter_files:
        console.print(f"  adapter file: {path}")


def _describe_variant(variant: dict) -> str:
    from .tune.variant import Variant

    return Variant.from_dict(variant).describe()


@app.command()
def pr(
    ported: Path = typer.Argument(..., help="A ported copy, e.g. runs/ports/<name>"),
    tune_report: Path | None = typer.Option(None, "--tune", help="Tuning report (default: the latest one for this target)"),
    price: list[str] | None = typer.Option(None, "--price", help="Price by hand: MODEL=INPUT/OUTPUT in USD per 1M tokens (repeatable)"),
    open_pr: bool = typer.Option(False, "--open", help="Commit, push to your fork and open the PR there"),
    upstream: bool = typer.Option(False, help="With --open: propose the PR to the original repo instead of your fork"),
) -> None:
    """Write the PR description for a port. With --open, push the branch to your fork and open the PR."""
    from . import pricing, report
    from .github import BRANCH

    if not (ported / report.SUMMARY_FILE).exists():
        _fail(f"{ported} has no {report.SUMMARY_FILE}; run `openport port` first")
    folder = ported.resolve().name
    name = folder.replace("__", "/")  # clones are stored as owner__name
    tune_path = tune_report or report.latest_tune_report(folder.split("__")[-1])
    tune = report.load_tune_report(tune_path)
    manual = {p.model: p for p in map(pricing.parse_manual, price or [])}
    missing: list[str] = []

    def price_of(model: str):
        found = pricing.price_for(model, manual)
        if found is None:
            missing.append(model)
        return found

    rows = report.build_rows(report.load_summary(ported), tune, price_of)
    body = report.render(name, rows, tune)
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    body_path = (config.RUNS_DIR / f"pr-{folder}.md").resolve()
    body_path.write_text(body, encoding="utf-8")
    console.print(f"PR description: {body_path}")
    console.print(f"Tuning report: {tune_path or 'none (the PR says the calls are untuned)'}")
    if missing:
        console.print(f"[yellow]No price for {', '.join(sorted(set(missing)))}: pass --price MODEL=IN/OUT, or set TAVILY_API_KEY and NEBIUS_API_KEY[/yellow]")
    _open_or_show_pr(ported, report.TITLE, body_path, open_pr, upstream, BRANCH)


def _open_or_show_pr(folder: Path, title: str, body_path: Path, open_pr: bool, upstream: bool, branch: str) -> None:
    """With --open, commit the folder on `branch`, push it to your fork and open the PR; else show the commands."""
    from .github import execute, github_login, plan_pr

    try:
        login = github_login()
    except (OSError, subprocess.CalledProcessError):
        login = "<your GitHub login>"
    try:
        plan = plan_pr(folder, title, body_path, login, upstream, branch)
    except (ValueError, subprocess.CalledProcessError) as error:
        if open_pr:
            _fail(f"Can't open a PR from {folder}: {error}")
        console.print(f"[yellow]Can't open a PR from {folder} yet: {error}[/yellow]")
        return
    if not open_pr:
        where = f"your fork {plan.fork}" if plan.base_repo == plan.fork else f"{plan.base_repo} (upstream)"
        console.print(f"Dry run. With --open, OpenPort would open the PR in {where} by running, in {folder}:")
        for command in plan.commands:
            console.print("  " + " ".join(f'"{part}"' if " " in part else part for part in command))
        return
    url = execute(plan, folder)
    console.print(f"Opened {url}")


@app.command()
def check(
    target: Path = typer.Argument(Path("."), help="The repo to check (it needs an openport.evals.yaml)"),
    min_pass_rate: float | None = typer.Option(None, "--min", help="Pass rate every call must reach (default: from openport.yaml, else 0.9)"),
    sites: str | None = typer.Option(None, help="Comma-separated eval call-site IDs (default: all)"),
    concurrency: int = typer.Option(8, help="Cases to run in parallel"),
    judge: bool = typer.Option(True, help="Score free-text outputs with a Nemotron Ultra judge"),
) -> None:
    """Quality gate: run each LLM call's evals on the model its code names now. Exits 1 if any call falls short."""
    from .check import run_gate

    try:
        results = run_gate(target, min_pass_rate=min_pass_rate, sites=sites.split(",") if sites else None, concurrency=concurrency, use_judge=judge)
    except (config.MissingKeyError, RuntimeError, FileNotFoundError, KeyError) as error:
        _fail(str(error))

    table = Table(title="OpenPort quality gate")
    for column in ("Call site", "Model", "Pass rate", "Needs", "Result"):
        table.add_column(column)
    for result in results:
        site = result.site
        verdict = "[green]pass[/green]" if result.ok else "[red]FAIL[/red]"
        table.add_row(site.title, site.model, f"{site.pass_rate:.0%} ({site.passed}/{len(site.cases)})", f"{result.required:.0%}", verdict)
    console.print(table)
    for result in results:
        if not result.ok:
            failed = [c for c in result.site.cases if not c.passed]
            for case in failed[:5]:
                console.print(f"[red]  {case.case_id}: {case.error or '; '.join(case.failures)}[/red]")
    if not all(r.ok for r in results):
        raise typer.Exit(1)


@app.command()
def upgrade(
    target: str = typer.Argument(..., help="A local folder, or a GitHub repo (URL or owner/name)"),
    min_age: int = typer.Option(7, "--min-age", help="Only propose a fix version at least this many days old"),
    apply: bool = typer.Option(False, "--apply", help="Upgrade a copy, run the repo's tests before and after in Docker, and have Super fix what breaks"),
    rounds: int = typer.Option(3, help="With --apply: fix attempts before giving up"),
    test_image: str | None = typer.Option(None, help="With --apply: container image for the tests (default: Python or Node, from the project)"),
    test_command: str | None = typer.Option(None, help="With --apply: shell command that installs and runs the tests (default: pip, uv, Poetry, npm, pnpm, Yarn or bun)"),
    out: Path | None = typer.Option(None, help="With --apply: where to write the upgraded copy (default: runs/upgrades/<name>)"),
    open_pr: bool = typer.Option(False, "--open", help="With --apply: if the tests prove the upgrade, push it to your fork and open the PR there"),
    upstream: bool = typer.Option(False, help="With --open: propose the PR to the original repo instead of your fork"),
    project: str | None = typer.Option(None, "--project", help="In a monorepo: the project to upgrade, as its folder in the repo (its own lockfiles and tests)"),
) -> None:
    """Find vulnerable dependencies and the safe version that fixes each (OSV.dev); with --apply, upgrade and prove it."""
    from .upgrade import plan_upgrades, read_lockfiles

    from .workspace import split_project

    if open_pr and not apply:
        _fail("--open needs --apply: a PR is opened only for an upgrade the tests prove")
    target, folder = split_project(target)  # https://github.com/owner/name/tree/main/apps/api: the apps/api project
    if folder and project and Path(project).as_posix().strip("/") != folder:
        _fail(f"The URL names the project {folder}, and --project names {project}: give one")
    root = _target_root(target)
    project = Path(project or folder).as_posix().strip("/") if project or folder else ""
    if project and not (root / project).is_dir():
        _fail(f"No folder {project} in {root}")
    deps = read_lockfiles(root / project)
    if not deps:
        _fail("No lockfiles found (requirements*.txt, uv.lock, poetry.lock, package-lock.json, pnpm-lock.yaml, yarn.lock, bun.lock).")
    console.print(f"Read {len(deps)} dependencies. Checking OSV.dev...")
    try:
        plans = plan_upgrades(deps, min_age_days=min_age)
    except Exception as error:  # OSV or a registry being unreachable shouldn't look like a crash
        _fail(f"Advisory lookup failed: {type(error).__name__}: {error}")
    if not plans:
        console.print(f"[green]No known vulnerabilities in {len(deps)} dependencies.[/green]")
        return

    colors = {"upgrade": "green", "partial": "green", "held: fix too new": "yellow", "held: no release dates": "yellow", "no fix available": "red"}
    table = Table(title=f"Vulnerable dependencies in {root.resolve().name}" + (f"/{project}" if project else ""))
    for column in ("Package", "Installed", "Fix", "Advisories", "Status", "Locked in"):
        table.add_column(column)
    for up in plans:
        fix = f"[green]{up.target.version}[/green] ({up.target.age_days}d old)" if up.target else "—"
        color = colors.get(up.status, "white")
        ids = up.advisory_ids
        table.add_row(_plain(up.dep.name), _plain(up.dep.version), fix, ", ".join(ids[:4]) + (f" +{len(ids) - 4}" if len(ids) > 4 else ""),
                      f"[{color}]{up.status}[/{color}]", _plain(", ".join((up.dep.source, *up.dep.also))))
    console.print(table)

    upgradable = sum(up.target is not None for up in plans)
    console.print(f"\n{len(plans)} vulnerable of {len(deps)} dependencies: {upgradable} have a safe fix.")
    projects = Counter(Path(project, up.dep.source).parent.as_posix() for up in plans if up.target and "/" in up.dep.source)
    for folder, count in sorted(projects.items()):  # a monorepo: --apply upgrades one project at a time
        console.print(f"{count} of them locked in {folder}/: upgrade that project with --project {folder}")
    if not apply:
        console.print("Dry run. With --apply, OpenPort upgrades a copy, runs the tests before and after, and fixes what breaks.")
        return
    _apply_upgrades(root, plans, rounds, test_image, test_command, out, min_age, open_pr, upstream, project)


def _apply_upgrades(root: Path, plans, rounds: int, test_image, test_command, out, min_age: int, open_pr: bool, upstream: bool,
                    project: str = "") -> None:
    from .github import UPGRADE_BRANCH
    from .upgrade import report as upgrade_report
    from .upgrade.apply import changed_files, make_pr_folder
    from .upgrade.changelog import changelog_digest
    from .upgrade.fix import make_fixer
    from .upgrade.research import research_upgrade
    from .upgrade.run import in_project, upgrade_repo
    from .upgrade.testrun import run_suites, test_setups

    if shutil.which("docker") is None:
        _fail("--apply runs the repo's tests in a Docker container; install Docker first.")
    try:
        fixer = make_fixer()
    except config.MissingKeyError:
        fixer = None
        console.print("[yellow]No NEBIUS_API_KEY: OpenPort will bump and test, but can't fix code the upgrade breaks.[/yellow]")
    researcher = research_upgrade if os.getenv("TAVILY_API_KEY") else None
    changelog = changelog_digest if os.getenv("TAVILY_API_KEY") else None

    def show(test_run):
        return f"[{'green' if test_run.ok else 'red'}]{'pass' if test_run.ok else 'FAIL'}[/] {test_run.summary} ({test_run.seconds}s)"

    def event(kind, data):
        if kind == "before":
            console.print(f"  Tests before the upgrade: {show(data)}")
        elif kind == "after_bump":
            console.print(f"  Tests after the version bump: {show(data)}")
        elif kind == "changelogs":
            for found in data:
                console.print(f"  [cyan]Tavily[/cyan]: {found.name}'s changelog, {len(found.releases)} release(s) in between, "
                              f"{len(found.quotes)} line(s) for the PR")
        elif kind == "research":
            console.print(f"  [cyan]Tavily[/cyan]: {len(data)} page(s) on what changed")
        elif kind == "round":
            for edit, why in data.rejected:
                console.print(f"  [yellow]Rejected Super's edit to {edit.file}: {why}[/yellow]")
            if data.edits:
                files = ", ".join(sorted({e.file for e in data.edits}))
                console.print(f"  Super changed {files}: {data.rationale}\n  Tests after the fix: {show(data.tests)}")

    name = root.resolve().name + (f"-{project.replace('/', '-')}" if project else "")
    out_dir = out or config.RUNS_DIR / "upgrades" / name
    console.print(f"\nUpgrading a copy in {out_dir} (the original is untouched)...")
    ecosystems = {p.dep.ecosystem for p in plans if p.target}

    def tests(folder: Path):  # a monorepo's project: the whole repo is mounted, and its suites run in the project's folder
        setups = test_setups(folder / project, ecosystems)
        if test_image or test_command:  # your own suite instead of the detected ones
            setups = [("tests", test_image or setups[0][1], test_command or setups[0][2])]
        return run_suites(folder, [(suite, image, in_project(project, command)) for suite, image, command in setups])

    try:
        run = upgrade_repo(root, out_dir, plans=plans, project=project, min_age_days=min_age, rounds=rounds, researcher=researcher,
                           changelog=changelog, fixer=fixer, on_event=event, run_tests=tests)
    except (FileExistsError, ValueError) as error:  # e.g. an --out folder OpenPort didn't make, which it won't empty
        _fail(str(error))
    if not run.upgraded:  # not an error: a weekly run with nothing safe to do yet just says why
        console.print("Nothing to upgrade here: " + "; ".join(f"{up.dep.name}: {why}" for up, why in run.skipped))
        return
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    diff_path = config.RUNS_DIR / f"upgrade-{name}.diff"
    diff_path.write_text(run.diff, encoding="utf-8")
    body_path = config.RUNS_DIR / f"pr-upgrade-{name}.md"
    body_path.write_text(upgrade_report.render(run, min_age), encoding="utf-8")
    verdict = "[green]Proven: the tests pass before and after the upgrade.[/green]" if run.proven else "[red]Not proven: see the test results.[/red]"
    console.print(f"\n{verdict}\nDiff: {diff_path}\nPR description: {body_path}")
    if run.proven:  # the PR comes from the original's .git plus the changed files, never from the tested copy
        try:
            pr_folder = make_pr_folder(root, out_dir, changed_files(root, out_dir, run.created), out_dir.with_name(out_dir.name + ".pr"))
        except (FileExistsError, ValueError) as error:
            _fail(str(error))
        title, branch = upgrade_report.TITLE, UPGRADE_BRANCH
        if project:  # each project of a monorepo gets its own PR
            title, branch = f"{title} in {project}", f"{branch}-{project.replace('/', '-')}"
        _open_or_show_pr(pr_folder.resolve(), title, body_path.resolve(), open_pr, upstream, branch)
    elif open_pr:
        _fail("Not opening a PR: the tests don't prove this upgrade.")


@app.command("upgrade-action")
def upgrade_action(
    folder: Path = typer.Argument(Path("."), help="The repo to add the workflow to"),
    project: str | None = typer.Option(None, "--project", help="In a monorepo: the project the workflow upgrades (one workflow each)"),
) -> None:
    """Add a GitHub Actions workflow that runs `openport upgrade --apply --open` every Monday."""
    from .upgrade.action import workflow, workflow_file

    project = Path(project).as_posix().strip("/") if project else ""
    if project and not (folder / project).is_dir():
        _fail(f"No folder {project} in {folder}")
    path = folder / workflow_file(project)
    if path.exists():
        _fail(f"{path} already exists; delete it first to write a fresh one")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(workflow(config.openport_source(), project).encode("utf-8"))
    console.print(f"Wrote {path}. Commit it, and add a NEBIUS_API_KEY repository secret (TAVILY_API_KEY too, for the changelog quotes).")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to listen on"),
    port: int = typer.Option(8000, help="Port to listen on"),
) -> None:
    """Run the dashboard: port a repo to Nemotron or upgrade its vulnerable dependencies, and watch each step live."""
    import uvicorn

    uvicorn.run("openport.server:app", host=host, port=port)


def _target_root(target: str) -> Path:
    from .workspace import fetch_repo, is_remote

    if is_remote(target):
        try:
            root, fresh = fetch_repo(target)
        except (ValueError, RuntimeError) as error:
            _fail(str(error))
        console.print(f"{'Cloned' if fresh else 'Using the earlier clone of'} {target} in {root}")
        return root
    root = Path(target)
    if not root.is_dir():
        _fail(f"No such folder: {target}")
    return root


def _tier_models(models: str | None) -> dict[str, str]:
    if models:
        pairs = dict(item.split("=", 1) for item in models.split(",") if "=" in item)
        missing = set(config.TIERS) - pairs.keys()
        if missing:
            _fail(f"--models needs all tiers; missing {', '.join(sorted(missing))}")
        return pairs
    try:
        return tokenfactory.tiers()
    except config.MissingKeyError:
        _fail("Model IDs come from Token Factory's model list, which needs NEBIUS_API_KEY. "
              "Or pass them: --models nano=ID,super=ID,ultra=ID")


@app.command()
def port(
    target: str = typer.Argument(..., help="A local folder, or a GitHub repo (URL or owner/name)"),
    tier: str = typer.Option("super", help="Nemotron tier for calls the plan doesn't assign"),
    assign: list[str] | None = typer.Option(None, "--assign", help="Per call site: ID=TIER, ID=MODEL_ID or ID=keep (repeatable)"),
    plan_file: Path | None = typer.Option(None, "--plan", help="JSON plan, e.g. from a tuning run"),
    models: str | None = typer.Option(None, help="Model ID per tier: nano=ID,super=ID,ultra=ID (default: from Token Factory)"),
    out: Path | None = typer.Option(None, help="Where to write the ported copy (default: runs/ports/<name>)"),
) -> None:
    """Rewrite a copy of a repo onto Token Factory: swap SDK clients for the OpenPort runtime, point plain HTTP calls at
    Token Factory, and set each call's model."""
    from .port import build_plan, load_assignments, port_repo
    from .scan import scan_repo

    if tier not in config.TIERS:
        _fail(f"Unknown tier {tier!r}; use one of {', '.join(config.TIERS)}")
    root = _target_root(target)
    report = scan_repo(root)
    tier_models = _tier_models(models)

    assignments = load_assignments(plan_file) if plan_file else {}
    for item in assign or []:
        site_id, _, choice = item.partition("=")
        assignments[site_id] = choice
    unknown = set(assignments) - {f.id for f in report.findings}
    if unknown:
        _fail(f"No call site called {', '.join(sorted(unknown))}. Run `openport scan` to list them.")

    plan = build_plan(report.findings, tier_models, tier, assignments)
    output = out or config.RUNS_DIR / "ports" / root.resolve().name
    try:
        result = port_repo(root, report, plan, output)
    except FileExistsError as error:
        _fail(str(error))

    table = Table(title=f"Port of {root.resolve().name}")
    for column in ("Call site", "Was", "Now", "Notes"):
        table.add_column(column)
    for site in result.sites:
        if site.status == "ported":
            now = f"[green]{_plain(site.now)}[/green]"
            notes = "; ".join(site.notes)
        else:
            now = "[yellow]kept[/yellow]"
            notes = "; ".join([site.reason, *site.notes])
        table.add_row(_plain(site.id), _plain(site.was or "?"), now, _plain(notes))
    console.print(table)

    ported = sum(s.status == "ported" for s in result.sites)
    console.print(
        f"{ported} of {len(result.sites)} calls ported. Changed {len(result.changed)} files"
        + (f", added {', '.join(result.added)}" if result.added else "")
        + f". Ported copy: {result.output}"
    )
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    diff_path = config.RUNS_DIR / f"port-{root.resolve().name}.diff"
    diff_path.write_text(result.diff, encoding="utf-8")
    console.print(f"Diff: {diff_path}")


def _needs_utf8_relaunch() -> bool:
    return os.name == "nt" and not sys.flags.utf8_mode


def main() -> None:
    """The `openport` command. On Windows it runs in Python's UTF-8 mode, so the apps it evaluates read their
    files the way they do in production: a bare open() there uses the legacy code page, which garbles prompts."""
    if _needs_utf8_relaunch():
        raise SystemExit(subprocess.call([sys.executable, "-X", "utf8", "-m", "openport.cli", *sys.argv[1:]]))
    app(prog_name="openport")  # the relaunch runs it as `python -m openport.cli`, but the command people type is this


if __name__ == "__main__":
    main()
