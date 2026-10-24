"""Fine-tune a smaller model on Token Factory from a distilled training set ("Ultra teaches a smaller model").

Token Factory runs supervised fine-tuning (full or LoRA) through OpenAI-style `/v1/files` and
`/v1/fine_tuning/jobs`. NVIDIA Nemotron isn't among its fine-tunable models yet, so the student is a
small open model (Llama 3.1 8B by default) trained only on Nemotron Ultra's answers. Serving custom
weights on Token Factory is in beta (by request), so when the job finishes OpenPort downloads the
adapter, which can also be served with vLLM on a Nebius AI Cloud GPU endpoint.
"""

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from openai import APIConnectionError, InternalServerError, OpenAI, RateLimitError

from . import config, tokenfactory

DEFAULT_BASE = "meta-llama/Llama-3.1-8B-Instruct"
TERMINAL = {"succeeded", "failed", "cancelled"}
# Token Factory's job endpoints can fail for a while (a 500 on the events list) while the job carries on.
TRANSIENT = (InternalServerError, APIConnectionError, RateLimitError)
# LoRA wants a higher learning rate than the full fine-tuning default (1e-5).
LORA_HYPERPARAMETERS = {"lora": True, "lora_r": 16, "lora_alpha": 32, "lora_dropout": 0.05, "n_epochs": 3, "learning_rate": 1e-4, "batch_size": 8}


@dataclass
class FineTune:
    job_id: str
    base: str
    status: str
    training_file: str
    validation_file: str | None
    examples: dict  # {"train": n, "validation": n}
    trained_tokens: int | None = None
    fine_tuned_model: str | None = None
    adapter_files: list[str] = field(default_factory=list)  # downloaded locally
    error: str | None = None


def validate(lines: list[str]) -> list[dict]:
    """Chat examples in Token Factory's format: a `messages` list that ends with the assistant's answer."""
    examples = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        example = json.loads(line)
        messages = example.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            raise ValueError(f"line {number}: needs a messages list")
        if messages[-1].get("role") != "assistant" or not str(messages[-1].get("content") or "").strip():
            raise ValueError(f"line {number}: the last message must be the assistant's answer")
        if any(m.get("role") not in ("system", "user", "assistant", "tool") for m in messages):
            raise ValueError(f"line {number}: unknown role")
        examples.append(example)
    if len(examples) < 10:
        raise ValueError(f"only {len(examples)} examples; fine-tuning needs at least 10")
    return examples


def split(examples: list[dict], validation_fraction: float = 0.1, seed: int = 7) -> tuple[list[dict], list[dict]]:
    shuffled = examples[:]
    random.Random(seed).shuffle(shuffled)
    held = max(1, round(len(shuffled) * validation_fraction))
    return shuffled[held:], shuffled[:held]


def _write(examples: list[dict], path: Path) -> Path:
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in examples), encoding="utf-8")
    return path


def start(dataset: Path, *, base: str = DEFAULT_BASE, suffix: str = "openport", hyperparameters: dict | None = None,
          client: OpenAI | None = None) -> FineTune:
    """Validate and split the dataset, upload both halves, and create the fine-tuning job."""
    client = client or tokenfactory.client()
    train, held = split(validate(Path(dataset).read_text(encoding="utf-8").splitlines()))
    folder = config.RUNS_DIR / "finetunes"
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(dataset).stem
    with open(_write(train, folder / f"{stem}.train.jsonl"), "rb") as handle:
        training = client.files.create(file=handle, purpose="fine-tune")
    with open(_write(held, folder / f"{stem}.validation.jsonl"), "rb") as handle:
        validation = client.files.create(file=handle, purpose="fine-tune")
    job = client.fine_tuning.jobs.create(
        model=base, training_file=training.id, validation_file=validation.id, suffix=suffix, seed=7,
        hyperparameters=hyperparameters or LORA_HYPERPARAMETERS,
    )
    return FineTune(job.id, base, job.status, training.id, validation.id, {"train": len(train), "validation": len(held)})


def follow(tune: FineTune, *, client: OpenAI | None = None, poll_seconds: float = 30, sleep: Callable[[float], None] = time.sleep,
           on_status: Callable[[str, list[str]], None] | None = None, deadline_seconds: float = 6 * 3600,
           max_failures: int = 20) -> FineTune:
    """Poll the job until it finishes, passing new event messages to `on_status`. A server error is retried at
    the next poll, up to `max_failures` in a row."""
    client = client or tokenfactory.client()
    seen: set[str] = set()
    started = time.monotonic()
    failures = 0
    while True:
        try:
            job = client.fine_tuning.jobs.retrieve(tune.job_id)
            events = list(client.fine_tuning.jobs.list_events(tune.job_id, limit=50))
        except TRANSIENT:
            failures += 1
            if failures >= max_failures:
                raise
        else:
            failures = 0
            tune.status = job.status
            tune.trained_tokens = getattr(job, "trained_tokens", None)
            tune.fine_tuned_model = getattr(job, "fine_tuned_model", None)
            error = getattr(job, "error", None)
            tune.error = getattr(error, "message", None) or (error.get("message") if isinstance(error, dict) else None)
            fresh = [e.message for e in reversed(events) if e.id not in seen]
            seen.update(e.id for e in events)
            if on_status:
                on_status(job.status, fresh)
            if job.status in TERMINAL:
                return tune
        if time.monotonic() - started > deadline_seconds:
            raise TimeoutError(f"fine-tuning job {tune.job_id} still {tune.status} after {deadline_seconds / 3600:.0f}h")
        sleep(poll_seconds)


def download_adapter(tune: FineTune, *, client: OpenAI | None = None) -> FineTune:
    """Save the final checkpoint's files (the LoRA adapter) under runs/finetunes/<job>/."""
    client = client or tokenfactory.client()
    checkpoints = list(client.fine_tuning.jobs.checkpoints.list(tune.job_id))
    if not checkpoints:
        raise RuntimeError(f"job {tune.job_id} has no checkpoints")
    final = max(checkpoints, key=lambda c: getattr(c, "step_number", 0) or 0)
    folder = config.RUNS_DIR / "finetunes" / tune.job_id
    folder.mkdir(parents=True, exist_ok=True)
    tune.adapter_files = []  # a retry downloads them all again
    for file_id in getattr(final, "result_files", None) or []:
        name = client.files.retrieve(file_id).filename or file_id
        path = folder / Path(name).name
        path.write_bytes(client.files.content(file_id).read())
        tune.adapter_files.append(str(path))
    return tune


def save(tune: FineTune) -> Path:
    folder = config.RUNS_DIR / "finetunes"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{tune.job_id}.json"
    path.write_text(json.dumps(asdict(tune), indent=2), encoding="utf-8")
    return path
