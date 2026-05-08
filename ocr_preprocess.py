import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image
from PIL import ImageOps


RASTER_DPI = 300
MIN_WIDTH_FOR_OCR = 2400
MAX_DESKEW_ANGLE = 12.0
DESKEW_SAMPLE_WIDTH = 1400
BACKGROUND_BLUR_SIGMA = 21
OCR_TIMEOUT_SECONDS = int(os.environ.get("TESSERACT_TIMEOUT_SECONDS", "25"))
OCR_PSM = os.environ.get("TESSERACT_PSM", "6")
OCR_OEM = os.environ.get("TESSERACT_OEM", "1")
PREFERRED_OCR_LANGS = os.environ.get("TESSERACT_LANG", "eng+tha")

_AVAILABLE_TESSERACT_LANGS: set[str] | None = None


def emit_progress(
    phase: str,
    page_number: int,
    page_count: int,
    *,
    filename: str = "",
    candidate: str = "",
) -> None:
    payload = {
        "phase": phase,
        "pageNumber": page_number,
        "pageCount": page_count,
    }
    if filename:
        payload["filename"] = filename
    if candidate:
        payload["candidate"] = candidate

    print("PROGRESS " + json.dumps(payload), file=sys.stderr, flush=True)


def ascii_safe_stem(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_only).strip("._-")
    return cleaned or "uploaded_file"


def to_rgb_array(image: Image.Image) -> np.ndarray:
    fixed = ImageOps.exif_transpose(image).convert("RGB")
    return np.array(fixed)


def resize_for_ocr(rgb: np.ndarray) -> np.ndarray:
    height, width = rgb.shape[:2]
    if width >= MIN_WIDTH_FOR_OCR:
        return rgb

    scale = MIN_WIDTH_FOR_OCR / max(width, 1)
    return cv2.resize(
        rgb,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_LANCZOS4,
    )


def crop_document(rgb: np.ndarray) -> np.ndarray:
    grayscale = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(grayscale, (5, 5), 0)
    _, thresholded = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    merged = cv2.morphologyEx(thresholded, cv2.MORPH_CLOSE, kernel, iterations=1)
    coordinates = cv2.findNonZero(merged)

    if coordinates is None:
        return rgb

    x, y, width, height = cv2.boundingRect(coordinates)
    page_area = rgb.shape[0] * rgb.shape[1]
    crop_area = width * height
    if crop_area < page_area * 0.18:
        return rgb

    margin_x = max(12, int(width * 0.02))
    margin_y = max(12, int(height * 0.02))
    x0 = max(0, x - margin_x)
    y0 = max(0, y - margin_y)
    x1 = min(rgb.shape[1], x + width + margin_x)
    y1 = min(rgb.shape[0], y + height + margin_y)
    return rgb[y0:y1, x0:x1]


