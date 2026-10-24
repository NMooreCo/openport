"""Tests for `openport finetune`: dataset checks, the job's lifecycle, and downloading the adapter."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import InternalServerError

from openport import config
from openport import finetune as ft


@pytest.fixture(autouse=True)
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")


def example(i):
    return {"messages": [{"role": "system", "content": "Brand voice."}, {"role": "user", "content": f"ticket {i}"},
                         {"role": "assistant", "content": f"<reply>Hi there, reply {i}</reply>"}]}


def dataset(tmp_path, n=20) -> Path:
    path = tmp_path / "distill-supportdesk-replies.jsonl"
    path.write_text("".join(json.dumps(example(i)) + "\n" for i in range(n)), encoding="utf-8")
    return path


class FakeFineTuning:
    """Just enough of the OpenAI client's files and fine_tuning APIs, scripted."""

    def __init__(self, statuses):
        self.uploads, self.created, self.statuses = [], [], list(statuses)
        self.files = SimpleNamespace(create=self._upload, retrieve=lambda fid: SimpleNamespace(filename=f"adapter/{fid}.safetensors"),
                                     content=lambda fid: SimpleNamespace(read=lambda: f"weights {fid}".encode()))
        jobs = SimpleNamespace(create=self._create, retrieve=self._retrieve, list_events=self._events,
                               checkpoints=SimpleNamespace(list=lambda job_id: [SimpleNamespace(step_number=1, result_files=["f-old"]),
                                                                                 SimpleNamespace(step_number=3, result_files=["f1", "f2"])]))
        self.fine_tuning = SimpleNamespace(jobs=jobs)

    def _upload(self, file, purpose):
        self.uploads.append((Path(file.name).name, purpose, len(file.read().splitlines())))
        return SimpleNamespace(id=f"file-{len(self.uploads)}")

    def _create(self, **request):
        self.created.append(request)
        return SimpleNamespace(id="ftjob-1", status="validating_files")

    def _retrieve(self, job_id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SimpleNamespace(status=status, trained_tokens=123_456 if status == "succeeded" else None,
                               fine_tuned_model="ft:llama-3.1-8b:openport" if status == "succeeded" else None, error=None)

    def _events(self, job_id, limit):
        return [SimpleNamespace(id=f"e{len(self.statuses)}", message=f"now {self.statuses[0]}")]


def test_datasets_must_end_on_the_assistants_answer():
    good = [json.dumps(example(i)) for i in range(10)]
    assert len(ft.validate(good)) == 10
    bad = json.dumps({"messages": [{"role": "user", "content": "hi"}, {"role": "user", "content": "again"}]})
    with pytest.raises(ValueError, match="last message"):
        ft.validate(good + [bad])
    with pytest.raises(ValueError, match="at least 10"):
        ft.validate(good[:3])


def test_split_is_stable():
    examples = [example(i) for i in range(20)]
    train, held = ft.split(examples)
    assert (len(train), len(held)) == (18, 2) and ft.split(examples) == (train, held)


def test_start_uploads_both_halves_and_asks_for_lora(tmp_path):
    client = FakeFineTuning(["queued"])
    tune = ft.start(dataset(tmp_path), client=client)
    assert client.uploads == [("distill-supportdesk-replies.train.jsonl", "fine-tune", 18),
                              ("distill-supportdesk-replies.validation.jsonl", "fine-tune", 2)]
    request = client.created[0]
    assert request["model"] == "meta-llama/Llama-3.1-8B-Instruct" and request["hyperparameters"]["lora"] is True
    assert (request["training_file"], request["validation_file"]) == ("file-1", "file-2")
    assert (tune.job_id, tune.examples) == ("ftjob-1", {"train": 18, "validation": 2})


def test_follow_until_done_then_download_the_final_checkpoint(tmp_path):
    client = FakeFineTuning(["queued", "running", "succeeded"])
    tune = ft.start(dataset(tmp_path), client=client)
    seen, naps = [], []
    tune = ft.follow(tune, client=client, sleep=naps.append, on_status=lambda status, messages: seen.append(status))
    assert seen == ["queued", "running", "succeeded"] and len(naps) == 2
    assert (tune.status, tune.trained_tokens, tune.fine_tuned_model) == ("succeeded", 123_456, "ft:llama-3.1-8b:openport")
    tune = ft.download_adapter(tune, client=client)
    assert [Path(p).name for p in tune.adapter_files] == ["f1.safetensors", "f2.safetensors"]  # the last checkpoint only
    assert Path(tune.adapter_files[0]).read_bytes() == b"weights f1"
    assert json.loads(ft.save(tune).read_text())["status"] == "succeeded"


def server_error():
    response = httpx.Response(500, request=httpx.Request("GET", "https://api.tokenfactory.nebius.com/v1/fine_tuning/jobs/ftjob-1/events"))
    return InternalServerError("Error code: 500", response=response, body={"detail": {"message": "Internal Server Error"}})


def flaky_events(client, failures):
    """The events list fails `failures` times in a row first, as Token Factory's did mid-training."""
    events = client._events

    def list_events(job_id, limit):
        if failures:
            failures.pop()
            raise server_error()
        return events(job_id, limit)

    client.fine_tuning.jobs.list_events = list_events


def test_follow_rides_out_server_errors(tmp_path):
    client = FakeFineTuning(["running", "running", "running", "succeeded"])
    flaky_events(client, [1, 1])
    seen, naps = [], []
    tune = ft.follow(ft.start(dataset(tmp_path), client=client), client=client, sleep=naps.append,
                     on_status=lambda status, messages: seen.append(status))
    assert tune.status == "succeeded" and seen == ["running", "succeeded"] and len(naps) == 3


def test_follow_gives_up_after_too_many_server_errors_in_a_row(tmp_path):
    client = FakeFineTuning(["running"])
    flaky_events(client, [1] * 5)
    naps = []
    with pytest.raises(InternalServerError):
        ft.follow(ft.start(dataset(tmp_path), client=client), client=client, sleep=naps.append, max_failures=3)
    assert len(naps) == 2
