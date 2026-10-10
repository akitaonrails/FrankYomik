package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

var errInvalidMetadata = errors.New("invalid metadata")

var objectMaskReferenceRe = regexp.MustCompile(`^objects/([a-f0-9]{64})$`)
var legacyMaskReferenceRe = regexp.MustCompile(`^derived://source/[A-Za-z0-9._:-]+$`)

func invalidMetadata(format string, args ...interface{}) error {
	return fmt.Errorf("%w: %s", errInvalidMetadata, fmt.Sprintf(format, args...))
}

func omitNullFields(value map[string]interface{}, fields ...string) {
	for _, field := range fields {
		if item, exists := value[field]; exists && item == nil {
			delete(value, field)
		}
	}
}

func setDefaultForMissingOrNull(value map[string]interface{}, field string, defaultValue interface{}) {
	if item, exists := value[field]; !exists || item == nil {
		value[field] = defaultValue
	}
}

func validatePageDocument(payload map[string]interface{}, expectedPipeline, expectedRevision, expectedSourceHash string) error {
	if err := rejectUnknownFields(payload, "page document",
		"schema_version", "page_id", "pipeline", "pipeline_revision",
		"source_hash", "image", "cleanup_base", "regions", "stage_provenance"); err != nil {
		return err
	}
	schema, err := requireJSONInt(payload["schema_version"], "schema version")
	if err != nil || schema != 2 {
		if err != nil {
			return err
		}
		return invalidMetadata("unsupported page document schema: %d", schema)
	}
	if _, err := requireNonEmptyJSONString(payload["page_id"], "page id"); err != nil {
		return err
	}
	pipeline, err := requireNonEmptyJSONString(payload["pipeline"], "pipeline")
	if err != nil {
		return err
	}
	if !validPipelines[pipeline] || pipeline != expectedPipeline {
		return invalidMetadata("metadata pipeline mismatch")
	}
	revision, err := requireNonEmptyJSONString(payload["pipeline_revision"], "pipeline revision")
	if err != nil {
		return err
	}
	if !pipelineRevisionRe.MatchString(revision) || revision != expectedRevision {
		return invalidMetadata("metadata pipeline revision mismatch")
	}
	sourceHash, err := requireNonEmptyJSONString(payload["source_hash"], "source hash")
	if err != nil {
		return err
	}
	if !isValidSHA256Hex(sourceHash) || sourceHash != expectedSourceHash {
		return invalidMetadata("metadata source hash mismatch")
	}

	image, err := requireJSONObject(payload["image"], "image")
	if err != nil {
		return err
	}
	if err := rejectUnknownFields(image, "image", "width", "height"); err != nil {
		return err
	}
	width, err := requirePositiveJSONInt(image["width"], "image width")
	if err != nil {
		return err
	}
	height, err := requirePositiveJSONInt(image["height"], "image height")
	if err != nil {
		return err
	}
	if raw, ok := payload["cleanup_base"]; ok && raw != nil {
		artifact, err := requireJSONObject(raw, "cleanup base")
		if err != nil {
			return err
		}
		if err := validateImageArtifact(artifact, width, height); err != nil {
			return err
		}
	}
	setDefaultForMissingOrNull(payload, "regions", []interface{}{})
	regions, err := requireJSONArray(payload["regions"], "regions")
	if err != nil {
		return err
	}
	setDefaultForMissingOrNull(payload, "stage_provenance", map[string]interface{}{})
	provenance, err := requireJSONObject(payload["stage_provenance"], "stage provenance")
	if err != nil {
		return err
	}
	for name, raw := range provenance {
		if strings.TrimSpace(name) == "" {
			return invalidMetadata("stage name must not be empty")
		}
		item, err := requireJSONObject(raw, "stage provenance item")
		if err != nil {
			return err
		}
		if err := validateProvenance(item); err != nil {
			return err
		}
	}

	ids := make(map[string]bool, len(regions))
	orders := make(map[int64]bool, len(regions))
	parents := make(map[string]string, len(regions))
	for _, raw := range regions {
		region, err := requireJSONObject(raw, "region")
		if err != nil {
			return err
		}
		id, order, parent, err := validatePageRegion(region, width, height)
		if err != nil {
			return err
		}
		if ids[id] {
			return invalidMetadata("region ids must be unique")
		}
		if orders[order] {
			return invalidMetadata("region reading orders must be unique")
		}
		ids[id] = true
		orders[order] = true
		parents[id] = parent
	}
	for id, parent := range parents {
		if parent != "" && !ids[parent] {
			return invalidMetadata("region %q has an unknown parent", id)
		}
	}
	for id := range parents {
		seen := map[string]bool{}
		for current := id; current != ""; current = parents[current] {
			if seen[current] {
				return invalidMetadata("region parent graph contains a cycle")
			}
			seen[current] = true
		}
	}
	return nil
}

