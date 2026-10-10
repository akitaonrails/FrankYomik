"""Unit tests for webtoon inpainter mask building and integration.

Tests mask-building logic without GPU. Backend inference is mocked.
"""

import hashlib
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

import webtoon.inpainter as inpainter_module
from webtoon.bubble_detector import WebtoonBubble
from webtoon.inpainter import (
    CleanupPolicy,
    LamaBackend,
    SupervisedInpaintError,
    UnexpectedInpaintError,
    _PersistentSupervisor,
    build_inpaint_mask,
    cleanup_bubble,
    inpaint_bubble,
    supervised_inpaint_tiles,
)
from webtoon.ocr import TextDetection


def _make_det(x1, y1, x2, y2, text="테스트", conf=0.9):
    """Helper to create a TextDetection."""
    return TextDetection(
        bbox_poly=[[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
        text=text,
        confidence=conf,
        bbox_rect=(x1, y1, x2, y2),
    )


def test_lama_backend_verifies_downloaded_weights_before_load(tmp_path):
    model = tmp_path / "big-lama.pt"
    model.write_bytes(b"pinned weights")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()

    with (
        patch("simple_lama_inpainting.utils.download_model", return_value=str(model)),
        patch("simple_lama_inpainting.SimpleLama") as simple_lama,
        patch(
            "webtoon.inpainter.stage_identity",
            return_value={"model": {"weights_revision": f"sha256:{digest}"}},
        ),
    ):
        assert LamaBackend().load() is True

    simple_lama.assert_called_once_with()


def test_lama_backend_rejects_unpinned_weights_before_load(tmp_path):
    model = tmp_path / "big-lama.pt"
    model.write_bytes(b"different weights")

    with (
        patch("simple_lama_inpainting.utils.download_model", return_value=str(model)),
        patch("simple_lama_inpainting.SimpleLama") as simple_lama,
        patch(
            "webtoon.inpainter.stage_identity",
            return_value={"model": {"weights_revision": "a" * 64}},
        ),
        pytest.raises(RuntimeError, match="digest mismatch"),
    ):
        LamaBackend().load()

    simple_lama.assert_not_called()


def _make_bubble(detections, bbox=None, bg_color=(255, 255, 255),
                 mask=None, has_boundary=False):
    """Helper to create a WebtoonBubble."""
    if bbox is None:
        x1 = min(d.bbox_rect[0] for d in detections)
        y1 = min(d.bbox_rect[1] for d in detections)
        x2 = max(d.bbox_rect[2] for d in detections)
        y2 = max(d.bbox_rect[3] for d in detections)
        bbox = (x1, y1, x2, y2)
    return WebtoonBubble(
        bbox=bbox,
        text_regions=detections,
        combined_text=" ".join(d.text for d in detections),
        has_bubble_boundary=has_boundary,
        bg_color=bg_color,
        bubble_mask=mask,
    )


def _make_circular_mask(w, h, cx, cy, r):
    """Create a circular mask of given size."""
    mask = np.zeros((h, w), dtype=np.uint8)
    Y, X = np.ogrid[:h, :w]
    dist = (X - cx) ** 2 + (Y - cy) ** 2
    mask[dist <= r ** 2] = 255
    return mask


class _TestBackend:
    def __init__(self, *, load_delay=0.0, inference_delay=0.0,
                 failure=None, wrong_size=False, fail_after=None):
        self.load_delay = load_delay
        self.inference_delay = inference_delay
        self.failure = failure
        self.wrong_size = wrong_size
        self.fail_after = fail_after
        self.calls = 0

    def load(self):
        time.sleep(self.load_delay)
        return True

    def inpaint(self, image, _mask):
        time.sleep(self.inference_delay)
        self.calls += 1
        if self.failure is not None or (
            self.fail_after is not None and self.calls > self.fail_after
        ):
            error = self.failure or ValueError("failed")
            raise error
        size = (1, 1) if self.wrong_size else image.size
        return Image.new("RGB", size, "red")

    def unload(self):
        return None


class _CountingBackend(_TestBackend):
    def __init__(self, marker):
        super().__init__()
        self.marker = marker

    def load(self):
        with open(self.marker, "ab") as marker:
            marker.write(b"1")
        return True


def _short_policy():
    return CleanupPolicy(
        max_wall_seconds=0.08,
        min_host_available_mb=0,
        min_gpu_available_mb=0,
        uniform_stddev=0,
        uniform_range=0,
    )


# --- TestBuildInpaintMask ---


class TestBuildInpaintMask:
    """Tests for build_inpaint_mask()."""

    def test_uses_text_rects_without_bubble_mask(self):
        """No bubble_mask → uses text rects directly for inpainting."""
        det = _make_det(50, 50, 150, 80)
        bubble = _make_bubble([det], mask=None)
        result = build_inpaint_mask(bubble, (200, 200), text_pad=5,
                                     text_dilate=0)
        assert result is not None
        result_arr = np.array(result)
        # Text rect area (padded by 5) should be white
        assert np.any(result_arr[45:85, 45:155] > 0)
        # Far corners should be black (no bubble mask to extend into)
        assert result_arr[0, 0] == 0

    def test_returns_none_without_text_regions(self):
        """Empty text_regions → returns None."""
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        bubble = _make_bubble([], bbox=(20, 20, 180, 180), mask=mask)
        result = build_inpaint_mask(bubble, (200, 200))
        assert result is None

    def test_mask_intersects_bubble_and_text(self):
        """Result mask is intersection of bubble mask and text area."""
        # 200x200 image, bubble circle centered at (100, 100), radius 60
        mask = _make_circular_mask(200, 200, 100, 100, 60)
        det = _make_det(80, 90, 120, 110)  # text inside the circle
        bubble = _make_bubble([det], bbox=(40, 40, 160, 160),
                              mask=mask, has_boundary=True)

        result = build_inpaint_mask(bubble, (200, 200), erode_px=0,
                                    text_pad=5, text_dilate=0)
        assert result is not None
        result_arr = np.array(result)

        # Mask should have white pixels only where both bubble mask and
        # text area overlap
        assert np.any(result_arr > 0), "Mask should have white pixels"

        # Nothing outside the original bubble mask
        assert np.all(result_arr[mask == 0] == 0), \
            "Mask should not extend outside bubble"

    def test_erode_shrinks_mask(self):
        """Erosion shrinks the bubble mask via MinFilter.

        Verifies that the eroded mask step in build_inpaint_mask
        actually reduces pixel count when text fills most of the bubble.
        Uses high coverage (>85% for both) by keeping text small and
        centered well inside the bubble.
        """
        # 300x300 circular mask, large radius — text+pad stays inside
        mask = _make_circular_mask(300, 300, 150, 150, 140)
        det = _make_det(50, 50, 250, 250)
        bubble = _make_bubble([det], bbox=(10, 10, 290, 290),
                              mask=mask, has_boundary=True)

        result_no_erode = build_inpaint_mask(
            bubble, (300, 300), erode_px=0, text_pad=10, text_dilate=0)
        result_eroded = build_inpaint_mask(
            bubble, (300, 300), erode_px=5, text_pad=10, text_dilate=0)

        assert result_no_erode is not None
        assert result_eroded is not None

        count_no_erode = np.count_nonzero(np.array(result_no_erode))
        count_eroded = np.count_nonzero(np.array(result_eroded))
        assert count_eroded < count_no_erode, \
            "Erosion should reduce mask area"

    def test_text_padding_expands_mask(self):
        """text_pad expands the area around text detections."""
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        det = _make_det(80, 80, 120, 120)
        bubble = _make_bubble([det], bbox=(0, 0, 200, 200),
                              mask=mask, has_boundary=True)

        result_small = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=2, text_dilate=0)
        result_large = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=20, text_dilate=0)

        assert result_small is not None
        assert result_large is not None

        count_small = np.count_nonzero(np.array(result_small))
        count_large = np.count_nonzero(np.array(result_large))
        assert count_large > count_small, \
            "Larger text_pad should produce larger mask"

    def test_text_dilate_catches_antialiasing(self):
        """text_dilate expands the final mask to catch glyph edges."""
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        det = _make_det(80, 80, 120, 120)
        bubble = _make_bubble([det], bbox=(0, 0, 200, 200),
                              mask=mask, has_boundary=True)

        result_no_dilate = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=5, text_dilate=0)
        result_dilated = build_inpaint_mask(
            bubble, (200, 200), erode_px=0, text_pad=5, text_dilate=3)

        assert result_no_dilate is not None
        assert result_dilated is not None

        count_no_dilate = np.count_nonzero(np.array(result_no_dilate))
        count_dilated = np.count_nonzero(np.array(result_dilated))
        assert count_dilated > count_no_dilate, \
            "Dilation should expand mask area"

    def test_multiple_detections_union(self):
        """Multiple text detections create a union in the text mask."""
        mask = np.ones((300, 300), dtype=np.uint8) * 255
        det1 = _make_det(50, 50, 100, 70)
        det2 = _make_det(50, 120, 100, 140)  # second line of text
        bubble = _make_bubble([det1, det2], bbox=(20, 20, 280, 280),
                              mask=mask, has_boundary=True)

        result = build_inpaint_mask(bubble, (300, 300), erode_px=0,
                                    text_pad=5, text_dilate=0)
        assert result is not None
        result_arr = np.array(result)

        # Both text areas should have white pixels
        assert np.any(result_arr[45:75, 45:105] > 0), \
            "First text area should be in mask"
        assert np.any(result_arr[115:145, 45:105] > 0), \
            "Second text area should be in mask"

    def test_text_outside_bubble_is_clipped(self):
        """Retained semantic geometry is an explicit cleanup boundary."""
        # Small circular mask
        mask = _make_circular_mask(200, 200, 100, 100, 30)
        # Text far outside the circle — coverage < 85%
        det = _make_det(5, 5, 25, 15)
        bubble = _make_bubble([det], bbox=(70, 70, 130, 130),
                              mask=mask, has_boundary=True)

        result = build_inpaint_mask(bubble, (200, 200), erode_px=0,
                                    text_pad=2, text_dilate=0)
        assert result is None


