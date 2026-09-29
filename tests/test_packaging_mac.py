"""Сборка для Mac: цель mac-arm64 в packaging/build.py, «Запустить.command», установщик."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
spec = importlib.util.spec_from_file_location("build", PACKAGING / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
MAC = build.TARGETS["mac-arm64"]
LAUNCHER = PACKAGING / "launcher.command"
INSTALLER = PACKAGING / "install-mac.sh"


def test_targets_keep_windows_and_add_mac():
    win = build.TARGETS["win64"]
    assert win.asset.fullmatch(
        "cpython-3.12.13+20260928-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
    )
    assert MAC.asset.fullmatch(
        "cpython-3.12.13+20260928-aarch64-apple-darwin-install_only_stripped.tar.gz"
    )
    assert not MAC.asset.fullmatch(
        "cpython-3.12.13+20260928-aarch64-apple-darwin-install_only.tar.gz"
    )
    assert MAC.python == "python/bin/python3.12"
    assert MAC.archive == "kadrovyi-agent-mac.zip"
    assert MAC.launcher == {
        "launcher.command": "Запустить.command",
        "readme-client-mac.txt": "Как запустить.txt",
    }
    workflow = (ROOT / ".github" / "workflows" / "portable-win.yml").read_text("utf-8")
    assert "python packaging/build.py --target win64" in workflow


def test_scripts_find_python_next_to_them(tmp_path):
    """uvicorn и другие скрипты в python/bin после сборки не ссылаются на машину сборки
    и запускаются из любой папки, куда клиент положил программу."""
    bin_dir = tmp_path / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.12").symlink_to(sys.executable)
    script = bin_dir / "hello"
    script.write_text(f"#!{bin_dir / 'python3.12'}\nimport sys\nprint('ok', sys.argv[1])\n")
    script.chmod(0o755)
    other = bin_dir / "other"
    other.write_text("#!/bin/sh\necho other\n")
    build.relocate_scripts(bin_dir)
    assert str(tmp_path) not in script.read_text()
    assert other.read_text() == "#!/bin/sh\necho other\n"
    moved = tmp_path / "moved"
    (tmp_path / "python").rename(moved)
    out = subprocess.run([moved / "bin" / "hello", "раз"], capture_output=True, text=True)
    assert out.stdout == "ok раз\n", out.stderr


def test_leaked_build_path_is_found(tmp_path):
    (tmp_path / "RECORD").write_text("app/x.py,sha256=…,1\n")
    (tmp_path / "evil.pth").write_bytes(b"/Users/builder/talent-agent/.venv\n")
    (tmp_path / "link").symlink_to(tmp_path / "evil.pth")
    assert build.leaked_paths(tmp_path, "/Users/builder") == [tmp_path / "evil.pth"]


def test_mac_launcher_files_are_unix(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "PKG", tmp_path)
    build.launcher(MAC)
    command = (tmp_path / "Запустить.command").read_bytes()
    readme = (tmp_path / "Как запустить.txt").read_bytes()
    for data in (command, readme):
        assert b"\r\n" not in data and not data.startswith(b"\xef\xbb\xbf")
    assert command.startswith(b"#!/bin/bash\n")
    assert os.access(tmp_path / "Запустить.command", os.X_OK)


def fake_pkg(root: Path) -> None:
    (root / "python" / "bin").mkdir(parents=True)
    python = root / "python" / "bin" / "python3.12"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (root / "python" / "bin" / "python3").symlink_to("python3.12")
    (root / "Запустить.command").write_text("#!/bin/bash\n")
    (root / "Запустить.command").chmod(0o755)
    (root / "Как запустить.txt").write_text("текст")


mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="ditto есть только в macOS")


@mac_only
def test_zip_keeps_exec_bits_and_links(tmp_path, monkeypatch):
    """Двойной клик по ZIP в Finder распаковывает его «Архиватором»: права и ссылки
    доходят до клиента, только если ZIP собран ditto."""
    out = tmp_path / "dist"
    monkeypatch.setattr(build, "OUT", out)
    monkeypatch.setattr(build, "PKG", out / "KadrovyAgent")
    fake_pkg(out / "KadrovyAgent")
    archive = out / MAC.archive
    build.pack_mac(MAC, archive)
    assert not (out / "check").exists()
    assert (out / "install-mac.sh").read_bytes() == INSTALLER.read_bytes()
    home = tmp_path / "home"
    subprocess.run(["ditto", "-x", "-k", archive, home], check=True)
    unpacked = home / "KadrovyAgent"
    assert os.access(unpacked / "Запустить.command", os.X_OK)
    assert os.access(unpacked / "python" / "bin" / "python3.12", os.X_OK)
    assert os.readlink(unpacked / "python" / "bin" / "python3") == "python3.12"
    assert (unpacked / "Как запустить.txt").read_text() == "текст"


@mac_only
def test_absolute_link_stops_the_build(tmp_path, monkeypatch):
    out = tmp_path / "dist"
    monkeypatch.setattr(build, "OUT", out)
    monkeypatch.setattr(build, "PKG", out / "KadrovyAgent")
    fake_pkg(out / "KadrovyAgent")
    (out / "KadrovyAgent" / "python" / "bin" / "python").symlink_to("/usr/bin/python3")
    with pytest.raises(AssertionError, match="абсолютная ссылка"):
        build.pack_mac(MAC, out / MAC.archive)


def test_launcher_command_sets_portable_environment():
    lines = LAUNCHER.read_text("utf-8").splitlines()
    assert lines[0] == "#!/bin/bash" and 'cd "$(dirname "$0")" || exit 1' in lines
    for line in (
        "export PYTHONUTF8=1",
        'export TA_DATA_DIR="$PWD/data"',
        'export TA_MODELS_DIR="$PWD/data/models"',
        "export HF_HUB_OFFLINE=1",
        ': "${TA_OPEN_BROWSER:=1}"',  # пробы выключают браузер TA_OPEN_BROWSER=0
        "export TA_OPEN_BROWSER",
    ):
        assert line in lines, line
    assert lines[-1] == "exec ./python/bin/python3.12 -m app.main"
    assert any("Не закрывайте это окно, пока работаете" in line for line in lines)


def test_launcher_outside_unpacked_folder_explains(tmp_path):
    """Файл запуска без папки python рядом (скопирован отдельно) — понятная фраза."""
    command = tmp_path / "Запустить.command"
    command.write_bytes(LAUNCHER.read_bytes())
    command.chmod(0o755)
    out = subprocess.run([command], capture_output=True, text=True)
    assert out.returncode == 1
    assert "Сначала распакуйте архив целиком" in out.stdout


@mac_only
def test_launcher_removes_quarantine_from_whole_folder(tmp_path):
    """Разрешение macOS клиент даёт только на «Запустить»; python и библиотеки внутри
    приходят из архива с карантином, и файл запуска снимает его со всей папки."""
    command = tmp_path / "Запустить.command"
    command.write_bytes(LAUNCHER.read_bytes())
    command.chmod(0o755)
    inner = tmp_path / "python" / "lib" / "libtorch.dylib"
    inner.parent.mkdir(parents=True)
    inner.write_bytes(b"lib")
    mark = "0083;66f00000;Safari;00000000-0000-0000-0000-000000000000"
    for path in (command, inner):
        subprocess.run(["xattr", "-w", "com.apple.quarantine", mark, path], check=True)
    subprocess.run([command], capture_output=True)  # python рядом нет — выйдет сразу
    for path in (command, inner):
        attrs = subprocess.run(["xattr", path], capture_output=True, text=True).stdout
        assert "com.apple.quarantine" not in attrs, path
    lines = LAUNCHER.read_text("utf-8").splitlines()
    first = next(i for i, line in enumerate(lines) if not line.startswith("#"))
    assert lines[first] == 'xattr -dr com.apple.quarantine "$(dirname "$0")" 2>/dev/null || true'


def test_installer_structure():
    text = INSTALLER.read_text("utf-8")
    lines = text.splitlines()
    assert 'URL="__ПОДСТАВИТЬ__"' in lines  # адрес подставляется при выкладке
    assert lines[-1] == "main"  # оборванная загрузка скрипта не выполнит его половину
    assert '"$(uname -m)" != "arm64"' in text
    assert 'curl -fL --progress-bar "$URL" -o "$archive"' in text
    assert 'local dir="$HOME/KadrovyAgent"' in text
    assert 'ditto -x -k "$archive" "$HOME"' in text
    assert 'mv "$dir" "$dir.old"' in text
    assert 'xattr -dr com.apple.quarantine "$dir" 2>/dev/null || true' in text
    assert 'exec "$dir/Запустить.command"' in text
    assert "Documents" not in text and "Downloads" not in text  # Терминалу нужен доступ


@mac_only
def test_installer_without_url_stops(tmp_path):
    if os.uname().machine != "arm64":
        pytest.skip("установщик сначала проверяет, что это Mac на Apple Silicon")
    home = tmp_path / "home"
    home.mkdir()
    out = subprocess.run(
        ["bash", INSTALLER], capture_output=True, text=True, env={**os.environ, "HOME": home}
    )
    assert out.returncode == 1 and "не указан адрес архива" in out.stdout
    assert not list(home.iterdir())


def test_client_texts():
    """Инструкция в архиве и сообщение в Telegram: сначала команда, потом запасной путь
    через файл; кнопки — как в статье Apple support.apple.com/ru-ru/102445."""
    for name in ("readme-client-mac.txt", "message-client-mac.txt"):
        text = (PACKAGING / name).read_text("utf-8")
        for words in (
            "Cmd + Пробел",
            "«Терминал»",
            "600 МБ",
            "KadrovyAgent",
            "«Кадровый агент» на рабочем столе",
            "Не закрывайте окно Терминала",
            "Если с командой не вышло",
            "kadrovyi-agent-mac.zip",
            "«Конфиденциальность и безопасность»",
            "«Подтвердить вход»",
            "«Разрешить»",
            "вымышленных данных",
        ):
            assert words in text, (name, words)
        assert text.index("«Терминал»") < text.index("Если с командой не вышло")
    message = (PACKAGING / "message-client-mac.txt").read_text("utf-8")
    assert "curl -fsSL __АДРЕС__/install-mac.sh | bash" in message
