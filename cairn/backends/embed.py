"""
cairn/backends/embed.py
Hardware-swappable embedding backends.

The EmbedBackend protocol is the interface.
EmbedRouter auto-detects the best available hardware and returns the right backend.

Hardware detection priority:
  1. CAIRN_EMBED_BACKEND env var — explicit override
  2. CUDA GPU available → GPUEmbed (FAISS-GPU / cuVS)
  3. Windows + ONNX Runtime + NPU → ONNXNPUEmbed (RTX Spark, Qualcomm)
  4. Apple Silicon + CoreML → CoreMLEmbed (M4/M5 Neural Engine)
  5. CPU fallback → CPUEmbed (sentence-transformers)

Each backend produces the same output: a bytes blob (packed float32 vector)
that can be stored in the vault and compared with cosine similarity.

Current default: all-MiniLM-L6-v2 (384 dims, ~91MB weight, runs everywhere)
Upgrade path:   all-mpnet-base-v2 (768 dims, better quality, more VRAM)
RTX Spark path: ONNX export of same model, runs on Blackwell NPU, <5ms
"""
from __future__ import annotations
import os, struct, logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("cairn.embed")

# Default model — small, fast, works on CPU, ONNX-exportable, NPU-ready
DEFAULT_MODEL = "all-MiniLM-L6-v2"
DIM = 384  # dimensions for DEFAULT_MODEL

# ── Supply-chain trust anchor (DEFAULT model only) ──────────────────────────
# Pin the default embedder to an immutable HuggingFace revision and verify its
# weight file's SHA-256 BEFORE the model reads it, so a fresh first-time download
# can never be silently swapped for a tampered artifact. The revision + hash
# below were verified byte-for-byte against a pre-breach (2026-06-09) local copy
# AND the live HF-served file (2026-07-20) — all three identical. Custom models
# (any non-default model_name) are deliberately NOT pinned or verified: the
# operator owns that trust decision. Verification is pure stdlib (hashlib); the
# huggingface_hub resolver used here is already a sentence-transformers dep, so
# no new dependency is introduced and the stdlib-only / local-first laws hold.
DEFAULT_MODEL_REPO = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
DEFAULT_MODEL_WEIGHT_SHA256 = {
    "model.safetensors": "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db",
}
# The COMPLETE set of files sentence-transformers needs to load all-MiniLM-L6-v2
# from a local snapshot. Doubles as (a) the snapshot_download allow_patterns —
# without it a bare snapshot of this repo pulls ~977 MB of ONNX/OpenVINO/bin/tf
# variants we never use, vs ~91 MB for just these — and (b) the completeness gate
# for the air-gap: a PARTIAL first download must NOT count as "cached" (else it
# gets forced offline and can never finish). Paths use '/' (HF repo convention);
# the local check splits them for the OS.
DEFAULT_MODEL_REQUIRED_FILES = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "special_tokens_map.json",
    "1_Pooling/config.json",
    "model.safetensors",
]


def _normalize_repo(model_name: str) -> str:
    """Canonical HF repo id for a model name. Bare names get the
    sentence-transformers/ org, matching how SentenceTransformer resolves them."""
    return model_name if "/" in model_name else f"sentence-transformers/{model_name}"


def _is_default_model(model_name: str) -> bool:
    """True only for Cairn's pinned default model. Custom models return False and
    skip all pin/verify logic."""
    return _normalize_repo(model_name) == DEFAULT_MODEL_REPO


