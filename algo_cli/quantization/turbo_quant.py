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
from typing import Any, Literal

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

    _rotation: np.ndarray = field(init=False, repr=False)
    _boundaries: np.ndarray = field(init=False, repr=False)
    _levels: np.ndarray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.dim < 2:
            raise ValueError("dim must be at least 2")
        if self.bits < 0 or self.bits > 8:
            raise ValueError("bits must be between 0 and 8")
        self._rotation = self._make_rotation()
        self._boundaries, self._levels = get_codebook(self.dim, self._mse_bits())

    def _mse_bits(self) -> int:
        """Bit width used by the MSE stage."""
        return self.bits

    def _coerce_vectors(self, x: np.ndarray, *, name: str = "x") -> tuple[np.ndarray, bool]:
        """Validate vectors and return a float32 matrix plus original rank."""
        values = np.asarray(x, dtype=np.float32)
        vector_input = values.ndim == 1
        if values.ndim not in (1, 2):
            raise ValueError(f"{name} must be a 1D vector or 2D matrix")
        if values.shape[-1] != self.dim:
            raise ValueError(
                f"{name} has dimension {values.shape[-1]}; expected {self.dim}"
            )
        if not np.all(np.isfinite(values)):
            raise ValueError(f"{name} must contain only finite values")
        if vector_input:
            values = values.reshape(1, -1)
        return values, vector_input

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
        x, vector_input = self._coerce_vectors(x)
        # Step 1: Rotate
        x_rot = x @ self._rotation.T
        # Step 2: Normalize to [-1, 1] range for quantization
        norms = np.linalg.norm(x_rot, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-8)
        x_normalized = x_rot / norms
        # Step 3: Quantize each coordinate using codebook boundaries
        codes = np.searchsorted(self._boundaries, x_normalized, side="right") - 1
        codes = np.clip(codes, 0, 2 ** self._mse_bits() - 1).astype(np.uint8)
        return codes[0] if vector_input else codes

    def dequantize(self, codes: np.ndarray, norms: np.ndarray | None = None) -> np.ndarray:
        """Dequantize codes back to approximate vectors.

        Args:
            codes: Integer codes (n, dim) or (dim,).
            norms: Optional original norms for rescaling. If None, unit norm assumed.

        Returns:
            Reconstructed vectors (n, dim).
        """
        codes = np.asarray(codes)
        vector_input = codes.ndim == 1
        if codes.ndim not in (1, 2):
            raise ValueError("codes must be a 1D vector or 2D matrix")
        if codes.shape[-1] != self.dim:
            raise ValueError(
                f"codes have dimension {codes.shape[-1]}; expected {self.dim}"
            )
        if not np.issubdtype(codes.dtype, np.integer):
            raise ValueError("codes must use an integer dtype")
        if vector_input:
            codes = codes.reshape(1, -1)
        # Map codes to reconstruction levels
        level_indices = np.clip(codes, 0, len(self._levels) - 1)
        reconstructed = self._levels[level_indices]
        # Rotate back
        result = reconstructed @ self._rotation
        # Rescale if norms provided
        if norms is not None:
            norm_values = np.asarray(norms, dtype=np.float32).reshape(-1)
            if norm_values.size != result.shape[0]:
                raise ValueError(
                    f"norm count {norm_values.size} does not match "
                    f"vector count {result.shape[0]}"
                )
            if not np.all(np.isfinite(norm_values)) or np.any(norm_values < 0):
                raise ValueError("norms must be finite and non-negative")
            result = result * norm_values[:, np.newaxis]
        return result[0] if vector_input else result

    def compress(self, x: np.ndarray) -> dict[str, Any]:
        """Full compress pipeline: quantize + pack metadata.

        Returns a dict with codes, norms, and config for later decompression.
        """
        matrix, vector_input = self._coerce_vectors(x)
        norms = np.linalg.norm(matrix, axis=1).astype(np.float32)
        codes = self.quantize(x)
        return {
            "codes": codes,
            "norms": norms,
            "dim": self.dim,
            "bits": self.bits,
            "mse_bits": self._mse_bits(),
            "seed": self.seed,
            "n": matrix.shape[0],
            "method": "mse",
            "vector_input": vector_input,
        }

    def decompress(self, payload: dict[str, Any]) -> np.ndarray:
        """Decompress a payload back to approximate vectors."""
        self._validate_payload_config(payload, method="mse")
        return self.dequantize(payload["codes"], norms=payload["norms"])

    def _validate_payload_config(self, payload: dict[str, Any], *, method: str) -> None:
        """Reject payloads that were encoded with an incompatible quantizer."""
        payload_method = payload.get("method")
        if payload_method is not None and payload_method != method:
            raise ValueError(
                f"payload method is {payload_method!r}; expected {method!r}"
            )
        expected = {"dim": self.dim, "bits": self.bits, "seed": self.seed}
        for key, value in expected.items():
            if key in payload and int(payload[key]) != value:
                raise ValueError(
                    f"payload {key} is {payload[key]!r}; expected {value!r}"
                )

    def mse(self, x: np.ndarray) -> float:
        """Measure MSE distortion between original and reconstructed vectors."""
        matrix, vector_input = self._coerce_vectors(x)
        source = matrix[0] if vector_input else matrix
        norms = np.linalg.norm(matrix, axis=1)
        codes = self.quantize(source)
        recon = self.dequantize(codes, norms=norms)
        return float(np.mean((source - recon) ** 2))

    def compression_ratio(self, n: int = 1) -> float:
        """Ratio of float32 bytes to the in-memory NumPy payload bytes."""
        if n < 1:
            raise ValueError("n must be positive")
        original_bytes = n * self.dim * 4  # float32
        # Quantized codes are stored as uint8 and each vector retains a float32
        # norm. This reports real payload storage rather than an unimplemented
        # bit-packing ideal.
        compressed_bytes = n * self.dim + n * 4
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

    _S: np.ndarray = field(init=False, repr=False)

    def _mse_bits(self) -> int:
        return self.bits - 1

    def __post_init__(self) -> None:
        if self.bits < 1:
            raise ValueError("TurboQuantIP requires at least 1 total bit")
        super().__post_init__()
        qjl_dim = self.qjl_dim or self.dim
        if qjl_dim < 1:
            raise ValueError("qjl_dim must be positive")
        rng = np.random.default_rng(self.seed + 1000)
        self._S = rng.standard_normal((qjl_dim, self.dim)).astype(np.float32)

    def quantize_ip(self, x: np.ndarray) -> dict[str, Any]:
        """Quantize with IP-preservation: MSE stage (b-1 bits) + QJL residual (1 bit).

        Returns payload with codes, qjl_signs, norms, and config.
        """
        matrix, vector_input = self._coerce_vectors(x)
        norms = np.linalg.norm(matrix, axis=1).astype(np.float32)

        # Stage 1: MSE quantization uses one less bit than the total budget.
        codes = self.quantize(matrix)
        recon_mse = self.dequantize(codes, norms=norms)

        # Stage 2: Compute residual and QJL 1-bit sketch
        residual = matrix - recon_mse
        residual_norms = np.linalg.norm(residual, axis=1).astype(np.float32)
        qjl_dim = self.qjl_dim or self.dim
        projections = residual @ self._S.T
        signs = np.where(projections >= 0, 1, -1).astype(np.int8)

        return {
            "codes": codes[0] if vector_input else codes,
            "qjl_signs": signs[0] if vector_input else signs,
            "norms": norms,
            "residual_norms": residual_norms,
            "dim": self.dim,
            "bits": self.bits,
            "mse_bits": self._mse_bits(),
            "seed": self.seed,
            "qjl_dim": qjl_dim,
            "n": matrix.shape[0],
            "method": "ip",
            "vector_input": vector_input,
        }

    def dequantize_ip(self, payload: dict[str, Any]) -> np.ndarray:
        """Dequantize IP payload: MSE reconstruction + QJL residual correction.

        The QJL dequantization provides an unbiased inner-product estimator:
            Q_jl^{-1}(z) = sqrt(pi/2) / dim * S^T * z
        """
        self._validate_payload_config(payload, method="ip")
        codes = np.asarray(payload["codes"])
        norms = payload["norms"]
        qjl_signs = np.asarray(payload["qjl_signs"])
        vector_input = codes.ndim == 1
        if vector_input:
            codes = codes.reshape(1, -1)

        # MSE stage reconstruction
        recon_mse = self.dequantize(codes, norms=norms)

        # QJL residual correction
        qjl_dim = int(payload.get("qjl_dim", self.dim))
        if qjl_dim != self._S.shape[0]:
            raise ValueError(
                f"payload qjl_dim is {qjl_dim}; expected {self._S.shape[0]}"
            )
        if qjl_signs.ndim == 1:
            qjl_signs = qjl_signs.reshape(1, -1)
        if qjl_signs.shape != (codes.shape[0], qjl_dim):
            raise ValueError(
                f"qjl_signs shape is {qjl_signs.shape}; "
                f"expected {(codes.shape[0], qjl_dim)}"
            )
        if not np.all((qjl_signs == -1) | (qjl_signs == 1)):
            raise ValueError("qjl_signs must contain only -1 or +1")
        if "residual_norms" not in payload:
            raise ValueError("IP payload is missing residual_norms")
        residual_norms = np.asarray(
            payload["residual_norms"], dtype=np.float32
        ).reshape(-1)
        if residual_norms.size != codes.shape[0]:
            raise ValueError(
                "residual norm count does not match the number of vectors"
            )
        if not np.all(np.isfinite(residual_norms)) or np.any(residual_norms < 0):
            raise ValueError("residual_norms must be finite and non-negative")

        # Dequantize QJL signs to get unbiased residual estimate
        # E[sign(S*r)] = 0, E[S^T * sign(S*r)] = sqrt(2/pi) * r
        # So: r_hat = sqrt(pi/2) / m * ||r|| * S^T * signs.
        scale = math.sqrt(math.pi / 2) / qjl_dim
        residual_hat = (
            scale
            * residual_norms[:, np.newaxis]
            * (qjl_signs.astype(np.float32) @ self._S)
        )

        result = recon_mse + residual_hat
        return result[0] if vector_input else result

    def inner_product_error(self, x: np.ndarray, y: np.ndarray) -> float:
        """Measure inner-product estimation error between quantized and true IP."""
        x_values = np.asarray(x, dtype=np.float32)
        y_values = np.asarray(y, dtype=np.float32)
        if x_values.shape != (self.dim,) or y_values.shape != (self.dim,):
            raise ValueError("x and y must both be one-dimensional vectors")
        payload = self.quantize_ip(x_values)
        recon = self.dequantize_ip(payload)
        true_ip = float(np.dot(y_values, x_values))
        est_ip = float(np.dot(y_values, recon))
        return abs(true_ip - est_ip)

