"""Портативная сборка «Кадрового агента»: `python packaging/build.py --target <цель>`.

win64 — Windows 10/11 x64, собирается на Windows (раннер GitHub Actions windows-latest),
нужны uv и Visual Studio. Итог — dist/kadrovyi-agent-win64.zip с «Запустить.bat».

mac-arm64 — macOS на Apple Silicon, собирается на таком же маке, нужен uv. Итог —
dist/kadrovyi-agent-mac.zip с «Запустить.command» и установщик dist/install-mac.sh.

Внутри архива папка KadrovyAgent: свой Python, зависимости, код, веса модели поиска,
демо-база и файл запуска.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
OUT = ROOT / "dist"
PKG = OUT / "KadrovyAgent"
PBS_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"


@dataclass(frozen=True)
class Target:
    triple: str  # платформа в именах сборок python-build-standalone
    python: str  # интерпретатор внутри KadrovyAgent
    site: str  # site-packages внутри KadrovyAgent
    scripts: str  # куда uv и pip кладут скрипты пакетов
    archive: str
    launcher: dict  # файл в packaging/ → имя в архиве

    @property
    def asset(self) -> re.Pattern:
        return re.compile(rf"cpython-3\.12\.\d+\+\d+-{self.triple}-install_only_stripped\.tar\.gz")


TARGETS = {
    "win64": Target(
        "x86_64-pc-windows-msvc",
        "python/python.exe",
        "python/Lib/site-packages",
        "python/Scripts",
        "kadrovyi-agent-win64.zip",
        {"launcher.bat": "Запустить.bat", "readme-client.txt": "Как запустить.txt"},
    ),
    "mac-arm64": Target(
        "aarch64-apple-darwin",
        "python/bin/python3.12",
        "python/lib/python3.12/site-packages",
        "python/bin",
        "kadrovyi-agent-mac.zip",
        {
            "launcher.command": "Запустить.command",
            "readme-client-mac.txt": "Как запустить.txt",
            "readme-own-data.txt": "Свои данные.txt",
        },
    ),
}
VSWHERE = (
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio/Installer/vswhere.exe"
)
# Рантайм VC++ рядом с python.exe: без него на чистой Windows torch не импортируется
RUNTIME = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcomp140.dll")
# Самое старое колесо в uv.lock — torch 2.14 macosx_14_0_arm64. Без явной цели uv на
# новой macOS взял бы колёса под неё (orjson macosx_15_0), и у клиента на 14 не импортировалось бы
MACOS = "14.0"
MODEL_LIMIT = 600 * 2**20  # веса BERTA fp32 — около 490 МБ; больше — в сборке остались дубли
# Скрипты python/bin/* (uvicorn, alembic…) после uv pip install начинаются с абсолютного
# пути к интерпретатору на машине сборки; меняем его на python3.12 рядом со скриптом.
RELOCATABLE = """#!/bin/sh
'''exec' "$(dirname -- "$(realpath -- "$0")")"/'python3.12' "$0" "$@"
' '''
"""
# Нужно только для сборки расширений и установки пакетов: заголовки C++, cmake и компилятор
# protobuf из torch, pip. setuptools не трогаем — pymorphy2 (через natasha) импортирует
# pkg_resources. torch/bin/torch_shm_manager остаётся: им torch делит память между процессами.
PRUNE = ("torch/include", "torch/share", "torch/bin/protoc*", "pip", "pip-*.dist-info")
# Скрипты, которые приложение не вызывает: pip и консольная magika на Rust (26 МБ) —
# markitdown зовёт magika как библиотеку Python, а та работает через onnxruntime.
PRUNE_SCRIPTS = ("pip*", "magika*")
INSTALLER = "install-mac.sh"  # curl -fsSL <адрес>/install-mac.sh | bash


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def run(*args, **kwargs) -> None:
    print(" ", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def env(**extra) -> dict:
    return {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TA_DATA_DIR": str(PKG / "data"),
        "TA_MODELS_DIR": str(PKG / "data" / "models"),
        "TA_ENV_FILE": os.devnull,
        **extra,
    }


def fetch_python(target: Target) -> None:
    step("Python 3.12 из python-build-standalone")
    request = urllib.request.Request(PBS_API)
    if token := os.environ.get("GH_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request) as r:
        release = json.load(r)
    asset = next(a for a in release["assets"] if target.asset.fullmatch(a["name"]))
    print(" ", asset["name"], flush=True)
    archive = OUT / asset["name"]
    urllib.request.urlretrieve(asset["browser_download_url"], archive)
    with tarfile.open(archive) as tar:
        tar.extractall(PKG, filter="data")  # внутри — папка python/
    archive.unlink()
    run(PKG / target.python, "--version")


def install_deps(target: Target) -> None:
    step("Зависимости из uv.lock, без dev-группы")
    requirements = OUT / "requirements.txt"
    run("uv", "export", "--frozen", "--no-dev", "--no-emit-project", "-o", requirements, cwd=ROOT)
    platform = ["--python-platform", target.triple] if target.triple.endswith("darwin") else []
    run(
        "uv", "pip", "install", "--python", PKG / target.python, "--break-system-packages",
        "--link-mode", "copy", *platform, "-r", requirements,
        env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": MACOS},
    )  # fmt: skip
    requirements.unlink()


def relocate_scripts(bin_dir: Path) -> None:
    """Первая строка скриптов в python/bin — абсолютный путь к python3.12 на машине
    сборки (длинный путь uv пишет через /bin/sh). У клиента такого пути нет: заменяем
    заголовок поиском интерпретатора рядом со скриптом."""
    python = bin_dir / "python3.12"
    headers = [f"#!{python}\n", f"#!/bin/sh\n'''exec' '{python}' \"$0\" \"$@\"\n' '''\n"]
    for script in bin_dir.iterdir():
        if script.is_symlink() or not script.is_file():
            continue
        data = script.read_bytes()
        for header in map(str.encode, headers):
            if data.startswith(header):
                script.write_bytes(RELOCATABLE.encode() + data[len(header) :])


def copy_code() -> None:
    step("Код приложения")
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("app", "migrations"):
        shutil.copytree(ROOT / name, PKG / name, ignore=skip)
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copy2(ROOT / name, PKG / name)


def copy_runtime(target: Target) -> None:
    step("Рантайм VC++ рядом с python.exe")
    vs = subprocess.run(
        [VSWHERE, "-latest", "-products", "*", "-property", "installationPath"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    redist = Path(vs) / "VC" / "Redist" / "MSVC"
    versions = sorted(
        (d for d in redist.iterdir() if re.fullmatch(r"[\d.]+", d.name)),
        key=lambda d: [int(x) for x in d.name.split(".")],
    )
    x64 = versions[-1] / "x64"
    print(" ", x64, flush=True)
    for dll in RUNTIME:
        [found] = [p for p in x64.glob(f"Microsoft.VC*.*/{dll}") if "Debug" not in str(p)]
        shutil.copy2(found, (PKG / target.python).parent / dll)
        print(f"  {dll} ← {found.parent.name}", flush=True)


def fetch_model(target: Target) -> None:
    step("Веса модели поиска")
    python = PKG / target.python
    load = (
        "from sentence_transformers import SentenceTransformer as S;"
        "from app import config, embed;"
        "m = S(config.DEFAULTS['embed_model'], cache_folder=str(embed.models_dir())"
    )
    run(python, "-c", load + ")", cwd=PKG, env=env())
    models = PKG / "data" / "models"
    size = flatten_cache(models)
    print(f"  веса и настройки модели: {size / 2**20:.0f} МБ", flush=True)
    # Второй раз — без сети и только из папки: так модель грузится у клиента
    check = load + ", local_files_only=True); print('  вектор:', m.encode(['литьё']).shape)"
    run(python, "-c", check, cwd=PKG, env=env(HF_HUB_OFFLINE="1"))


def writable(func, path, _exc) -> None:
    """Блоб весов в кэше — «только чтение», и Windows его не удаляет: снимаем и повторяем."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def flatten_cache(models: Path) -> int:
    """Кэш Hugging Face держит файлы в blobs/, а в snapshots/ — ссылки на них. ZIP ссылок
    не хранит и положил бы каждый файл дважды-трижды: ссылки заменяем самими файлами,
    blobs/ убираем. Загрузка с local_files_only идёт через refs/ и snapshots/."""
    for link in [p for p in models.rglob("*") if p.is_symlink()]:
        target = link.resolve()
        link.unlink()
        shutil.copy2(target, link)
        os.chmod(link, 0o644)  # у блоба «только чтение», с такого файла карантин не снимается
    for junk in [p for p in models.rglob("*") if p.is_dir() and p.name in ("blobs", ".locks")]:
        shutil.rmtree(junk, onexc=writable)
    if left := [p for p in models.rglob("blobs") if p.is_dir()]:
        sys.exit(f"Не удалось убрать кэш весов, он лёг бы в ZIP второй раз: {left}")
    size = sum(p.stat().st_size for p in models.rglob("*") if p.is_file())
    if size > MODEL_LIMIT:
        sys.exit(f"Папка модели — {size / 2**20:.0f} МБ, ждали меньше 600: в ней дубли весов")
    return size


