#!/usr/bin/env python3
"""Recompute the frozen Phase 4 cleanup quality and integrity gates.

This script performs no model inference. It reads the tracked eight-case
corpus and saved outputs, then recomputes every metric from pixels.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
SERVER = REPOSITORY / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from pipeline import cleanup as production_cleanup  # noqa: E402
from pipeline.revision import get_pipeline_revision  # noqa: E402
from webtoon import inpainter as production_inpainter  # noqa: E402
from webtoon.bubble_detector import WebtoonBubble  # noqa: E402
from webtoon.ocr import TextDetection  # noqa: E402
ASSETS = ROOT / "assets" / "inpainting_eval"
OUTPUTS = ROOT / "outputs" / "comparison-raw" / "inpainting-controlled"
SYSTEM_DIRS = {
    "frank_lama": "frank_lama",
    "koharu_lama": "lama",
    "koharu_aot": "aot_inpainting",
}
HARD_CASES = frozenset(
    {
        "synthetic_02_hair_halftone",
        "synthetic_05_street_lines",
        "synthetic_06_speed_lines",
        "synthetic_08_dense_art",
    }
)
EXPECTED_FRANK_MEAN = 29.142394145321486
EXPECTED_FLUX_HARD_MEAN = 25.751420108032757


def _rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def _mask(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L")) >= 127


def _score(reference: np.ndarray, actual: np.ndarray, mask: np.ndarray) -> float:
    if reference.shape != actual.shape or reference.shape[:2] != mask.shape:
        raise ValueError("input, output, mask, and reference dimensions must agree")
    return float(
        np.abs(reference.astype(np.int16) - actual.astype(np.int16))[mask].mean()
    )


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)


def _background(source: np.ndarray, mask: np.ndarray) -> tuple[int, int, int]:
    """Approximate the detector's local background from observable border pixels."""
    x1, y1, x2, y2 = _bbox(mask)
    x1, y1 = max(0, x1 - 8), max(0, y1 - 8)
    x2, y2 = min(source.shape[1], x2 + 8), min(source.shape[0], y2 + 8)
    local_mask = mask[y1:y2, x1:x2]
    sample = source[y1:y2, x1:x2][~local_mask]
    if not len(sample):
        sample = source[~mask]
    return tuple(int(value) for value in np.median(sample, axis=0))


def _bubble(mask: np.ndarray, background: tuple[int, int, int]) -> WebtoonBubble:
    bbox = _bbox(mask)
    detection = TextDetection(
        bbox_poly=[
            [bbox[0], bbox[1]], [bbox[2], bbox[1]],
            [bbox[2], bbox[3]], [bbox[0], bbox[3]],
        ],
        text="評価",
        confidence=1.0,
        bbox_rect=bbox,
    )
    return WebtoonBubble(
        bbox=bbox,
        text_regions=[detection],
        combined_text=detection.text,
        bg_color=background,
        # These are artwork fixtures; production manga artwork also has no
        # semantic balloon mask and therefore cannot take the uniform route.
        bubble_mask=None,
    )


def _effective_mask(image: Image.Image, bubble: WebtoonBubble) -> Image.Image:
    mask = production_inpainter.build_effective_cleanup_mask(
        image, bubble, production_inpainter._policy(),
    )
    if mask is None:
        raise AssertionError("production policy produced an empty mask")
    return mask


@dataclass
class _SavedOutputBackend:
    output: Image.Image
    boxes: list[tuple[int, int, int, int]]
    calls: int = 0

    def load(self) -> bool:
        return True

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        del mask
        box = self.boxes[self.calls]
        self.calls += 1
        crop = self.output.crop(box)
        if crop.size != image.size:
            raise AssertionError("production tile order or geometry changed")
        return crop

    def unload(self) -> None:
        return None


class _FailingBackend:
    calls = 0

    def load(self) -> bool:
        return True

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        del image, mask
        self.calls += 1
        raise MemoryError("deterministic evaluation OOM")

    def unload(self) -> None:
        return None


class _ForbiddenBackend:
    calls = 0

    def load(self) -> bool:
        raise AssertionError("uniform route loaded a model")

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        del image, mask
        self.calls += 1
        raise AssertionError("uniform route invoked a model")

    def unload(self) -> None:
        return None