# ---------------------------------------------------------------------------
# TurboVec Integration (production-ready drop-in for RAG / agent memory)
# ---------------------------------------------------------------------------

_TURBOVEC_AVAILABLE = False
try:
    from turbovec import TurboQuantIndex as _TurboQuantIndex  # type: ignore[import-not-found]
    _TURBOVEC_AVAILABLE = True
except ImportError:
    _TurboQuantIndex = None


def create_vector_index(
    dim: int = 768,
    bits: int = 4,
    *,
    use_turbovec: bool = True,
) -> object:
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
    vectors: np.ndarray,
    dim: int | None = None,
    bits: int = 4,
    *,
    method: Literal["mse", "ip"] = "mse",
) -> dict[str, Any]:
    """Compress an embedding matrix for agent memory storage.

    Args:
        vectors: Float32 array of shape (n, dim).
        dim: Override dimensionality (defaults to vectors.shape[1]).
        bits: Bits per coordinate.
        method: "mse" for TurboQuant-MSE, "ip" for TurboQuant-IP (inner-product).

    Returns:
        Compressed payload dict with codes, norms, and config.
    """
    values = np.asarray(vectors)
    if values.ndim not in (1, 2):
        raise ValueError("vectors must be a 1D vector or 2D matrix")
    if dim is None:
        dim = values.shape[-1]
    if dim != values.shape[-1]:
        raise ValueError(
            f"dim is {dim}, but vectors have dimension {values.shape[-1]}"
        )

    if method == "ip":
        return TurboQuantIP(dim=dim, bits=bits).quantize_ip(values)
    if method == "mse":
        return TurboQuantMSE(dim=dim, bits=bits).compress(values)
    raise ValueError("method must be 'mse' or 'ip'")


def decompress_embeddings(
    payload: dict[str, Any],
    method: Literal["mse", "ip"] | None = None,
) -> np.ndarray:
    """Decompress a previously compressed embedding payload.

    Args:
        payload: Compressed payload from compress_embeddings.
        method: Must match the method used for compression ("mse" or "ip").

    Returns:
        Reconstructed float32 array of shape (n, dim).
    """
    payload_method = str(payload.get("method", "mse"))
    selected_method = method or payload_method
    if selected_method not in {"mse", "ip"}:
        raise ValueError("method must be 'mse' or 'ip'")
    if "method" in payload and payload_method != selected_method:
        raise ValueError(
            f"payload method is {payload_method!r}; requested {selected_method!r}"
        )

    dim = int(payload.get("dim", 768))
    bits = int(payload.get("bits", 4))
    seed = int(payload.get("seed", 42))
    if selected_method == "ip":
        qjl_dim = int(payload.get("qjl_dim", dim))
        return TurboQuantIP(
            dim=dim, bits=bits, seed=seed, qjl_dim=qjl_dim
        ).dequantize_ip(payload)
    return TurboQuantMSE(dim=dim, bits=bits, seed=seed).decompress(payload)
