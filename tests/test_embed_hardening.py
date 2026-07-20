"""
Regression tests for the HuggingFace supply-chain hardening in
cairn/backends/embed.py (commit #2 on fix/cpu-torch-install).

Hermetic — no network, no real model download:
  • a fake ``huggingface_hub`` exposing ONLY ``hf_hub_download`` (no
    ``snapshot_download`` / ``allow_patterns``) emulates the oldest hub the
    ``sentence-transformers>=2.2.0`` floor can inherit;
  • a fake ``sentence_transformers`` whose constructor RAISES on a ``revision=``
    kwarg emulates the 2.2.0 floor's constructor;
  • the HF cache root is redirected to a temp dir via ``os.path.expanduser``.

Covers Codex's required regressions: partial-snapshot recovery, constructor
without a ``revision`` keyword, custom-model constructor preservation, complete
pinned-snapshot offline load, and the old-hub shim — plus corrupted-weight and
offline-missing guards.
"""
import os
import sys
import types
import hashlib
import shutil

import pytest

from cairn.backends import embed


MODEL_FOLDER = "models--sentence-transformers--all-MiniLM-L6-v2"
REV = embed.DEFAULT_MODEL_REVISION
GOOD_WEIGHT = b"PINNED-GOOD-WEIGHTS-v1"
GOOD_HASH = hashlib.sha256(GOOD_WEIGHT).hexdigest()


def _write_snapshot(home, files):
    """Create <home>/.cache/huggingface/hub/<folder>/snapshots/<REV>/ populated
    with ``files`` (rel_path -> bytes). Returns the snapshot dir."""
    snap = os.path.join(str(home), ".cache", "huggingface", "hub",
                        MODEL_FOLDER, "snapshots", REV)
    for rel, data in files.items():
        p = os.path.join(snap, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
    return snap


def _complete_files(weight=GOOD_WEIGHT):
    files = {rel: b"{}" for rel in embed.DEFAULT_MODEL_REQUIRED_FILES}
    files["model.safetensors"] = weight
    return files


def _install_home(monkeypatch, home):
    """Redirect ~ to <home> and clear cache-root env vars so the only cache the
    code can see is the temp one."""
    home = str(home)

    def fake_expand(path):
        if path == "~":
            return home
        if path[:2] in ("~/", "~\\"):
            return os.path.join(home, path[2:])
        return path

    monkeypatch.setattr(os.path, "expanduser", fake_expand)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)


def _install_fake_hub(monkeypatch, home, *, forbid=False, remote=None):
    """Inject a fake huggingface_hub with ONLY hf_hub_download (no
    snapshot_download / allow_patterns).

    Resolves from the temp cache. If a file is absent locally: when ``remote``
    (a fixture dir standing in for the Hub) is given, the file is materialized
    into the cache and its local path returned -- modelling a real online fetch;
    otherwise FileNotFoundError is raised -- modelling offline, or a file the Hub
    itself lacks."""
    home = str(home)
    mod = types.ModuleType("huggingface_hub")

    def hf_hub_download(repo_id=None, filename=None, revision=None, **kw):
        if forbid:
            raise AssertionError(
                "hf_hub_download must NOT be called for a custom model")
        local = os.path.join(home, ".cache", "huggingface", "hub",
                             "models--" + repo_id.replace("/", "--"),
                             "snapshots", revision, *filename.split("/"))
        if not os.path.isfile(local):
            if remote is not None:
                src = os.path.join(str(remote), *filename.split("/"))
                if os.path.isfile(src):
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    shutil.copyfile(src, local)   # "download" into the cache
                    return local
            raise FileNotFoundError(f"{filename}@{revision} not cached")
        return local

    mod.hf_hub_download = hf_hub_download
    # deliberately NO snapshot_download / allow_patterns (old-hub shim)
    monkeypatch.setitem(sys.modules, "huggingface_hub", mod)
    return mod


