"""Reproducible multimedia image-processing experiments.

The module covers GRBG Bayer sampling, demosaicing, intensity fusion,
JPEG-style block DCT, coefficient corruption, and coefficient recovery.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

import cv2
import matplotlib
import numpy as np
from scipy.interpolate import interp1d
from skimage.metrics import peak_signal_noise_ratio, structural_similarity

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


JPEG_LUMA_QUANTIZATION = np.array(
    [
        [16, 11, 10, 16, 24, 40, 51, 61],
        [12, 12, 14, 19, 26, 58, 60, 55],
        [14, 13, 16, 24, 40, 57, 69, 56],
        [14, 17, 22, 29, 51, 87, 80, 62],
        [18, 22, 37, 56, 68, 109, 103, 77],
        [24, 35, 55, 64, 81, 104, 113, 92],
        [49, 64, 78, 87, 103, 121, 120, 101],
        [72, 92, 95, 98, 112, 100, 103, 99],
    ],
    dtype=np.float32,
)


def crop_even(image: np.ndarray) -> np.ndarray:
    """Crop an image to even dimensions without interpolation."""
    height, width = image.shape[:2]
    return image[: height - height % 2, : width - width % 2].copy()


def load_rgb(path: Path) -> np.ndarray:
    """Load an image as an even-sized uint8 RGB array."""
    data = np.fromfile(path, dtype=np.uint8)
    image_bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise ValueError(f"Unable to decode image: {path}")
    return crop_even(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))


def mosaic_grbg(image_rgb: np.ndarray) -> np.ndarray:
    """Sample an RGB image using a GRBG Bayer color-filter array."""
    red, green, blue = cv2.split(image_rgb)
    bayer = np.zeros(image_rgb.shape[:2], dtype=np.uint8)
    bayer[0::2, 0::2] = green[0::2, 0::2]
    bayer[0::2, 1::2] = red[0::2, 1::2]
    bayer[1::2, 0::2] = blue[1::2, 0::2]
    bayer[1::2, 1::2] = green[1::2, 1::2]
    return bayer


def demosaic_grbg(
    bayer: np.ndarray, method: Literal["bilinear", "vng"] = "vng"
) -> np.ndarray:
    """Reconstruct RGB data from a GRBG Bayer image."""
    code = {
        "bilinear": cv2.COLOR_BAYER_GR2RGB,
        "vng": cv2.COLOR_BAYER_GR2RGB_VNG,
    }[method]
    return cv2.demosaicing(bayer, code)


def image_metrics(reference: np.ndarray, candidate: np.ndarray) -> dict[str, float]:
    """Return PSNR and SSIM for same-shaped grayscale or RGB images."""
    channel_axis = -1 if reference.ndim == 3 else None
    return {
        "psnr_db": float(peak_signal_noise_ratio(reference, candidate, data_range=255)),
        "ssim": float(
            structural_similarity(
                reference, candidate, data_range=255, channel_axis=channel_axis
            )
        ),
    }


def add_rgb_noise(
    image_rgb: np.ndarray,
    sigmas: tuple[float, float, float],
    rng: np.random.Generator,
) -> np.ndarray:
    """Add independent Gaussian noise to the R, G, and B channels."""
    noisy = image_rgb.astype(np.float32).copy()
    for channel, sigma in enumerate(sigmas):
        noisy[..., channel] += rng.normal(0, sigma, image_rgb.shape[:2])
    return np.clip(noisy, 0, 255).astype(np.uint8)


def fuse_intensity(image_rgb: np.ndarray, intensity: np.ndarray) -> np.ndarray:
    """Replace the Y channel of an RGB image with an aligned intensity image."""
    if image_rgb.shape[:2] != intensity.shape:
        raise ValueError("RGB and intensity images must have matching dimensions")
    ycrcb = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2YCrCb)
    ycrcb[..., 0] = intensity
    return cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2RGB)


def pad_to_block(image: np.ndarray, block_size: int = 8) -> tuple[np.ndarray, tuple[int, int]]:
    """Pad a 2D image to a block-size multiple and return the original shape."""
    original_shape = image.shape
    pad_h = (-image.shape[0]) % block_size
    pad_w = (-image.shape[1]) % block_size
    return np.pad(image, ((0, pad_h), (0, pad_w))), original_shape


def dct_quantize(
    gray: np.ndarray, q_table: np.ndarray = JPEG_LUMA_QUANTIZATION
) -> tuple[np.ndarray, tuple[int, int], tuple[int, int]]:
    """Apply 8×8 DCT and JPEG-style quantization."""
    padded, original_shape = pad_to_block(gray.astype(np.float32), 8)
    quantized = np.zeros_like(padded, dtype=np.int32)
    for row in range(0, padded.shape[0], 8):
        for col in range(0, padded.shape[1], 8):
            block = padded[row : row + 8, col : col + 8] - 128.0
            quantized[row : row + 8, col : col + 8] = np.rint(
                cv2.dct(block) / q_table
            ).astype(np.int32)
    return quantized, padded.shape, original_shape


def restore_from_quantized(
    quantized: np.ndarray, q_table: np.ndarray = JPEG_LUMA_QUANTIZATION
) -> np.ndarray:
    """Dequantize and reconstruct an image with inverse block DCT."""
    restored = np.zeros(quantized.shape, dtype=np.float32)
    for row in range(0, quantized.shape[0], 8):
        for col in range(0, quantized.shape[1], 8):
            block = quantized[row : row + 8, col : col + 8].astype(np.float32)
            restored[row : row + 8, col : col + 8] = np.clip(
                cv2.idct(block * q_table) + 128.0, 0, 255
            )
    return restored.astype(np.uint8)


def corrupt_coefficients(
    coefficients: np.ndarray,
    mode: Literal["additive", "dropout", "sign_flip", "shift"],
    rng: np.random.Generator,
    probability: float = 0.2,
    sigma: float = 3.0,
) -> np.ndarray:
    """Apply a synthetic transmission/coding error to quantized coefficients."""
    result = coefficients.copy()
    if mode == "additive":
        return result + np.rint(rng.normal(0, sigma, result.shape)).astype(np.int32)

    selected = rng.random(result.shape) < probability
    if mode == "dropout":
        result[selected] = 0
    elif mode == "sign_flip":
        result[selected] *= -1
    elif mode == "shift":
        flat_source = coefficients.reshape(-1)
        flat_result = result.reshape(-1)
        indices = np.flatnonzero(selected.reshape(-1))
        indices = indices[(indices >= 2) & (indices < flat_result.size - 1)]
        flat_result[indices] = flat_source[indices + 1]
    else:
        raise ValueError(f"Unknown corruption mode: {mode}")
    return result


def coefficient_loss(
    coefficients: np.ndarray,
    rng: np.random.Generator,
    probability: float = 0.1,
) -> tuple[np.ndarray, np.ndarray]:
    """Drop coefficients and return the corrupted array plus a known-value mask."""
    known_mask = rng.random(coefficients.shape) >= probability
    corrupted = coefficients.copy()
    corrupted[~known_mask] = 0
    return corrupted, known_mask


def impute_coefficients(
    corrupted: np.ndarray,
    known_mask: np.ndarray,
    method: Literal["zero", "mean", "dc_ac_hybrid", "spline"],
) -> np.ndarray:
    """Recover missing coefficients using one of four strategies."""
    if corrupted.shape != known_mask.shape:
        raise ValueError("Coefficient and mask shapes must match")
    result = corrupted.copy()
    missing = ~known_mask

    if method == "zero":
        return result

    if method == "mean":
        rows, cols = np.nonzero(missing)
        source = corrupted.astype(np.float64)
        for row, col in zip(rows, cols):
            r0, r1 = max(0, row - 1), min(source.shape[0], row + 2)
            c0, c1 = max(0, col - 1), min(source.shape[1], col + 2)
            neighborhood = source[r0:r1, c0:c1]
            neighborhood_mask = known_mask[r0:r1, c0:c1]
            values = neighborhood[neighborhood_mask]
            result[row, col] = int(np.rint(values.mean())) if values.size else 0
        return result

    if method == "dc_ac_hybrid":
        for row in range(0, result.shape[0], 8):
            for col in range(0, result.shape[1], 8):
                if known_mask[row, col]:
                    continue
                neighbors = []
                for dr, dc in ((-8, 0), (8, 0), (0, -8), (0, 8)):
                    rr, cc = row + dr, col + dc
                    if (
                        0 <= rr < result.shape[0]
                        and 0 <= cc < result.shape[1]
                        and known_mask[rr, cc]
                    ):
                        neighbors.append(corrupted[rr, cc])
                result[row, col] = int(np.rint(np.mean(neighbors))) if neighbors else 0
        return result

    if method == "spline":
        for row in range(0, result.shape[0], 8):
            for col in range(0, result.shape[1], 8):
                block = result[row : row + 8, col : col + 8]
                block_mask = known_mask[row : row + 8, col : col + 8]
                flat = block.reshape(-1)  # view: assignments update result
                flat_mask = block_mask.reshape(-1)
                known_idx = np.flatnonzero(flat_mask)
                missing_idx = np.flatnonzero(~flat_mask)
                if not missing_idx.size:
                    continue
                kind = "cubic" if known_idx.size >= 4 else "linear"
                if known_idx.size >= 2:
                    interpolator = interp1d(
                        known_idx,
                        flat[known_idx],
                        kind=kind,
                        bounds_error=False,
                        fill_value="extrapolate",
                    )
                    flat[missing_idx] = np.rint(interpolator(missing_idx)).astype(np.int32)
        return result

    raise ValueError(f"Unknown imputation method: {method}")


def save_comparison(
    images: list[tuple[str, np.ndarray]], output: Path, columns: int | None = None
) -> None:
    """Save a compact comparison figure."""
    columns = columns or len(images)
    rows = (len(images) + columns - 1) // columns
    figure, axes = plt.subplots(rows, columns, figsize=(5 * columns, 4 * rows))
    axes_array = np.atleast_1d(axes).reshape(-1)
    for axis, (title, image) in zip(axes_array, images):
        axis.imshow(image, cmap="gray" if image.ndim == 2 else None)
        axis.set_title(title)
        axis.axis("off")
    for axis in axes_array[len(images) :]:
        axis.axis("off")
    figure.tight_layout()
    figure.savefig(output, dpi=160, bbox_inches="tight")
    plt.close(figure)


def run_experiments(input_path: Path, output_dir: Path, seed: int = 42) -> dict:
    """Run all experiments and save metrics plus comparison figures."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    image = load_rgb(input_path)
    bayer = mosaic_grbg(image)

    bilinear = demosaic_grbg(bayer, "bilinear")
    vng = demosaic_grbg(bayer, "vng")
    demosaicing = {
        "bilinear": image_metrics(image, bilinear),
        "vng": image_metrics(image, vng),
    }
    save_comparison(
        [("Original", image), ("Bilinear", bilinear), ("VNG", vng)],
        output_dir / "demosaicing_comparison.png",
    )

    noisy_rgb = add_rgb_noise(image, (20, 15, 25), rng)
    noisy_bayer = mosaic_grbg(noisy_rgb)
    demosaic_first = cv2.fastNlMeansDenoisingColored(
        demosaic_grbg(noisy_bayer, "vng"), None, 10, 10, 7, 21
    )
    denoise_first_bayer = cv2.fastNlMeansDenoising(noisy_bayer, None, 10, 7, 21)
    denoise_first = demosaic_grbg(denoise_first_bayer, "vng")
    noise_order = {
        "demosaic_then_denoise": image_metrics(image, demosaic_first),
        "denoise_then_demosaic": image_metrics(image, denoise_first),
    }
    save_comparison(
        [
            ("Noisy input", noisy_rgb),
            ("Demosaic → denoise", demosaic_first),
            ("Denoise → demosaic", denoise_first),
        ],
        output_dir / "noise_order_comparison.png",
    )

    clean_intensity = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    clean_fusion = fuse_intensity(vng, clean_intensity)
    noisy_rgb_strong = add_rgb_noise(image, (25, 25, 25), rng)
    noisy_bayer_strong = mosaic_grbg(noisy_rgb_strong)
    noisy_intensity = np.clip(
        clean_intensity.astype(np.float32)
        + rng.normal(0, 5, clean_intensity.shape),
        0,
        255,
    ).astype(np.uint8)
    denoised_intensity = cv2.fastNlMeansDenoising(noisy_intensity, None, 5, 7, 21)
    noisy_vng = demosaic_grbg(noisy_bayer_strong, "vng")
    noisy_fusion = fuse_intensity(noisy_vng, denoised_intensity)
    bayer_only = demosaic_grbg(
        cv2.fastNlMeansDenoising(noisy_bayer_strong, None, 10, 7, 21), "vng"
    )
    intensity_fusion = {
        "clean_vng": image_metrics(image, vng),
        "clean_vng_plus_intensity": image_metrics(image, clean_fusion),
        "noisy_bayer_only": image_metrics(image, bayer_only),
        "noisy_bayer_plus_intensity": image_metrics(image, noisy_fusion),
    }
    save_comparison(
        [
            ("VNG", vng),
            ("Clean intensity fusion", clean_fusion),
            ("Noisy Bayer only", bayer_only),
            ("Noisy hybrid fusion", noisy_fusion),
        ],
        output_dir / "intensity_fusion.png",
        columns=2,
    )

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    quantized, _, original_shape = dct_quantize(gray)
    restored = restore_from_quantized(quantized)[: original_shape[0], : original_shape[1]]
    jpeg_baseline = image_metrics(gray, restored)

    corruption_results = {}
    corruption_images = [("JPEG baseline", restored)]
    for mode in ("additive", "dropout", "sign_flip", "shift"):
        corrupted = corrupt_coefficients(quantized, mode, rng)
        reconstructed = restore_from_quantized(corrupted)[: original_shape[0], : original_shape[1]]
        corruption_results[mode] = image_metrics(gray, reconstructed)
        corruption_images.append((mode.replace("_", " ").title(), reconstructed))
    save_comparison(
        corruption_images,
        output_dir / "jpeg_corruption.png",
        columns=3,
    )

    lost, known_mask = coefficient_loss(quantized, rng, probability=0.1)
    recovery_results = {}
    recovery_images = []
    for method in ("zero", "mean", "dc_ac_hybrid", "spline"):
        recovered_coefficients = impute_coefficients(lost, known_mask, method)
        reconstructed = restore_from_quantized(recovered_coefficients)[
            : original_shape[0], : original_shape[1]
        ]
        recovery_results[method] = image_metrics(gray, reconstructed)
        recovery_images.append((method.replace("_", " ").title(), reconstructed))
    save_comparison(
        recovery_images,
        output_dir / "coefficient_recovery.png",
        columns=2,
    )

    results = {
        "input": str(input_path),
        "shape": list(image.shape),
        "seed": seed,
        "demosaicing": demosaicing,
        "noise_processing_order": noise_order,
        "intensity_fusion": intensity_fusion,
        "jpeg_baseline": jpeg_baseline,
        "coefficient_corruption": corruption_results,
        "coefficient_recovery": recovery_results,
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Input RGB image")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = run_experiments(args.image, args.output_dir, args.seed)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
