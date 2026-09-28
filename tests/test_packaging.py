"""Сборка для Windows: то, что можно проверить без Windows."""

import importlib.util
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
spec = importlib.util.spec_from_file_location("build_win", PACKAGING / "build_win.py")
build_win = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_win)


def test_build_output_is_utf8_on_runner_console():
    """Консоль раннера — cp1252: кириллица в выводе сборки не должна ронять её."""
    first = inspect.getsource(build_win.main).splitlines()[1].strip()
    assert first.startswith('sys.stdout.reconfigure(encoding="utf-8")')
    workflow = (ROOT / ".github" / "workflows" / "portable-win.yml").read_text("utf-8")
    assert 'PYTHONUTF8: "1"' in workflow
