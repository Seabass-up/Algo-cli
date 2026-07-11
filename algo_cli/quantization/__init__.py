"""TurboQuant-inspired vector quantization for agent memory and RAG compression.

Implements PolarQuant/TurboQuant-MSE (random rotation + optimal scalar quantization)
and QJL residual correction for inner-product preservation, based on arXiv:2504.19874.

Local-first: uses NumPy for all operations. No external ML frameworks required.
TurboVec (Rust-accelerated) is used when available for production throughput.
"""

from .turbo_quant import TurboQuantMSE, TurboQuantIP
from .lloyd_max import precompute_codebook, beta_distribution_pdf

# High-level API
from .turbo_quant import create_vector_index, compress_embeddings, decompress_embeddings

__all__ = [
    "TurboQuantMSE",
    "TurboQuantIP",
    "precompute_codebook",
    "beta_distribution_pdf",
    "create_vector_index",
    "compress_embeddings",
    "decompress_embeddings",
]
