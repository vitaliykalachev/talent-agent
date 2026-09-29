"""Портативные сборки: то, что проверяется без сборки целиком."""

import importlib.util
import inspect
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
spec = importlib.util.spec_from_file_location("build", PACKAGING / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_build_output_is_utf8_on_runner_console():
    """Консоль раннера — cp1252: кириллица в выводе сборки не должна ронять её."""
    first = inspect.getsource(build.main).splitlines()[1].strip()
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
    build.flatten_cache(tmp_path)
    weights = snapshot / "model.safetensors"
    assert not weights.is_symlink() and weights.read_bytes() == b"weights"
    assert not (repo / "blobs").exists() and not (tmp_path / "blobs").exists()
    assert (repo / "refs" / "main").read_text() == "h1"
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert sum(p.read_bytes() == b"weights" for p in files) == 1


def test_launcher_explains_unpacked_zip_before_start():
    """Двойной клик по bat прямо в ZIP: вместо «не найден python» — что сделать."""
    lines = (PACKAGING / "launcher.bat").read_text("utf-8").splitlines()
    check = lines.index('if not exist "python\\python.exe" (')
    start = lines.index('"python\\python.exe" -m app.main')
    block = lines[check : check + 5]
    assert check < start and block[-1] == ")"
    assert "Сначала распакуйте архив целиком, затем запустите этот файл" in block[1]
    assert "pause" in block[2] and "exit /b 1" in block[3]


def test_client_readme_names_both_warning_buttons():
    """Окно Windows про неизвестного издателя бывает двух видов — в инструкции оба."""
    text = (PACKAGING / "readme-client.txt").read_text("utf-8")
    step = next(line for line in text.splitlines() if line.startswith("3."))
    assert "«Выполнить»" in step and "«Подробнее», затем «Выполнить в любом случае»" in step


def fake_cache(root: Path) -> Path:
    repo = root / "models--org--model"
    (repo / "blobs").mkdir(parents=True)
    blob = repo / "blobs" / "abc"
    blob.write_bytes(b"weights")
    blob.chmod(0o444)  # как в кэше Hugging Face
    snapshot = repo / "snapshots" / "h1"
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").symlink_to(Path("../../blobs/abc"))
    return repo


def test_read_only_blob_is_removed(tmp_path):
    repo = fake_cache(tmp_path)
    assert build.flatten_cache(tmp_path) == len(b"weights")
    assert not (repo / "blobs").exists()
    # «Только чтение» не переходит на веса: иначе «Запустить.command» не снимет с них карантин
    assert os.access(repo / "snapshots" / "h1" / "model.safetensors", os.W_OK)
    path = tmp_path / "ro"
    path.write_text("x")
    path.chmod(0o444)
    build.writable(os.remove, str(path), None)  # обработчик снимает «только чтение»
    assert not path.exists()


def test_blobs_left_behind_stop_the_build(tmp_path, monkeypatch):
    """Кэш не удалился — сборка падает, а не кладёт веса в ZIP дважды."""
    fake_cache(tmp_path)
    monkeypatch.setattr(build.shutil, "rmtree", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="Не удалось убрать кэш весов"):
        build.flatten_cache(tmp_path)


def test_oversized_model_stops_the_build(tmp_path, monkeypatch):
    fake_cache(tmp_path)
    monkeypatch.setattr(build, "MODEL_LIMIT", 3)
    with pytest.raises(SystemExit, match="ждали меньше 600"):
        build.flatten_cache(tmp_path)