def _install_fake_st(monkeypatch):
    """Inject a fake sentence_transformers whose SentenceTransformer REJECTS a
    ``revision=`` kwarg (like the >=2.2.0 floor). Records constructor calls."""
    record = []
    mod = types.ModuleType("sentence_transformers")

    class SentenceTransformer:
        def __init__(self, model_name_or_path, modules=None, device=None, **kwargs):
            if "revision" in kwargs:
                raise TypeError(
                    "__init__() got an unexpected keyword argument 'revision'")
            record.append({"path": model_name_or_path, "device": device,
                           "kwargs": dict(kwargs)})
            self.model_name_or_path = model_name_or_path

        def encode(self, texts, batch_size=32, show_progress_bar=False,
                   convert_to_numpy=False):
            import numpy as np
            return np.zeros((len(texts), embed.DIM), dtype="float32")

    mod.SentenceTransformer = SentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    return record


def _install_fake_torch(monkeypatch, cuda=False):
    mod = types.ModuleType("torch")

    class _cuda:
        @staticmethod
        def is_available():
            return cuda

    mod.cuda = _cuda
    monkeypatch.setitem(sys.modules, "torch", mod)
    return mod


def _online(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)


def _offline(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


# ── completeness / air-gap gate ─────────────────────────────────────────────

def test_complete_snapshot_is_cached(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    _write_snapshot(home, _complete_files())
    assert embed._hf_model_cached(
        embed.DEFAULT_MODEL, revision=REV,
        required_files=embed.DEFAULT_MODEL_REQUIRED_FILES)


def test_partial_snapshot_not_cached_stays_online(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    files = _complete_files()
    del files["tokenizer.json"]          # interrupted first download
    _write_snapshot(home, files)
    # incomplete snapshot must NOT count as cached -> air-gap stays online
    assert not embed._hf_model_cached(
        embed.DEFAULT_MODEL, revision=REV,
        required_files=embed.DEFAULT_MODEL_REQUIRED_FILES)


def test_partial_snapshot_recovers_when_online(tmp_path, monkeypatch):
    # Start with a genuinely PARTIAL local snapshot (one required file missing),
    # let an online Hub materialize the missing file from a separate remote
    # fixture, then confirm the snapshot recovers to complete + verified.
    home = tmp_path / "home"
    remote = tmp_path / "remote"                 # stands in for the Hub's copy
    _install_home(monkeypatch, home)
    monkeypatch.setattr(embed, "DEFAULT_MODEL_WEIGHT_SHA256",
                        {"model.safetensors": GOOD_HASH})

    # the Hub (remote) holds the COMPLETE required set
    for rel, data in _complete_files().items():
        rp = os.path.join(str(remote), *rel.split("/"))
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "wb") as fh:
            fh.write(data)

    # the local cache starts PARTIAL: everything EXCEPT tokenizer.json
    partial = _complete_files()
    del partial["tokenizer.json"]
    snap = _write_snapshot(home, partial)
    missing = os.path.join(snap, "tokenizer.json")
    assert not os.path.exists(missing)                              # precondition: absent
    assert not embed._hf_model_cached(                             # partial => NOT cached
        embed.DEFAULT_MODEL, revision=REV,
        required_files=embed.DEFAULT_MODEL_REQUIRED_FILES)

    _install_fake_hub(monkeypatch, home, remote=remote)           # online Hub can fetch
    _online(monkeypatch)

    local = embed._verify_default_model_or_raise(embed.DEFAULT_MODEL)

    assert local is not None                                       # (1) verification succeeds
    assert os.path.isfile(missing)                                 # (2) missing file now present
    assert embed._hf_model_cached(                                 # (3) now complete/cached
        embed.DEFAULT_MODEL, revision=REV,
        required_files=embed.DEFAULT_MODEL_REQUIRED_FILES)
    assert os.path.normcase(local) == os.path.normcase(snap)       # (4) returned = pinned snapshot


# ── constructor compatibility (no revision= kwarg) ──────────────────────────

def test_constructor_no_revision_kwarg_cpu(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    monkeypatch.setattr(embed, "DEFAULT_MODEL_WEIGHT_SHA256",
                        {"model.safetensors": GOOD_HASH})
    snap = _write_snapshot(home, _complete_files())
    _install_fake_hub(monkeypatch, home)
    rec = _install_fake_st(monkeypatch)
    _online(monkeypatch)
    embed.CPUEmbed()._load()
    assert rec, "SentenceTransformer was not constructed"
    call = rec[-1]
    assert "revision" not in call["kwargs"]                      # never pass revision=
    assert os.path.normcase(call["path"]) == os.path.normcase(snap)  # from local dir


def test_constructor_no_revision_kwarg_gpu(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    monkeypatch.setattr(embed, "DEFAULT_MODEL_WEIGHT_SHA256",
                        {"model.safetensors": GOOD_HASH})
    snap = _write_snapshot(home, _complete_files())
    _install_fake_hub(monkeypatch, home)
    rec = _install_fake_st(monkeypatch)
    _install_fake_torch(monkeypatch, cuda=False)
    _online(monkeypatch)
    embed.GPUEmbed()._load()
    call = rec[-1]
    assert "revision" not in call["kwargs"]
    assert call["device"] == "cpu"
    assert os.path.normcase(call["path"]) == os.path.normcase(snap)


# ── custom-model constructor preservation ───────────────────────────────────

def test_custom_model_constructor_preserved(tmp_path, monkeypatch):
    """Scope: proves the CONSTRUCTOR is preserved for a custom model -- the
    original name is passed through, no revision= kwarg, and the pin/verify path
    (hf_hub_download) is never touched. It deliberately does NOT assert that a
    custom model downloads fresh: on a machine where the default pinned model is
    cached, Cairn's module-level air-gap forces global HF offline, which would
    also gate a not-yet-cached custom model. That module-level offline behavior
    is pre-existing (unchanged by this hardening) and is out of scope here."""
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    _install_fake_hub(monkeypatch, home, forbid=True)   # hub must not be touched
    rec = _install_fake_st(monkeypatch)
    _online(monkeypatch)
    embed.CPUEmbed("intfloat/e5-small-v2")._load()
    call = rec[-1]
    assert call["path"] == "intfloat/e5-small-v2"        # original name, unchanged
    assert "revision" not in call["kwargs"]
    assert embed._verify_default_model_or_raise("intfloat/e5-small-v2") is None


# ── complete pinned-snapshot offline load ───────────────────────────────────

def test_complete_pinned_snapshot_offline_load(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    monkeypatch.setattr(embed, "DEFAULT_MODEL_WEIGHT_SHA256",
                        {"model.safetensors": GOOD_HASH})
    snap = _write_snapshot(home, _complete_files())
    _install_fake_hub(monkeypatch, home)
    rec = _install_fake_st(monkeypatch)
    _offline(monkeypatch)                                # complete + offline
    blobs = embed.CPUEmbed().encode(["hello world"])     # loads then encodes
    assert len(blobs) == 1 and len(blobs[0]) == embed.DIM * 4
    assert os.path.normcase(rec[-1]["path"]) == os.path.normcase(snap)


# ── fail-loud guards ────────────────────────────────────────────────────────

def test_offline_missing_revision_raises(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    _install_fake_hub(monkeypatch, home)                 # nothing written -> missing
    _offline(monkeypatch)
    with pytest.raises(RuntimeError, match="offline mode is active"):
        embed._verify_default_model_or_raise(embed.DEFAULT_MODEL)


def test_corrupted_weight_raises(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    monkeypatch.setattr(embed, "DEFAULT_MODEL_WEIGHT_SHA256",
                        {"model.safetensors": GOOD_HASH})
    _write_snapshot(home, _complete_files(weight=b"TAMPERED-WEIGHTS"))
    _install_fake_hub(monkeypatch, home)
    _online(monkeypatch)
    with pytest.raises(RuntimeError, match="SUPPLY-CHAIN CHECK FAILED"):
        embed._verify_default_model_or_raise(embed.DEFAULT_MODEL)


# ── old-hub shim: hf_hub_download present, allow_patterns absent ─────────────

def test_hub_shim_without_allow_patterns(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _install_home(monkeypatch, home)
    monkeypatch.setattr(embed, "DEFAULT_MODEL_WEIGHT_SHA256",
                        {"model.safetensors": GOOD_HASH})
    snap = _write_snapshot(home, _complete_files())
    hub = _install_fake_hub(monkeypatch, home)
    assert not hasattr(hub, "snapshot_download")         # old-hub shim
    _online(monkeypatch)
    local = embed._verify_default_model_or_raise(embed.DEFAULT_MODEL)
    assert os.path.normcase(local) == os.path.normcase(snap)
