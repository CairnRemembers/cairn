"""
cairn/backends — hardware-swappable storage and embedding backends.

Today:    SQLite + CPU sentence-transformers
Fall 2026: LanceDB + ONNX NPU (RTX Spark, Qualcomm Snapdragon)
2027+:    Unified memory pool + FAISS-GPU (cuVS) + CXL tier

The protocols define the interface. Swap the backend, nothing else changes.
"""
from .embed import EmbedBackend, EmbedRouter, CPUEmbed, get_embedder

__all__ = ["EmbedBackend", "EmbedRouter", "CPUEmbed", "get_embedder"]
