"""Lloyd-Max optimal scalar quantizer codebook generation.

Precomputes quantization boundaries and reconstruction levels for coordinates
distributed on the unit hypersphere (Beta distribution), as required by
TurboQuant/PolarQuant for data-oblivious codebooks.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
from scipy.stats import beta as beta_dist


def beta_distribution_pdf(x: np.ndarray, d: int) -> np.ndarray:
    """Compute the PDF of coordinate distribution on the unit sphere in R^d.

    Each coordinate of a random unit vector in R^d follows a scaled Beta distribution:
        f(x) = (1/B(1/2, (d-1)/2)) * (1 - x^2)^((d-3)/2) / (2 * sqrt(pi))

    For high dimensions, this concentrates around 0 (approaches Gaussian).
    """
    half = (d - 1) / 2.0
    return beta_dist.pdf(x, 0.5, half, loc=0, scale=1)


def precompute_codebook(
    dim: int,
    bits: int = 4,
    *,
    n_grid: int = 10_000,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> Tuple[np.ndarray, np.ndarray]:
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
    k = 2 ** bits
    half = (dim - 1) / 2.0

    # PDF of coordinate on unit sphere
    grid = np.linspace(-1, 1, n_grid)
    dx = grid[1] - grid[0]
    pdf = beta_dist.pdf(grid, 0.5, half, loc=0, scale=1)
    pdf = np.nan_to_num(pdf, nan=0.0, posinf=0.0, neginf=0.0)

    # Initialize boundaries uniformly by CDF
    cdf = np.cumsum(pdf) * dx
    cdf = np.clip(cdf / max(cdf[-1], 1e-12), 0, 1)
    boundaries = np.interp(np.linspace(0, 1, k + 1), cdf, grid)

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
_CODEBOOK_CACHE: dict[tuple[int, int], tuple] = {}


def get_codebook(dim: int, bits: int = 4) -> Tuple[np.ndarray, np.ndarray]:
    """Get a cached codebook, computing on first access."""
    key = (dim, bits)
    if key not in _CODEBOOK_CACHE:
        _CODEBOOK_CACHE[key] = precompute_codebook(dim, bits)
    return _CODEBOOK_CACHE[key]