def estimate_skew_angle(grayscale: np.ndarray) -> float:
    sample = grayscale
    height, width = grayscale.shape[:2]
    if width > DESKEW_SAMPLE_WIDTH:
        scale = DESKEW_SAMPLE_WIDTH / width
        sample = cv2.resize(
            grayscale,
            (DESKEW_SAMPLE_WIDTH, max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )

    inverted = cv2.bitwise_not(sample)
    _, thresholded = cv2.threshold(
        inverted, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    coordinates = cv2.findNonZero(thresholded)
    if coordinates is None or len(coordinates) < 50:
        return 0.0

    angle = cv2.minAreaRect(coordinates)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    if abs(angle) < 0.15 or abs(angle) > MAX_DESKEW_ANGLE:
        return 0.0

    return float(angle)


def rotate_image(image: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.01:
        return image

    height, width = image.shape[:2]
    center = (width // 2, height // 2)
    rotation_matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    border_value = 255 if image.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(
        image,
        rotation_matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_value,
    )


def build_background_mask(rgb: np.ndarray) -> np.ndarray:
    grayscale = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    bright_threshold = max(168, int(np.percentile(grayscale, 62)))
    mask = (grayscale >= bright_threshold) & (hsv[:, :, 1] <= 96)
    if np.count_nonzero(mask) < 1500:
        fallback_threshold = max(150, int(np.percentile(grayscale, 52)))
        mask = grayscale >= fallback_threshold
    return mask


def estimate_document_profile(rgb: np.ndarray) -> dict:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    background_mask = build_background_mask(rgb)
    background_pixels = rgb[background_mask]
    if background_pixels.size == 0:
        background_pixels = rgb.reshape(-1, 3)

    background_rgb = background_pixels.mean(axis=0)
    background_sat = float(hsv[:, :, 1][background_mask].mean()) if np.any(background_mask) else float(
        hsv[:, :, 1].mean()
    )
    chroma = float(background_rgb.max() - background_rgb.min())

    accent_mask = (hsv[:, :, 1] > 65) & (hsv[:, :, 2] > 40)
    blue_fraction = float(
        np.mean(accent_mask & (hsv[:, :, 0] >= 88) & (hsv[:, :, 0] <= 126))
    )
    green_fraction = float(
        np.mean(accent_mask & (hsv[:, :, 0] >= 36) & (hsv[:, :, 0] <= 84))
    )

    profile = "neutral"
    if background_sat > 26 and background_rgb[1] > background_rgb[0] + 10 and background_rgb[1] > background_rgb[2] + 10:
        profile = "green_cast"
    elif background_sat > 18 and background_rgb[0] > background_rgb[1] + 8 and background_rgb[2] > background_rgb[1] + 3:
        profile = "warm_pink"
    elif chroma > 26 and background_sat > 22:
        profile = "strong_tint"
    elif blue_fraction > 0.008:
        profile = "blue_line"
    elif green_fraction > 0.008:
        profile = "green_line"

    tint_strength = float(np.clip((background_sat * 0.9 + chroma * 1.1) / 90.0, 0.0, 1.0))
    return {
        "profile": profile,
        "backgroundRgb": [round(float(channel), 2) for channel in background_rgb],
        "backgroundSaturation": round(background_sat, 2),
        "tintStrength": round(tint_strength, 3),
        "blueFraction": round(blue_fraction, 4),
        "greenFraction": round(green_fraction, 4),
        "backgroundMask": background_mask,
    }


def apply_profile_correction(rgb: np.ndarray, profile_info: dict) -> np.ndarray:
    corrected = rgb.astype(np.float32).copy()
    background_rgb = np.array(profile_info["backgroundRgb"], dtype=np.float32)
    background_mask = profile_info["backgroundMask"]
    tint_strength = float(profile_info["tintStrength"])
    profile = profile_info["profile"]

    target = float(np.mean(background_rgb))
    gains = np.clip(target / np.maximum(background_rgb, 1.0), 0.82, 1.24)
    gain_strength = 0.28 + tint_strength * 0.52
    if profile in {"green_cast", "warm_pink", "strong_tint"}:
        gain_strength += 0.12
    gains = 1.0 + (gains - 1.0) * np.clip(gain_strength, 0.0, 0.9)
    corrected *= gains.reshape(1, 1, 3)
    corrected = np.clip(corrected, 0, 255).astype(np.uint8)

    lab = cv2.cvtColor(corrected, cv2.COLOR_RGB2LAB).astype(np.float32)
    l_channel, a_channel, b_channel = cv2.split(lab)

    if np.any(background_mask):
        desaturate_strength = 0.10 + tint_strength * 0.45
        if profile in {"green_cast", "warm_pink", "strong_tint"}:
            desaturate_strength += 0.15

        a_channel[background_mask] = a_channel[background_mask] * (1.0 - desaturate_strength) + 128.0 * desaturate_strength
        b_channel[background_mask] = b_channel[background_mask] * (1.0 - desaturate_strength) + 128.0 * desaturate_strength
        lift = 3.0 + tint_strength * 10.0
        l_channel[background_mask] = np.clip(l_channel[background_mask] + lift, 0, 255)

    merged = cv2.merge((l_channel, a_channel, b_channel)).astype(np.uint8)
    return cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)


def remove_colored_lines(rgb: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    saturation_mask = cv2.inRange(hsv[:, :, 1], 70, 255)
    value_mask = cv2.inRange(hsv[:, :, 2], 30, 255)
    base_mask = cv2.bitwise_and(saturation_mask, value_mask)

    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (55, 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 55))
    horizontal = cv2.morphologyEx(base_mask, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(base_mask, cv2.MORPH_OPEN, vertical_kernel)
    line_mask = cv2.bitwise_or(horizontal, vertical)

    if not np.any(line_mask):
        return rgb

    line_mask = cv2.dilate(
        line_mask,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    return cv2.inpaint(rgb, line_mask, 3, cv2.INPAINT_TELEA)


def reduce_watermark_and_normalize(grayscale: np.ndarray) -> np.ndarray:
    background = cv2.GaussianBlur(
        grayscale,
        (0, 0),
        sigmaX=BACKGROUND_BLUR_SIGMA,
        sigmaY=BACKGROUND_BLUR_SIGMA,
    )
    normalized = cv2.divide(grayscale, background, scale=255)
    normalized = cv2.normalize(normalized, None, 0, 255, cv2.NORM_MINMAX)
    # Blend the normalized pass with the original grayscale to avoid washed-out output.
    return cv2.addWeighted(grayscale, 0.62, normalized, 0.38, 0)


def trim_document_border(grayscale: np.ndarray) -> np.ndarray:
    _, thresholded = cv2.threshold(
        grayscale, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    coordinates = cv2.findNonZero(thresholded)
    if coordinates is None:
        return grayscale

    x, y, width, height = cv2.boundingRect(coordinates)
    if width * height < grayscale.shape[0] * grayscale.shape[1] * 0.16:
        return grayscale

    margin_x = max(8, int(width * 0.01))
    margin_y = max(8, int(height * 0.01))
    x0 = max(0, x - margin_x)
    y0 = max(0, y - margin_y)
    x1 = min(grayscale.shape[1], x + width + margin_x)
    y1 = min(grayscale.shape[0], y + height + margin_y)
    return grayscale[y0:y1, x0:x1]


def light_denoise(grayscale: np.ndarray) -> np.ndarray:
    return cv2.medianBlur(grayscale, 3)


def enhance_contrast(grayscale: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    enhanced = clahe.apply(grayscale)
    return cv2.normalize(enhanced, None, 0, 255, cv2.NORM_MINMAX)


def light_sharpen(grayscale: np.ndarray) -> np.ndarray:
    softened = cv2.GaussianBlur(grayscale, (0, 0), sigmaX=0.8, sigmaY=0.8)
    sharpened = cv2.addWeighted(grayscale, 1.18, softened, -0.18, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)


def build_text_emphasis_mask(grayscale: np.ndarray) -> np.ndarray:
    adaptive_inv = cv2.adaptiveThreshold(
        grayscale,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        29,
        8,
    )
    _, otsu_inv = cv2.threshold(
        grayscale, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    blackhat = cv2.morphologyEx(
        grayscale,
        cv2.MORPH_BLACKHAT,
        cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)),
    )
    _, blackhat_mask = cv2.threshold(
        blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )

    combined = cv2.bitwise_or(adaptive_inv, otsu_inv)
    combined = cv2.bitwise_or(combined, blackhat_mask)
    darkness_gate = cv2.inRange(grayscale, 0, 214)
    combined = cv2.bitwise_and(combined, darkness_gate)
    combined = cv2.morphologyEx(
        combined,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)),
    )
    combined = cv2.morphologyEx(
        combined,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1)),
    )

    height, width = grayscale.shape[:2]
    page_area = height * width
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(combined, 8)
    filtered = np.zeros_like(combined)

    for component_index in range(1, component_count):
        x = stats[component_index, cv2.CC_STAT_LEFT]
        y = stats[component_index, cv2.CC_STAT_TOP]
        component_width = stats[component_index, cv2.CC_STAT_WIDTH]
        component_height = stats[component_index, cv2.CC_STAT_HEIGHT]
        area = stats[component_index, cv2.CC_STAT_AREA]

        if area < 12 or area > page_area * 0.0022:
            continue

        fill_ratio = area / max(component_width * component_height, 1)
        is_text_line_shape = component_height <= max(14, int(height * 0.035))
        is_reasonable_width = component_width <= width * 0.42
        is_very_wide_blob = component_width > width * 0.22 and component_height > height * 0.03
        is_tall_blob = component_height > height * 0.09
        is_dense_blob = fill_ratio > 0.58 and area > 160

        if is_very_wide_blob or is_tall_blob or is_dense_blob:
            continue

        if not (is_reasonable_width or is_text_line_shape):
            continue

        filtered[labels == component_index] = 255

    filtered = cv2.dilate(
        filtered,
        cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1)),
        iterations=1,
    )
    return cv2.GaussianBlur(filtered, (0, 0), sigmaX=0.8, sigmaY=0.8)