func validatePageRegion(region map[string]interface{}, pageWidth, pageHeight int64) (string, int64, string, error) {
	if err := rejectUnknownFields(region, "region",
		"id", "kind", "bbox", "reading_order", "confidence", "polygon", "mask",
		"parent_id", "detector_label", "detection_provenance", "ocr",
		"translation", "transform", "cleanup", "render", "source_font_size",
		"user"); err != nil {
		return "", 0, "", err
	}
	omitNullFields(region, "confidence", "polygon", "mask", "parent_id",
		"detector_label", "detection_provenance", "ocr", "translation",
		"transform", "cleanup", "render", "source_font_size")
	id, err := requireNonEmptyJSONString(region["id"], "region id")
	if err != nil {
		return "", 0, "", err
	}
	if _, err := requireNonEmptyJSONString(region["kind"], "region kind"); err != nil {
		return "", 0, "", err
	}
	regionBox, err := validateBoundingBox(region["bbox"], "bounding box", pageWidth, pageHeight)
	if err != nil {
		return "", 0, "", err
	}
	order, err := requireJSONInt(region["reading_order"], "reading order")
	if err != nil || order < 0 {
		if err != nil {
			return "", 0, "", err
		}
		return "", 0, "", invalidMetadata("reading order must be non-negative")
	}
	if err := validateOptionalConfidence(region, "confidence", "region confidence"); err != nil {
		return "", 0, "", err
	}
	if raw, ok := region["polygon"]; ok && raw != nil {
		points, err := requireJSONArray(raw, "polygon")
		if err != nil {
			return "", 0, "", err
		}
		if len(points) < 3 {
			return "", 0, "", invalidMetadata("polygon must contain at least three points")
		}
		for _, rawPoint := range points {
			point, err := requireJSONArray(rawPoint, "polygon point")
			if err != nil || len(point) != 2 {
				return "", 0, "", invalidMetadata("polygon point must contain two coordinates")
			}
			x, err := requireJSONInt(point[0], "polygon x coordinate")
			if err != nil {
				return "", 0, "", err
			}
			y, err := requireJSONInt(point[1], "polygon y coordinate")
			if err != nil {
				return "", 0, "", err
			}
			if x < 0 || y < 0 || x >= pageWidth || y >= pageHeight {
				return "", 0, "", invalidMetadata("region %q polygon lies outside the page", id)
			}
		}
	}
	if raw, ok := region["mask"]; ok && raw != nil {
		if err := validateMask(raw, pageWidth, pageHeight); err != nil {
			return "", 0, "", err
		}
	}
	parent, err := optionalJSONString(region["parent_id"], "parent id")
	if err != nil {
		return "", 0, "", err
	}
	if parent == id {
		return "", 0, "", invalidMetadata("a region cannot be its own parent")
	}
	if _, err := optionalJSONString(region["detector_label"], "detector label"); err != nil {
		return "", 0, "", err
	}
	if raw, ok := region["detection_provenance"]; ok && raw != nil {
		item, err := requireJSONObject(raw, "detection provenance")
		if err != nil {
			return "", 0, "", err
		}
		if err := validateProvenance(item); err != nil {
			return "", 0, "", err
		}
	}
	if raw, ok := region["ocr"]; ok && raw != nil {
		if err := validateOCR(raw); err != nil {
			return "", 0, "", err
		}
	}
	if raw, ok := region["translation"]; ok && raw != nil {
		if err := validateTranslation(raw); err != nil {
			return "", 0, "", err
		}
	}
	if raw, ok := region["transform"]; ok && raw != nil {
		item, err := requireJSONObject(raw, "transform")
		if err != nil {
			return "", 0, "", err
		}
		if err := rejectUnknownFields(item, "transform", "kind", "value", "provenance"); err != nil {
			return "", 0, "", err
		}
		if _, err := requireNonEmptyJSONString(item["kind"], "transform kind"); err != nil {
			return "", 0, "", err
		}
		if _, ok := item["value"]; !ok {
			item["value"] = nil
		}
		prov, err := requireJSONObject(item["provenance"], "transform provenance")
		if err != nil {
			return "", 0, "", err
		}
		if err := validateProvenance(prov); err != nil {
			return "", 0, "", err
		}
	}
	if raw, ok := region["cleanup"]; ok && raw != nil {
		if err := validateCleanup(raw, pageWidth, pageHeight); err != nil {
			return "", 0, "", err
		}
	}
	if raw, ok := region["render"]; ok && raw != nil {
		if err := validateRender(raw, pageWidth, pageHeight, regionBox); err != nil {
			return "", 0, "", err
		}
	}
	if raw, ok := region["source_font_size"]; ok && raw != nil {
		fontSize, err := requireJSONInt(raw, "source font size")
		if err != nil || fontSize <= 0 {
			if err != nil {
				return "", 0, "", err
			}
			return "", 0, "", invalidMetadata("source font size must be positive")
		}
	}
	setDefaultForMissingOrNull(region, "user", map[string]interface{}{"manual_translation": ""})
	if raw, ok := region["user"]; ok {
		user, err := requireJSONObject(raw, "user")
		if err != nil {
			return "", 0, "", err
		}
		if err := rejectUnknownFields(user, "user edit", "manual_translation"); err != nil {
			return "", 0, "", err
		}
		setDefaultForMissingOrNull(user, "manual_translation", "")
		if rawManual, ok := user["manual_translation"]; ok {
			if _, err := requireJSONString(rawManual, "manual translation"); err != nil {
				return "", 0, "", err
			}
		}
	}
	return id, order, parent, nil
}

