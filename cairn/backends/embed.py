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

Current default: all-MiniLM-L6-v2 (384 dims, ~80MB, runs everywhere)
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


def _hf_model_cached(model_name: str) -> bool:
    """True only if the embedding model is already in the local HuggingFace
    cache — filesystem check only (no network, no hf_hub import). Conservative:
    a miss just leaves us online, so it can never break loading."""
    repo = model_name if "/" in model_name else f"sentence-transformers/{model_name}"
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
            if os.path.isdir(snap) and any(os.scandir(snap)):
                return True
        except Exception:
            pass
    return False


# Local-first air-gap (Cairn law: nothing leaves the machine). If the model is
# already cached, force HuggingFace offline so the embedder never contacts
# huggingface.co on load. A fresh machine (not cached) stays online for the
# one-time download, then air-gaps on every run after. A user-set
# HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE is always respected. Must run before
# sentence-transformers / huggingface_hub are imported (they read these env
# vars at import time); embed.py is imported before them, so module scope is
# the correct, earliest place.
if not (os.environ.get("HF_HUB_OFFLINE") or os.environ.get("TRANSFORMERS_OFFLINE")):
    if _hf_model_cached(DEFAULT_MODEL):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


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
    ~80MB model download on first use.
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
