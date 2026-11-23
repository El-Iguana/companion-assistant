"""VRM import: streamed to disk, audited from the header, size-capped, nothing left behind.

    .venv/bin/python -m pytest -q tests/test_vrm_upload.py
"""
from __future__ import annotations

import asyncio
import io
import json
import struct
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import main
from services import vrm

BASELINE = Path(__file__).resolve().parents[1] / "data" / "vrm" / "baseline.vrm"


def _glb_with_padding(src: bytes, pad: int) -> bytes:
    """baseline's JSON chunk, then a BIN chunk of `pad` zero bytes: a big file whose
    header is a real model's."""
    off = 12
    clen, ctype = struct.unpack_from("<II", src, off)
    assert ctype == vrm.CHUNK_JSON
    js = src[off + 8: off + 8 + clen]
    bin_chunk = struct.pack("<II", pad, 0x004E4942) + b"\0" * pad
    body = struct.pack("<II", len(js), vrm.CHUNK_JSON) + js + bin_chunk
    return struct.pack("<III", vrm.GLB_MAGIC, 2, 12 + len(body)) + body


def _upload(data: bytes, name: str = "model.vrm"):
    return asyncio.run(main.upload_vrm(UploadFile(file=io.BytesIO(data), filename=name), None))


def _leftovers():
    return [p.name for p in main._vrm_dir().glob(".upload-*")]


def test_audit_reads_only_the_header(tmp_path, monkeypatch):
    big = tmp_path / "big.vrm"
    big.write_bytes(_glb_with_padding(BASELINE.read_bytes(), 30 * 1024 * 1024))
    read = []
    real_open = open

    def spy_open(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        if "b" in mode:
            orig = f.read
            f.read = lambda n=-1: (read.append(n), orig(n))[1]
        return f
    monkeypatch.setattr("builtins.open", spy_open)
    report = vrm.audit_file(big)
    monkeypatch.undo()
    assert report["usable"] and report["size"] == big.stat().st_size
    assert -1 not in read and sum(read) < 5 * 1024 * 1024        # never the whole 30 MB
    whole = vrm.audit(BASELINE.read_bytes())
    assert {k: report[k] for k in ("spec", "visemes", "triangles")} == \
           {k: whole[k] for k in ("spec", "visemes", "triangles")}


def test_a_good_model_is_kept_without_replacing_one(monkeypatch):
    data = BASELINE.read_bytes()
    first = _upload(data, "upload-test.vrm")
    second = _upload(data, "upload-test.vrm")
    try:
        assert first["name"] == "upload-test.vrm" and second["name"] == "upload-test-1.vrm"
        assert first["usable"] and first["size"] == len(data)
        assert (main._vrm_dir() / "upload-test.vrm").read_bytes() == data
        assert _leftovers() == []
    finally:
        for n in ("upload-test.vrm", "upload-test-1.vrm"):
            (main._vrm_dir() / n).unlink(missing_ok=True)


def test_too_large_is_refused_while_streaming(monkeypatch):
    monkeypatch.setattr(vrm, "MAX_BYTES", 2 * 1024 * 1024)
    with pytest.raises(HTTPException) as e:
        _upload(b"\0" * (3 * 1024 * 1024))
    assert e.value.status_code == 413 and "2 MB" in e.value.detail
    assert _leftovers() == []


def test_not_a_vrm_is_refused_and_cleaned_up():
    with pytest.raises(HTTPException) as e:
        _upload(b"definitely not a glb container")
    assert e.value.status_code == 400
    assert _leftovers() == []


def test_the_request_cap_follows_cantineros_limit():
    # pytincture takes only the PytinctureConfig's values, so the cap must be in it.
    from pytincture import PytinctureConfig
    assert "max_request_body_bytes" in PytinctureConfig.__dataclass_fields__
    src = (Path(__file__).resolve().parents[1] / "service.py").read_text()
    assert "max_request_body_bytes=REQUEST_MAX_BYTES" in src
    assert "vrm.MAX_BYTES + 1024 * 1024" in src
