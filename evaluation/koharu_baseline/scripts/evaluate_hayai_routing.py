#!/usr/bin/env python3
"""Score Hayai's frozen difficulty oracle and audit deployable route signals."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common import BASE_DIR, read_json, write_json

SCORE_PATH = BASE_DIR / "outputs/comparison-raw/ocr-verified-score.json"
SCORE_SHA256 = "7867fc1edbbcf50c4288fcd28c4ac6018720fc8a2ca51f06807067bc410f5db8"
MANIFEST_PATH = BASE_DIR / "assets/ocr_eval/manifest.json"
FRANK_OCR_PATH = BASE_DIR / "outputs/comparison-raw/ocr-corpus-frank.json"
KOHARU_DETECTION_DIR = BASE_DIR / "outputs/comparison-raw/koharu"
FRANK_DETECTION_DIR = BASE_DIR / "outputs/comparison-raw/frank"
KOHARU_CORPUS_THRESHOLD = 0.95


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    errors = sum(case["distance"] for case in cases)
    characters = sum(case["characters"] for case in cases)
    exact = sum(case["distance"] == 0 for case in cases)
    return {
        "cases": len(cases),
        "errors": errors,
        "characters": characters,
        "character_error_rate": errors / characters,
        "exact": exact,
        "exact_match_rate": exact / len(cases),
    }


def is_valid_japanese(text: str) -> bool:
    """Mirror the current production validator without importing model code."""
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    content = other = 0
    for character in stripped:
        codepoint = ord(character)
        if (
            0x3040 <= codepoint <= 0x309F
            or 0x30A0 <= codepoint <= 0x30FF
            or 0x4E00 <= codepoint <= 0x9FFF
        ):
            content += 1
        elif not (
            0x3000 <= codepoint <= 0x303F
            or 0xFF01 <= codepoint <= 0xFF60
        ):
            other += 1
    meaningful = content + other
    return content >= 1 and meaningful > 0 and content / meaningful > 0.5


def meaningful_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def contains_center(container: list[float], child: list[float]) -> bool:
    center_x = (child[0] + child[2]) / 2
    center_y = (child[1] + child[3]) / 2
    return (
        container[0] <= center_x <= container[2]
        and container[1] <= center_y <= container[3]
    )


def area(box: list[float]) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def validate_manifest_provenance(manifest: dict[str, dict[str, Any]]) -> None:
    """Prove that manifest score/geometry came from Koharu text output."""
    pages: dict[str, list[dict[str, Any]]] = {}
    for case in manifest.values():
        detections = pages.setdefault(
            case["page"],
            read_json(KOHARU_DETECTION_DIR / f"{case['page']}.json")["detections"],
        )
        matches = [
            detection
            for detection in detections
            if detection["label"] == "text"
            and detection["score"] == case["score"]
            and all(
                abs(actual - recorded) <= 0.011
                for actual, recorded in zip(
                    detection["bbox"], case["bbox"], strict=True
                )
            )
        ]
        if len(matches) != 1:
            raise SystemExit(
                f"{case['id']}: crop manifest does not map uniquely to Koharu output"
            )


def signal_summary(
    rows: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    selected = [row for row in rows if predicate(row)]
    return {
        "selected": len(selected),
        "difficult_selected": sum(row["difficult"] for row in selected),
        "ordinary_selected": sum(not row["difficult"] for row in selected),
        "selected_case_ids": [row["id"] for row in selected],
    }


def audit_current_signals(
    case_ids: Any, manifest: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    frank_ocr = read_json(FRANK_OCR_PATH)["cases"]
    pages: dict[str, list[dict[str, Any]]] = {}
    rows = []
    for case_id in case_ids:
        case = manifest[case_id]
        predictions = pages.setdefault(
            case["page"],
            read_json(
                FRANK_DETECTION_DIR / case["page"] / "predictions.json"
            )["predictions"],
        )
        containing = [
            prediction
            for prediction in predictions
            if contains_center(prediction["bbox"], case["bbox"])
        ]
        parent = min(containing, key=lambda item: area(item["bbox"])) if containing else None
        text = frank_ocr[case_id]["text"]
        width = case["bbox"][2] - case["bbox"][0]
        height = case["bbox"][3] - case["bbox"][1]
        rows.append(
            {
                "id": case_id,
                "difficult": case_id.endswith("__hard"),
                "primary_text": text,
                "primary_valid_japanese": is_valid_japanese(text),
                "primary_meaningful_characters": meaningful_count(text),
                "primary_contains_digit": any(character.isdigit() for character in text),
                "primary_punctuation_only": bool(text.strip())
                and meaningful_count(text) == 0,
                "koharu_crop_is_horizontal": width > height,
                "current_detector_parent_found": parent is not None,
                "current_detector_parent_confidence": None
                if parent is None
                else parent["score"],
                "current_detector_parent_class": None
                if parent is None
                else parent["class"],
            }
        )

    matched_ordinary = [
        row
        for row in rows
        if not row["difficult"] and row["current_detector_parent_found"]
    ]
    matched_difficult = [
        row
        for row in rows
        if row["difficult"] and row["current_detector_parent_found"]
    ]
    no_false_positive_cutoff = min(
        row["current_detector_parent_confidence"] for row in matched_ordinary
    )
    all_difficult_cutoff = max(
        row["current_detector_parent_confidence"] for row in matched_difficult
    )
    signals = {
        "primary_invalid": signal_summary(
            rows, lambda row: not row["primary_valid_japanese"]
        ),
        "primary_shorter_than_15_meaningful_characters": signal_summary(
            rows, lambda row: row["primary_meaningful_characters"] < 15
        ),
        "primary_contains_digit": signal_summary(
            rows, lambda row: row["primary_contains_digit"]
        ),
        "primary_punctuation_only": signal_summary(
            rows, lambda row: row["primary_punctuation_only"]
        ),
        "koharu_crop_horizontal": signal_summary(
            rows, lambda row: row["koharu_crop_is_horizontal"]
        ),
        "current_detector_has_no_parent_region": signal_summary(
            rows, lambda row: not row["current_detector_parent_found"]
        ),
        "current_detector_below_no_false_positive_cutoff_or_missing": {
            "cutoff_exclusive": no_false_positive_cutoff,
            **signal_summary(
                rows,
                lambda row: not row["current_detector_parent_found"]
                or row["current_detector_parent_confidence"]
                < no_false_positive_cutoff,
            ),
        },
        "current_detector_at_most_max_difficult_confidence_or_missing": {
            "cutoff_inclusive": all_difficult_cutoff,
            **signal_summary(
                rows,
                lambda row: not row["current_detector_parent_found"]
                or row["current_detector_parent_confidence"]
                <= all_difficult_cutoff,
            ),
        },
    }
    return {
        "case_rows": rows,
        "signals": signals,
        "limitations": [
            "Current-detector association is retrospective center containment, not a production region ID.",
            "Seven difficult Koharu text crops have no current Frank parent region and never reach production OCR.",
            "The OCR benchmark used Koharu text crops; production OCR receives Frank parent-region crops.",
            "Confidence cutoffs were inspected on this selected corpus and are not calibrated thresholds.",
            "Two-crop Manga OCR consistency was not recorded and cannot be evaluated here.",
        ],
    }


def evaluate() -> dict[str, Any]:
    if sha256(SCORE_PATH) != SCORE_SHA256:
        raise SystemExit("frozen OCR score input changed; review before rescoring")
    score = read_json(SCORE_PATH)
    manifest = {case["id"]: case for case in read_json(MANIFEST_PATH)["cases"]}
    validate_manifest_provenance(manifest)
    primary = {case["id"]: case for case in score["systems"]["frank"]["cases"]}
    fallback = {
        case["id"]: case for case in score["systems"]["koharu_hayai"]["cases"]
    }
    if primary.keys() != fallback.keys():
        raise SystemExit("primary and fallback OCR cases differ")

    difficult_ids = {case_id for case_id in primary if case_id.endswith("__hard")}
    koharu_low_score = {
        case_id
        for case_id in primary
        if float(manifest[case_id]["score"]) < KOHARU_CORPUS_THRESHOLD
    }
    if koharu_low_score != difficult_ids:
        raise SystemExit("frozen Koharu difficulty-set construction changed")

    selected = [
        fallback[case_id] if case_id in difficult_ids else primary[case_id]
        for case_id in primary
    ]
    difficult = [case for case in selected if case["id"] in difficult_ids]
    ordinary = [case for case in selected if case["id"] not in difficult_ids]
    comparisons = {
        label: sum(
            compare(fallback[case_id]["distance"], primary[case_id]["distance"])
            for case_id in difficult_ids
        )
        for label, compare in {
            "improved": lambda fallback_distance, primary_distance: fallback_distance
            < primary_distance,
            "equal": lambda fallback_distance, primary_distance: fallback_distance
            == primary_distance,
            "regressed": lambda fallback_distance, primary_distance: fallback_distance
            > primary_distance,
        }.items()
    }
    metrics = {
        "overall": aggregate(selected),
        "difficult": aggregate(difficult),
        "ordinary": aggregate(ordinary),
    }
    references = {
        "hayai_all": aggregate(list(fallback.values())),
        "hayai_difficult": aggregate(
            [case for case_id, case in fallback.items() if case_id in difficult_ids]
        ),
    }
    acceptance = {
        "overall_cer_no_worse_than_hayai_all": metrics["overall"][
            "character_error_rate"
        ]
        <= references["hayai_all"]["character_error_rate"],
        "overall_exact_no_worse_than_hayai_all": metrics["overall"][
            "exact_match_rate"
        ]
        >= references["hayai_all"]["exact_match_rate"],
        "difficult_cer_matches_hayai": metrics["difficult"]["character_error_rate"]
        == references["hayai_difficult"]["character_error_rate"],
        "difficult_exact_matches_hayai": metrics["difficult"]["exact_match_rate"]
        == references["hayai_difficult"]["exact_match_rate"],
        "ordinary_unchanged": all(
            case["distance"] == primary[case["id"]]["distance"] for case in ordinary
        ),
    }
    return {
        "schema_version": 2,
        "source_sha256": SCORE_SHA256,
        "manifest_provenance": {
            "source": "Koharu semantic detector text-class predictions",
            "manifest_cases_verified": len(manifest),
            "scored_cases": len(primary),
            "koharu_corpus_construction_threshold": KOHARU_CORPUS_THRESHOLD,
            "current_frank_detector_score": False,
        },
        "evaluation_selection": {
            "kind": "oracle_difficulty_category",
            "primary": "frank_manga_ocr",
            "fallback": "koharu_hayai_v2.1_greedy",
            "case_ids": sorted(difficult_ids),
            "deployable_policy": False,
        },
        "comparisons_on_oracle_difficult_cases": comparisons,
        "metrics": metrics,
        "reference_metrics": references,
        "oracle_quality_acceptance": acceptance,
        "oracle_quality_gate_passed": all(acceptance.values()),
        "production_router_validated": False,
        "current_signal_audit": audit_current_signals(primary, manifest),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR / "outputs/phase2-hayai-routing.json",
    )
    args = parser.parse_args()
    report = evaluate()
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