def _policy_run(
    source: Image.Image,
    reference: Image.Image,
    bubble: WebtoonBubble,
    backend: object,
    frozen_reference_mask: np.ndarray,
) -> tuple[dict[str, object], Image.Image]:
    effective = _effective_mask(source, bubble)
    effective_array = np.asarray(effective) > 0
    plan = production_cleanup.plan_cleanup(
        source,
        effective,
        semantic_mask=bubble.bubble_mask,
        policy=production_inpainter._policy(),
    )
    target = source.copy()
    started = perf_counter()
    with (
        patch.object(production_inpainter, "INPAINT_ENABLED", True),
        patch.object(production_inpainter, "host_memory_available", return_value=True),
        patch.object(production_inpainter, "gpu_memory_available", return_value=True),
    ):
        outcome = production_inpainter.cleanup_bubble(
            source, bubble, backend=backend, target_img=target,
        )
    elapsed_ms = (perf_counter() - started) * 1000
    source_array = np.asarray(source.convert("RGB"))
    target_array = np.asarray(target.convert("RGB"))
    reference_array = np.asarray(reference.convert("RGB"))
    changed_outside = int(np.count_nonzero(
        np.any(source_array[~effective_array] != target_array[~effective_array], axis=1)
    ))
    return ({
        "route": outcome.method,
        "degraded": outcome.degraded,
        "reason": outcome.reason,
        "effective_mask_pixels": int(effective_array.sum()),
        "effective_masked_mae": _score(
            reference_array, target_array, effective_array,
        ),
        "frozen_reference_masked_mae": _score(
            reference_array, target_array, frozen_reference_mask,
        ),
        "outside_effective_mask_changed_pixels": changed_outside,
        "runtime_ms": elapsed_ms,
        "planned_tile_count": len(plan.tiles),
        # Backend work occurs in a killable child process. Its mutable call
        # count cannot truthfully be observed in the parent.
        "parent_observed_backend_calls": int(getattr(backend, "calls", 0)),
    }, effective)