func validateProvenance(value map[string]interface{}) error {
	if err := rejectUnknownFields(value, "provenance", "backend", "revision", "model", "config_hash"); err != nil {
		return err
	}
	omitNullFields(value, "model", "config_hash")
	if _, err := requireNonEmptyJSONString(value["backend"], "backend"); err != nil {
		return err
	}
	if _, err := requireNonEmptyJSONString(value["revision"], "revision"); err != nil {
		return err
	}
	for _, key := range []string{"model", "config_hash"} {
		if _, err := optionalJSONString(value[key], key); err != nil {
			return err
		}
	}
	return nil
}

func validateMask(raw interface{}, pageWidth, pageHeight int64) error {
	mask, err := requireJSONObject(raw, "mask")
	if err != nil {
		return err
	}
	omitNullFields(mask, "sha256")
	if err := rejectUnknownFields(mask, "mask", "reference", "encoding", "width", "height", "sha256"); err != nil {
		return err
	}
	reference, err := requireNonEmptyJSONString(mask["reference"], "mask reference")
	if err != nil {
		return err
	}
	encoding, err := requireNonEmptyJSONString(mask["encoding"], "mask encoding")
	if err != nil {
		return err
	}
	width, err := requirePositiveJSONInt(mask["width"], "mask width")
	if err != nil {
		return err
	}
	height, err := requirePositiveJSONInt(mask["height"], "mask height")
	if err != nil {
		return err
	}
	if width != pageWidth || height != pageHeight {
		return invalidMetadata("mask dimensions do not match the page")
	}
	objectMatch := objectMaskReferenceRe.FindStringSubmatch(reference)
	if objectMatch == nil && !legacyMaskReferenceRe.MatchString(reference) {
		return invalidMetadata("mask reference must be a safe object or legacy reference")
	}
	if rawHash, ok := mask["sha256"]; ok && rawHash != nil {
		hash, err := requireJSONString(rawHash, "mask sha256")
		if err != nil {
			return err
		}
		if !isValidSHA256Hex(hash) {
			return invalidMetadata("mask sha256 must be a 64-character hexadecimal digest")
		}
		if objectMatch != nil && objectMatch[1] != hash {
			return invalidMetadata("mask object reference must match its sha256")
		}
		if objectMatch != nil && encoding != "png-mask-l1" {
			return invalidMetadata("mask object encoding must be png-mask-l1")
		}
	} else if objectMatch != nil {
		return invalidMetadata("mask object reference requires sha256")
	}
	return nil
}

