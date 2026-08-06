from __future__ import annotations

import os
from pathlib import Path


def register_nvidia_dlls() -> None:
    """Make pip-installed NVIDIA CUDA DLLs discoverable on Windows.

    Both ``ctranslate2`` (faster-whisper's backend) and ``torch`` (pyannote's
    backend) need cuBLAS / CUDA runtime DLLs at runtime when using the GPU.  The
    ``nvidia-cublas-cu12`` and ``nvidia-cuda-runtime-cu12`` wheels ship the DLLs
    under ``site-packages/nvidia/<pkg>/bin``.  ``os.add_dll_directory`` alone is
    NOT enough for the C runtimes' lazy loading — we also prepend the directory
    to ``PATH`` so any DLL lookup can succeed.

    Without this, ``torch.cuda.is_available()`` silently returns False and heavy
    work (e.g. pyannote diarization) falls back to the CPU and grinds to a halt
    on long recordings — the exact symptom users hit where a 115-min meeting
    "hangs at diarization".
    """
    _nvidia_root = Path(__file__).resolve().parents[1] / ".venv" / "Lib" / "site-packages" / "nvidia"
    for _sub in ("cublas/bin", "cuda_runtime/bin"):
        _candidate = _nvidia_root / _sub
        if _candidate.is_dir():
            os.add_dll_directory(str(_candidate))
            os.environ["PATH"] = str(_candidate) + os.pathsep + os.environ.get("PATH", "")


# Backwards-compatible alias kept so existing call sites that imported the
# underscore-prefixed helper keep working.
_register_nvidia_dlls = register_nvidia_dlls