def evaluate_production_policy() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for input_path in sorted(ASSETS.glob("*__input.png")):
        case_id = input_path.name.removesuffix("__input.png")
        source = Image.open(input_path).convert("RGB")
        reference = Image.open(
            ASSETS / f"{case_id}__reference.png"
        ).convert("RGB")
        frozen_mask = _mask(ASSETS / f"{case_id}__mask.png")
        source_array = np.asarray(source)
        background = _background(source_array, frozen_mask)

        bubble = _bubble(frozen_mask, background)
        effective = _effective_mask(source, bubble)
        plan = production_cleanup.plan_cleanup(
            source,
            effective,
            semantic_mask=bubble.bubble_mask,
            policy=production_inpainter._policy(),
        )
        saved_output = Image.open(
            OUTPUTS / "frank_lama" / f"{case_id}.png"
        ).convert("RGB")
        lama_backend = _SavedOutputBackend(
            saved_output, [tile.bbox for tile in plan.tiles],
        )
        lama, _ = _policy_run(
            source, reference, bubble, lama_backend, frozen_mask,
        )

        degraded_bubble = _bubble(frozen_mask, background)
        degraded, _ = _policy_run(
            source, reference, degraded_bubble, _FailingBackend(), frozen_mask,
        )
        cases.append({
            "id": case_id,
            "background_proxy": list(background),
            "lama_saved_output_adapter": lama,
            "degraded_backend_failure": degraded,
        })

    # The artwork corpus should route to LaMa. Exercise the production uniform
    # decision with the same dimensions and first frozen mask on a flat control.
    first_mask = _mask(sorted(ASSETS.glob("*__mask.png"))[0])
    flat = Image.new("RGB", (384, 384), (247, 248, 249))
    uniform_bubble = _bubble(first_mask, (247, 248, 249))
    uniform_bubble.bubble_mask = np.full((384, 384), 255, dtype=np.uint8)
    uniform_backend = _ForbiddenBackend()
    uniform, _ = _policy_run(
        flat, flat, uniform_bubble, uniform_backend, first_mask,
    )

    all_runs = [
        result
        for case in cases
        for result in (
            case["lama_saved_output_adapter"],
            case["degraded_backend_failure"],
        )
    ]
    lama_results = [case["lama_saved_output_adapter"] for case in cases]
    degraded_results = [case["degraded_backend_failure"] for case in cases]
    checks = {
        "all_artwork_cases_route_lama": all(
            case["lama_saved_output_adapter"]["route"] == "lama_tiles"
            for case in cases
        ),
        "all_backend_failures_degrade_to_fill": all(
            case["degraded_backend_failure"]["route"] == "uniform_fill"
            and case["degraded_backend_failure"]["degraded"] is True
            and case["degraded_backend_failure"]["reason"] == "oom"
            for case in cases
        ),
        "all_production_runs_preserve_outside_effective_mask": all(
            result["outside_effective_mask_changed_pixels"] == 0
            for result in all_runs
        ),
        "uniform_control_skips_backend": (
            uniform["route"] == "uniform_fill"
            and uniform["degraded"] is False
            and uniform["parent_observed_backend_calls"] == 0
            and uniform["outside_effective_mask_changed_pixels"] == 0
        ),
        "production_compositor_retains_frozen_lama_quality": abs(
            float(np.mean([
                result["frozen_reference_masked_mae"]
                for result in lama_results
            ])) - EXPECTED_FRANK_MEAN
        ) < 1e-9,
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise AssertionError(f"production policy gate failed: {', '.join(failed)}")
    return {
        "schema_version": 1,
        "method": (
            "Exact production planner, mask builder, tiler, cleanup_bubble, "
            "rollback, fill, and compositor with deterministic adapters."
        ),
        "saved_output_adapter_note": (
            "The fake backend returns the frozen Frank LaMa pixels for each "
            "production tile. It evaluates current routing and compositing; "
            "it does not rerun LaMa inference."
        ),
        "background_proxy_note": (
            "The frozen corpus lacks detector background metadata. Each case "
            "uses the observable median of the 8-pixel ring outside its frozen mask."
        ),
        "cases": cases,
        "uniform_control": uniform,
        "summary": {
            "lama_mean_effective_masked_mae": float(np.mean([
                result["effective_masked_mae"] for result in lama_results
            ])),
            "lama_mean_frozen_reference_masked_mae": float(np.mean([
                result["frozen_reference_masked_mae"] for result in lama_results
            ])),
            "lama_mean_policy_runtime_ms": float(np.mean([
                result["runtime_ms"] for result in lama_results
            ])),
            "degraded_mean_effective_masked_mae": float(np.mean([
                result["effective_masked_mae"] for result in degraded_results
            ])),
            "degraded_mean_frozen_reference_masked_mae": float(np.mean([
                result["frozen_reference_masked_mae"]
                for result in degraded_results
            ])),
            "degraded_mean_policy_runtime_ms": float(np.mean([
                result["runtime_ms"] for result in degraded_results
            ])),
        },
        "checks": checks,
    }


def _runtime_observation(
    revision: str, production: dict[str, object],
) -> dict[str, object]:
    cases = production["cases"]
    assert isinstance(cases, list)
    uniform = production["uniform_control"]
    assert isinstance(uniform, dict)
    return {
        "schema_version": 1,
        "pipeline_revision": revision,
        "note": (
            "Local wall-clock observations from deterministic fake backends. "
            "These values are intentionally excluded from the tracked "
            "acceptance artifact and do not include learned-model inference."
        ),
        "cases": [
            {
                "id": case["id"],
                "lama_policy_runtime_ms": case["lama_saved_output_adapter"][
                    "runtime_ms"
                ],
                "degraded_oom_policy_runtime_ms": case[
                    "degraded_backend_failure"
                ]["runtime_ms"],
            }
            for case in cases
        ],
        "uniform_control_runtime_ms": uniform["runtime_ms"],
    }


def _without_runtimes(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_runtimes(child)
            for key, child in value.items()
            if key != "runtime_ms" and not key.endswith("_runtime_ms")
        }
    if isinstance(value, list):
        return [_without_runtimes(child) for child in value]
    return value


def evaluate() -> dict[str, object]:
    case_ids = sorted(
        path.name.removesuffix("__input.png")
        for path in ASSETS.glob("*__input.png")
    )
    if len(case_ids) != 8:
        raise ValueError(f"expected 8 frozen cases, found {len(case_ids)}")

    systems: dict[str, dict[str, object]] = {}
    per_case: dict[str, dict[str, float]] = {case_id: {} for case_id in case_ids}
    for system, directory in SYSTEM_DIRS.items():
        values: list[float] = []
        outside_changes = 0
        for case_id in case_ids:
            source = _rgb(ASSETS / f"{case_id}__input.png")
            reference = _rgb(ASSETS / f"{case_id}__reference.png")
            mask = _mask(ASSETS / f"{case_id}__mask.png")
            actual = _rgb(OUTPUTS / directory / f"{case_id}.png")
            value = _score(reference, actual, mask)
            values.append(value)
            per_case[case_id][system] = value
            outside_changes += int(
                np.count_nonzero(np.any(source[~mask] != actual[~mask], axis=1))
            )
        systems[system] = {
            "case_count": len(values),
            "mean_masked_mae": float(np.mean(values)),
            "median_masked_mae": float(np.median(values)),
            "outside_mask_changed_pixels": outside_changes,
        }

    flux_values: list[float] = []
    flux_outside_changes = 0
    for case_id in sorted(HARD_CASES):
        source = _rgb(ASSETS / f"{case_id}__input.png")
        reference = _rgb(ASSETS / f"{case_id}__reference.png")
        mask = _mask(ASSETS / f"{case_id}__mask.png")
        actual = _rgb(OUTPUTS / "flux2_klein" / f"{case_id}.png")
        value = _score(reference, actual, mask)
        flux_values.append(value)
        per_case[case_id]["koharu_flux"] = value
        flux_outside_changes += int(
            np.count_nonzero(np.any(source[~mask] != actual[~mask], axis=1))
        )
    systems["koharu_flux_hard"] = {
        "case_count": len(flux_values),
        "mean_masked_mae": float(np.mean(flux_values)),
        "outside_mask_changed_pixels": flux_outside_changes,
    }

    selective = [
        per_case[case_id]["koharu_flux"]
        if case_id in HARD_CASES
        else per_case[case_id]["frank_lama"]
        for case_id in case_ids
    ]
    frank_mean = systems["frank_lama"]["mean_masked_mae"]
    flux_mean = systems["koharu_flux_hard"]["mean_masked_mae"]
    assert isinstance(frank_mean, float) and isinstance(flux_mean, float)
    checks = {
        "frank_mean_matches_frozen_result": abs(frank_mean - EXPECTED_FRANK_MEAN) < 1e-9,
        "flux_hard_mean_matches_frozen_result": abs(flux_mean - EXPECTED_FLUX_HARD_MEAN) < 1e-9,
        "routine_default_meets_29_14_reference": frank_mean <= EXPECTED_FRANK_MEAN + 1e-9,
        "oracle_selective_beats_frank_mean": float(np.mean(selective)) < frank_mean,
        "all_saved_outputs_preserve_outside_mask": all(
            int(result["outside_mask_changed_pixels"]) == 0
            for result in systems.values()
        ),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise AssertionError(f"Phase 4 gate failed: {', '.join(failed)}")
    return {
        "schema_version": 1,
        "note": (
            "The hard-case selection is a manually curated oracle subset; this "
            "does not validate a production router."
        ),
        "hard_cases": sorted(HARD_CASES),
        "systems": systems,
        "oracle_selective_mean_masked_mae": float(np.mean(selective)),
        "per_case": per_case,
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--runtime-output",
        type=Path,
        help="optional ignored local timing observation",
    )
    args = parser.parse_args()
    revision = get_pipeline_revision()
    production = evaluate_production_policy()
    if args.runtime_output:
        args.runtime_output.write_text(
            json.dumps(
                _runtime_observation(revision, production),
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    report = json.dumps(
        {
            "pipeline_revision": revision,
            "historical_backend_baseline": evaluate(),
            "current_production_policy": _without_runtimes(production),
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
