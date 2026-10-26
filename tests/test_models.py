"""Which Token Factory model each tier gets, from the catalog: the text tiers, Lightning, and the Nemotron that
reads images, picked from what `/v1/models?verbose=true` says each model takes in."""

import struct
import zlib

import pytest

from openport import tokenfactory
from openport.smoke import square_png

# The NVIDIA models Token Factory listed on Oct 25, 2026, with the modality each one reports.
CATALOG = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": "text->text",
    "nvidia/nemotron-3-super-120b-a12b": "text->text",
    "nvidia/Nemotron-3-Ultra-550b-a55b": "text->text",
    "nvidia/Nemotron-3_5-Lightning": "text->text",
    "nvidia/Nemotron-3.5-Super-VL": "text+image->text",
    "openbmb/MiniCPM-V-4_5": "text+image->text",
    "Qwen/Qwen3-Embedding-8B": "text->embedding",
}


@pytest.fixture
def catalog(monkeypatch):
    models = {name: tokenfactory.ModelInfo(name, None, None, None, {"id": name, "architecture": {"modality": modality}})
              for name, modality in CATALOG.items()}
    monkeypatch.setattr(tokenfactory, "catalog", lambda: models)
    tokenfactory.tiers.cache_clear()
    yield models
    tokenfactory.tiers.cache_clear()


def test_each_tier_gets_its_model_and_the_extra_rungs_come_along(catalog):
    assert tokenfactory.tiers() == {
        "nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "super": "nvidia/nemotron-3-super-120b-a12b",  # not the VL model, though its name says Super
        "ultra": "nvidia/Nemotron-3-Ultra-550b-a55b",
        "lightning": "nvidia/Nemotron-3_5-Lightning",
        "vision": "nvidia/Nemotron-3.5-Super-VL",  # a Nemotron that takes images in; MiniCPM-V does too, but isn't one
    }


def test_the_extra_rungs_are_left_out_when_token_factory_has_none(catalog, monkeypatch):
    for name in ("nvidia/Nemotron-3_5-Lightning", "nvidia/Nemotron-3.5-Super-VL"):
        del catalog[name]
    assert set(tokenfactory.tiers()) == {"nano", "super", "ultra"}
    monkeypatch.setenv("OPENPORT_MODEL_VISION", "nvidia/my-vl")  # or pinned by hand
    tokenfactory.tiers.cache_clear()
    assert tokenfactory.tiers()["vision"] == "nvidia/my-vl"


def test_the_smoke_checks_image_is_a_red_square_on_white():
    png = square_png(8)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    start = png.index(b"IDAT") + 4
    rows = zlib.decompress(png[start:start + struct.unpack(">I", png[start - 8:start - 4])[0]])
    pixels = [rows[y * 25 + 1:(y + 1) * 25] for y in range(8)]  # 1 filter byte + 8 RGB pixels per row
    assert (width, height) == (8, 8)
    assert pixels[0][:3] == b"\xff\xff\xff" and pixels[4][12:15] == bytes((220, 30, 30))
