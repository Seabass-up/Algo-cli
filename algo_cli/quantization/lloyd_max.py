"""Lloyd-Max optimal scalar quantizer codebook generation.

Precomputes quantization boundaries and reconstruction levels for coordinates
distributed on the unit hypersphere (Beta distribution), as required by
TurboQuant/PolarQuant for data-oblivious codebooks.
"""

from __future__ import annotations

import math
import threading

import numpy as np


def beta_distribution_pdf(x: np.ndarray, d: int) -> np.ndarray:
    """Compute the PDF of coordinate distribution on the unit sphere in R^d.

    Each coordinate of a random unit vector in R^d follows the symmetric density:
        f(x) = Gamma(d/2) / (sqrt(pi) Gamma((d-1)/2))
               * (1 - x^2)^((d-3)/2), x in [-1, 1]

    For high dimensions, this concentrates around 0 (approaches Gaussian).
    """
    if d < 2:
        raise ValueError("dimension must be at least 2")

    values = np.asarray(x, dtype=np.float64)
    density = np.zeros_like(values)
    inside = np.abs(values) <= 1.0
    if not np.any(inside):
        return density

    exponent = (d - 3) / 2.0
    log_normalizer = (
        math.lgamma(d / 2.0)
        - 0.5 * math.log(math.pi)
        - math.lgamma((d - 1) / 2.0)
    )
    # The d=2 density has integrable singularities at +/-1. Clipping only
    # protects direct endpoint evaluations; codebook integration uses bin
    # midpoints and therefore never samples those singularities.
    base = np.maximum(
        1.0 - np.square(values[inside]),
        np.finfo(np.float64).tiny,
    )
    density[inside] = np.exp(log_normalizer + exponent * np.log(base))
    return density


def precompute_codebook(
    dim: int,
    bits: int = 4,
    *,
    n_grid: int = 10_000,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute Lloyd-Max optimal quantization codebook for sphere coordinates.

    Args:
        dim: Dimensionality of the embedding space (determines the Beta shape).
        bits: Number of bits per coordinate (2^bits levels).
        n_grid: Grid points for numerical integration.
        max_iter: Maximum Lloyd iterations.
        tol: Convergence tolerance for boundary shifts.

    Returns:
        (boundaries, levels) where boundaries has 2^bits + 1 entries
        and levels has 2^bits entries.
    """
    if dim < 2:
        raise ValueError("dimension must be at least 2")
    if bits < 0 or bits > 8:
        raise ValueError("bits must be between 0 and 8")
    if max_iter < 1:
        raise ValueError("max_iter must be positive")
    if tol <= 0:
        raise ValueError("tol must be positive")

    k = 2**bits
    if n_grid < max(128, 4 * k):
        raise ValueError(f"n_grid must be at least {max(128, 4 * k)}")

    # PDF of coordinate on unit sphere
    dx = 2.0 / n_grid
    grid = -1.0 + (np.arange(n_grid, dtype=np.float64) + 0.5) * dx
    pdf = beta_distribution_pdf(grid, dim)

    # Initialize boundaries uniformly by CDF
    cdf = np.cumsum(pdf) * dx
    cdf = np.clip(cdf / max(cdf[-1], 1e-12), 0, 1)
    boundaries = np.interp(np.linspace(0, 1, k + 1), cdf, grid)
    boundaries[0] = -1.0
    boundaries[-1] = 1.0

    # Lloyd iterations: recompute levels as centroids, recompute boundaries as midpoints
    for _ in range(max_iter):
        levels = np.zeros(k)
        for i in range(k):
            mask = (grid >= boundaries[i]) & (grid < boundaries[i + 1])
            weight = pdf[mask]
            if weight.sum() > tol:
                levels[i] = np.average(grid[mask], weights=weight)
            else:
                levels[i] = (boundaries[i] + boundaries[i + 1]) / 2

        # The source density is exactly symmetric. Enforcing that invariant
        # prevents tiny grid/CDF rounding errors from biasing the codebook.
        levels = 0.5 * (levels - levels[::-1])

        # New boundaries are midpoints between adjacent levels
        new_boundaries = np.zeros(k + 1)
        new_boundaries[0] = -1.0
        new_boundaries[-1] = 1.0
        for i in range(1, k):
            new_boundaries[i] = (levels[i - 1] + levels[i]) / 2

        shift = np.max(np.abs(new_boundaries - boundaries))
        boundaries = new_boundaries
        if shift < tol:
            break

    return boundaries, levels


# Pre-built codebooks for common dimensions and bit widths
_CODEBOOK_CACHE: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
_CODEBOOK_LOCK = threading.Lock()


def get_codebook(dim: int, bits: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """Get a cached codebook, computing on first access."""
    key = (dim, bits)
    with _CODEBOOK_LOCK:
        cached = _CODEBOOK_CACHE.get(key)
        if cached is None:
            cached = precompute_codebook(dim, bits)
            for values in cached:
                values.setflags(write=False)
            _CODEBOOK_CACHE[key] = cached
        return cached