func validateImageArtifact(value map[string]interface{}, pageWidth, pageHeight int64) error {
	if err := rejectUnknownFields(value, "image artifact", "reference", "encoding", "width", "height", "mode", "sha256"); err != nil {
		return err
	}
	reference, err := requireNonEmptyJSONString(value["reference"], "image reference")
	if err != nil {
		return err
	}
	digest, err := requireNonEmptyJSONString(value["sha256"], "image sha256")
	if err != nil || !isValidSHA256Hex(digest) || reference != "objects/"+digest {
		return invalidMetadata("invalid content-addressed image artifact")
	}
	encoding, err := requireNonEmptyJSONString(value["encoding"], "image encoding")
	if err != nil || encoding != "png" {
		return invalidMetadata("cleanup base encoding must be png")
	}
	mode, err := requireNonEmptyJSONString(value["mode"], "image mode")
	if err != nil || mode != "RGB" {
		return invalidMetadata("cleanup base mode must be RGB")
	}
	width, err := requirePositiveJSONInt(value["width"], "image artifact width")
	if err != nil {
		return err
	}
	height, err := requirePositiveJSONInt(value["height"], "image artifact height")
	if err != nil {
		return err
	}
	if width != pageWidth || height != pageHeight {
		return invalidMetadata("cleanup base dimensions do not match page")
	}
	return nil
}

func validateOCR(raw interface{}) error {
	ocr, err := requireJSONObject(raw, "OCR result")
	if err != nil {
		return err
	}
	omitNullFields(ocr, "confidence", "routing_reason")
	if err := rejectUnknownFields(ocr, "OCR result", "text", "language", "valid", "confidence", "provenance", "alternatives", "routing_reason"); err != nil {
		return err
	}
	if _, err := requireJSONString(ocr["text"], "OCR text"); err != nil {
		return err
	}
	if _, err := requireNonEmptyJSONString(ocr["language"], "OCR language"); err != nil {
		return err
	}
	if _, ok := ocr["valid"].(bool); !ok {
		return invalidMetadata("OCR valid must be a boolean")
	}
	if err := validateOptionalConfidence(ocr, "confidence", "OCR confidence"); err != nil {
		return err
	}
	prov, err := requireJSONObject(ocr["provenance"], "OCR provenance")
	if err != nil {
		return err
	}
	if err := validateProvenance(prov); err != nil {
		return err
	}
	setDefaultForMissingOrNull(ocr, "alternatives", []interface{}{})
	if rawAlternatives, ok := ocr["alternatives"]; ok {
		alternatives, err := requireJSONArray(rawAlternatives, "OCR alternatives")
		if err != nil {
			return err
		}
		for _, rawAlternative := range alternatives {
			alternative, err := requireJSONObject(rawAlternative, "OCR alternative")
			if err != nil {
				return err
			}
			if err := rejectUnknownFields(alternative, "OCR alternative", "text", "confidence", "provenance"); err != nil {
				return err
			}
			omitNullFields(alternative, "confidence")
			if _, err := requireJSONString(alternative["text"], "OCR alternative text"); err != nil {
				return err
			}
			if err := validateOptionalConfidence(alternative, "confidence", "OCR alternative confidence"); err != nil {
				return err
			}
			prov, err := requireJSONObject(alternative["provenance"], "OCR alternative provenance")
			if err != nil {
				return err
			}
			if err := validateProvenance(prov); err != nil {
				return err
			}
		}
	}
	_, err = optionalJSONString(ocr["routing_reason"], "OCR routing reason")
	return err
}

