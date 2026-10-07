"""The settings importer and MinerU must share the real frozen model location."""

import importlib.util
import os
from pathlib import Path
import sys


def test_frozen_home_matches_imported_model_directory(monkeypatch, tmp_path):
    launcher_path = Path(__file__).resolve().parents[2] / "tools/packaging/zhilan_launcher.py"
    spec = importlib.util.spec_from_file_location("_settings_model_launcher", launcher_path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "zhilan.exe"))
    for key in launcher.FROZEN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    launcher._apply_frozen_env_defaults()
    # MinerU's ModelConfig appends "models" to MINERU_HOME; the shipped archive lives here.
    actual = Path(os.environ["MINERU_HOME"]) / "models" / "MinerU-4_models_onnx"
    assert actual == tmp_path / "models" / "MinerU-4_models_onnx"
