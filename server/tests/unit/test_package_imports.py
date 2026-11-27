import subprocess
import sys


def test_importing_kindle_config_does_not_import_model_runtime():
    command = (
        "import sys; import kindle.config; "
        "assert 'torch' not in sys.modules; "
        "assert 'kindle.processor' not in sys.modules"
    )

    subprocess.run([sys.executable, "-c", command], check=True)


def test_importing_worker_orchestration_does_not_import_model_backends():
    command = (
        "import sys; import worker.job; "
        "assert 'torch' not in sys.modules; "
        "assert 'fugashi' not in sys.modules; "
        "assert 'kindle.processor' not in sys.modules; "
        "assert 'webtoon.processor' not in sys.modules"
    )

    subprocess.run([sys.executable, "-c", command], check=True)


def test_kindle_public_processor_api_is_advertised_without_loading_it():
    command = (
        "import sys; import kindle; "
        "assert 'kindle.processor' not in sys.modules; "
        "assert 'PipelineMode' in kindle.__all__; "
        "assert 'render_page' in kindle.__all__; "
        "assert 'kindle.processor' not in sys.modules"
    )

    subprocess.run([sys.executable, "-c", command], check=True)


def test_importing_kindle_processor_and_furigana_does_not_require_fugashi():
    command = (
        "import sys; sys.modules['fugashi'] = None; "
        "import numpy as np; from PIL import Image; "
        "import kindle.furigana; "
        "from kindle.processor import ("
        "    PipelineMode, load_page_from_memory, render_page_to_bytes"
        "); "
        "img_pil = Image.new('RGB', (32, 32), (255, 255, 255)); "
        "img_cv = np.array(img_pil)[:, :, ::-1].copy(); "
        "page = load_page_from_memory(img_cv, img_pil, name='no-fugashi'); "
        "png = render_page_to_bytes(page, PipelineMode.TRANSLATE); "
        "assert png[:4] == b'\\x89PNG'"
    )

    subprocess.run([sys.executable, "-c", command], check=True)