func validateTranslation(raw interface{}) error {
	translation, err := requireJSONObject(raw, "translation")
	if err != nil {
		return err
	}
	omitNullFields(translation, "source_language")
	if err := rejectUnknownFields(translation, "translation", "text", "target_language", "source_language", "provenance"); err != nil {
		return err
	}
	if _, err := requireJSONString(translation["text"], "translation text"); err != nil {
		return err
	}
	if _, err := requireNonEmptyJSONString(translation["target_language"], "target language"); err != nil {
		return err
	}
	if _, err := optionalJSONString(translation["source_language"], "source language"); err != nil {
		return err
	}
	prov, err := requireJSONObject(translation["provenance"], "translation provenance")
	if err != nil {
		return err
	}
	return validateProvenance(prov)
}

func validateCleanup(raw interface{}, pageWidth, pageHeight int64) error {
	cleanup, err := requireJSONObject(raw, "cleanup")
	if err != nil {
		return err
	}
	omitNullFields(cleanup, "mask")
	if err := rejectUnknownFields(cleanup, "cleanup", "method", "provenance", "mask"); err != nil {
		return err
	}
	if _, err := requireNonEmptyJSONString(cleanup["method"], "cleanup method"); err != nil {
		return err
	}
	prov, err := requireJSONObject(cleanup["provenance"], "cleanup provenance")
	if err != nil {
		return err
	}
	if err := validateProvenance(prov); err != nil {
		return err
	}
	if mask, ok := cleanup["mask"]; ok && mask != nil {
		return validateMask(mask, pageWidth, pageHeight)
	}
	return nil
}

func validateRender(raw interface{}, pageWidth, pageHeight int64, regionBox [4]int64) error {
	render, err := requireJSONObject(raw, "render")
	if err != nil {
		return err
	}
	omitNullFields(render, "font_family", "font_size", "text_color", "orientation", "line_count", "fit_status", "source_boxes")
	if err := rejectUnknownFields(render, "render", "bbox", "provenance", "font_family", "font_size", "vertical", "text_color", "orientation", "source_boxes", "line_count", "fit_status", "ellipsized"); err != nil {
		return err
	}
	renderBox, err := validateBoundingBox(render["bbox"], "render box", pageWidth, pageHeight)
	if err != nil {
		return err
	}
	if renderBox[0] < regionBox[0] || renderBox[1] < regionBox[1] || renderBox[2] > regionBox[2] || renderBox[3] > regionBox[3] {
		return invalidMetadata("render box escapes region bounds")
	}
	prov, err := requireJSONObject(render["provenance"], "render provenance")
	if err != nil {
		return err
	}
	if err := validateProvenance(prov); err != nil {
		return err
	}
	for _, key := range []string{"font_family", "text_color", "orientation"} {
		if _, err := optionalJSONString(render[key], key); err != nil {
			return err
		}
	}
	rawSourceBoxes := render["source_boxes"]
	if rawSourceBoxes == nil {
		rawSourceBoxes = []interface{}{}
	}
	sourceBoxes, err := requireJSONArray(rawSourceBoxes, "source boxes")
	if err != nil {
		return err
	}
	for _, rawBox := range sourceBoxes {
		box, err := validateBoundingBox(rawBox, "source box", pageWidth, pageHeight)
		if err != nil {
			return err
		}
		if box[0] < regionBox[0] || box[1] < regionBox[1] || box[2] > regionBox[2] || box[3] > regionBox[3] {
			return invalidMetadata("source box escapes region bounds")
		}
	}
	if len(sourceBoxes) == 0 {
		delete(render, "source_boxes")
	}
	if rawSize, ok := render["font_size"]; ok && rawSize != nil {
		size, err := requireJSONInt(rawSize, "font size")
		if err != nil || size <= 0 {
			if err != nil {
				return err
			}
			return invalidMetadata("font size must be positive")
		}
	}
	if rawCount, ok := render["line_count"]; ok && rawCount != nil {
		count, err := requireJSONInt(rawCount, "render line count")
		if err != nil || count <= 0 {
			if err != nil {
				return err
			}
			return invalidMetadata("render line count must be positive")
		}
	}
	fitStatus, err := optionalJSONString(render["fit_status"], "render fit status")
	if err != nil {
		return err
	}
	if fitStatus != "" && fitStatus != "fit" && fitStatus != "ellipsized" && fitStatus != "overflow" {
		return invalidMetadata("invalid render fit status")
	}
	setDefaultForMissingOrNull(render, "ellipsized", false)
	ellipsized, ok := render["ellipsized"].(bool)
	if !ok {
		return invalidMetadata("render ellipsized must be a boolean")
	}
	if ellipsized && fitStatus != "ellipsized" {
		return invalidMetadata("ellipsized render must have ellipsized fit status")
	}
	setDefaultForMissingOrNull(render, "vertical", false)
	if rawVertical, ok := render["vertical"]; ok {
		if _, ok := rawVertical.(bool); !ok {
			return invalidMetadata("vertical must be a boolean")
		}
	}
	return nil
}

