from __future__ import annotations

import numpy as np
import pytest

from algo_cli.quantization.lloyd_max import beta_distribution_pdf, precompute_codebook
from algo_cli.quantization.turbo_quant import (
    TurboQuantIP,
    TurboQuantMSE,
    compress_embeddings,
    decompress_embeddings,
)


@pytest.mark.parametrize("dim", [2, 3, 16])
def test_sphere_coordinate_density_is_symmetric_and_normalized(dim: int) -> None:
    count = 20_000
    step = 2.0 / count
    grid = -1.0 + (np.arange(count) + 0.5) * step

    density = beta_distribution_pdf(grid, dim)

    np.testing.assert_allclose(density, density[::-1], rtol=1e-10, atol=1e-10)
    assert float(density.sum() * step) == pytest.approx(1.0, abs=0.005)


def test_lloyd_max_codebook_preserves_distribution_symmetry() -> None:
    boundaries, levels = precompute_codebook(16, 3, n_grid=2_000)

    assert boundaries.shape == (9,)
    assert levels.shape == (8,)
    assert boundaries[0] == -1.0
    assert boundaries[-1] == 1.0
    assert np.all(np.diff(boundaries) > 0)
    assert np.all(np.diff(levels) > 0)
    np.testing.assert_allclose(boundaries, -boundaries[::-1], atol=1e-12)
    np.testing.assert_allclose(levels, -levels[::-1], atol=1e-12)


def test_mse_quantizer_preserves_input_rank_and_round_trips() -> None:
    rng = np.random.default_rng(7)
    vector = rng.standard_normal(8).astype(np.float32)
    matrix = vector.reshape(1, -1)
    quantizer = TurboQuantMSE(dim=8, bits=4, seed=7)

    vector_codes = quantizer.quantize(vector)
    matrix_codes = quantizer.quantize(matrix)
    payload = quantizer.compress(matrix)
    reconstructed = quantizer.decompress(payload)

    assert vector_codes.shape == (8,)
    assert matrix_codes.shape == (1, 8)
    assert matrix_codes.dtype == np.uint8
    assert reconstructed.shape == matrix.shape
    assert np.all(np.isfinite(reconstructed))
    assert np.mean((matrix - reconstructed) ** 2) < np.mean(matrix**2)
    assert quantizer.compression_ratio() == pytest.approx(32 / 12)


def test_quantizer_rejects_malformed_vectors_and_payloads() -> None:
    quantizer = TurboQuantMSE(dim=8, bits=4)

    with pytest.raises(ValueError, match="expected 8"):
        quantizer.quantize(np.zeros(7, dtype=np.float32))
    with pytest.raises(ValueError, match="finite"):
        quantizer.quantize(np.full(8, np.nan, dtype=np.float32))
    with pytest.raises(ValueError, match="integer dtype"):
        quantizer.dequantize(np.zeros(8, dtype=np.float32))
    with pytest.raises(ValueError, match="norm count"):
        quantizer.dequantize(
            np.zeros((2, 8), dtype=np.uint8),
            norms=np.ones(1, dtype=np.float32),
        )


def test_ip_quantizer_uses_one_bit_for_qjl_and_supports_reduced_sketch() -> None:
    rng = np.random.default_rng(11)
    vectors = rng.standard_normal((3, 8)).astype(np.float32)
    quantizer = TurboQuantIP(dim=8, bits=4, seed=11, qjl_dim=5)

    payload = quantizer.quantize_ip(vectors)
    reconstructed = quantizer.dequantize_ip(payload)

    assert payload["mse_bits"] == 3
    assert int(np.max(payload["codes"])) < 2**3
    assert payload["qjl_signs"].shape == (3, 5)
    assert set(np.unique(payload["qjl_signs"])) <= {-1, 1}
    assert payload["residual_norms"].shape == (3,)
    assert reconstructed.shape == vectors.shape
    assert np.all(np.isfinite(reconstructed))


def test_ip_payload_requires_residual_norms() -> None:
    quantizer = TurboQuantIP(dim=8, bits=3)
    payload = quantizer.quantize_ip(np.ones(8, dtype=np.float32))
    payload.pop("residual_norms")

    with pytest.raises(ValueError, match="residual_norms"):
        quantizer.dequantize_ip(payload)


@pytest.mark.parametrize("method", ["mse", "ip"])
def test_high_level_round_trip_infers_method_and_seed(method: str) -> None:
    vectors = np.arange(16, dtype=np.float32).reshape(2, 8)
    payload = compress_embeddings(vectors, bits=4, method=method)  # type: ignore[arg-type]
    payload["seed"] = 123

    # Re-encode with the declared seed so decoding must honor payload metadata.
    if method == "mse":
        payload = TurboQuantMSE(dim=8, bits=4, seed=123).compress(vectors)
    else:
        payload = TurboQuantIP(dim=8, bits=4, seed=123).quantize_ip(vectors)

    reconstructed = decompress_embeddings(payload)

    assert reconstructed.shape == vectors.shape
    assert np.all(np.isfinite(reconstructed))
    with pytest.raises(ValueError, match="payload method"):
        decompress_embeddings(payload, method="ip" if method == "mse" else "mse")


def test_quantizer_configuration_is_bounded() -> None:
    with pytest.raises(ValueError, match="dim"):
        TurboQuantMSE(dim=1, bits=4)
    with pytest.raises(ValueError, match="bits"):
        TurboQuantMSE(dim=8, bits=9)
    with pytest.raises(ValueError, match="at least 1"):
        TurboQuantIP(dim=8, bits=0)
