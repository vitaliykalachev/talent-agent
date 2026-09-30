"""Сборка для Mac: цель mac-arm64 в packaging/build.py, «Запустить.command», установщик."""

import contextlib
import http.server
import importlib.util
import os
import socket
import subprocess
import sys
import threading
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
    long = bin_dir / "long"  # путь длиннее предела shebang uv пишет через /bin/sh
    long.write_text(
        f"#!/bin/sh\n'''exec' '{bin_dir / 'python3.12'}' \"$0\" \"$@\"\n' '''\n"
        "import sys\nprint('long', sys.argv[1])\n"
    )
    long.chmod(0o755)
    other = bin_dir / "other"
    other.write_text("#!/bin/sh\necho other\n")
    build.relocate_scripts(bin_dir)
    assert str(tmp_path) not in script.read_text() + long.read_text()
    assert other.read_text() == "#!/bin/sh\necho other\n"
    moved = tmp_path / "moved"
    (tmp_path / "python").rename(moved)
    out = subprocess.run([moved / "bin" / "hello", "раз"], capture_output=True, text=True)
    assert out.stdout == "ok раз\n", out.stderr
    out = subprocess.run([moved / "bin" / "long", "два"], capture_output=True, text=True)
    assert out.stdout == "long два\n", out.stderr


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
    assert lines[first] == 'cd "$(dirname "$0")" || exit 1'
    assert lines[first + 1] == 'xattr -dr com.apple.quarantine "$PWD" 2>/dev/null || true'


def test_installer_structure():
    text = INSTALLER.read_text("utf-8")
    lines = text.splitlines()
    assert 'URL="__ПОДСТАВИТЬ__"' in lines  # адрес подставляется при выкладке
    assert lines[-1] == "main"  # оборванная загрузка скрипта не выполнит его половину
    assert '"$(uname -m)" != "arm64"' in text
    assert '[ -n "${HOME:-}" ]' in text
    assert 'curl -fL --progress-bar "$URL" -o "$archive"' in text
    assert 'local dir="$HOME/KadrovyAgent"' in text
    assert 'xattr -dr com.apple.quarantine "$dir" 2>/dev/null || true' in text
    assert 'exec "$dir/Запустить.command"' in text
    assert 'rm -rf "$dir' not in text  # прежняя установка с базой клиента не удаляется
    assert "read " not in text  # stdin занят самим скриптом (curl … | bash)
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
            "900 МБ",
            "KadrovyAgent",
            "«Кадровый агент» на рабочем столе",
            "Не закрывайте окно Терминала",
            "Если с командой не вышло",
            "kadrovyi-agent-mac.zip",
            "«Конфиденциальность и безопасность»",
            "«Подтвердить вход»",
            "«Разрешить»",
            "доступ к рабочему столу — нажмите «Разрешить», это нужно только для ярлыка",
            "вымышленных данных",
        ):
            assert words in text, (name, words)
        assert text.index("«Терминал»") < text.index("Если с командой не вышло")
    message = (PACKAGING / "message-client-mac.txt").read_text("utf-8")
    assert "curl -fsSL __АДРЕС__/install-mac.sh | bash" in message


def test_mac_wheels_target_oldest_supported_macos(tmp_path, monkeypatch):
    """Сборка идёт на новой macOS, а клиент может сидеть на 14: колёса выбираются под 14."""
    calls = []
    monkeypatch.setattr(build, "OUT", tmp_path)
    monkeypatch.setattr(build, "run", lambda *a, **kw: calls.append((a, kw)))
    (tmp_path / "requirements.txt").touch()
    build.install_deps(MAC)
    args, kwargs = calls[-1]
    i = args.index("--python-platform")
    assert args[i + 1] == "aarch64-apple-darwin"
    assert kwargs["env"]["MACOSX_DEPLOYMENT_TARGET"] == "14.0"