func validateBoundingBox(raw interface{}, name string, pageWidth, pageHeight int64) ([4]int64, error) {
	var result [4]int64
	values, err := requireJSONArray(raw, name)
	if err != nil {
		return result, err
	}
	if len(values) != 4 {
		return result, invalidMetadata("%s must contain four coordinates", name)
	}
	for index, rawValue := range values {
		result[index], err = requireJSONInt(rawValue, name+" coordinate")
		if err != nil {
			return result, err
		}
	}
	if result[0] < 0 || result[1] < 0 || result[2] <= result[0] || result[3] <= result[1] {
		return result, invalidMetadata("%s is invalid", name)
	}
	if result[2] > pageWidth || result[3] > pageHeight {
		return result, invalidMetadata("%s lies outside the page", name)
	}
	return result, nil
}

func validateOptionalConfidence(value map[string]interface{}, key, name string) error {
	raw, ok := value[key]
	if !ok || raw == nil {
		return nil
	}
	number, ok := raw.(json.Number)
	if !ok {
		return invalidMetadata("%s must be a number", name)
	}
	parsed, err := strconv.ParseFloat(number.String(), 64)
	if err != nil || math.IsInf(parsed, 0) || math.IsNaN(parsed) {
		return invalidMetadata("%s must be finite", name)
	}
	if parsed < 0 || parsed > 1 {
		return invalidMetadata("%s must be between 0 and 1", name)
	}
	return nil
}

func rejectUnknownFields(value map[string]interface{}, name string, allowed ...string) error {
	known := make(map[string]struct{}, len(allowed))
	for _, field := range allowed {
		known[field] = struct{}{}
	}
	for field := range value {
		if _, ok := known[field]; !ok {
			return invalidMetadata("%s contains unknown field: %s", name, field)
		}
	}
	return nil
}

func requireJSONObject(value interface{}, name string) (map[string]interface{}, error) {
	object, ok := value.(map[string]interface{})
	if !ok {
		return nil, invalidMetadata("%s must be an object", name)
	}
	return object, nil
}

func requireJSONArray(value interface{}, name string) ([]interface{}, error) {
	array, ok := value.([]interface{})
	if !ok {
		return nil, invalidMetadata("%s must be an array", name)
	}
	return array, nil
}

func requireJSONString(value interface{}, name string) (string, error) {
	text, ok := value.(string)
	if !ok {
		return "", invalidMetadata("%s must be a string", name)
	}
	return text, nil
}

func requireNonEmptyJSONString(value interface{}, name string) (string, error) {
	text, err := requireJSONString(value, name)
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(text) == "" {
		return "", invalidMetadata("%s must not be empty", name)
	}
	return text, nil
}

func optionalJSONString(value interface{}, name string) (string, error) {
	if value == nil {
		return "", nil
	}
	return requireJSONString(value, name)
}

func requireJSONInt(value interface{}, name string) (int64, error) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, invalidMetadata("%s must be an integer", name)
	}
	parsed, err := number.Int64()
	if err != nil {
		return 0, invalidMetadata("%s must be an integer", name)
	}
	return parsed, nil
}

func requirePositiveJSONInt(value interface{}, name string) (int64, error) {
	parsed, err := requireJSONInt(value, name)
	if err != nil {
		return 0, err
	}
	if parsed <= 0 {
		return 0, invalidMetadata("%s must be positive", name)
	}
	return parsed, nil
}
