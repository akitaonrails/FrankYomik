#!/usr/bin/env python3
"""Prepare, run, and score the focused Frank/Koharu comparison.

No model is imported by this module. GPU work begins only under `run`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from common import (
    BASE_DIR,
    REPO_DIR,
    bbox_iou,
    edit_distance,
    expand_path,
    load_config,
    normalized_text,
    read_json,
    sha256,
    write_json,
)


DATASET = BASE_DIR / "dataset.json"
DETECTION_GT = BASE_DIR / "annotations" / "detection.json"
OCR_GT = BASE_DIR / "annotations" / "ocr.json"
INPAINT_GT = BASE_DIR / "annotations" / "inpainting.json"
ADAPTER = Path(__file__).with_name("frank_adapter.py")


def safe_id(value: str) -> str:
    return "".join(character if character.isalnum() or character in "_-" else "_" for character in value)


def image_details(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def copy_asset(source: Path, destination_dir: Path, prefix: str) -> tuple[str, Path]:
    digest = sha256(source)
    identifier = safe_id(f"{prefix}__{source.stem}")
    destination = destination_dir / f"{identifier}{source.suffix.lower()}"
    destination_dir.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or sha256(destination) != digest:
        shutil.copy2(source, destination)
    return identifier, destination


def prepare(args: argparse.Namespace) -> None:
    dataset = read_json(DATASET)
    detection = read_json(DETECTION_GT)
    sources: list[tuple[Path, str]] = []
    if args.seed_docs:
        docs = REPO_DIR / "docs"
        names = ["adult.png", "adult2.png", "adult3.png", "adult4.png", "adult5.png", "adult6_original.png", "tiny.png"]
        names += ["shounen.png"] + [f"shounen{index}.png" for index in range(2, 13)]
        sources.extend((docs / name, "manga") for name in names)
    if args.add_pages:
        extensions = {".png", ".jpg", ".jpeg", ".webp"}
        sources.extend(
            (path, args.kind)
            for path in sorted(args.add_pages.iterdir())
            if path.is_file() and path.suffix.lower() in extensions
        )
    existing = {page["id"]: page for page in dataset["pages"]}
    for source, kind in sources:
        if not source.exists():
            raise SystemExit(f"missing source page: {source}")
        identifier, copied = copy_asset(source, BASE_DIR / "assets" / "pages", kind)
        width, height = image_details(copied)
        existing[identifier] = {
            "id": identifier,
            "kind": kind,
            "image": str(copied.relative_to(BASE_DIR)),
            "width": width,
            "height": height,
            "sha256": sha256(copied),
            "source_note": "repository sample" if args.seed_docs and source.parent == REPO_DIR / "docs" else "private evaluation page",
        }
        detection["pages"].setdefault(identifier, {"instances": [], "annotation_status": "unlabeled"})
    dataset["pages"] = sorted(existing.values(), key=lambda page: page["id"])
    write_json(DATASET, dataset)
    write_json(DETECTION_GT, detection)
    print(f"Prepared {len(sources)} source pages; dataset now has {len(dataset['pages'])} pages.")
    print("No inference was run. Label detection instances before scoring.")


def add_ocr(args: argparse.Namespace) -> None:
    dataset = read_json(DATASET)
    annotations = read_json(OCR_GT)
    identifier, copied = copy_asset(args.image, BASE_DIR / "assets" / "ocr", f"ocr_{args.language}")
    identifier = safe_id(args.id or identifier)
    destination = copied.with_name(f"{identifier}{copied.suffix}")
    if destination != copied:
        copied.replace(destination)
    cases = {case["id"]: case for case in dataset["ocr_cases"]}
    cases[identifier] = {
        "id": identifier,
        "image": str(destination.relative_to(BASE_DIR)),
        "language": args.language,
        "sha256": sha256(destination),
    }
    dataset["ocr_cases"] = sorted(cases.values(), key=lambda case: case["id"])
    annotations["cases"][identifier] = {"text": args.text, "verified": bool(args.verified)}
    write_json(DATASET, dataset)
    write_json(OCR_GT, annotations)
    print(f"Added OCR case {identifier}.")


def add_inpainting(args: argparse.Namespace) -> None:
    dataset = read_json(DATASET)
    annotations = read_json(INPAINT_GT)
    identifier, image_path = copy_asset(args.image, BASE_DIR / "assets" / "inpainting", "inpaint")
    identifier = safe_id(args.id or identifier)
    mask_id, mask_path = copy_asset(args.mask, BASE_DIR / "assets" / "inpainting", f"{identifier}_mask")
    del mask_id
    reference_path = None
    if args.reference:
        _, reference_path = copy_asset(args.reference, BASE_DIR / "assets" / "inpainting", f"{identifier}_reference")
    dimensions = {image_details(path) for path in (image_path, mask_path, reference_path) if path}
    if len(dimensions) != 1:
        raise SystemExit("input, mask, and reference must have identical dimensions")
    cases = {case["id"]: case for case in dataset["inpainting_cases"]}
    cases[identifier] = {
        "id": identifier,
        "kind": args.kind,
        "input": str(image_path.relative_to(BASE_DIR)),
        "mask": str(mask_path.relative_to(BASE_DIR)),
        "reference": str(reference_path.relative_to(BASE_DIR)) if reference_path else None,
        "sha256": sha256(image_path),
    }
    dataset["inpainting_cases"] = sorted(cases.values(), key=lambda case: case["id"])
    annotations["cases"][identifier] = {
        "description": args.description,
        "review_required": True,
        "has_clean_reference": bool(reference_path),
    }
    write_json(DATASET, dataset)
    write_json(INPAINT_GT, annotations)
    print(f"Added inpainting case {identifier}.")


def validate(args: argparse.Namespace) -> None:
    dataset = read_json(DATASET)
    detection = read_json(DETECTION_GT)
    ocr = read_json(OCR_GT)
    errors: list[str] = []
    warnings: list[str] = []
    ids: set[str] = set()
    for page in dataset["pages"]:
        if page["id"] in ids:
            errors.append(f"duplicate page id {page['id']}")
        ids.add(page["id"])
        path = BASE_DIR / page["image"]
        if not path.exists():
            errors.append(f"missing page {path}")
            continue
        if sha256(path) != page["sha256"]:
            errors.append(f"hash mismatch for {page['id']}")
        if image_details(path) != (page["width"], page["height"]):
            errors.append(f"dimension mismatch for {page['id']}")
        ground_truth = detection["pages"].get(page["id"])
        if not ground_truth:
            errors.append(f"missing detection annotation entry for {page['id']}")
        elif not ground_truth.get("instances"):
            warnings.append(f"{page['id']} has no labeled detection instances")
        else:
            for instance in ground_truth["instances"]:
                label = instance.get("class")
                if label not in {"bubble", "text", "onomatopoeia", "panel"}:
                    errors.append(f"invalid class {label!r} in {page['id']}")
                bbox = instance.get("bbox", [])
                if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    errors.append(f"invalid bbox for {instance.get('id')} in {page['id']}")
                elif bbox[0] < 0 or bbox[1] < 0 or bbox[2] > page["width"] or bbox[3] > page["height"]:
                    errors.append(f"out-of-bounds bbox for {instance.get('id')} in {page['id']}")
                if instance.get("mask"):
                    mask = BASE_DIR / instance["mask"]
                    if not mask.exists():
                        errors.append(f"missing mask {mask}")
                    elif image_details(mask) != (page["width"], page["height"]):
                        errors.append(f"mask dimensions differ for {instance.get('id')} in {page['id']}")
    for case in dataset["ocr_cases"]:
        path = BASE_DIR / case["image"]
        if not path.exists():
            errors.append(f"missing OCR crop {path}")
        truth = ocr["cases"].get(case["id"])
        if not truth or not truth.get("text"):
            errors.append(f"missing OCR ground truth for {case['id']}")
        elif not truth.get("verified"):
            warnings.append(f"OCR transcription {case['id']} is not marked verified")
    for case in dataset["inpainting_cases"]:
        paths = [BASE_DIR / case["input"], BASE_DIR / case["mask"]]
        if case.get("reference"):
            paths.append(BASE_DIR / case["reference"])
        if any(not path.exists() for path in paths):
            errors.append(f"missing inpainting asset for {case['id']}")
        elif len({image_details(path) for path in paths}) != 1:
            errors.append(f"inpainting dimensions differ for {case['id']}")
    for message in warnings:
        print(f"WARNING: {message}")
    for message in errors:
        print(f"ERROR: {message}")
    if errors or (args.require_labels and warnings):
        raise SystemExit(1)
    print(f"Valid: {len(dataset['pages'])} pages, {len(dataset['ocr_cases'])} OCR cases, {len(dataset['inpainting_cases'])} inpainting cases.")


def build_commands(_: argparse.Namespace) -> None:
    print("Run from the Koharu checkout after confirming the pinned commit:")
    print("git rev-parse HEAD")
    print(f"git apply {BASE_DIR / 'patches' / 'koharu-mask-json.patch'}")
    binaries = [
        "koharu_layout_rfdetr_seg_2xl", "manga_ocr", "baberu_ocr", "hayai_ocr",
        "paddle_ocr_vl", "lama", "aot_inpainting", "rorem_mixed", "flux2_klein",
    ]
    print("cargo build --release -p koharu-ml " + " ".join(f"--bin {binary}" for binary in binaries))
    print("git restore --source=HEAD -- crates/koharu-ml/src/koharu_layout_rfdetr_seg_2xl/processor.rs")
    print("The temporary patch exposes masks in detector JSON. The restore leaves the built binary intact.")
    print("This command only prints the build instructions; it does not patch, build, restore, or download models.")


def run_process(command: list[str], stdout_path: Path, stderr_path: Path) -> float:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(command, stdout=stdout, stderr=stderr, text=True, check=False)
    elapsed = time.perf_counter() - started
    if completed.returncode:
        raise SystemExit(f"command failed ({completed.returncode}); see {stderr_path}\n{' '.join(command)}")
    return elapsed


def selected(items: list[dict[str, Any]], requested: list[str] | None) -> list[dict[str, Any]]:
    if not requested:
        return items
    wanted = set(requested)
    result = [item for item in items if item["id"] in wanted]
    missing = wanted - {item["id"] for item in result}
    if missing:
        raise SystemExit(f"unknown case IDs: {', '.join(sorted(missing))}")
    return result


def koharu_binary(config: dict[str, Any], name: str) -> Path:
    root = expand_path(config["koharu"]["repo"])
    actual_commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual_commit != config["koharu"]["commit"]:
        raise SystemExit(
            f"Koharu checkout is {actual_commit}; expected {config['koharu']['commit']}"
        )
    binary = root / config["koharu"]["binary_dir"] / name
    if not binary.exists():
        raise SystemExit(f"Koharu binary not found: {binary}\nUse the printed build commands first.")
    return binary


def normalize_koharu_detection(raw_path: Path, output_path: Path, mask_dir: Path, image_path: Path) -> None:
    raw = read_json(raw_path)
    if raw.get("detections") and not any(detection.get("mask") for detection in raw["detections"]):
        raise SystemExit(
            "Koharu detector JSON contains no masks; rebuild it with patches/koharu-mask-json.patch"
        )
    width = int(raw.get("image_width") or Image.open(image_path).width)
    height = int(raw.get("image_height") or Image.open(image_path).height)
    mask_dir.mkdir(parents=True, exist_ok=True)
    predictions = []
    for index, detection in enumerate(raw["detections"]):
        identifier = f"koharu-{safe_id(detection['label'])}-{index:04d}"
        mask_data = detection.get("mask")
        mask_name = None
        if mask_data and mask_data.get("pixels"):
            local = Image.frombytes("L", (int(mask_data["width"]), int(mask_data["height"])), bytes(mask_data["pixels"]))
            canvas = Image.new("L", (width, height), 0)
            canvas.paste(local, (int(mask_data["x"]), int(mask_data["y"])))
            path = mask_dir / f"{identifier}.png"
            canvas.save(path)
            mask_name = str(path.relative_to(output_path.parent))
        predictions.append({
            "id": identifier,
            "class": detection["label"],
            "score": float(detection["score"]),
            "bbox": [float(value) for value in detection["bbox"]],
            "mask": mask_name,
        })
    write_json(output_path, {
        "schema_version": 1, "system": "koharu", "stage": "detection",
        "image": str(image_path), "width": width, "height": height,
        "predictions": predictions,
    })


def run_detection(args: argparse.Namespace, config: dict[str, Any], cases: list[dict[str, Any]], root: Path) -> None:
    for system in args.system:
        system_dir = root / "detection" / safe_id(system)
        for case in cases:
            case_dir = system_dir / case["id"]
            output = case_dir / "predictions.json"
            image = BASE_DIR / case["image"]
            if system == "frank":
                command = [config["frank"]["python"], str(ADAPTER), "detection", "--input", str(image), "--output", str(output), "--mask-dir", str(case_dir / "masks")]
                run_process(command, case_dir / "stdout.log", case_dir / "stderr.log")
            elif system == "koharu":
                binary = koharu_binary(config, config["koharu"]["detection_binary"])
                raw = case_dir / "raw.json"
                command = [str(binary), "--input", str(image), "--output", str(raw)]
                elapsed = run_process(command, case_dir / "stdout.log", case_dir / "stderr.log")
                normalize_koharu_detection(raw, output, case_dir / "masks", image)
                value = read_json(output)
                value["elapsed_seconds"] = elapsed
                write_json(output, value)
            else:
                raise SystemExit(f"unknown detection system: {system}")
            print(f"completed detection {system} {case['id']}")


def run_ocr(args: argparse.Namespace, config: dict[str, Any], cases: list[dict[str, Any]], root: Path) -> None:
    for system in args.system:
        for case in cases:
            case_dir = root / "ocr" / safe_id(system) / case["id"]
            output = case_dir / "result.json"
            image = BASE_DIR / case["image"]
            if system == "frank":
                command = [config["frank"]["python"], str(ADAPTER), "ocr", "--input", str(image), "--language", case["language"], "--output", str(output)]
                run_process(command, case_dir / "stdout.log", case_dir / "stderr.log")
            elif system.startswith("koharu:"):
                engine = system.split(":", 1)[1]
                name = config["koharu"]["ocr_binaries"].get(engine)
                if not name:
                    raise SystemExit(f"unknown Koharu OCR engine: {engine}")
                command = [str(koharu_binary(config, name)), "--input", str(image)]
                if engine == "paddle_ocr_vl":
                    command += ["--task", "ocr"]
                elapsed = run_process(command, case_dir / "stdout.log", case_dir / "stderr.log")
                text = (case_dir / "stdout.log").read_text(encoding="utf-8").strip().splitlines()[-1]
                write_json(output, {"schema_version": 1, "system": "koharu", "stage": "ocr", "engine": engine, "language": case["language"], "text": text, "elapsed_seconds": elapsed})
            else:
                raise SystemExit(f"unknown OCR system: {system}")
            print(f"completed OCR {system} {case['id']}")


def composite_inside_mask(source_path: Path, generated_path: Path, mask_path: Path) -> None:
    source = Image.open(source_path).convert("RGB")
    generated = Image.open(generated_path).convert("RGB")
    mask = Image.open(mask_path).convert("L").point(lambda value: 255 if value >= 127 else 0)
    if generated.size != source.size:
        generated = generated.resize(source.size, Image.Resampling.LANCZOS)
    Image.composite(generated, source, mask).save(generated_path)


def run_inpainting(args: argparse.Namespace, config: dict[str, Any], cases: list[dict[str, Any]], root: Path) -> None:
    for system in args.system:
        for case in cases:
            case_dir = root / "inpainting" / safe_id(system) / case["id"]
            case_dir.mkdir(parents=True, exist_ok=True)
            output = case_dir / "output.png"
            metadata = case_dir / "result.json"
            source = BASE_DIR / case["input"]
            mask = BASE_DIR / case["mask"]
            if system.startswith("frank:"):
                engine = system.split(":", 1)[1]
                command = [config["frank"]["python"], str(ADAPTER), "inpaint", "--input", str(source), "--mask", str(mask), "--backend", engine, "--output", str(output), "--metadata", str(metadata)]
                run_process(command, case_dir / "stdout.log", case_dir / "stderr.log")
            elif system.startswith("koharu:"):
                engine = system.split(":", 1)[1]
                name = config["koharu"]["inpainting_binaries"].get(engine)
                if not name:
                    raise SystemExit(f"unknown Koharu inpainting engine: {engine}")
                command = [str(koharu_binary(config, name)), "--input", str(source), "--mask", str(mask), "--output", str(output)]
                if engine == "rorem":
                    command += ["--seed", str(config["inpainting"]["seed"])]
                elif engine == "flux2_klein":
                    command += ["--prompt", "Remove the text and reconstruct the original manga artwork.", "--seed", str(config["inpainting"]["seed"])]
                elapsed = run_process(command, case_dir / "stdout.log", case_dir / "stderr.log")
                composite_inside_mask(source, output, mask)
                write_json(metadata, {"schema_version": 1, "system": "koharu", "stage": "inpainting", "engine": engine, "elapsed_seconds": elapsed, "output": str(output)})
            else:
                raise SystemExit(f"unknown inpainting system: {system}")
            print(f"completed inpainting {system} {case['id']}")


def run(args: argparse.Namespace) -> None:
    config = load_config()
    dataset = read_json(DATASET)
    run_id = safe_id(args.run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    root = BASE_DIR / "outputs" / run_id
    cases = selected(dataset[f"{args.stage}_cases"] if args.stage != "detection" else dataset["pages"], args.case)
    manifest_path = root / "run.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "frank_commit": subprocess.run(["git", "-C", str(REPO_DIR), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
        "koharu_expected_commit": config["koharu"]["commit"],
        "invocations": [],
    }
    manifest["invocations"].append({"stage": args.stage, "systems": args.system, "cases": [case["id"] for case in cases]})
    write_json(manifest_path, manifest)
    if args.stage == "detection":
        run_detection(args, config, cases, root)
    elif args.stage == "ocr":
        run_ocr(args, config, cases, root)
    else:
        run_inpainting(args, config, cases, root)
    print(f"Run output: {root}")


def load_mask(base: Path, relative: str | None) -> Image.Image | None:
    return Image.open(base / relative).convert("L") if relative else None


def score_detection(run_root: Path) -> dict[str, Any]:
    config = load_config()
    ground_truth = read_json(DETECTION_GT)["pages"]
    page_kinds = {page["id"]: page["kind"] for page in read_json(DATASET)["pages"]}
    threshold = float(config["detection"]["iou_threshold"])
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    labeled_pages = 0
    for prediction_path in run_root.glob("detection/*/*/predictions.json"):
        system = prediction_path.parent.parent.name
        case_id = prediction_path.parent.name
        annotation = ground_truth.get(case_id, {})
        if annotation.get("annotation_status") != "verified":
            continue
        labeled_pages += 1
        truth = annotation.get("instances", [])
        predictions = read_json(prediction_path)["predictions"]
        for group in ("overall", page_kinds.get(case_id, "unknown")):
            values = totals[(system, group)]
            classes = sorted({item["class"] for item in truth + predictions})
            for label in classes:
                expected = [item for item in truth if item["class"] == label and not item.get("ignore")]
                candidates = sorted((item for item in predictions if item["class"] == label), key=lambda item: item.get("score", 0), reverse=True)
                unmatched = set(range(len(expected)))
                for candidate in candidates:
                    options = [(bbox_iou(candidate["bbox"], expected[index]["bbox"]), index) for index in unmatched]
                    overlap, index = max(options, default=(0.0, -1))
                    if overlap >= threshold:
                        values[f"{label}.tp"] += 1
                        values[f"{label}.bbox_iou"] += overlap
                        values[f"{label}.matched"] += 1
                        predicted_mask = load_mask(prediction_path.parent, candidate.get("mask"))
                        expected_mask = load_mask(BASE_DIR, expected[index].get("mask"))
                        if predicted_mask and expected_mask:
                            left = predicted_mask.point(lambda value: 255 if value >= 127 else 0)
                            right = expected_mask.point(lambda value: 255 if value >= 127 else 0)
                            intersection = ImageStat.Stat(ImageChops.logical_and(left.convert("1"), right.convert("1"))).sum[0]
                            union = ImageStat.Stat(ImageChops.logical_or(left.convert("1"), right.convert("1"))).sum[0]
                            values[f"{label}.mask_iou"] += intersection / union if union else 1.0
                            values[f"{label}.mask_matched"] += 1
                        unmatched.remove(index)
                    else:
                        values[f"{label}.fp"] += 1
                values[f"{label}.fn"] += len(unmatched)
    if not labeled_pages:
        raise SystemExit("no verified detection annotations overlap this run")
    report: dict[str, Any] = {"schema_version": 1, "systems": {}}
    for (system, group), values in totals.items():
        result: dict[str, Any] = {}
        labels = sorted({key.split(".")[0] for key in values})
        for label in labels:
            tp, fp, fn = values[f"{label}.tp"], values[f"{label}.fp"], values[f"{label}.fn"]
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
            result[label] = {
                "tp": int(tp), "fp": int(fp), "fn": int(fn), "precision": precision, "recall": recall,
                "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                "mean_bbox_iou": values[f"{label}.bbox_iou"] / values[f"{label}.matched"] if values[f"{label}.matched"] else None,
                "mean_mask_iou": values[f"{label}.mask_iou"] / values[f"{label}.mask_matched"] if values[f"{label}.mask_matched"] else None,
            }
        report["systems"].setdefault(system, {})[group] = result
    return report


def score_ocr(run_root: Path) -> dict[str, Any]:
    truth = read_json(OCR_GT)["cases"]
    case_metadata = {case["id"]: case for case in read_json(DATASET)["ocr_cases"]}
    systems: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for result_path in run_root.glob("ocr/*/*/result.json"):
        system = result_path.parent.parent.name
        case_id = result_path.parent.name
        expected = normalized_text(truth[case_id]["text"])
        actual = normalized_text(read_json(result_path)["text"])
        distance = edit_distance(expected, actual)
        item = {"case": case_id, "expected": expected, "actual": actual, "distance": distance, "characters": len(expected), "exact": expected == actual}
        systems[(system, "overall")].append(item)
        systems[(system, case_metadata[case_id]["language"])].append(item)
    report: dict[str, Any] = {"schema_version": 1, "systems": {}}
    for (system, group), cases in systems.items():
        characters = sum(case["characters"] for case in cases)
        report["systems"].setdefault(system, {})[group] = {
            "cases": cases,
            "character_error_rate": sum(case["distance"] for case in cases) / characters if characters else None,
            "exact_match_rate": sum(case["exact"] for case in cases) / len(cases) if cases else None,
        }
    return report


def score_inpainting(run_root: Path, ratings_path: Path | None = None) -> dict[str, Any]:
    dataset = {case["id"]: case for case in read_json(DATASET)["inpainting_cases"]}
    systems: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for output in run_root.glob("inpainting/*/*/output.png"):
        system = output.parent.parent.name
        case_id = output.parent.name
        case = dataset[case_id]
        item: dict[str, Any] = {"case": case_id}
        if case.get("reference"):
            reference = Image.open(BASE_DIR / case["reference"]).convert("RGB")
            actual = Image.open(output).convert("RGB")
            mask = Image.open(BASE_DIR / case["mask"]).convert("L").point(lambda value: 255 if value >= 127 else 0)
            difference = ImageChops.difference(reference, actual)
            black = Image.new("RGB", reference.size, 0)
            masked = Image.composite(difference, black, mask)
            changed_pixels = max(1, sum(1 for value in mask.getdata() if value))
            item["masked_mean_absolute_error"] = sum(ImageStat.Stat(masked).sum) / (changed_pixels * 3)
        systems[system].append(item)
    report: dict[str, Any] = {
        "schema_version": 1,
        "note": "Real-page cases require blind review; automatic error is reported only when a clean reference exists.",
        "systems": dict(systems),
    }
    if ratings_path:
        ratings = read_json(ratings_path)
        mapping_path = BASE_DIR / "reviews" / "private" / f"{run_root.name}.mapping.json"
        mapping = read_json(mapping_path)["items"]
        dimensions = ("removal_completeness", "artwork_preservation", "boundary_preservation", "texture_consistency")
        aggregates: dict[str, dict[str, Any]] = defaultdict(lambda: {dimension: [] for dimension in dimensions} | {"acceptable": []})
        for item_id, rating in ratings["items"].items():
            if item_id not in mapping:
                raise SystemExit(f"rating has no private mapping: {item_id}")
            system = mapping[item_id]["system"]
            for dimension in dimensions:
                value = rating.get(dimension)
                if not isinstance(value, int) or not 1 <= value <= 5:
                    raise SystemExit(f"{item_id} has invalid or missing {dimension}")
                aggregates[system][dimension].append(value)
            if not isinstance(rating.get("acceptable"), bool):
                raise SystemExit(f"{item_id} has invalid or missing acceptable")
            aggregates[system]["acceptable"].append(rating["acceptable"])
        report["blind_review"] = {
            "reviewer": ratings.get("reviewer", ""),
            "systems": {
                system: {
                    **{dimension: sum(values[dimension]) / len(values[dimension]) for dimension in dimensions},
                    "acceptance_rate": sum(values["acceptable"]) / len(values["acceptable"]),
                    "ratings": len(values["acceptable"]),
                }
                for system, values in aggregates.items()
            },
        }
    return report


def score(args: argparse.Namespace) -> None:
    run_root = BASE_DIR / "outputs" / safe_id(args.run_id)
    if not run_root.exists():
        raise SystemExit(f"run does not exist: {run_root}")
    if args.stage == "detection":
        report = score_detection(run_root)
    elif args.stage == "ocr":
        report = score_ocr(run_root)
    else:
        report = score_inpainting(run_root, args.ratings)
    path = run_root / f"{args.stage}_score.json"
    write_json(path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Saved {path}")


def blind(args: argparse.Namespace) -> None:
    run_root = BASE_DIR / "outputs" / safe_id(args.run_id)
    dataset = {case["id"]: case for case in read_json(DATASET)["inpainting_cases"]}
    candidates: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for output in run_root.glob("inpainting/*/*/output.png"):
        candidates[output.parent.name].append((output.parent.parent.name, output))
    destination = BASE_DIR / "reviews" / "blind" / safe_id(args.run_id)
    private = BASE_DIR / "reviews" / "private" / f"{safe_id(args.run_id)}.mapping.json"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    rng = random.Random(load_config()["inpainting"]["seed"])
    mapping: dict[str, Any] = {"schema_version": 1, "items": {}}
    ratings: dict[str, Any] = {"schema_version": 1, "reviewer": "", "items": {}}
    rows = []
    for case_id, values in sorted(candidates.items()):
        rng.shuffle(values)
        source = BASE_DIR / dataset[case_id]["input"]
        mask = BASE_DIR / dataset[case_id]["mask"]
        source_name, mask_name = f"{case_id}__source.png", f"{case_id}__mask.png"
        Image.open(source).save(destination / source_name)
        Image.open(mask).save(destination / mask_name)
        cells = []
        for index, (system, output) in enumerate(values, start=1):
            item_id = f"{case_id}__candidate_{index:02d}"
            filename = f"{item_id}.png"
            shutil.copy2(output, destination / filename)
            mapping["items"][item_id] = {"case": case_id, "system": system}
            ratings["items"][item_id] = {"removal_completeness": None, "artwork_preservation": None, "boundary_preservation": None, "texture_consistency": None, "acceptable": None, "notes": ""}
            cells.append(f'<figure><img src="{html.escape(filename)}"><figcaption>{html.escape(item_id)}</figcaption></figure>')
        rows.append(f'<section><h2>{html.escape(case_id)}</h2><div class="reference"><figure><img src="{source_name}"><figcaption>Source</figcaption></figure><figure><img src="{mask_name}"><figcaption>Mask</figcaption></figure></div><div class="candidates">{"".join(cells)}</div></section>')
    document = f'''<!doctype html><meta charset="utf-8"><title>Blind inpainting review</title><style>body{{font-family:sans-serif;background:#181818;color:#eee}}section{{border-top:1px solid #555;padding:1rem}}.reference,.candidates{{display:flex;gap:1rem;flex-wrap:wrap}}figure{{margin:0}}img{{max-width:360px;max-height:500px;object-fit:contain;background:#333}}figcaption{{text-align:center}}</style><h1>Blind inpainting review</h1><p>Score each candidate from 1 to 5 for removal completeness, artwork preservation, boundary preservation, and texture consistency. Mark whether it is acceptable for reading.</p>{''.join(rows)}'''
    (destination / "index.html").write_text(document, encoding="utf-8")
    write_json(destination / "ratings.json", ratings)
    write_json(private, mapping)
    print(f"Blind review: {destination / 'index.html'}")
    print(f"Enter ratings in: {destination / 'ratings.json'}")
    print(f"Private system mapping: {private}")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("prepare", help="copy private assets without running models")
    command.add_argument("--seed-docs", action="store_true")
    command.add_argument("--add-pages", type=Path)
    command.add_argument("--kind", choices=("manga", "webtoon"), default="manga")
    command.set_defaults(function=prepare)

    command = commands.add_parser("add-ocr")
    command.add_argument("--id")
    command.add_argument("--image", type=Path, required=True)
    command.add_argument("--language", choices=("ja", "ko"), required=True)
    command.add_argument("--text", required=True)
    command.add_argument("--verified", action="store_true")
    command.set_defaults(function=add_ocr)

    command = commands.add_parser("add-inpainting")
    command.add_argument("--id")
    command.add_argument("--image", type=Path, required=True)
    command.add_argument("--mask", type=Path, required=True)
    command.add_argument("--reference", type=Path)
    command.add_argument("--kind", choices=("flat_balloon", "textured_balloon", "artwork", "sfx"), required=True)
    command.add_argument("--description", default="")
    command.set_defaults(function=add_inpainting)

    command = commands.add_parser("validate")
    command.add_argument("--require-labels", action="store_true")
    command.set_defaults(function=validate)

    command = commands.add_parser("build-commands")
    command.set_defaults(function=build_commands)

    command = commands.add_parser("run", help="run model inference; this is the only GPU-using command")
    command.add_argument("stage", choices=("detection", "ocr", "inpainting"))
    command.add_argument("--system", action="append", required=True, help="frank, koharu, frank:lama, koharu:lama, or koharu:<ocr-engine>")
    command.add_argument("--case", action="append")
    command.add_argument("--run-id")
    command.set_defaults(function=run)

    command = commands.add_parser("score")
    command.add_argument("stage", choices=("detection", "ocr", "inpainting"))
    command.add_argument("--run-id", required=True)
    command.add_argument("--ratings", type=Path, help="completed blind ratings for inpainting")
    command.set_defaults(function=score)

    command = commands.add_parser("blind")
    command.add_argument("--run-id", required=True)
    command.set_defaults(function=blind)
    return parser


if __name__ == "__main__":
    arguments = make_parser().parse_args()
    arguments.function(arguments)