arm64_mac = pytest.mark.skipif(
    sys.platform != "darwin" or os.uname().machine != "arm64",
    reason="установщик сначала проверяет, что это Mac на Apple Silicon",
)


@contextlib.contextmanager
def serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def answer(body: bytes, headers: dict | None = None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return Handler


def fake_zip(tmp_path: Path) -> bytes:
    pkg = tmp_path / "src" / "KadrovyAgent"
    fake_pkg(pkg)
    (pkg / "Запустить.command").write_text("#!/bin/bash\necho запущен новый\n")
    archive = tmp_path / "src.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", pkg, archive], check=True)
    return archive.read_bytes()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def install(tmp_path: Path, home: Path, port: int, agent_port: int | None = None):
    script = tmp_path / "install-mac.sh"
    url = f"http://127.0.0.1:{port}/kadrovyi-agent-mac.zip"
    script.write_text(INSTALLER.read_text("utf-8").replace("__ПОДСТАВИТЬ__", url))
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TA_OPEN_BROWSER": "0"}
    env |= {"TMPDIR": str(tmp_path), "TA_PORT": str(agent_port or free_port())}
    return subprocess.run(["bash", script], capture_output=True, text=True, env=env)


def old_install(home: Path) -> None:
    fake_pkg(home / "KadrovyAgent")
    (home / "KadrovyAgent" / "data").mkdir()
    (home / "KadrovyAgent" / "data" / "app.db").write_text("база клиента")


def temp_left(tmp_path: Path) -> list:
    return list(tmp_path.glob("kadrovyi-agent.*"))


@arm64_mac
def test_reinstall_keeps_every_previous_version(tmp_path):
    """Повторная установка: новая версия на месте, прежние — в KadrovyAgent.old-<время>,
    ни одна не удаляется, временные файлы убраны."""
    home = tmp_path / "home"
    old_install(home)
    (home / "KadrovyAgent.old-20260101-000000").mkdir()  # ещё более ранняя установка
    with serve(answer(fake_zip(tmp_path))) as port:
        out = install(tmp_path, home, port)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "запущен новый" in out.stdout
    olds = sorted(p.name for p in home.glob("KadrovyAgent.old-*"))
    assert len(olds) == 2 and "KadrovyAgent.old-20260101-000000" in olds
    assert (home / olds[-1] / "data" / "app.db").read_text() == "база клиента"
    assert f"Прежняя версия и её данные сохранены в {home / olds[-1]}" in out.stdout
    assert (home / "KadrovyAgent" / "Как запустить.txt").read_text() == "текст"
    assert not (home / "KadrovyAgent.new").exists() and not temp_left(tmp_path)


@arm64_mac
def test_broken_archive_leaves_installation_untouched(tmp_path):
    """Битый или недокачанный ZIP: прежняя установка с базой на месте, мусора нет."""
    home = tmp_path / "home"
    old_install(home)
    with serve(answer(b"not a zip")) as port:
        out = install(tmp_path, home, port)
    assert out.returncode != 0 and "Архив не распаковался" in out.stdout
    assert (home / "KadrovyAgent" / "data" / "app.db").read_text() == "база клиента"
    assert sorted(p.name for p in home.iterdir()) == ["KadrovyAgent"]
    assert not temp_left(tmp_path)


@arm64_mac
def test_running_agent_stops_the_installer(tmp_path):
    """Агент запущен: установщик просит закрыть его окно и ничего не трогает, иначе
    старый сервер остался бы работать из перенесённой папки."""
    home = tmp_path / "home"
    old_install(home)
    agent = answer(b"ok", {"X-Agent-Instance": "abc"})
    with serve(agent) as agent_port, serve(answer(fake_zip(tmp_path))) as port:
        out = install(tmp_path, home, port, agent_port - 3)  # агент на четвёртом порту из 11
    assert out.returncode == 1
    assert "Кадровый агент сейчас запущен. Закройте его окно в Терминале" in out.stdout
    assert sorted(p.name for p in home.iterdir()) == ["KadrovyAgent"]
