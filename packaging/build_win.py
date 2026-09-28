"""Портативная сборка «Кадрового агента» для Windows 10/11 x64.

Запуск на Windows (раннер GitHub Actions windows-latest) из корня репозитория:
`python packaging/build_win.py`. Нужны uv и установленная Visual Studio (на раннере
есть). Итог — dist/kadrovyi-agent-win64.zip с папкой KadrovyAgent внутри:
свой Python, зависимости, код, веса модели поиска, демо-база, «Запустить.bat».
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
OUT = ROOT / "dist"
PKG = OUT / "KadrovyAgent"
PYTHON = PKG / "python" / "python.exe"
ZIP = OUT / "kadrovyi-agent-win64.zip"
PBS_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
PBS_ASSET = re.compile(
    r"cpython-3\.12\.\d+\+\d+-x86_64-pc-windows-msvc-install_only_stripped\.tar\.gz"
)
VSWHERE = (
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio/Installer/vswhere.exe"
)
# Рантайм VC++ рядом с python.exe: без него на чистой Windows torch не импортируется
RUNTIME = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcomp140.dll")
LAUNCHER = {"launcher.bat": "Запустить.bat", "readme-client.txt": "Как запустить.txt"}


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


def fetch_python() -> None:
    step("Python 3.12 из python-build-standalone")
    request = urllib.request.Request(PBS_API)
    if token := os.environ.get("GH_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request) as r:
        release = json.load(r)
    asset = next(a for a in release["assets"] if PBS_ASSET.fullmatch(a["name"]))
    print(" ", asset["name"], flush=True)
    archive = OUT / asset["name"]
    urllib.request.urlretrieve(asset["browser_download_url"], archive)
    with tarfile.open(archive) as tar:
        tar.extractall(PKG, filter="data")  # внутри — папка python/
    archive.unlink()
    run(PYTHON, "--version")


def install_deps() -> None:
    step("Зависимости из uv.lock, без dev-группы")
    requirements = OUT / "requirements.txt"
    run("uv", "export", "--frozen", "--no-dev", "--no-emit-project", "-o", requirements, cwd=ROOT)
    run(
        "uv", "pip", "install", "--python", PYTHON, "--break-system-packages",
        "--link-mode", "copy", "-r", requirements,
    )  # fmt: skip
    requirements.unlink()


def copy_code() -> None:
    step("Код приложения")
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("app", "migrations"):
        shutil.copytree(ROOT / name, PKG / name, ignore=skip)
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copy2(ROOT / name, PKG / name)


def copy_runtime() -> None:
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
        shutil.copy2(found, PYTHON.parent / dll)
        print(f"  {dll} ← {found.parent.name}", flush=True)


def fetch_model() -> None:
    step("Веса модели поиска")
    load = (
        "from sentence_transformers import SentenceTransformer as S;"
        "from app import config, embed;"
        "m = S(config.DEFAULTS['embed_model'], cache_folder=str(embed.models_dir())"
    )
    run(PYTHON, "-c", load + ")", cwd=PKG, env=env())
    # Второй раз — без сети и только из папки: так модель грузится у клиента
    check = load + ", local_files_only=True); print('  вектор:', m.encode(['литьё']).shape)"
    run(PYTHON, "-c", check, cwd=PKG, env=env(HF_HUB_OFFLINE="1"))


def build_demo() -> None:
    step("Демо-база на записанных ответах")
    run(PYTHON, "-m", "app.demo", cwd=PKG, env=env(HF_HUB_OFFLINE="1"))


def clean() -> None:
    step("Уборка")
    for junk in [*PKG.rglob("__pycache__"), *PKG.rglob(".git"), PKG / "tests"]:
        if junk.is_dir():
            shutil.rmtree(junk)
    for name in ("app.db-wal", "app.db-shm"):
        (PKG / "data" / name).unlink(missing_ok=True)


def launcher() -> None:
    step("Запустить.bat и «Как запустить.txt»")
    for source, target in LAUNCHER.items():
        text = (HERE / source).read_text("utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
        # Блокнот узнаёт UTF-8 по BOM; cmd BOM не понимает — у .bat его нет
        encoding = "utf-8" if target.endswith(".bat") else "utf-8-sig"
        (PKG / target).write_bytes(text.encode(encoding))


def pack() -> None:
    step("ZIP")
    files = sorted(p for p in PKG.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in files)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in files:  # не-ASCII имена zipfile пишет в UTF-8 с флагом 0x800
            z.write(path, "KadrovyAgent/" + path.relative_to(PKG).as_posix())
    with zipfile.ZipFile(ZIP) as z:
        info = z.getinfo("KadrovyAgent/Запустить.bat")
        assert info.flag_bits & 0x800, "имя «Запустить.bat» записано не в UTF-8"
        assert z.testzip() is None
    print(f"  папка: {size / 2**20:.0f} МБ, файлов: {len(files)}")
    print(f"  ZIP: {ZIP.stat().st_size / 2**20:.0f} МБ")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # консоль раннера — cp1252, а вывод по-русски
    shutil.rmtree(OUT, ignore_errors=True)
    PKG.mkdir(parents=True)
    fetch_python()
    install_deps()
    copy_code()
    copy_runtime()
    fetch_model()
    build_demo()
    clean()
    launcher()
    pack()


if __name__ == "__main__":
    main()