# --- TestInpaintBubbleIntegration ---


class TestInpaintBubbleIntegration:
    """Integration tests with mocked backend (no GPU needed)."""

    def test_returns_false_when_disabled(self):
        """When INPAINT_ENABLED is False, returns False."""
        img = Image.new("RGB", (200, 200), (255, 255, 255))
        mask = np.ones((200, 200), dtype=np.uint8) * 255
        det = _make_det(50, 50, 150, 80)
        bubble = _make_bubble([det], mask=mask, has_boundary=True)

        with patch("webtoon.inpainter.INPAINT_ENABLED", False):
            result = inpaint_bubble(img, bubble)
        assert result is False

    def test_uniform_balloon_does_not_load_backend(self):
        img = Image.new("RGB", (100, 100), (245, 246, 247))
        semantic = np.full((100, 100), 255, dtype=np.uint8)
        bubble = _make_bubble(
            [_make_det(20, 20, 60, 50)], mask=semantic,
            has_boundary=True,
        )
        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter.supervised_inpaint_tiles") as run_backend,
        ):
            outcome = cleanup_bubble(img, bubble)
        assert outcome.method == "uniform_fill"
        assert outcome.degraded is False
        run_backend.assert_not_called()

    def test_resource_guard_runs_before_backend_load(self):
        img = Image.new("RGB", (100, 100), "white")
        bubble = _make_bubble([_make_det(20, 20, 60, 50)], mask=None)
        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter.host_memory_available", return_value=False),
            patch("webtoon.inpainter.supervised_inpaint_tiles") as run_backend,
        ):
            outcome = cleanup_bubble(img, bubble)
        assert outcome.degraded is True
        assert outcome.reason == "host_memory"
        run_backend.assert_not_called()

    def test_second_tile_failure_rolls_back_partial_model_output(self):
        img = Image.new("RGB", (120, 60), (100, 100, 100))
        target = img.copy()
        bubble = _make_bubble([
            _make_det(10, 10, 20, 20),
            _make_det(90, 35, 100, 45),
        ], bbox=(0, 0, 120, 60), mask=None)
        backend = _TestBackend(fail_after=1)
        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter.gpu_memory_available", return_value=True),
        ):
            outcome = cleanup_bubble(img, bubble, backend=backend,
                                     target_img=target)
        assert outcome.method == "uniform_fill"
        assert outcome.degraded is True
        assert np.asarray(target)[15, 15, 0] == 255
        assert np.asarray(target)[40, 95, 0] == 255
        assert np.asarray(target)[0, 55, 0] == 100

    def test_enabled_backend_failure_degrades_without_failing_page(self):
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        det = _make_det(20, 20, 60, 50)
        bubble = _make_bubble([det], mask=None)
        backend = _TestBackend(failure=ValueError("bad model"))

        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter.gpu_memory_available", return_value=True),
        ):
            outcome = cleanup_bubble(img, bubble, backend=backend)
        assert outcome.applied is True
        assert outcome.degraded is True
        assert outcome.method == "uniform_fill"
        assert outcome.reason == "backend_failure"

    @pytest.mark.parametrize("delay_field", ["load_delay", "inference_delay"])
    def test_supervisor_bounds_slow_load_and_inference(self, delay_field):
        img = Image.new("RGB", (100, 100), (100, 100, 100))
        bubble = _make_bubble([_make_det(20, 20, 60, 50)], mask=None)
        backend = _TestBackend(**{delay_field: 0.5})
        poisoned = MagicMock()
        started = time.monotonic()
        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter._policy", return_value=_short_policy()),
            patch.object(inpainter_module, "_supervisor", poisoned),
        ):
            outcome = cleanup_bubble(img, bubble, backend=backend)
            assert inpainter_module._supervisor is None
        assert time.monotonic() - started < 0.5
        assert outcome.reason == "wall_time"
        assert outcome.degraded is True
        poisoned.close.assert_called_once()

    def test_supervisor_bounds_lock_wait(self):
        img = Image.new("RGB", (100, 100), (100, 100, 100))
        bubble = _make_bubble([_make_det(20, 20, 60, 50)], mask=None)
        inpainter_module._infer_lock.acquire()
        started = time.monotonic()
        try:
            with (
                patch("webtoon.inpainter.INPAINT_ENABLED", True),
                patch("webtoon.inpainter._policy", return_value=_short_policy()),
            ):
                outcome = cleanup_bubble(img, bubble, backend=_TestBackend())
        finally:
            inpainter_module._infer_lock.release()
        assert time.monotonic() - started < 0.5
        assert outcome.reason == "lock_timeout"

    @pytest.mark.parametrize("remaining", [-1.0, 0.001])
    def test_max_payload_expired_deadline_never_starts_transport(
        self, remaining,
    ):
        image = Image.new("RGB", (1024, 1024), "white")
        mask = Image.new("L", (1024, 1024), "white")
        requests = [(image, mask, index) for index in range(16)]
        started = time.monotonic()
        with (
            patch("webtoon.inpainter._stage_artifacts") as stage,
            pytest.raises(SupervisedInpaintError, match="wall_time"),
        ):
            supervised_inpaint_tiles(
                requests, end=time.monotonic() + remaining,
                backend=_TestBackend(),
            )
        assert time.monotonic() - started < 0.1
        stage.assert_not_called()

    def test_max_payload_uses_only_capped_control_datagrams(self):
        image = Image.new("RGB", (1024, 1024), "white")
        mask = Image.new("L", (1024, 1024), "white")
        requests = [(image, mask, index) for index in range(16)]
        supervisor = _PersistentSupervisor(_TestBackend, "fork")
        sent_sizes = []
        original_send = inpainter_module._send_control

        def recording_send(connection, value, end):
            sent_sizes.append(len(inpainter_module.json.dumps(value).encode()))
            return original_send(connection, value, end)

        try:
            with (
                patch.object(inpainter_module, "_supervisor", supervisor),
                patch("webtoon.inpainter._send_control", recording_send),
            ):
                results = supervised_inpaint_tiles(
                    requests, end=time.monotonic() + 10,
                )
            assert len(results) == 16
            assert all(result.size == (1024, 1024) for result in results)
            assert sent_sizes
            assert max(sent_sizes) <= inpainter_module._MAX_CONTROL_BYTES
        finally:
            supervisor.close()

    def test_control_send_and_receive_are_deadline_bounded(self):
        sender, receiver = inpainter_module._control_pair()
        try:
            receiver.setblocking(False)
            while True:
                try:
                    sender.send(b"x" * inpainter_module._MAX_CONTROL_BYTES)
                except BlockingIOError:
                    break
            started = time.monotonic()
            with pytest.raises(SupervisedInpaintError, match="wall_time"):
                inpainter_module._send_control(
                    sender, ("inpaint", []), time.monotonic() + 0.001,
                )
            assert time.monotonic() - started < 0.1

            while True:
                try:
                    receiver.recv(inpainter_module._MAX_CONTROL_BYTES)
                except BlockingIOError:
                    break
            started = time.monotonic()
            with pytest.raises(SupervisedInpaintError, match="wall_time"):
                inpainter_module._recv_control(
                    receiver, time.monotonic() + 0.001,
                )
            assert time.monotonic() - started < 0.1
        finally:
            sender.close()
            receiver.close()

    def test_persistent_supervisor_reuses_warmed_process(self, tmp_path):
        request = [(
            Image.new("RGB", (10, 10), "white"),
            Image.new("L", (10, 10), "white"), 0,
        )]
        marker = tmp_path / "loads"
        supervisor = _PersistentSupervisor(
            lambda: _CountingBackend(marker), "fork",
        )
        try:
            with patch.object(inpainter_module, "_supervisor", supervisor):
                first = supervised_inpaint_tiles(
                    request, end=time.monotonic() + 1,
                )
                first_pid = supervisor.pid
                second = supervised_inpaint_tiles(
                    request, end=time.monotonic() + 1,
                )
                assert supervisor.pid == first_pid
            assert first[0].size == second[0].size == (10, 10)
            assert marker.read_bytes() == b"1"
        finally:
            supervisor.close()

    def test_failed_persistent_supervisor_is_reset(self):
        class FailBackend(_TestBackend):
            def inpaint(self, image, mask):
                raise MemoryError("OOM")

        request = [(
            Image.new("RGB", (10, 10), "white"),
            Image.new("L", (10, 10), "white"), 0,
        )]
        supervisor = _PersistentSupervisor(FailBackend, "fork")
        with patch.object(inpainter_module, "_supervisor", supervisor):
            with pytest.raises(SupervisedInpaintError, match="oom"):
                supervised_inpaint_tiles(request, end=time.monotonic() + 1)
            assert inpainter_module._supervisor is None
            assert not supervisor.alive()

    def test_failure_is_followed_by_fresh_successful_supervisor(self):
        class FailBackend(_TestBackend):
            def inpaint(self, image, mask):
                raise MemoryError("OOM")

        request = [(
            Image.new("RGB", (10, 10), "white"),
            Image.new("L", (10, 10), "white"), 0,
        )]
        supervisors = iter([
            _PersistentSupervisor(FailBackend, "fork"),
            _PersistentSupervisor(_TestBackend, "fork"),
        ])
        with (
            patch.object(inpainter_module, "_supervisor", None),
            patch.object(
                inpainter_module, "_supervisor_factory",
                side_effect=lambda: next(supervisors),
            ),
        ):
            with pytest.raises(SupervisedInpaintError, match="oom"):
                supervised_inpaint_tiles(request, end=time.monotonic() + 1)
            result = supervised_inpaint_tiles(
                request, end=time.monotonic() + 1,
            )
            assert result[0].size == (10, 10)
            inpainter_module._reset_backend()

    def test_oom_resets_backend_and_degrades_non_model_result(self):
        img = Image.new("RGB", (100, 100), (100, 100, 100))
        bubble = _make_bubble([_make_det(20, 20, 60, 50)], mask=None)
        backend = _TestBackend(failure=MemoryError("CUDA out of memory"))
        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter._policy", return_value=_short_policy()),
        ):
            outcome = cleanup_bubble(img, bubble, backend=backend)
        assert outcome.method == "uniform_fill"
        assert outcome.reason == "oom"
        assert outcome.degraded is True

    def test_wrong_size_output_rolls_back_without_lama_credit(self):
        img = Image.new("RGB", (100, 100), (100, 100, 100))
        target = img.copy()
        bubble = _make_bubble([_make_det(20, 20, 60, 50)], mask=None)
        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter._policy", return_value=_short_policy()),
        ):
            outcome = cleanup_bubble(
                img, bubble, backend=_TestBackend(wrong_size=True),
                target_img=target,
            )
        assert outcome.method == "uniform_fill"
        assert outcome.reason == "wrong_size"
        assert outcome.degraded is True
        assert np.asarray(target)[99, 99, 0] == 100

    def test_unexpected_adapter_error_fails_instead_of_degrading(self):
        img = Image.new("RGB", (100, 100), (100, 100, 100))
        bubble = _make_bubble([_make_det(20, 20, 60, 50)], mask=None)
        with (
            patch("webtoon.inpainter.INPAINT_ENABLED", True),
            patch("webtoon.inpainter._policy", return_value=_short_policy()),
            pytest.raises(UnexpectedInpaintError, match="TypeError"),
        ):
            cleanup_bubble(
                img, bubble,
                backend=_TestBackend(failure=TypeError("programming bug")),
            )

    def test_inpaints_without_bubble_mask(self):
        """Bubble without bubble_mask still inpaints using text rects."""
        img = Image.new("RGB", (200, 200), (255, 255, 255))
        target = img.copy()
        det = _make_det(50, 50, 150, 80)
        bubble = _make_bubble([det], mask=None)

        mock_backend = _TestBackend()

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            result = inpaint_bubble(img, bubble, backend=mock_backend,
                                    target_img=target)
        assert result is True

    def test_inpaint_modifies_only_masked_pixels(self):
        """Inpainting result should only modify pixels where mask is white."""
        # Create image with known pattern
        img = Image.new("RGB", (200, 200), (100, 150, 200))
        target = img.copy()
        mask = _make_circular_mask(200, 200, 100, 100, 50)
        det = _make_det(80, 80, 120, 120)
        bubble = _make_bubble([det], bbox=(50, 50, 150, 150),
                              mask=mask, has_boundary=True)

        # Mock backend that returns solid red
        mock_backend = _TestBackend()

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            result = inpaint_bubble(img, bubble, backend=mock_backend,
                                    target_img=target)

        assert result is True
        target_arr = np.array(target)
        orig_arr = np.array(img)

        # Pixels outside the mask should be unchanged
        # (check a corner that's definitely outside the circle)
        assert np.array_equal(target_arr[0, 0], orig_arr[0, 0]), \
            "Corner pixel should be unchanged"

    def test_accumulates_across_bubbles(self):
        """Multiple inpaint_bubble calls accumulate on the same target."""
        img = Image.new("RGB", (300, 300), (100, 100, 100))
        target = img.copy()

        # Two separate bubbles at different locations
        det1 = _make_det(45, 45, 75, 75)
        bubble1 = _make_bubble([det1], bbox=(30, 30, 90, 90), mask=None)

        det2 = _make_det(185, 185, 215, 215)
        bubble2 = _make_bubble([det2], bbox=(170, 170, 230, 230), mask=None)

        mock_backend = _TestBackend()

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            inpaint_bubble(img, bubble1, backend=mock_backend,
                           target_img=target)
            inpaint_bubble(img, bubble2, backend=mock_backend,
                           target_img=target)

        target_arr = np.array(target)
        # Both regions should have red pixels (from mock backend)
        assert target_arr[60, 60, 0] == 255, "First bubble region should be red"
        assert target_arr[200, 200, 0] == 255, "Second bubble region should be red"

    def test_handles_edge_bubble_bbox(self):
        """Bubble at image edge should not crash (clamp to bounds)."""
        img = Image.new("RGB", (100, 100), (200, 200, 200))
        target = img.copy()
        mask = np.ones((100, 100), dtype=np.uint8) * 255
        # Bubble touching right and bottom edges
        det = _make_det(70, 70, 98, 98)
        bubble = _make_bubble([det], bbox=(60, 60, 100, 100),
                              mask=mask, has_boundary=True)

        mock_backend = _TestBackend()

        with patch("webtoon.inpainter.INPAINT_ENABLED", True):
            # Should not raise
            result = inpaint_bubble(img, bubble, backend=mock_backend,
                                    target_img=target)
        assert result is True
