import numpy as np

from multimedia_project import (
    JPEG_LUMA_QUANTIZATION,
    crop_even,
    dct_quantize,
    impute_coefficients,
    mosaic_grbg,
    restore_from_quantized,
)


def test_crop_even_preserves_existing_pixels():
    image = np.arange(5 * 7 * 3, dtype=np.uint8).reshape(5, 7, 3)
    cropped = crop_even(image)
    assert cropped.shape == (4, 6, 3)
    np.testing.assert_array_equal(cropped, image[:4, :6])


def test_grbg_mosaic_uses_expected_channels():
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[..., 0] = 10
    image[..., 1] = 20
    image[..., 2] = 30
    bayer = mosaic_grbg(image)
    assert np.all(bayer[0::2, 0::2] == 20)
    assert np.all(bayer[0::2, 1::2] == 10)
    assert np.all(bayer[1::2, 0::2] == 30)
    assert np.all(bayer[1::2, 1::2] == 20)


def test_dct_round_trip_has_expected_shape_and_range():
    gray = np.tile(np.arange(16, dtype=np.uint8), (16, 1)) * 8
    coeffs, padded_shape, original_shape = dct_quantize(gray)
    restored = restore_from_quantized(coeffs, JPEG_LUMA_QUANTIZATION)
    assert padded_shape == (16, 16)
    assert original_shape == (16, 16)
    assert restored.shape == (16, 16)
    assert restored.dtype == np.uint8


def test_spline_imputation_changes_missing_values_only():
    coeffs = np.arange(64, dtype=np.int32).reshape(8, 8)
    mask = np.ones_like(coeffs, dtype=bool)
    mask[0, 2] = False
    corrupted = coeffs.copy()
    corrupted[~mask] = 0
    recovered = impute_coefficients(corrupted, mask, method="spline")
    np.testing.assert_array_equal(recovered[mask], corrupted[mask])
    assert recovered[0, 2] != 0
