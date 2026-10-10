"""AI inpainting for webtoon balloon text removal.

Replaces the solid-fill text clearing with AI-powered inpainting that
naturally reconstructs balloon interiors (semi-transparent panels, gradients,
brush textures) before rendering English text on top.

Follows the lazy-singleton + thread-lock pattern from webtoon/ocr.py.
"""

import atexit
import json
import logging
import multiprocessing
import os
import sys
import select
import socket
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from time import monotonic

import numpy as np
from PIL import Image

from pipeline.cleanup import (
    CleanupBudgetExceeded,
    CleanupPolicy,
    build_cleanup_mask,
    deadline,
    estimate_ocr_strokes,
    gpu_memory_available,
    host_memory_available,
    plan_cleanup,
    seed_inference,
)
from pipeline.model_verification import verify_file_sha256
from pipeline.revision import stage_identity

from .bubble_detector import WebtoonBubble
from .config import (
    INPAINT_CONTEXT_PAD,
    INPAINT_MAX_TILES,
    INPAINT_MAX_TILE_WIDTH,
    INPAINT_MAX_TILE_HEIGHT,
    INPAINT_MAX_TILE_PIXELS,
    INPAINT_MAX_PAGE_PIXELS,
    INPAINT_MAX_WALL_SECONDS,
    INPAINT_MIN_HOST_MB,
    INPAINT_MIN_GPU_MB,
    INPAINT_SEED,
    INPAINT_UNIFORM_STDDEV,
    INPAINT_UNIFORM_RANGE,
    INPAINT_ENABLED,
    INPAINT_ERODE_PX,
    INPAINT_TEXT_DILATE,
    INPAINT_TEXT_PAD,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend abstraction
# ---------------------------------------------------------------------------

class InpaintBackend(ABC):
    """Abstract base for inpainting model backends."""

    @abstractmethod
    def load(self) -> bool:
        """Load model weights. Return True on success."""

    @abstractmethod
    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        """Inpaint *image* where *mask* is white. Return result same size."""

    @abstractmethod
    def unload(self) -> None:
        """Release GPU memory."""


class LamaBackend(InpaintBackend):
    """LaMa (Large Mask Inpainting) via simple-lama-inpainting."""

    def __init__(self):
        self._model = None

    def load(self) -> bool:
        try:
            from simple_lama_inpainting import SimpleLama
            from simple_lama_inpainting.models.model import LAMA_MODEL_URL
            from simple_lama_inpainting.utils import download_model

            configured_path = os.environ.get("LAMA_MODEL")
            model_path = configured_path or download_model(LAMA_MODEL_URL)
            identity = stage_identity("webtoon", "cleanup")["model"]
            verified_path = verify_file_sha256(
                model_path, identity["weights_revision"]
            )

            previous_path = os.environ.get("LAMA_MODEL")
            os.environ["LAMA_MODEL"] = str(verified_path)
            try:
                self._model = SimpleLama()
            finally:
                if previous_path is None:
                    os.environ.pop("LAMA_MODEL", None)
                else:
                    os.environ["LAMA_MODEL"] = previous_path
            log.info("LaMa inpainting model loaded")
            return True
        except ImportError:
            log.warning("simple-lama-inpainting not installed; "
                        "pip install simple-lama-inpainting")
            return False

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        result = self._model(image.convert("RGB"), mask.convert("L"))
        return result.convert("RGB")

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
            _cuda_empty_cache()


def _cuda_empty_cache():
    """Free GPU memory only when torch is already safely imported."""
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


# ---------------------------------------------------------------------------
# Singleton backend management
# ---------------------------------------------------------------------------

_BACKEND_MAP = {"lama": LamaBackend}
_infer_lock = threading.Lock()
_supervisor: "_PersistentSupervisor | None" = None
_supervisor_factory = None


class SupervisedInpaintError(RuntimeError):
    """A bounded worker failed, with a stable degradation reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UnexpectedInpaintError(Exception):
    """An adapter programming error which must fail the page."""


def _failure_reason(error: BaseException) -> str:
    message = str(error).lower()
    if isinstance(error, MemoryError) or "out of memory" in message or "oom" in message:
        return "oom"
    if message == "wrong_size":
        return "wrong_size"
    if message == "backend_unavailable":
        return "backend_unavailable"
    return "backend_failure"


_MIN_TRANSPORT_SECONDS = 0.02
_MAX_CONTROL_BYTES = 32 * 1024


def _control_pair() -> tuple[socket.socket, socket.socket]:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    parent.setblocking(False)
    child.setblocking(True)
    return parent, child


def _send_control(connection: socket.socket, value, end: float) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_CONTROL_BYTES:
        raise SupervisedInpaintError("backend_failure")
    remaining = end - monotonic()
    if remaining <= 0:
        raise SupervisedInpaintError("wall_time")
    _, writable, _ = select.select([], [connection], [], remaining)
    if not writable:
        raise SupervisedInpaintError("wall_time")
    try:
        written = connection.send(payload)
    except BlockingIOError as error:
        raise SupervisedInpaintError("wall_time") from error
    if written != len(payload):
        raise SupervisedInpaintError("backend_failure")


def _recv_control(connection: socket.socket, end: float):
    remaining = end - monotonic()
    if remaining <= 0:
        raise SupervisedInpaintError("wall_time")
    readable, _, _ = select.select([connection], [], [], remaining)
    if not readable:
        raise SupervisedInpaintError("wall_time")
    try:
        payload = connection.recv(_MAX_CONTROL_BYTES)
    except BlockingIOError as error:
        raise SupervisedInpaintError("wall_time") from error
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SupervisedInpaintError("backend_failure") from error


def _child_send(connection: socket.socket, value) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) <= _MAX_CONTROL_BYTES:
        connection.send(payload)


def _execute_artifacts(backend: InpaintBackend, specs) -> int:
    for image_path, mask_path, output_path, seed in specs:
        image_array = np.load(image_path, allow_pickle=False)
        mask_array = np.load(mask_path, allow_pickle=False)
        image = Image.fromarray(image_array).convert("RGB")
        mask = Image.fromarray(mask_array).convert("L")
        seed_inference(seed)
        result = backend.inpaint(image, mask)
        if not isinstance(result, Image.Image) or result.size != image.size:
            raise ValueError("wrong_size")
        np.save(output_path, np.asarray(result.convert("RGB")), allow_pickle=False)
    return len(specs)


def _stage_artifacts_process(connection, specs, requests) -> None:
    try:
        for spec, (image, mask, _seed) in zip(specs, requests, strict=True):
            np.save(spec[0], np.asarray(image.convert("RGB")), allow_pickle=False)
            np.save(spec[1], np.asarray(mask.convert("L")), allow_pickle=False)
        _child_send(connection, ("ok", len(specs)))
    except BaseException as error:
        try:
            _child_send(connection, ("unexpected", type(error).__name__))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _one_shot_process(connection, backend, specs) -> None:
    try:
        if not backend.load():
            raise RuntimeError("backend_unavailable")
        _child_send(connection, ("ok", _execute_artifacts(backend, specs)))
    except (MemoryError, OSError, RuntimeError, ValueError) as error:
        try:
            _child_send(connection, ("error", _failure_reason(error)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    except BaseException as error:
        try:
            _child_send(connection, ("unexpected", type(error).__name__))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        try:
            backend.unload()
        except Exception:
            pass
        _cuda_empty_cache()
        connection.close()


def _persistent_process(connection, backend_factory) -> None:
    backend = None
    try:
        backend = backend_factory()
        if not backend.load():
            raise RuntimeError("backend_unavailable")
        _child_send(connection, ("ready", None))
        while True:
            command, payload = json.loads(connection.recv(_MAX_CONTROL_BYTES))
            if command == "stop":
                return
            try:
                _child_send(connection, ("ok", _execute_artifacts(backend, payload)))
            except (MemoryError, OSError, RuntimeError, ValueError) as error:
                _child_send(connection, ("error", _failure_reason(error)))
                return
            except BaseException as error:
                _child_send(connection, ("unexpected", type(error).__name__))
                return
    except (MemoryError, OSError, RuntimeError, ValueError) as error:
        try:
            _child_send(connection, ("error", _failure_reason(error)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    except BaseException as error:
        try:
            _child_send(connection, ("unexpected", type(error).__name__))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if backend is not None:
            try:
                backend.unload()
            except Exception:
                pass
        _cuda_empty_cache()
        connection.close()


class _PersistentSupervisor:
    """Parent handle for one warmed, killable model process."""

    def __init__(self, backend_factory=LamaBackend, context_name: str = "spawn"):
        self._context = multiprocessing.get_context(context_name)
        self._factory = backend_factory
        self._process = None
        self._connection = None

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _start(self, end: float) -> None:
        parent, child = _control_pair()
        process = self._context.Process(
            target=_persistent_process,
            args=(child, self._factory),
            daemon=True,
        )
        process.start()
        child.close()
        self._connection, self._process = parent, process
        status, reason = _recv_control(parent, end)
        if status == "unexpected":
            raise UnexpectedInpaintError(str(reason))
        if status != "ready":
            raise SupervisedInpaintError(str(reason))

    def run(self, specs, end: float) -> None:
        if not self.alive():
            self.close()
            self._start(end)
        _send_control(self._connection, ("inpaint", specs), end)
        status, value = _recv_control(self._connection, end)
        if status == "unexpected":
            raise UnexpectedInpaintError(str(value))
        if status != "ok":
            raise SupervisedInpaintError(str(value))
        if value != len(specs):
            raise SupervisedInpaintError("backend_failure")

    def close(self) -> None:
        connection, process = self._connection, self._process
        self._connection = self._process = None
        if connection is not None:
            if process is not None and process.is_alive():
                try:
                    _send_control(connection, ("stop", None), monotonic() + 0.05)
                except (BrokenPipeError, EOFError, OSError, SupervisedInpaintError):
                    pass
            connection.close()
        if process is not None:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join()


def _reset_backend() -> None:
    """Kill a poisoned model owner and release parent accelerator caches."""
    global _supervisor
    previous, _supervisor = _supervisor, None
    if previous is not None:
        previous.close()
    _cuda_empty_cache()


atexit.register(_reset_backend)


def _bounded_child(target, args, end: float) -> tuple[str, object]:
    context = multiprocessing.get_context("fork")
    parent, child = _control_pair()
    process = context.Process(target=target, args=(child, *args), daemon=True)
    try:
        process.start()
        child.close()
        status, value = _recv_control(parent, end)
        process.join(timeout=max(0.0, end - monotonic()))
        return status, value
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()


def _stage_artifacts(specs, requests, end: float) -> None:
    status, value = _bounded_child(
        _stage_artifacts_process, (specs, requests), end,
    )
    if status == "unexpected":
        raise UnexpectedInpaintError(str(value))
    if status != "ok" or value != len(specs):
        raise SupervisedInpaintError("backend_failure")


def _run_one_shot(specs, backend, end: float) -> None:
    status, value = _bounded_child(_one_shot_process, (backend, specs), end)
    if status == "unexpected":
        raise UnexpectedInpaintError(str(value))
    if status != "ok":
        raise SupervisedInpaintError(str(value))
    if value != len(specs):
        raise SupervisedInpaintError("backend_failure")


def _read_artifacts(specs, end: float) -> list[Image.Image]:
    results: list[Image.Image] = []
    for image_path, _mask_path, output_path, _seed in specs:
        if end - monotonic() < _MIN_TRANSPORT_SECONDS:
            raise SupervisedInpaintError("wall_time")
        result = np.load(output_path, allow_pickle=False)
        expected = np.load(image_path, mmap_mode="r", allow_pickle=False).shape
        if result.shape != expected:
            raise SupervisedInpaintError("wrong_size")
        results.append(Image.fromarray(result).convert("RGB"))
        if monotonic() > end:
            raise SupervisedInpaintError("wall_time")
    return results


def supervised_inpaint_tiles(
    requests: list[tuple[Image.Image, Image.Image, int]],
    *,
    end: float,
    backend: InpaintBackend | None = None,
) -> list[Image.Image]:
    """Bound staging, lock wait, model load, IPC, and inference."""
    global _supervisor
    if end - monotonic() < _MIN_TRANSPORT_SECONDS:
        _reset_backend()
        raise SupervisedInpaintError("wall_time")
    remaining = end - monotonic()
    if remaining <= 0 or not _infer_lock.acquire(timeout=remaining):
        _reset_backend()
        raise SupervisedInpaintError("lock_timeout")
    try:
        if end - monotonic() < _MIN_TRANSPORT_SECONDS:
            raise SupervisedInpaintError("wall_time")
        with TemporaryDirectory(prefix="yomik-cleanup-") as directory:
            specs = [(
                os.path.join(directory, f"image-{index}.npy"),
                os.path.join(directory, f"mask-{index}.npy"),
                os.path.join(directory, f"output-{index}.npy"),
                seed,
            ) for index, (_image, _mask, seed) in enumerate(requests)]
            _stage_artifacts(specs, requests, end)
            if backend is not None:
                _run_one_shot(specs, backend, end)
            else:
                if _supervisor is None:
                    factory = _supervisor_factory or _PersistentSupervisor
                    _supervisor = factory()
                _supervisor.run(specs, end)
            return _read_artifacts(specs, end)
    except SupervisedInpaintError:
        _reset_backend()
        raise
    except UnexpectedInpaintError:
        _reset_backend()
        raise
    except (EOFError, OSError, RuntimeError, ValueError) as error:
        _reset_backend()
        raise SupervisedInpaintError("backend_failure") from error
    finally:
        _infer_lock.release()

def get_backend_for_name(name: str) -> InpaintBackend | None:
    """Create and load a backend by name (for A/B testing). Caller manages lifecycle."""
    cls = _BACKEND_MAP.get(name)
    if cls is None:
        log.error("Unknown inpainting model %r", name)
        return None
    instance = cls()
    if not instance.load():
        return None
    return instance


# ---------------------------------------------------------------------------
# Mask building
# ---------------------------------------------------------------------------

def _policy(context_pad: int = INPAINT_CONTEXT_PAD) -> CleanupPolicy:
    return CleanupPolicy(
        dilation_px=INPAINT_TEXT_DILATE,
        context_px=context_pad,
        max_tiles=INPAINT_MAX_TILES,
        max_tile_width=INPAINT_MAX_TILE_WIDTH,
        max_tile_height=INPAINT_MAX_TILE_HEIGHT,
        max_tile_pixels=INPAINT_MAX_TILE_PIXELS,
        max_page_pixels=INPAINT_MAX_PAGE_PIXELS,
        max_wall_seconds=INPAINT_MAX_WALL_SECONDS,
        min_host_available_mb=INPAINT_MIN_HOST_MB,
        min_gpu_available_mb=INPAINT_MIN_GPU_MB,
        uniform_stddev=INPAINT_UNIFORM_STDDEV,
        uniform_range=INPAINT_UNIFORM_RANGE,
        seed=INPAINT_SEED,
    )


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    applied: bool
    method: str
    degraded: bool = False
    reason: str | None = None
    mask: Image.Image | None = None


def build_inpaint_mask(
    bubble: WebtoonBubble,
    img_size: tuple[int, int],
    erode_px: int = INPAINT_ERODE_PX,
    text_pad: int = INPAINT_TEXT_PAD,
    text_dilate: int = INPAINT_TEXT_DILATE,
) -> Image.Image | None:
    """Build an expanded OCR mask, clipped to the retained balloon mask."""
    return build_cleanup_mask(
        img_size,
        [det.bbox_rect for det in bubble.text_regions],
        semantic_mask=bubble.bubble_mask,
        padding_px=text_pad,
        dilation_px=text_dilate,
        erosion_px=erode_px,
    )


def build_effective_cleanup_mask(
    original_img: Image.Image,
    bubble: WebtoonBubble,
    policy: CleanupPolicy | None = None,
) -> Image.Image | None:
    """Build the exact production removal mask for one webtoon bubble."""
    selected = policy or _policy()
    boxes = [det.bbox_rect for det in bubble.text_regions]
    mask = build_cleanup_mask(
        original_img.size,
        boxes,
        semantic_mask=bubble.bubble_mask,
        padding_px=INPAINT_TEXT_PAD,
        dilation_px=INPAINT_TEXT_DILATE,
        erosion_px=INPAINT_ERODE_PX,
        max_page_pixels=selected.max_page_pixels,
        ocr_stroke_mask=estimate_ocr_strokes(
            original_img, boxes, bubble.bg_color,
        ),
    )
    if mask is None:
        return None
    pixels = np.asarray(mask)
    contained = np.zeros(pixels.shape, dtype=np.uint8)
    x1, y1, x2, y2 = bubble.bbox
    contained[y1:y2, x1:x2] = pixels[y1:y2, x1:x2]
    return Image.fromarray(contained, mode="L")


def _fill_masked(
    target: Image.Image, mask: Image.Image, color: tuple[int, int, int],
) -> None:
    fill = Image.new("RGB", target.size, color)
    target.paste(fill, (0, 0), mask)


def cleanup_bubble(
    original_img: Image.Image,
    bubble: WebtoonBubble,
    context_pad: int = INPAINT_CONTEXT_PAD,
    backend: InpaintBackend | None = None,
    target_img: Image.Image | None = None,
) -> CleanupOutcome:
    """Apply bounded cleanup and preserve the last valid result on failure."""
    if not INPAINT_ENABLED:
        return CleanupOutcome(False, "disabled")
    dest = target_img if target_img is not None else original_img
    policy = _policy(context_pad)
    try:
        mask = build_effective_cleanup_mask(original_img, bubble, policy)
    except CleanupBudgetExceeded:
        return CleanupOutcome(False, "none", True, "page_budget")
    if mask is None:
        return CleanupOutcome(False, "none", True, "empty_mask")
    try:
        plan = plan_cleanup(
            original_img, mask, semantic_mask=bubble.bubble_mask, policy=policy,
        )
    except CleanupBudgetExceeded as error:
        _fill_masked(dest, mask, bubble.bg_color)
        return CleanupOutcome(True, "uniform_fill", True, str(error), mask)

    if plan.uniform_color is not None:
        _fill_masked(dest, mask, plan.uniform_color)
        return CleanupOutcome(True, "uniform_fill", mask=mask)
    if not host_memory_available(policy.min_host_available_mb):
        _fill_masked(dest, mask, bubble.bg_color)
        return CleanupOutcome(True, "uniform_fill", True, "host_memory", mask)
    if not gpu_memory_available(policy.min_gpu_available_mb):
        _fill_masked(dest, mask, bubble.bg_color)
        return CleanupOutcome(True, "uniform_fill", True, "gpu_memory", mask)

    end = deadline(policy)
    requests: list[tuple[Image.Image, Image.Image, int]] = []
    for index, tile in enumerate(plan.tiles):
        crop_box = tile.bbox
        requests.append((
            original_img.crop(crop_box), mask.crop(crop_box), plan.seed + index,
        ))
    try:
        results = supervised_inpaint_tiles(requests, end=end, backend=backend)
    except SupervisedInpaintError as error:
        log.warning("LaMa cleanup degraded: %s", error.reason)
        _reset_backend()
        _fill_masked(dest, mask, bubble.bg_color)
        return CleanupOutcome(True, "uniform_fill", True, error.reason, mask)
    for tile, result_crop in zip(plan.tiles, results, strict=True):
        crop_box = tile.bbox
        result_arr = np.asarray(result_crop.convert("RGB"))
        target_arr = np.asarray(dest.crop(crop_box).convert("RGB"))
        mask_arr = np.asarray(mask.crop(crop_box)) > 0
        composite = np.where(mask_arr[:, :, None], result_arr, target_arr)
        dest.paste(Image.fromarray(composite.astype(np.uint8)), crop_box[:2])
    return CleanupOutcome(True, "lama_tiles", mask=mask)


def inpaint_bubble(
    original_img: Image.Image,
    bubble: WebtoonBubble,
    context_pad: int = INPAINT_CONTEXT_PAD,
    backend: InpaintBackend | None = None,
    target_img: Image.Image | None = None,
) -> bool:
    """Compatibility adapter returning whether a safe cleanup was applied."""
    outcome = cleanup_bubble(
        original_img, bubble, context_pad, backend, target_img,
    )
    bubble.cleanup_method = outcome.method
    bubble.cleanup_degraded = outcome.degraded
    bubble.cleanup_mask = (None if outcome.mask is None else
                           np.asarray(outcome.mask))
    if outcome.degraded:
        bubble.cleanup_method = f"{outcome.method}:degraded"
    return outcome.applied
