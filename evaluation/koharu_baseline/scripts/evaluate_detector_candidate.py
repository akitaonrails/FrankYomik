#!/usr/bin/env python3
"""Evaluate ogkalu's compact v4 detector against the frozen detector run.

The script only writes below the ignored evaluation output directory. It does
not import Frank's production pipeline or download a model implicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort
from common import BASE_DIR, read_json, write_json
from PIL import Image

MODEL_REVISION = "16e8a622f91fabc6b5b65c96d32d1183f8843546"
MODEL_SHA256 = "5fe9e4f576e49d4e7e8b0e029d6d3cdc252abd4694113e1cae120e62c931ea79"
LABELS = {0: "bubble", 1: "text_bubble", 2: "text_free"}
POLICIES = (
    (0.35, 0.60),
    (0.30, 0.30),
    (0.25, 0.25),
    (0.20, 0.20),
    (0.10, 0.10),
)
DEFAULT_REVIEW_ANNOTATIONS = (
    BASE_DIR / "annotations" / "phase1_detector_v4_disagreements.json"
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def overlap_ratio(left: list[float], right: list[float]) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    return intersection / area if area else 0.0


def deduplicate(predictions: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for prediction in sorted(predictions, key=lambda item: item["score"], reverse=True):
        if any(
            overlap_ratio(prediction["bbox"], existing["bbox"]) > threshold
            or overlap_ratio(existing["bbox"], prediction["bbox"]) > threshold
            for existing in kept
        ):
            continue
        kept.append(prediction)
    return kept


def center(box: list[float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def center_matches(left: list[float], right: list[float]) -> bool:
    left_center, right_center = center(left), center(right)
    return (
        left[0] <= right_center[0] <= left[2]
        and left[1] <= right_center[1] <= left[3]
    ) or (
        right[0] <= left_center[0] <= right[2]
        and right[1] <= left_center[1] <= right[3]
    )


def match_count(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> int:
    return len(match_pairs(left, right))


def match_pairs(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[tuple[int, int]]:
    unmatched = set(range(len(right)))
    matches: list[tuple[int, int]] = []
    for candidate_index, candidate in enumerate(left):
        options = [
            index
            for index in unmatched
            if center_matches(candidate["bbox"], right[index]["bbox"])
        ]
        if not options:
            continue
        candidate_center = center(candidate["bbox"])
        index = min(
            options,
            key=lambda item: sum(
                (candidate_center[axis] - center(right[item]["bbox"])[axis]) ** 2
                for axis in (0, 1)
            ),
        )
        unmatched.remove(index)
        matches.append((candidate_index, index))
    return matches


def candidate_key(page_id: str, prediction: dict[str, Any]) -> str:
    rounded_box = ",".join(str(round(value)) for value in prediction["bbox"])
    return f"{page_id}:{prediction['label']}:{rounded_box}"


def validate_manual_review(
    pages: dict[str, dict[str, Any]],
    current_root: Path,
    annotation_path: Path,
) -> dict[str, int]:
    annotations = read_json(annotation_path)
    if annotations.get("schema_version") != 1:
        raise SystemExit("unsupported detector disagreement annotation schema")
    identity = annotations.get("candidate", {})
    if identity.get("revision") != MODEL_REVISION or identity.get("sha256") != MODEL_SHA256:
        raise SystemExit("detector disagreement annotations target a different candidate")
    if annotations.get("policy") != {
        "bubble_threshold": 0.3,
        "free_text_threshold": 0.3,
        "dedup_containment_threshold": 0.5,
    }:
        raise SystemExit("detector disagreement annotations target a different policy")

    actual_candidate: dict[str, dict[str, Any]] = {}
    actual_dropped: set[str] = set()
    for page_id, result in pages.items():
        candidate = deduplicate(
            [item for item in result["predictions"] if item["score"] >= 0.30], 0.5
        )
        current = read_json(current_root / page_id / "predictions.json")["predictions"]
        pairs = match_pairs(candidate, current)
        matched_candidate = {left for left, _ in pairs}
        matched_current = {right for _, right in pairs}
        for index, prediction in enumerate(candidate):
            if index not in matched_candidate:
                actual_candidate[candidate_key(page_id, prediction)] = prediction
        for index, prediction in enumerate(current):
            if index not in matched_current:
                actual_dropped.add(f"{page_id}:{prediction['id']}")

    expected_candidate = {item["key"]: item for item in annotations["candidate_only"]}
    expected_dropped = {item["key"] for item in annotations["dropped_current"]}
    if actual_candidate.keys() != expected_candidate.keys():
        raise SystemExit("candidate-only disagreement annotations do not match inference")
    if actual_dropped != expected_dropped:
        raise SystemExit("dropped-current disagreement annotations do not match inference")
    for key, actual in actual_candidate.items():
        expected = expected_candidate[key]
        if abs(actual["score"] - expected["score"]) > 1e-6 or any(
            abs(left - right) > 1e-4
            for left, right in zip(actual["bbox"], expected["bbox"], strict=True)
        ):
            raise SystemExit(f"reviewed candidate geometry or score changed: {key}")

    decisions = {"useful", "false_positive"}
    reviewed = annotations["candidate_only"] + annotations["dropped_current"]
    if any(item.get("classification") not in decisions for item in reviewed):
        raise SystemExit("detector disagreement annotation has an invalid classification")
    measured_counts = {
        "candidate_only": len(annotations["candidate_only"]),
        "candidate_only_useful": sum(
            item["classification"] == "useful" for item in annotations["candidate_only"]
        ),
        "candidate_only_false_positive": sum(
            item["classification"] == "false_positive"
            for item in annotations["candidate_only"]
        ),
        "dropped_current": len(annotations["dropped_current"]),
        "dropped_current_useful": sum(
            item["classification"] == "useful" for item in annotations["dropped_current"]
        ),
        "dropped_current_false_positive": sum(
            item["classification"] == "false_positive"
            for item in annotations["dropped_current"]
        ),
    }
    if measured_counts != annotations.get("counts"):
        raise SystemExit("detector disagreement annotation counts are stale")
    return measured_counts


def extract_bright_component_mask(
    image: np.ndarray, bbox: list[float]
) -> np.ndarray:
    """Frozen copy of Frank's Phase 0 ordinary-balloon mask policy."""
    height, width = image.shape[:2]
    x1, y1, x2, y2 = (int(value) for value in bbox)
    box_width, box_height = x2 - x1, y2 - y1
    mask = np.zeros((height, width), dtype=np.uint8)
    if box_width < 10 or box_height < 10:
        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
        return mask

    padding = 10
    px1, py1 = max(0, x1 - padding), max(0, y1 - padding)
    px2, py2 = min(width, x2 + padding), min(height, y2 + padding)
    gray = cv2.cvtColor(image[py1:py2, px1:px2], cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    box_area = box_width * box_height
    center_x, center_y = (x1 + x2) // 2 - px1, (y1 + y2) // 2 - py1
    best_label, best_area = -1, 0
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < box_area * 0.3:
            continue
        if labels[center_y, center_x] == label:
            best_label, best_area = label, area
            break
        if area > best_area:
            best_label, best_area = label, area
    if best_label < 0 or best_area > box_area * 2.0:
        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
        return mask

    component = (labels == best_label).astype(np.uint8) * 255
    contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
        return mask
    contour = max(contours, key=cv2.contourArea) + np.asarray([px1, py1])
    cv2.drawContours(mask, [contour], -1, 255, -1)
    return mask


def measure_hybrid_masks(pages: dict[str, dict[str, Any]], koharu_root: Path) -> dict[str, Any]:
    mask_root = BASE_DIR / "outputs" / "comparison-raw" / "koharu-masks"
    overlaps: list[float] = []
    for page_id, result in pages.items():
        candidates = deduplicate(
            [
                item
                for item in result["predictions"]
                if item["label"] != "text_free" and item["score"] >= 0.30
            ],
            0.5,
        )
        all_koharu = read_json(koharu_root / f"{page_id}.json")["detections"]
        indexed_koharu = [
            (index, item) for index, item in enumerate(all_koharu) if item["label"] == "bubble"
        ]
        pairs = match_pairs(candidates, [item for _, item in indexed_koharu])
        image = cv2.imread(str(BASE_DIR / "assets" / "pages" / f"{page_id}.png"))
        if image is None:
            raise SystemExit(f"could not read source page {page_id}")
        for candidate_index, koharu_index in pairs:
            detection_index = indexed_koharu[koharu_index][0]
            expected_path = mask_root / page_id / f"bubble-{detection_index:03d}.png"
            expected = cv2.imread(str(expected_path), cv2.IMREAD_GRAYSCALE)
            if expected is None:
                raise SystemExit(f"missing Koharu mask {expected_path}")
            actual = extract_bright_component_mask(image, candidates[candidate_index]["bbox"])
            intersection = np.logical_and(actual > 0, expected > 0).sum()
            union = np.logical_or(actual > 0, expected > 0).sum()
            overlaps.append(float(intersection / union if union else 1.0))
    return {
        "pairs": len(overlaps),
        "mean_iou": statistics.mean(overlaps),
        "median_iou": statistics.median(overlaps),
    }


def infer(session: ort.InferenceSession, image_path: Path) -> tuple[list[dict[str, Any]], float]:
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    resized = image.resize((640, 640), Image.Resampling.BILINEAR)
    pixels = np.asarray(resized, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    started = time.perf_counter()
    labels, boxes, scores = session.run(
        None,
        {
            "images": pixels,
            # This exported graph expects width, height despite the input name.
            "orig_target_sizes": np.asarray([[width, height]], dtype=np.int64),
        },
    )
    elapsed = time.perf_counter() - started
    predictions = [
        {
            "label_id": int(label),
            "label": LABELS[int(label)],
            "score": float(score),
            "bbox": [float(value) for value in box],
        }
        for label, box, score in zip(labels[0], boxes[0], scores[0], strict=True)
    ]
    return predictions, elapsed


def evaluate(model: Path, output: Path, review_annotations: Path | None) -> dict[str, Any]:
    actual_sha256 = file_sha256(model)
    if actual_sha256 != MODEL_SHA256:
        raise SystemExit(f"model checksum mismatch: {actual_sha256}")

    current_root = BASE_DIR / "outputs" / "comparison-raw" / "frank"
    koharu_root = BASE_DIR / "outputs" / "comparison-raw" / "koharu"
    if not current_root.exists() or not koharu_root.exists():
        raise SystemExit("the frozen Frank and Koharu detector outputs are required")

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = 1
    session_options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    pages: dict[str, dict[str, Any]] = {}
    timings: list[float] = []
    for image_path in sorted((BASE_DIR / "assets" / "pages").glob("*.png")):
        predictions, elapsed = infer(session, image_path)
        pages[image_path.stem] = {"predictions": predictions, "elapsed_seconds": elapsed}
        timings.append(elapsed)

    policy_reports = []
    for bubble_threshold, free_text_threshold in POLICIES:
        totals: defaultdict[str, int] = defaultdict(int)
        for page_id, result in pages.items():
            current = read_json(current_root / page_id / "predictions.json")["predictions"]
            koharu = [
                item
                for item in read_json(koharu_root / f"{page_id}.json")["detections"]
                if item["label"] in {"text", "onomatopoeia"}
            ]
            selected = [
                item
                for item in result["predictions"]
                if item["score"]
                >= (free_text_threshold if item["label"] == "text_free" else bubble_threshold)
            ]
            candidate = deduplicate(selected, 0.5)
            current_matches = match_count(candidate, current)
            koharu_matches = match_count(candidate, koharu)
            totals["candidate_regions"] += len(candidate)
            totals["current_regions"] += len(current)
            totals["koharu_text_sfx_regions"] += len(koharu)
            totals["matched_current"] += current_matches
            totals["missed_current"] += len(current) - current_matches
            totals["candidate_only_vs_current"] += len(candidate) - current_matches
            totals["matched_koharu"] += koharu_matches
            totals["missed_koharu"] += len(koharu) - koharu_matches
            totals["candidate_only_vs_koharu"] += len(candidate) - koharu_matches
        policy_reports.append(
            {
                "bubble_threshold": bubble_threshold,
                "free_text_threshold": free_text_threshold,
                "dedup_containment_threshold": 0.5,
                **totals,
            }
        )

    deduplication_reports = []
    for threshold in (0.30, 0.40, 0.50, 0.60, 0.70, 0.80):
        totals: defaultdict[str, int] = defaultdict(int)
        for page_id, result in pages.items():
            current = read_json(current_root / page_id / "predictions.json")["predictions"]
            selected = [item for item in result["predictions"] if item["score"] >= 0.30]
            candidate = deduplicate(selected, threshold)
            matches = match_count(candidate, current)
            totals["candidate_regions"] += len(candidate)
            totals["matched_current"] += matches
            totals["missed_current"] += len(current) - matches
        deduplication_reports.append({"containment_threshold": threshold, **totals})

    report = {
        "schema_version": 1,
        "candidate": {
            "repository": "ogkalu/comic-text-and-bubble-detector",
            "revision": MODEL_REVISION,
            "artifact": model.name,
            "sha256": actual_sha256,
        },
        "corpus_pages": len(pages),
        "cpu_inference_seconds": {
            "mean": statistics.mean(timings),
            "median": statistics.median(timings),
            "minimum": min(timings),
            "maximum": max(timings),
        },
        "policies": policy_reports,
        "deduplication_sweep": deduplication_reports,
        "hybrid_mask_agreement": measure_hybrid_masks(pages, koharu_root),
        "limits": [
            "Koharu detections are an audited comparison baseline, not independent box ground truth.",
            "The candidate emits boxes only, so mask quality requires Frank's existing hybrid extractor.",
        ],
    }
    if review_annotations is not None:
        report["manual_disagreement_review"] = {
            "annotations": str(review_annotations),
            "counts": validate_manual_review(pages, current_root, review_annotations),
        }
    write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR / "outputs" / "phase1-detector-candidate" / "evaluation.json",
    )
    parser.add_argument(
        "--review-annotations",
        type=Path,
        default=DEFAULT_REVIEW_ANNOTATIONS,
        help="manual disagreement annotations to validate",
    )
    parser.add_argument("--skip-review-validation", action="store_true")
    args = parser.parse_args()
    review_annotations = None if args.skip_review_validation else args.review_annotations
    report = evaluate(args.model, args.output, review_annotations)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