def _hf_model_cached(model_name: str, revision: str | None = None,
                     required_files: list[str] | None = None) -> bool:
    """True only if the embedding model is already in the local HuggingFace
    cache — filesystem check only (no network, no hf_hub import). Conservative:
    a miss just leaves us online, so it can never break loading.

    When ``revision`` is given the check is REVISION-AWARE: only a snapshot for
    that exact commit counts. When ``required_files`` is also given it is
    COMPLETENESS-AWARE: every one of those files must be present for the snapshot
    to count as cached. Together these let the air-gap flip safely —
      - a box that cached some *other* (unpinned) revision stays online so the
        pinned revision can be fetched, and
      - a box with a *partial* pinned snapshot (interrupted first download) stays
        online so the download can finish next run,
    instead of being forced offline against a snapshot it can't load from."""
    repo = _normalize_repo(model_name)
    folder = "models--" + repo.replace("/", "--")
    roots = []
    if os.environ.get("HF_HUB_CACHE"):
        roots.append(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        roots.append(os.path.join(os.environ["HF_HOME"], "hub"))
    roots.append(os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub"))
    for r in roots:
        snap = os.path.join(r, folder, "snapshots")
        try:
            if revision is not None:
                d = os.path.join(snap, revision)
                if not os.path.isdir(d):
                    continue
                if required_files:
                    if all(os.path.isfile(os.path.join(d, *rel.split("/")))
                           for rel in required_files):
                        return True
                    continue  # snapshot present but INCOMPLETE — not cached
                if any(os.scandir(d)):
                    return True
            elif os.path.isdir(snap) and any(os.scandir(snap)):
                return True
        except Exception:
            pass
    return False


# Local-first air-gap (Cairn law: nothing leaves the machine). If the COMPLETE
# pinned default snapshot is already cached, force HuggingFace offline so the
# embedder never contacts huggingface.co on load. A fresh machine (pinned
# revision absent or only partially downloaded) stays online for the one-time
# download, then air-gaps on every run after. Revision- and completeness-aware
# on purpose: a box with only a stale/unpinned OR half-downloaded snapshot must
# NOT be forced offline (it would then be unable to fetch/finish the pinned
# revision — a hard upgrade break). A user-set HF_HUB_OFFLINE /
# TRANSFORMERS_OFFLINE is always respected. Must run before sentence-transformers
# / huggingface_hub are imported (they read these env vars at import time);
# embed.py is imported before them, so module scope is the correct place.
if not (os.environ.get("HF_HUB_OFFLINE") or os.environ.get("TRANSFORMERS_OFFLINE")):
    if _hf_model_cached(DEFAULT_MODEL, revision=DEFAULT_MODEL_REVISION,
                        required_files=DEFAULT_MODEL_REQUIRED_FILES):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _verify_default_model_or_raise(model_name: str) -> str | None:
    """Pre-load supply-chain gate for the DEFAULT model only.

    Resolves the pinned revision's COMPLETE required file set into the cache
    (downloading only those ~10 files, never the ~977 MB of unused variants),
    verifies the weight file's SHA-256 against the trusted value, and returns the
    local snapshot directory so the caller can construct SentenceTransformer from
    that exact verified path. Returns None for a custom model (caller then uses
    the original model_name unchanged).

    Constructing from the returned local path — rather than passing revision= to
    SentenceTransformer — keeps this working on the sentence-transformers>=2.2.0
    floor, whose constructor has no revision keyword.

    - Custom models return None immediately: not pinned or verified here; the
      operator owns that trust.
    - Respects user-set offline mode: if the complete pinned snapshot is not
      cached AND offline is active, it fails with clear remediation and NEVER
      silently goes online.
    - Fails LOUD on any hash mismatch (mirrors the dim-contract guard below).
    """
    if not _is_default_model(model_name):
        return None  # custom model — operator owns that trust, no pin/verify

    import hashlib

    offline = bool(os.environ.get("HF_HUB_OFFLINE")
                   or os.environ.get("TRANSFORMERS_OFFLINE"))
    complete = _hf_model_cached(DEFAULT_MODEL, revision=DEFAULT_MODEL_REVISION,
                                required_files=DEFAULT_MODEL_REQUIRED_FILES)

    if not complete and offline:
        raise RuntimeError(
            "cairn: the pinned embedding model revision "
            f"{DEFAULT_MODEL_REVISION[:12]}... for '{DEFAULT_MODEL}' is not fully in "
            "the local HuggingFace cache, and offline mode is active "
            "(HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE). Cairn will NOT silently go "
            "online to fetch it. To proceed, either:\n"
            "  - unset HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE for one run so the "
            "pinned revision downloads once (it air-gaps again afterward), or\n"
            "  - place the verified snapshot into the HuggingFace cache manually "
            "(models--sentence-transformers--all-MiniLM-L6-v2/snapshots/"
            f"{DEFAULT_MODEL_REVISION})."
        )

    # Resolve the required files individually with hf_hub_download — a stable API
    # across every huggingface_hub the >=2.2.0 floor can inherit. (snapshot_download's
    # allow_patterns filter only exists in hub >=0.8.0; ST 2.2.0 floors hub at >=0.4.0,
    # so a resolver could hand us an old hub where that filter is missing and a bare
    # snapshot would pull ~977 MB.) A cache hit is offline-safe; a miss (or partial
    # snapshot) fetches just these files when online — the offline+incomplete case
    # already raised above. Then derive the single pinned snapshot directory and
    # confirm every resolved file actually lives inside .../snapshots/<pinned revision>.
    try:
        from huggingface_hub import hf_hub_download
    except Exception as e:  # pragma: no cover - hf_hub ships with sentence-transformers
        raise RuntimeError(
            "cairn: huggingface_hub is unavailable, so the pinned embedding model "
            f"cannot be integrity-verified ({type(e).__name__}: {e}). Refusing to "
            "load an unverified model."
        ) from e

    expected_folder = "models--" + DEFAULT_MODEL_REPO.replace("/", "--")
    local_dir = None
    for rel in DEFAULT_MODEL_REQUIRED_FILES:
        try:
            p = hf_hub_download(
                repo_id=DEFAULT_MODEL_REPO,
                filename=rel,
                revision=DEFAULT_MODEL_REVISION,
            )
        except Exception as e:
            raise RuntimeError(
                f"cairn: could not obtain '{rel}' for the pinned embedding model "
                f"revision {DEFAULT_MODEL_REVISION[:12]}... ({type(e).__name__}: {e}). "
                "The vault will not embed against an unverified model. Check network "
                "access to huggingface.co, or pre-place the verified snapshot in the "
                "cache."
            ) from e

        ap = os.path.abspath(p)
        if not os.path.isfile(ap):
            raise RuntimeError(
                f"cairn: resolved path for '{rel}' is not a file ({p}). Refusing to load."
            )
        # Walk up one directory per path component to reach the snapshot root, then
        # confirm it is exactly .../<model folder>/snapshots/<pinned revision>. This
        # rejects anything the hub might resolve outside the pinned snapshot.
        d = ap
        for _ in rel.split("/"):
            d = os.path.dirname(d)
        if (os.path.basename(d) != DEFAULT_MODEL_REVISION
                or os.path.basename(os.path.dirname(d)) != "snapshots"
                or os.path.basename(os.path.dirname(os.path.dirname(d))) != expected_folder):
            raise RuntimeError(
                f"cairn: resolved file '{rel}' does not belong to the pinned snapshot "
                f"{DEFAULT_MODEL_REVISION[:12]}... (got {p}). Refusing to load."
            )
        if local_dir is None:
            local_dir = d
        elif os.path.normcase(d) != os.path.normcase(local_dir):
            raise RuntimeError(
                "cairn: pinned model files resolved to more than one snapshot "
                f"directory ({local_dir} vs {d}). Refusing to load."
            )

    for fname, expected in DEFAULT_MODEL_WEIGHT_SHA256.items():
        fpath = os.path.join(local_dir, *fname.split("/"))
        if not os.path.isfile(fpath):
            raise RuntimeError(
                f"cairn: pinned model snapshot is missing its expected weight "
                f"file '{fname}' (looked in {local_dir}). Refusing to load."
            )
        h = hashlib.sha256()
        with open(fpath, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        got = h.hexdigest()
        if got != expected:
            raise RuntimeError(
                "cairn: SUPPLY-CHAIN CHECK FAILED for the default embedding model. "
                f"'{fname}' at pinned revision {DEFAULT_MODEL_REVISION[:12]}... hashes "
                f"to {got}, but the trusted value is {expected}. Refusing to load a "
                "model whose weights do not match the pinned, pre-verified artifact. "
                "(If you deliberately changed the pin, update DEFAULT_MODEL_REVISION "
                "and DEFAULT_MODEL_WEIGHT_SHA256 together.)"
            )

    log.debug(
        "cairn: verified pinned default model %s @ %s",
        DEFAULT_MODEL, DEFAULT_MODEL_REVISION[:12],
    )
    return local_dir


@runtime_checkable
class EmbedBackend(Protocol):
    """
    The one thing every embed backend must do:
    take a list of strings, return a list of float32 byte blobs.

    dim property lets the vault know what it's storing.
    """
    @property
    def dim(self) -> int: ...

    def encode(self, texts: list[str]) -> list[bytes]:
        """Encode texts to float32 blobs."""
        ...

    def encode_one(self, text: str) -> bytes:
        """Convenience: encode a single text."""
        ...


class CPUEmbed:
    """
    sentence-transformers on CPU.
    Works everywhere. No GPU required. Lazy-loaded.
    ~91MB weight download on first use.
    ~50-200ms per batch depending on hardware.
    On RTX Spark NPU this auto-accelerates via sentence-transformers' ONNX path.
    """
    def __init__(self, model_name: str = DEFAULT_MODEL):
        self._model_name = model_name
        self._model = None

    @property
    def dim(self) -> int:
        return 384 if "MiniLM" in self._model_name else 768

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            # pre-load supply-chain gate: for the default model this returns the
            # verified local snapshot path; for custom models it returns None and
            # the original constructor call is used unchanged.
            verified_dir = _verify_default_model_or_raise(self._model_name)
            if verified_dir is not None:
                self._model = SentenceTransformer(verified_dir)
            else:
                self._model = SentenceTransformer(self._model_name)
            log.debug(f"cairn: loaded {self._model_name} on CPU")

    def encode(self, texts: list[str]) -> list[bytes]:
        self._load()
        vecs = self._model.encode(texts, batch_size=64, show_progress_bar=False)
        # dim-contract: refuse to write embeddings the vault can't search. The
        # store + index are fixed at DIM; a model producing any other size would
        # silently vanish from search. Fail LOUD here instead.
        blobs = []
        for v in vecs:
            v = v.tolist()
            if len(v) != DIM:
                raise RuntimeError(
                    f"cairn: embedder dim mismatch — model '{self._model_name}' "
                    f"produced {len(v)}-dim vectors but the vault stores and "
                    f"searches at {DIM} dims. Refusing to write mismatched "
                    f"embeddings (they would silently disappear from search). "
                    f"Use a {DIM}-dim model, or re-embed the whole vault for the "
                    f"new model.")
            blobs.append(struct.pack(f"{len(v)}f", *v))
        return blobs

    def encode_one(self, text: str) -> bytes:
        return self.encode([text])[0]


class ONNXNPUEmbed:
    """
    ONNX Runtime backend — runs on NPU when available.
    Works on RTX Spark (Blackwell NPU), Qualcomm Snapdragon X2 (Hexagon NPU),
    Intel Core Ultra (NPU), Apple ANE via CoreML EP.

    Falls back to CPU execution if no NPU available — still faster than
    raw PyTorch due to ONNX graph optimizations.

    Setup: export your model once with optimum:
      pip install optimum[exporters]
      optimum-cli export onnx --model sentence-transformers/all-MiniLM-L6-v2 ./onnx_model/

    Then set: CAIRN_ONNX_MODEL_PATH=./onnx_model/
    """
    def __init__(self, model_path: str | None = None):
        self._path = model_path or os.environ.get("CAIRN_ONNX_MODEL_PATH")
        self._session = None
        self._tokenizer = None

    @property
    def dim(self) -> int:
        return DIM

    def _load(self):
        if self._session is None:
            if not self._path:
                raise RuntimeError(
                    "CAIRN_ONNX_MODEL_PATH not set. "
                    "Export with: optimum-cli export onnx --model "
                    "sentence-transformers/all-MiniLM-L6-v2 ./onnx_model/"
                )
            import onnxruntime as ort
            from transformers import AutoTokenizer

            # provider priority: NPU > CUDA > CPU
            # ONNX Runtime auto-detects — DirectML for Windows NPU/GPU
            providers = ort.get_available_providers()
            log.debug(f"cairn: ONNX providers available: {providers}")

            self._session = ort.InferenceSession(
                str(self._path) + "/model.onnx",
                providers=providers
            )
            self._tokenizer = AutoTokenizer.from_pretrained(self._path)
            log.debug(f"cairn: loaded ONNX model from {self._path}")

    def encode(self, texts: list[str]) -> list[bytes]:
        import numpy as np
        self._load()

        enc = self._tokenizer(
            texts, padding=True, truncation=True,
            max_length=128, return_tensors="np"
        )
        outputs = self._session.run(None, dict(enc))
        # mean pooling over token embeddings
        token_embs  = outputs[0]
        attention   = enc["attention_mask"]
        mask_exp    = attention[:, :, np.newaxis].astype(float)
        sum_embs    = (token_embs * mask_exp).sum(axis=1)
        count       = mask_exp.sum(axis=1).clip(min=1e-9)
        pooled      = sum_embs / count

        # L2 normalize
        norms  = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
        pooled = pooled / norms

        return [struct.pack(f"{v.shape[0]}f", *v.tolist()) for v in pooled]

    def encode_one(self, text: str) -> bytes:
        return self.encode([text])[0]


class GPUEmbed:
    """
    FAISS-GPU / NVIDIA cuVS backend.
    Requires: pip install faiss-gpu sentence-transformers
    Requires: NVIDIA GPU with CUDA

    On RTX Spark (Blackwell): <5ms per batch, 1M nodes @ 3GB VRAM
    On B200 server: essentially instant, full vault in VRAM

    Set CAIRN_GPU_DEVICE=0 to choose GPU index.
    """
    def __init__(self, model_name: str = DEFAULT_MODEL,
                 device: int | None = None):
        self._model_name = model_name
        self._device = device if device is not None else int(
            os.environ.get("CAIRN_GPU_DEVICE", "0")
        )
        self._model = None

    @property
    def dim(self) -> int:
        return 384 if "MiniLM" in self._model_name else 768

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            import torch
            device = f"cuda:{self._device}" if torch.cuda.is_available() else "cpu"
            # pre-load supply-chain gate: default model → verified local snapshot
            # path; custom model → None, original constructor call unchanged.
            verified_dir = _verify_default_model_or_raise(self._model_name)
            if verified_dir is not None:
                self._model = SentenceTransformer(verified_dir, device=device)
            else:
                self._model = SentenceTransformer(self._model_name, device=device)
            log.debug(f"cairn: loaded {self._model_name} on {device}")

    def encode(self, texts: list[str]) -> list[bytes]:
        self._load()
        vecs = self._model.encode(
            texts, batch_size=256,   # larger batch on GPU
            show_progress_bar=False,
            convert_to_numpy=True
        )
        return [struct.pack(f"{len(v)}f", *v.tolist()) for v in vecs]

    def encode_one(self, text: str) -> bytes:
        return self.encode([text])[0]


class EmbedRouter:
    """
    Auto-detects the best available hardware and returns the right backend.

    CAIRN_EMBED_BACKEND env var overrides detection:
      cpu    → CPUEmbed (always works)
      onnx   → ONNXNPUEmbed (RTX Spark NPU, Qualcomm, Intel NPU)
      gpu    → GPUEmbed (NVIDIA CUDA)

    Without override, detection order:
      1. CUDA GPU present → GPUEmbed
      2. ONNX model path set + onnxruntime installed → ONNXNPUEmbed
      3. CPU fallback → CPUEmbed

    This means: on RTX Spark with ONNX model exported, you get NPU acceleration
    automatically. On a server with A100, you get GPU automatically.
    On any machine, you get CPU as fallback.
    """

    @staticmethod
    def detect() -> EmbedBackend:
        override = os.environ.get("CAIRN_EMBED_BACKEND", "").lower()

        if override == "gpu":
            log.debug("cairn: EmbedRouter → GPUEmbed (explicit override)")
            return GPUEmbed()

        if override == "onnx":
            log.debug("cairn: EmbedRouter → ONNXNPUEmbed (explicit override)")
            return ONNXNPUEmbed()

        if override == "cpu" or override:
            log.debug("cairn: EmbedRouter → CPUEmbed (explicit override)")
            return CPUEmbed()

        # auto-detect
        try:
            import torch
            if torch.cuda.is_available():
                log.debug("cairn: EmbedRouter → GPUEmbed (CUDA detected)")
                return GPUEmbed()
        except ImportError:
            pass

        onnx_path = os.environ.get("CAIRN_ONNX_MODEL_PATH")
        if onnx_path:
            try:
                import onnxruntime  # noqa: F401
                log.debug("cairn: EmbedRouter → ONNXNPUEmbed (ONNX model + runtime found)")
                return ONNXNPUEmbed(onnx_path)
            except ImportError:
                pass

        log.debug("cairn: EmbedRouter → CPUEmbed (CPU fallback)")
        return CPUEmbed()


# module-level singleton — one embedder per process
_embedder: EmbedBackend | None = None


def get_embedder(force_reload: bool = False) -> EmbedBackend:
    """
    Get the process-level embedder singleton.
    Auto-routes to best available hardware.
    Set CAIRN_EMBED_BACKEND=cpu/onnx/gpu to override.
    """
    global _embedder
    if _embedder is None or force_reload:
        _embedder = EmbedRouter.detect()
    return _embedder
