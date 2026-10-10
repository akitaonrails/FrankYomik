"""Pinned Hayai OCR v2 adapter with killable inference deadlines."""

from __future__ import annotations

import hashlib
import io
import logging
import multiprocessing
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image

log = logging.getLogger(__name__)

MODEL_ID: Final = "JustANormalTinkerer/hayai-ocr-v2"
MODEL_REVISION: Final = "4a4ce477c9a8841f208b94e1d9ed5c0938965e05"
MODEL_LICENSE: Final = "Apache-2.0"
MODEL_FILES: Final = {
    "config.json": "581b762f1dfd55d0108f3f84e3f157bc762524af37fb0c19a7172a18b75582e2",
    "model.safetensors": "4c645b221db8428cda04991be234c18133bb8861142a3d87cba04c5099b02328",
    "tokenizer.json": "f8a0a909c628a684fe463094614e236a8b1d3609e7770f77e7beafaf1056bf13",
    "tokenizer_config.json": "6fb6c69afaedf1275872d3e62e276fd4467bd00da7a84cbbb5566a2cd28f58f6",
}


class HayaiUnavailable(RuntimeError):
    pass


class HayaiTimeout(RuntimeError):
    pass


class HayaiOutOfMemory(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_snapshot() -> Path:
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError

    try:
        path = Path(snapshot_download(
            repo_id=MODEL_ID,
            revision=MODEL_REVISION,
            allow_patterns=list(MODEL_FILES),
            local_files_only=True,
        ))
    except (HfHubHTTPError, LocalEntryNotFoundError, OSError) as error:
        raise HayaiUnavailable("Hayai checkpoint is unavailable") from error
    for name, expected in MODEL_FILES.items():
        candidate = path / name
        if not candidate.is_file() or _sha256(candidate) != expected:
            raise HayaiUnavailable(f"Hayai model byte check failed: {name}")
    return path


def _load_runtime(device_name: str):
    import torch
    from transformers import PreTrainedTokenizerFast, Siglip2ImageProcessor

    from .hayai_configuration import HayaiConfig
    from .hayai_model import HayaiModel

    snapshot = _verified_snapshot()
    if device_name == "auto":
        device_name = "cuda" if torch.cuda.is_available() else "cpu"
    if device_name == "cuda" and not torch.cuda.is_available():
        raise HayaiUnavailable("Hayai CUDA device is unavailable")
    config = HayaiConfig.from_pretrained(snapshot, local_files_only=True)
    model = HayaiModel.from_pretrained(
        snapshot,
        config=config,
        local_files_only=True,
        use_safetensors=True,
    ).to(device_name).eval()
    tokenizer = PreTrainedTokenizerFast.from_pretrained(
        snapshot, local_files_only=True,
    )
    processor = Siglip2ImageProcessor(
        do_resize=True,
        do_rescale=True,
        do_normalize=True,
        do_convert_rgb=None,
        image_mean=[0.5, 0.5, 0.5],
        image_std=[0.5, 0.5, 0.5],
        patch_size=16,
        max_num_patches=256,
        resample=Image.Resampling.BILINEAR,
        rescale_factor=1 / 255,
    )
    return torch, model, tokenizer, processor, device_name


def _runtime_process(connection, device_name: str) -> None:
    """Own the accelerator context so the parent can terminate a timeout."""
    try:
        runtime = _load_runtime(device_name)
    except (ImportError, OSError, HayaiUnavailable) as error:
        connection.send(("unavailable", str(error)))
        connection.close()
        return
    except MemoryError as error:
        connection.send(("oom", str(error)))
        connection.close()
        return
    except RuntimeError as error:
        status = "oom" if "out of memory" in str(error).lower() else "error"
        connection.send((status, repr(error)))
        connection.close()
        return
    except Exception as error:
        connection.send(("error", repr(error)))
        connection.close()
        return

    torch, model, tokenizer, processor, device_name = runtime
    while True:
        try:
            request = connection.recv()
        except EOFError:
            return
        if request is None:
            return
        try:
            image = Image.open(io.BytesIO(request)).convert("RGB")
            inputs = processor(
                images=[image], max_num_patches=256, return_tensors="pt",
            ).to(device_name)
            with torch.no_grad():
                values = model.generate(
                    pixel_values=inputs["pixel_values"],
                    pixel_attention_mask=inputs["pixel_attention_mask"],
                    spatial_shapes=inputs["spatial_shapes"],
                    tokenizer=tokenizer,
                    max_new_tokens=256,
                    num_beams=1,
                    repetition_penalty=1.0,
                )
            connection.send(("ok", values[0].strip()))
        except (torch.OutOfMemoryError, MemoryError) as error:
            connection.send(("oom", str(error)))
            return
        except RuntimeError as error:
            if "out of memory" in str(error).lower():
                connection.send(("oom", str(error)))
                return
            connection.send(("error", repr(error)))
            return
        except Exception as error:
            connection.send(("error", repr(error)))
            return


@dataclass
class HayaiOcrBackend:
    device: str = "auto"
    timeout_seconds: float = 45.0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._process = None
        self._connection = None

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._process = context.Process(
            target=_runtime_process,
            args=(child, self.device),
            daemon=True,
            name="hayai-ocr",
        )
        self._connection = parent
        self._process.start()
        child.close()

    def _stop_process(self) -> None:
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            connection.close()
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)

    def __call__(self, image: Image.Image) -> str:
        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG")
        with self._lock:
            self._ensure_process()
            assert self._connection is not None
            self._connection.send(output.getvalue())
            if not self._connection.poll(self.timeout_seconds):
                self._stop_process()
                raise HayaiTimeout(
                    f"Hayai inference exceeded {self.timeout_seconds:g}s"
                )
            try:
                status, value = self._connection.recv()
            except EOFError as error:
                self._stop_process()
                raise HayaiUnavailable("Hayai worker exited") from error
            if status == "ok":
                return value
            self._stop_process()
            if status == "unavailable":
                raise HayaiUnavailable(value)
            if status == "oom":
                raise HayaiOutOfMemory(value)
            raise RuntimeError(f"Hayai worker failed: {value}")


_backend: HayaiOcrBackend | None = None
_backend_lock = threading.Lock()


def extract_text(image: Image.Image) -> str:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                from .config import HAYAI_DEVICE, HAYAI_TIMEOUT_SECONDS
                _backend = HayaiOcrBackend(HAYAI_DEVICE, HAYAI_TIMEOUT_SECONDS)
    return _backend(image)