def build_visual_output(
    rgb: np.ndarray, grayscale_reference: np.ndarray, profile_info: dict
) -> np.ndarray:
    corrected_rgb = apply_profile_correction(rgb, profile_info)
    lab = cv2.cvtColor(corrected_rgb, cv2.COLOR_RGB2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clip_limit = 1.35 if profile_info["profile"] in {"neutral", "blue_line"} else 1.7
    l_channel = cv2.createCLAHE(
        clipLimit=clip_limit, tileGridSize=(8, 8)
    ).apply(l_channel)
    merged = cv2.merge((l_channel, a_channel, b_channel))
    enhanced = cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)
    softened = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=0.75, sigmaY=0.75)
    sharpened = cv2.addWeighted(enhanced, 1.08, softened, -0.08, 0)

    emphasis_mask = build_text_emphasis_mask(grayscale_reference).astype(np.float32) / 255.0
    profile_boost = 0.74 if profile_info["profile"] in {"neutral", "blue_line"} else 0.42
    emphasis_mask = np.clip(emphasis_mask * profile_boost, 0.0, profile_boost)[..., None]
    darker_text = cv2.cvtColor(
        np.clip(grayscale_reference.astype(np.float32) * 0.72, 0, 255).astype(np.uint8),
        cv2.COLOR_GRAY2RGB,
    )
    blended = sharpened.astype(np.float32) * (1.0 - emphasis_mask) + darker_text.astype(
        np.float32
    ) * emphasis_mask
    return np.clip(blended, 0, 255).astype(np.uint8)