def build_demo(target: Target) -> None:
    step("Демо-база на записанных ответах")
    run(PKG / target.python, "-m", "app.demo", cwd=PKG, env=env(HF_HUB_OFFLINE="1"))


def clean() -> None:
    step("Уборка")
    for junk in [*PKG.rglob("__pycache__"), *PKG.rglob(".git"), PKG / "tests"]:
        if junk.is_dir():
            shutil.rmtree(junk)
    for name in ("app.db-wal", "app.db-shm"):
        (PKG / "data" / name).unlink(missing_ok=True)


def prune(pkg: Path, target: Target) -> None:
    step("Файлы, которые рантайму не нужны")
    site = pkg / target.site
    scripts = pkg / target.scripts
    found = [p for pattern in PRUNE for p in site.glob(pattern)]
    for path in found + [p for pattern in PRUNE_SCRIPTS for p in scripts.glob(pattern)]:
        print(f"  {path.relative_to(pkg).as_posix()}", flush=True)
        shutil.rmtree(path) if path.is_dir() else path.unlink()


def launcher(target: Target) -> None:
    step(" и ".join(f"«{name}»" for name in target.launcher.values()))
    for source, name in target.launcher.items():
        text = (HERE / source).read_text("utf-8").replace("\r\n", "\n")
        path = PKG / name
        if name.endswith(".bat"):  # cmd BOM не понимает — у .bat его нет
            path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        elif target.python.endswith(".exe"):  # Блокнот узнаёт UTF-8 по BOM
            path.write_bytes(text.replace("\n", "\r\n").encode("utf-8-sig"))
        else:
            path.write_bytes(text.encode("utf-8"))
        if name.endswith(".command"):
            path.chmod(0o755)


