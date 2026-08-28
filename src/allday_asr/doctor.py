from __future__ import annotations

import importlib.metadata
import shutil
import sys
from dataclasses import dataclass

from allday_asr.audio.tools import executable_version
from allday_asr.paths import ensure_runtime_dirs


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_checks() -> list[CheckResult]:
    results = [
        CheckResult("Python", sys.version_info[:2] == (3, 12), sys.version.split()[0]),
    ]
    ensure_runtime_dirs()

    for executable in ("ffmpeg", "ffprobe"):
        try:
            version = executable_version(executable)
            results.append(CheckResult(executable, True, version))
        except Exception as exc:
            results.append(CheckResult(executable, False, str(exc)))

    for package in (
        "funasr",
        "modelscope",
        "qwen-asr",
        "pyannote.audio",
        "transformers",
        "soundfile",
        "typer",
    ):
        try:
            version = importlib.metadata.version(package)
            results.append(CheckResult(package, True, version))
        except importlib.metadata.PackageNotFoundError:
            results.append(CheckResult(package, False, "未安装"))

    try:
        import torch
        import torchaudio

        results.append(CheckResult("torch", True, torch.__version__))
        results.append(CheckResult("torchaudio", True, torchaudio.__version__))
        if not torch.cuda.is_available():
            results.append(CheckResult("CUDA", False, "torch.cuda.is_available() = False"))
        else:
            left = torch.randn((512, 512), device="cuda")
            product = left @ left
            torch.cuda.synchronize()
            capability = torch.cuda.get_device_capability()
            detail = (
                f"{torch.cuda.get_device_name(0)} | CUDA {torch.version.cuda} | "
                f"sm_{capability[0]}{capability[1]} | matmul {tuple(product.shape)} OK"
            )
            results.append(CheckResult("CUDA", True, detail))
    except Exception as exc:
        results.append(CheckResult("CUDA", False, repr(exc)))

    if shutil.disk_usage(".").free < 2 * 1024**3:
        results.append(CheckResult("磁盘空间", False, "可用空间少于 2 GiB"))
    else:
        free_gib = shutil.disk_usage(".").free / 1024**3
        results.append(CheckResult("磁盘空间", True, f"可用 {free_gib:.1f} GiB"))
    return results
