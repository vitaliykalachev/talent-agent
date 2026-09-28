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


def test_model_cache_packed_once(tmp_path):
    """Веса лежат в сборке один раз: ссылки snapshots/ → blobs/ заменены файлами,
    blobs/ убраны, refs/ на месте — local_files_only найдёт модель по ним."""
    repo = tmp_path / "models--org--model"
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / "abc").write_bytes(b"weights")
    (tmp_path / "blobs" / "ab").mkdir(parents=True)  # общий кэш новых версий hub
    (tmp_path / "blobs" / "ab" / "abc").write_bytes(b"weights")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("h1")
    snapshot = repo / "snapshots" / "h1"
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").symlink_to(Path("../../blobs/abc"))
    build_win.flatten_cache(tmp_path)
    weights = snapshot / "model.safetensors"
    assert not weights.is_symlink() and weights.read_bytes() == b"weights"
    assert not (repo / "blobs").exists() and not (tmp_path / "blobs").exists()
    assert (repo / "refs" / "main").read_text() == "h1"
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert sum(p.read_bytes() == b"weights" for p in files) == 1