def leaked_paths(pkg: Path, needle: str) -> list[Path]:
    """Файлы сборки, где записан путь машины сборки: у клиента его нет."""
    found = needle.encode()
    files = [p for p in pkg.rglob("*") if p.is_file() and not p.is_symlink()]
    return [p for p in files if found in p.read_bytes()]


def check_paths() -> None:
    step("Пути машины сборки")
    if leaked := leaked_paths(PKG, str(Path.home())):
        sys.exit(f"В сборке остался путь {Path.home()}: {[str(p) for p in leaked[:20]]}")
    print("  не найдены", flush=True)


def pack_zip(archive: Path) -> None:
    step("ZIP")
    assert not [p for p in PKG.rglob("*") if p.is_symlink()], "в сборке остались ссылки"
    files = sorted(p for p in PKG.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in files)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in files:  # не-ASCII имена zipfile пишет в UTF-8 с флагом 0x800
            z.write(path, "KadrovyAgent/" + path.relative_to(PKG).as_posix())
    with zipfile.ZipFile(archive) as z:
        info = z.getinfo("KadrovyAgent/Запустить.bat")
        assert info.flag_bits & 0x800, "имя «Запустить.bat» записано не в UTF-8"
        assert z.testzip() is None
    print(f"  папка: {size / 2**20:.0f} МБ, файлов: {len(files)}")
    print(f"  ZIP: {archive.stat().st_size / 2**20:.0f} МБ")


def pack_mac(target: Target, archive: Path) -> None:
    step("ZIP через ditto")
    for link in [p for p in PKG.rglob("*") if p.is_symlink()]:  # python3 → python3.12 и т. п.
        assert not os.readlink(link).startswith("/"), f"абсолютная ссылка: {link}"
    files = [p for p in PKG.rglob("*") if p.is_file() and not p.is_symlink()]
    size = sum(p.stat().st_size for p in files)
    # ditto, в отличие от zipfile, хранит права и ссылки, а «Архиватор» их восстанавливает:
    # «Запустить.command» и python3.12 остаются исполняемыми после двойного клика по ZIP
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", PKG, archive)
    check = OUT / "check"
    run("ditto", "-x", "-k", archive, check)
    for name in ("Запустить.command", target.python):
        assert os.access(check / "KadrovyAgent" / name, os.X_OK), f"{name} не исполняемый"
    shutil.rmtree(check)
    shutil.copy2(HERE / INSTALLER, OUT / INSTALLER)
    print(f"  папка: {size / 2**20:.0f} МБ, файлов: {len(files)}")
    print(f"  ZIP: {archive.stat().st_size / 2**20:.0f} МБ")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # консоль раннера — cp1252, а вывод по-русски
    parser = argparse.ArgumentParser(description="Портативная сборка «Кадрового агента»")
    parser.add_argument("--target", choices=TARGETS, required=True)
    name = parser.parse_args().target
    target = TARGETS[name]
    shutil.rmtree(OUT, ignore_errors=True)
    PKG.mkdir(parents=True)
    fetch_python(target)
    install_deps(target)
    copy_code()
    if name == "win64":
        copy_runtime(target)
    else:
        relocate_scripts((PKG / target.python).parent)
    fetch_model(target)
    build_demo(target)
    clean()
    prune(PKG, target)
    launcher(target)
    if name == "win64":
        pack_zip(OUT / target.archive)
    else:
        check_paths()
        pack_mac(target, OUT / target.archive)


if __name__ == "__main__":
    main()
