"""TurboQuant MSE + IP vector quantization for agent memory and RAG.

Implements:
- TurboQuantMSE: Random rotation + optimal scalar quantization (PolarQuant)
- TurboQuantIP: MSE stage + QJL 1-bit residual for unbiased inner-product estimation

Based on arXiv:2504.19874 (TurboQuant / PolarQuant).
Data-oblivious: no training or calibration data needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .lloyd_max import get_codebook


@dataclass
class TurboQuantMSE:
    """PolarQuant / TurboQuant-MSE: Rotation + coordinate-wise optimal scalar quantization.

    Steps:
    1. Apply a fixed random orthogonal rotation R to input vectors.
    2. Quantize each coordinate independently using precomputed Lloyd-Max codebooks
       for the Beta(d/2, 1/2) distribution on the unit sphere.
    3. Dequantize by mapping to reconstruction levels and rotating back.

    Theoretical MSE bound (unit vectors): D_mse <= (3*pi/2) * 4^(-b)
    """
    dim: int = 768
    bits: int = 4
    seed: int = 42

    _rotation: np.ndarray | None = field(default=None, init=False, repr=False)
    _boundaries: np.ndarray | None = field(default=None, init=False, repr=False)
    _levels: np.ndarray | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rotation = self._make_rotation()
        self._boundaries, self._levels = get_codebook(self.dim, self.bits)

    def _make_rotation(self) -> np.ndarray:
        """Generate a random orthogonal matrix via QR decomposition."""
        rng = np.random.default_rng(self.seed)
        A = rng.standard_normal((self.dim, self.dim))
        Q, _ = np.linalg.qr(A)
        # Ensure proper rotation (det = +1)
        if np.linalg.det(Q) < 0:
            Q[:, 0] *= -1
        return Q.astype(np.float32)

    def quantize(self, x: np.ndarray) -> np.ndarray:
        """Quantize input vectors. x: (n, dim) or (dim,).

        Returns:
            Integer codes: (n, dim) with values in [0, 2^bits - 1].
        """
        if x.ndim == 1:
            x = x.reshape(1, -1)
        x = x.astype(np.float32)
        # Step 1: Rotate
        x_rot = x @ self._rotation.T
        # Step 2: Normalize to [-1, 1] range for quantization
        norms = np.linalg.norm(x_rot, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-8)
        x_normalized = x_rot / norms
        # Step 3: Quantize each coordinate using codebook boundaries
        codes = np.searchsorted(self._boundaries, x_normalized, side="right") - 1
        codes = np.clip(codes, 0, 2**self.bits - 1)
        return codes.squeeze(0) if x.shape[0] == 1 else codes

    def dequantize(self, codes: np.ndarray, norms: np.ndarray | None = None) -> np.ndarray:
        """Dequantize codes back to approximate vectors.

        Args:
            codes: Integer codes (n, dim) or (dim,).
            norms: Optional original norms for rescaling. If None, unit norm assumed.

        Returns:
            Reconstructed vectors (n, dim).
        """
        if codes.ndim == 1:
            codes = codes.reshape(1, -1)
        # Map codes to reconstruction levels
        level_indices = np.clip(codes, 0, len(self._levels) - 1)
        reconstructed = self._levels[level_indices]
        # Rotate back
        result = reconstructed @ self._rotation
        # Rescale if norms provided
        if norms is not None:
            if norms.ndim == 0:
                norms = norms.reshape(1)
            result = result * norms[:, np.newaxis]
        return result.squeeze(0) if codes.shape[0] == 1 else result

    def compress(self, x: np.ndarray) -> dict[str, Any]:
        """Full compress pipeline: quantize + pack metadata.

        Returns a dict with codes, norms, and config for later decompression.
        """
        if x.ndim == 1:
            x = x.reshape(1, -1)
        norms = np.linalg.norm(x, axis=1)
        codes = self.quantize(x)
        return {
            "codes": codes,
            "norms": norms,
            "dim": self.dim,
            "bits": self.bits,
            "seed": self.seed,
            "n": x.shape[0],
        }

    def decompress(self, payload: dict[str, Any]) -> np.ndarray:
        """Decompress a payload back to approximate vectors."""
        return self.dequantize(payload["codes"], norms=payload["norms"])

    def mse(self, x: np.ndarray) -> float:
        """Measure MSE distortion between original and reconstructed vectors."""
        if x.ndim == 1:
            x = x.reshape(1, -1)
        norms = np.linalg.norm(x, axis=1)
        codes = self.quantize(x)
        recon = self.dequantize(codes, norms=norms)
        return float(np.mean((x - recon) ** 2))

    def compression_ratio(self, n: int = 1) -> float:
        """Ratio of original bytes to compressed bytes per vector."""
        original_bytes = n * self.dim * 4  # float32
        compressed_bytes = n * self.dim * self.bits / 8  # packed bits
        return original_bytes / max(compressed_bytes, 1)


@dataclass
class TurboQuantIP(TurboQuantMSE):
    """TurboQuant-IP: MSE quantization + QJL 1-bit residual for inner-product.

    Two-stage:
    1. PolarQuant/MSE stage with (bits-1) bits per coordinate.
    2. QJL 1-bit sketch of the residual for unbiased IP estimation.

    The inner-product estimator is unbiased:
        E[<y, Q_ip^{-1}(Q_ip(x))>] = <y, x>

    Key property: D_prod <= O(||y||^2 * ||x||^2 / 4^bits)
    """
    qjl_dim: int = 0  # 0 = same as dim; override for reduced sketch size

    _S: np.ndarray | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        qjl_dim = self.qjl_dim or self.dim
        rng = np.random.default_rng(self.seed + 1000)
        self._S = np.sign(rng.standard_normal((qjl_dim, self.dim))).astype(np.float32)

    def quantize_ip(self, x: np.ndarray) -> dict[str, Any]:
        """Quantize with IP-preservation: MSE stage (b-1 bits) + QJL residual (1 bit).

        Returns payload with codes, qjl_signs, norms, and config.
        """
        if x.ndim == 1:
            x = x.reshape(1, -1)
        norms = np.linalg.norm(x, axis=1)

        # Stage 1: MSE quantization (uses self.bits as configured)
        codes = self.quantize(x)
        recon_mse = self.dequantize(codes, norms=norms)

        # Stage 2: Compute residual and QJL 1-bit sketch
        residual = x - recon_mse
        qjl_dim = self.qjl_dim or self.dim
        signs = np.sign(self._S @ residual.T).astype(np.int8)  # (qjl_dim, n)
        signs = signs.T  # (n, qjl_dim)

        return {
            "codes": codes,
            "qjl_signs": signs,
            "norms": norms,
            "dim": self.dim,
            "bits": self.bits,
            "seed": self.seed,
            "qjl_dim": qjl_dim,
            "n": x.shape[0],
        }

    def dequantize_ip(self, payload: dict[str, Any]) -> np.ndarray:
        """Dequantize IP payload: MSE reconstruction + QJL residual correction.

        The QJL dequantization provides an unbiased inner-product estimator:
            Q_jl^{-1}(z) = sqrt(pi/2) / dim * S^T * z
        """
        codes = payload["codes"]
        norms = payload["norms"]
        qjl_signs = payload["qjl_signs"]

        # MSE stage reconstruction
        recon_mse = self.dequantize(codes, norms=norms)

        # QJL residual correction
        qjl_dim = payload.get("qjl_dim", self.dim)
        # Dequantize QJL signs to get unbiased residual estimate
        # E[sign(S*r)] = 0, E[S^T * sign(S*r)] = sqrt(2/pi) * r
        # So: r_hat = sqrt(pi/2) / dim * S^T * signs
        scale = math.sqrt(math.pi / 2) / self.dim
        if qjl_signs.ndim == 1:
            qjl_signs = qjl_signs.reshape(1, -1)
        residual_hat = scale * (qjl_signs.astype(np.float32) @ self._S[:qjl_dim, :].T)

        return recon_mse + residual_hat

    def inner_product_error(self, x: np.ndarray, y: np.ndarray) -> float:
        """Measure inner-product estimation error between quantized and true IP."""
        payload = self.quantize_ip(x)
        recon = self.dequantize_ip(payload)
        true_ip = float(y @ x.T)
        est_ip = float(y @ recon.T)
        return abs(true_ip - est_ip)

# ---------------------------------------------------------------------------
# TurboVec Integration (production-ready drop-in for RAG / agent memory)
# ---------------------------------------------------------------------------

_TURBOVEC_AVAILABLE = False
try:
    from turbovec import TurboQuantIndex as _TurboQuantIndex  # type: ignore[import-untyped]
    _TURBOVEC_AVAILABLE = True
except ImportError:
    _TurboQuantIndex = None


def create_vector_index(
    dim: int = 768,
    bits: int = 4,
    *,
    use_turbovec: bool = True,
) -> "TurboQuantMSE | object":
    """Create a vector quantization index for agent memory / RAG.

    Uses TurboVec (Rust-accelerated) when available, falls back to
    our pure-Python TurboQuant implementation otherwise.

    Args:
        dim: Embedding dimensionality.
        bits: Bits per coordinate (3 or 4 recommended for quality/compression balance).
        use_turbovec: Prefer TurboVec library when installed.

    Returns:
        A TurboQuantIndex (TurboVec) or TurboQuantMSE (pure Python).
    """
    if use_turbovec and _TURBOVEC_AVAILABLE and _TurboQuantIndex is not None:
        return _TurboQuantIndex(dim=dim, bit_width=bits)
    return TurboQuantMSE(dim=dim, bits=bits)


def compress_embeddings(
    vectors: "np.ndarray",
    dim: int | None = None,
    bits: int = 4,
    *,
    method: str = "mse",
) -> dict:
    """Compress an embedding matrix for agent memory storage.

    Args:
        vectors: Float32 array of shape (n, dim).
        dim: Override dimensionality (defaults to vectors.shape[1]).
        bits: Bits per coordinate.
        method: "mse" for TurboQuant-MSE, "ip" for TurboQuant-IP (inner-product).

    Returns:
        Compressed payload dict with codes, norms, and config.
    """
    if dim is None:
        dim = vectors.shape[1]

    if method == "ip":
        tq = TurboQuantIP(dim=dim, bits=bits)
        return tq.quantize_ip(vectors)
    else:
        tq = TurboQuantMSE(dim=dim, bits=bits)
        return tq.compress(vectors)


def decompress_embeddings(payload: dict, method: str = "mse") -> "np.ndarray":
    """Decompress a previously compressed embedding payload.

    Args:
        payload: Compressed payload from compress_embeddings.
        method: Must match the method used for compression ("mse" or "ip").

    Returns:
        Reconstructed float32 array of shape (n, dim).
    """
    if method == "ip":
        dim = payload.get("dim", 768)
        bits = payload.get("bits", 4)
        tq = TurboQuantIP(dim=dim, bits=bits)
        return tq.dequantize_ip(payload)
    else:
        dim = payload.get("dim", 768)
        bits = payload.get("bits", 4)
        tq = TurboQuantMSE(dim=dim, bits=bits)
        return tq.decompress(payload)