def build_candidates(clean_grayscale: np.ndarray) -> dict[str, np.ndarray]:
    _, otsu = cv2.threshold(
        clean_grayscale, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    adaptive = cv2.adaptiveThreshold(
        clean_grayscale,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )

    blackhat = cv2.morphologyEx(
        clean_grayscale,
        cv2.MORPH_BLACKHAT,
        cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)),
    )
    emphasized = cv2.subtract(
        clean_grayscale,
        cv2.normalize(blackhat, None, 0, 90, cv2.NORM_MINMAX),
    )
    dark_text = cv2.normalize(emphasized, None, 0, 255, cv2.NORM_MINMAX)
    digit_focus = cv2.adaptiveThreshold(
        dark_text,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY,
        21,
        6,
    )

    return {
        "clean": clean_grayscale,
        "otsu_threshold": otsu,
        "adaptive_threshold": adaptive,
        "dark_text": dark_text,
        "digit_focus": digit_focus,
    }


def get_available_tesseract_langs() -> set[str]:
    global _AVAILABLE_TESSERACT_LANGS
    if _AVAILABLE_TESSERACT_LANGS is not None:
        return _AVAILABLE_TESSERACT_LANGS

    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
            check=False,
        )
    except OSError:
        _AVAILABLE_TESSERACT_LANGS = {"eng"}
        return _AVAILABLE_TESSERACT_LANGS

    languages = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.lower().startswith("list of available languages")
    }
    _AVAILABLE_TESSERACT_LANGS = languages or {"eng"}
    return _AVAILABLE_TESSERACT_LANGS


def resolve_tesseract_langs() -> str:
    available = get_available_tesseract_langs()
    requested = [part.strip() for part in PREFERRED_OCR_LANGS.split("+") if part.strip()]
    resolved = [part for part in requested if part in available]
    if resolved:
        return "+".join(resolved)
    if "eng" in available:
        return "eng"
    return next(iter(sorted(available)), "eng")


def run_tesseract_tsv(image_path: Path) -> str:
    result = subprocess.run(
        [
            "tesseract",
            str(image_path),
            "stdout",
            "--oem",
            OCR_OEM,
            "--psm",
            OCR_PSM,
            "-l",
            resolve_tesseract_langs(),
            "tsv",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=OCR_TIMEOUT_SECONDS,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Tesseract OCR failed.")

    return result.stdout


def score_ocr_tsv(tsv_text: str) -> dict:
    lines = [line for line in tsv_text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return {
            "meanConfidence": 0.0,
            "wordCount": 0,
            "textLength": 0,
            "score": 0.0,
        }

    confidences: list[float] = []
    text_length = 0
    word_count = 0

    for row in lines[1:]:
        parts = row.split("\t")
        if len(parts) < 12:
            continue

        text = parts[11].strip()
        if not text:
            continue

        try:
            confidence = float(parts[10])
        except ValueError:
            continue

        if confidence < 0:
            continue

        confidences.append(confidence)
        word_count += 1
        text_length += len(text)

    if not confidences:
        return {
            "meanConfidence": 0.0,
            "wordCount": 0,
            "textLength": 0,
            "score": 0.0,
        }

    mean_confidence = float(sum(confidences) / len(confidences))
    density_bonus = min(text_length, 500) * 0.04
    word_bonus = min(word_count, 80) * 0.12
    digit_bonus = min(
        sum(ch.isdigit() for line in lines[1:] for ch in line.split("\t")[-1]), 80
    ) * 0.18
    score = mean_confidence + density_bonus + word_bonus + digit_bonus
    return {
        "meanConfidence": round(mean_confidence, 2),
        "wordCount": word_count,
        "textLength": text_length,
        "score": round(score, 2),
    }


def evaluate_candidates(
    candidates: dict[str, np.ndarray],
    page_number: int,
    page_count: int,
    filename: str,
) -> tuple[str, dict]:
    best_name = "clean"
    best_meta = {
        "meanConfidence": 0.0,
        "wordCount": 0,
        "textLength": 0,
        "score": 0.0,
    }

    with tempfile.TemporaryDirectory(prefix="ocr-candidates-") as temp_dir:
        temp_root = Path(temp_dir)

        for candidate_name, candidate_image in candidates.items():
            emit_progress(
                "scoring_ocr_candidates",
                page_number,
                page_count,
                filename=filename,
                candidate=candidate_name,
            )
            candidate_path = temp_root / f"{candidate_name}.png"
            Image.fromarray(candidate_image).save(candidate_path, format="PNG", optimize=True)

            try:
                tsv_text = run_tesseract_tsv(candidate_path)
                candidate_meta = score_ocr_tsv(tsv_text)
            except Exception:
                candidate_meta = {
                    "meanConfidence": 0.0,
                    "wordCount": 0,
                    "textLength": 0,
                    "score": 0.0,
                }

            if candidate_meta["score"] > best_meta["score"]:
                best_name = candidate_name
                best_meta = candidate_meta

    return best_name, {
        "ocrCandidate": best_name,
        "ocrConfidence": best_meta["meanConfidence"],
        "ocrScore": best_meta["score"],
        "ocrTextLength": best_meta["textLength"],
    }


def prepare_document_image(
    rgb: np.ndarray,
    page_number: int,
    page_count: int,
    filename: str,
) -> tuple[np.ndarray, dict]:
    emit_progress("normalizing_size", page_number, page_count, filename=filename)
    working = resize_for_ocr(rgb)

    emit_progress("cropping_document", page_number, page_count, filename=filename)
    working = crop_document(working)

    emit_progress("deskewing_document", page_number, page_count, filename=filename)
    skew_angle = estimate_skew_angle(cv2.cvtColor(working, cv2.COLOR_RGB2GRAY))
    working = rotate_image(working, skew_angle)

    emit_progress("detecting_document_profile", page_number, page_count, filename=filename)
    profile_info = estimate_document_profile(working)

    emit_progress("cleanup_document", page_number, page_count, filename=filename)
    ocr_rgb = remove_colored_lines(working.copy())

    emit_progress("grayscale_document", page_number, page_count, filename=filename)
    grayscale = cv2.cvtColor(ocr_rgb, cv2.COLOR_RGB2GRAY)
    grayscale = reduce_watermark_and_normalize(grayscale)

    emit_progress("visual_cleanup", page_number, page_count, filename=filename)
    visual_output = build_visual_output(working, grayscale, profile_info)

    grayscale = trim_document_border(grayscale)

    emit_progress("denoise_document", page_number, page_count, filename=filename)
    grayscale = light_denoise(grayscale)

    emit_progress("enhance_document", page_number, page_count, filename=filename)
    grayscale = enhance_contrast(grayscale)
    grayscale = light_sharpen(grayscale)

    candidates = build_candidates(grayscale)
    _, ocr_meta = evaluate_candidates(
        candidates, page_number, page_count, filename
    )
    ocr_meta.update(
        {
            "documentProfile": profile_info["profile"],
            "backgroundSaturation": profile_info["backgroundSaturation"],
            "tintStrength": profile_info["tintStrength"],
        }
    )
    return visual_output, ocr_meta


def save_png(image: np.ndarray, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image).save(
        output_path,
        format="PNG",
        optimize=True,
        compress_level=3,
    )
    return output_path.stat().st_size


def process_pdf(input_path: Path, output_dir: Path, source_stem: str) -> list[dict]:
    document = fitz.open(input_path)
    results = []
    page_count = len(document)

    for page_index, page in enumerate(document):
        page_number = page_index + 1
        output_name = f"{source_stem}-page-{page_number:03d}-ocr.png"

        emit_progress("rendering_pdf", page_number, page_count, filename=output_name)
        pixmap = page.get_pixmap(dpi=RASTER_DPI, colorspace=fitz.csRGB, alpha=False)
        rgb = np.array(Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB"))

        selected_image, ocr_meta = prepare_document_image(
            rgb, page_number, page_count, output_name
        )

        output_path = output_dir / output_name
        file_size = save_png(selected_image, output_path)
        results.append(
            {
                "filename": output_name,
                "path": str(output_path),
                "mimeType": "image/png",
                "size": file_size,
                "pageNumber": page_number,
                "pageCount": page_count,
                "processedKind": "pdf_page_ocr_best_candidate",
                **ocr_meta,
            }
        )

    document.close()
    return results


def process_image(input_path: Path, output_dir: Path, source_stem: str) -> list[dict]:
    output_name = f"{source_stem}-ocr.png"
    with Image.open(input_path) as image:
        rgb = to_rgb_array(image)

    selected_image, ocr_meta = prepare_document_image(rgb, 1, 1, output_name)
    output_path = output_dir / output_name
    file_size = save_png(selected_image, output_path)
    return [
        {
            "filename": output_name,
            "path": str(output_path),
            "mimeType": "image/png",
            "size": file_size,
            "pageNumber": 1,
            "pageCount": 1,
            "processedKind": "image_ocr_best_candidate",
            **ocr_meta,
        }
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Preprocess files for OCR-friendly upload.")
    parser.add_argument("--input", required=True, help="Input file path")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument("--original-name", required=True, help="Original uploaded file name")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    source_stem = ascii_safe_stem(Path(args.original_name).stem)
    source_suffix = input_path.suffix.lower()

    if source_suffix == ".pdf":
        outputs = process_pdf(input_path, output_dir, source_stem)
    elif source_suffix in {".png", ".jpg", ".jpeg"}:
        outputs = process_image(input_path, output_dir, source_stem)
    else:
        raise ValueError(f"Unsupported preprocessing type: {source_suffix}")

    print(json.dumps({"outputs": outputs}))


if __name__ == "__main__":
    main()
