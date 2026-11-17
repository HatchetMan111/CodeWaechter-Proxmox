#!/usr/bin/env python3
"""
CodeWächter – Lokale Code-Review- & Planungs-App (Backend).

FastAPI-Anwendung: serviert die React/Vite-Oberfläche aus static/ und die
JSON-API unter /api/*. Läuft vollständig lokal – Analyse offline, optionaler
LLM-Endpoint nur auf explizite Konfiguration.
"""

import io
import json
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analyzer
import planner
from db import (APP_VERSION, DATA_DIR, PROJECTS_DIR, get_conn,
                get_setting, init_db, now_iso, set_setting)

from llm import test_connection

STATIC_DIR = Path(__file__).resolve().parent / "static"

UPLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200 MB – ZIP/Tarball eines Repos
CLONE_TIMEOUT = 300

app = FastAPI(title="CodeWächter", version=APP_VERSION, docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# ── Modelle ────────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str = ""
    source_type: str = "path"           # git | path | upload
    source_ref: str = ""                # Git-URL bzw. lokaler Pfad
    description: str = ""


class FindingPatch(BaseModel):
    status: str                         # offen | erledigt | ignoriert


class PlanPatch(BaseModel):
    done: bool


class SettingsPatch(BaseModel):
    # Optional[...] statt `str | None`: läuft unter Pydantic v1 UND v2
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    exclude_patterns: Optional[str] = None


class LlmTest(BaseModel):
    llm_base_url: str
    llm_api_key: str = ""
    llm_model: str


# ── Helfer ─────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-").lower()
    return slug or "projekt"


def _project_dir(pid: int, name: str) -> Path:
    return PROJECTS_DIR / f"{pid}-{_slugify(name)}"


def _get_project(conn, pid: int):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Projekt nicht gefunden.")
    return row


def _exclude_patterns(conn) -> list[str]:
    raw = get_setting(conn, "exclude_patterns")
    return [p for p in raw.split(",") if p.strip()]


def _llm_cfg(conn) -> dict:
    return {
        "base_url": get_setting(conn, "llm_base_url"),
        "api_key": get_setting(conn, "llm_api_key"),
        "model": get_setting(conn, "llm_model"),
    }


def _safe_member_path(member_name: str) -> Path:
    """Archiv-Eintrag gegen Path-Traversal absichern (Grube Zip-Slip)."""
    name = member_name.lstrip("/")
    if not name or name.startswith("~") or ":" in name.split("/")[0]:
        raise HTTPException(status_code=400,
                            detail=f"Unsicherer Archiv-Eintrag: {member_name}")
    path = Path(name)
    if ".." in path.parts:
        raise HTTPException(status_code=400,
                            detail=f"Unsicherer Archiv-Eintrag (..): {member_name}")
    return path


def _strip_common_root(members: list[str]) -> list[str]:
    """Wenn alle Einträge unter einem gemeinsamen Stammordner liegen
    (GitHub-Tarball-Muster repo-main/…), diesen abziehen."""
    firsts = {m.split("/")[0] for m in members if "/" in m}
    if len(firsts) == 1 and all(("/" in m) or m == "" for m in members):
        stripped = []
        for m in members:
            parts = m.split("/", 1)
            stripped.append(parts[1] if len(parts) == 2 else "")
        return stripped
    return members


def _extract_archive(data: bytes, filename: str, target: Path) -> int:
    """ZIP oder Tar.gz/tar nach target entpacken (traversal-sicher).
    Rückgabe: Anzahl entpackter Dateien."""
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            stripped = _strip_common_root([n for n in names if n.strip("/")])
            mapping = {}
            for orig, new in zip([n for n in names if n.strip("/")], stripped):
                if new.strip():
                    mapping[orig] = new
            for orig, new in mapping.items():
                dest = target / _safe_member_path(new)
                info = zf.getinfo(orig)
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(orig))
                    count += 1
    elif filename.lower().endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if filename.lower().endswith((".tar.gz", ".tgz")) else "r:"
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            stripped = _strip_common_root([m.name for m in members])
            for member, new_name in zip(members, stripped):
                if not new_name.strip():
                    continue
                dest = target / _safe_member_path(new_name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is not None:
                    dest.write_bytes(src.read())
                    count += 1
    else:
        raise HTTPException(status_code=400,
                            detail="Nur .zip, .tar.gz, .tgz oder .tar werden unterstützt.")
    return count


def _clone_repository(url: str, target: Path) -> None:
    """Git-Repo flach klonen (depth 1). Fehler mit voller Meldungskette."""
    if not re.match(r"^(https?://|git@ssh\.dev\.azure\.com:|git@[\w.-]+:)", url):
        raise HTTPException(
            status_code=400,
            detail=f"Ungültige Git-URL: {url} – erwartet https://… oder git@host:path",
        )
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(target)],
        capture_output=True, text=True, timeout=CLONE_TIMEOUT,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=f"git clone fehlgeschlagen (Exit {result.returncode}):\n"
                   f"stderr: {result.stderr.strip()[:1000]}\nstdout: {result.stdout.strip()[:300]}",
        )


# ── API: Status ────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    with get_conn() as conn:
        projects = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    return {"app": "CodeWächter", "version": APP_VERSION, "projects": projects,
            "data_dir": str(DATA_DIR)}


# ── API: Projekte ──────────────────────────────────────────────────────────

@app.get("/api/projects")
def api_projects():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM findings f WHERE f.project_id = p.id "
            " AND f.status = 'offen' AND f.severity = 'hoch') AS offen_hoch, "
            "(SELECT COUNT(*) FROM findings f WHERE f.project_id = p.id "
            " AND f.status = 'offen' AND f.severity = 'mittel') AS offen_mittel, "
            "(SELECT COUNT(*) FROM findings f WHERE f.project_id = p.id "
            " AND f.status = 'offen' AND f.severity = 'niedrig') AS offen_niedrig, "
            "(SELECT COUNT(*) FROM findings f WHERE f.project_id = p.id "
            " AND f.status = 'offen' AND f.severity = 'info') AS offen_info, "
            "(SELECT COUNT(*) FROM plan_items pi WHERE pi.project_id = p.id "
            " AND pi.run_id = (SELECT MAX(r.id) FROM runs r WHERE r.project_id = p.id)) AS plan_gesamt, "
            "(SELECT COUNT(*) FROM plan_items pi WHERE pi.project_id = p.id "
            " AND pi.run_id = (SELECT MAX(r.id) FROM runs r WHERE r.project_id = p.id) "
            " AND pi.done = 1) AS plan_erledigt "
            "FROM projects p ORDER BY p.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/projects")
def api_project_create(body: ProjectCreate):
    source_type = body.source_type
    if source_type not in ("git", "path", "upload"):
        raise HTTPException(status_code=400,
                            detail=f"Unbekannter Quell-Typ: {source_type}")
    with get_conn() as conn:
        pid = conn.execute(
            "INSERT INTO projects(name, source_type, source_ref, description, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (body.name or "Neues Projekt", source_type, body.source_ref,
             body.description, now_iso()),
        ).lastrowid
        conn.commit()

    # Verzeichnis erst nach der DB-Zeile anlegen (damit wir die ID haben)…
    target = _project_dir(pid, body.name or f"projekt-{pid}")
    local_path = ""
    try:
        if source_type == "git":
            if not body.source_ref.strip():
                raise HTTPException(status_code=400, detail="Git-URL fehlt.")
            _clone_repository(body.source_ref.strip(), target)
            local_path = str(target)
            name = body.name or target.name.split("-", 1)[-1]
        elif source_type == "path":
            src = Path(body.source_ref.strip() or "")
            if not src.is_dir():
                raise HTTPException(status_code=400,
                                    detail=f"Pfad existiert nicht oder ist kein Verzeichnis: {body.source_ref}")
            local_path = str(src.resolve())
            name = body.name or src.name
        else:  # upload
            name = body.name or f"upload-{pid}"
            target.mkdir(parents=True, exist_ok=True)
            local_path = str(target)
    except HTTPException:
        with get_conn() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
            conn.commit()
        shutil.rmtree(target, ignore_errors=True)
        raise
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
            conn.commit()
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(status_code=500,
                            detail=f"Projekt-Quelle konnte nicht eingerichtet werden: "
                                   f"{type(exc).__name__}: {exc}") from exc

    with get_conn() as conn:
        conn.execute("UPDATE projects SET name = ?, local_path = ? WHERE id = ?",
                     (name, local_path, pid))
        conn.commit()
        row = _get_project(conn, pid)
        return dict(row)


@app.post("/api/projects/{pid}/upload")
def api_project_upload(pid: int, file: UploadFile = File(...)):
    with get_conn() as conn:
        row = _get_project(conn, pid)
        current_path = row["local_path"]
    data = file.file.read(UPLOAD_MAX_BYTES + 1)
    if len(data) > UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Datei größer als {UPLOAD_MAX_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei hochgeladen.")
    # Nur VERWALTETE Verzeichnisse (unter data/projects/) dürfen geleert werden –
    # bei Pfad-Projekten würde sonst das externe Quellverzeichnis zerstört!
    target = Path(current_path) if current_path and \
        Path(current_path).resolve().is_relative_to(PROJECTS_DIR.resolve()) \
        else _project_dir(pid, row["name"])
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    try:
        count = _extract_archive(data, file.filename or "", target)
    except HTTPException:
        raise
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"Archiv kaputt oder nicht lesbar: {type(exc).__name__}: {exc}") from exc
    if count == 0:
        raise HTTPException(status_code=400,
                            detail="Archiv enthält keine auspackbaren Dateien.")
    with get_conn() as conn:
        conn.execute("UPDATE projects SET local_path = ? WHERE id = ?",
                     (str(target), pid))
        conn.commit()
    return {"ok": True, "dateien": count, "ziel": str(target)}


@app.get("/api/projects/{pid}")
def api_project_detail(pid: int):
    with get_conn() as conn:
        project = dict(_get_project(conn, pid))
        run = conn.execute(
            "SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        project["letzter_lauf"] = dict(run) if run else None
        if run:
            project["letzter_lauf"]["counts"] = json.loads(run["counts_json"])
            project["letzter_lauf"]["languages"] = json.loads(run["languages_json"])
            del project["letzter_lauf"]["counts_json"]
            del project["letzter_lauf"]["languages_json"]
        return project


@app.delete("/api/projects/{pid}")
def api_project_delete(pid: int):
    with get_conn() as conn:
        row = _get_project(conn, pid)
        local_path = row["local_path"]
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        conn.commit()
    # Externe Pfade (z. B. /root/repos/…) bleiben unangetastet – nur
    # verwaltete Verzeichnisse unter data/projects/ werden gelöscht.
    if local_path and Path(local_path).resolve().is_relative_to(PROJECTS_DIR.resolve()):
        shutil.rmtree(local_path, ignore_errors=True)
    return {"ok": True}


# ── API: Analyse ───────────────────────────────────────────────────────────

@app.post("/api/projects/{pid}/analyze")
def api_project_analyze(pid: int):
    with get_conn() as conn:
        row = _get_project(conn, pid)
        local_path = row["local_path"]
        project_name = row["name"]
    if not local_path or not Path(local_path).is_dir():
        raise HTTPException(status_code=400,
                            detail=f"Projekt-Verzeichnis fehlt: {local_path}\n"
                                   f"Lösung: bei Upload-Projekten zuerst eine Archiv-Datei hochladen.")

    with get_conn() as conn:
        exclude = _exclude_patterns(conn)
        llm_cfg = _llm_cfg(conn)

    result = analyzer.analyze_project(Path(local_path), exclude)

    # Bestehende Erledigt-Markierungen für Fortschritts-Übernahme einsammeln
    with get_conn() as conn:
        old_plan = conn.execute(
            "SELECT title, priority, done FROM plan_items WHERE project_id = ? "
            "ORDER BY id DESC LIMIT 200", (pid,),
        ).fetchall()

    steps, plan_source = planner.build_plan(result["findings"], project_name, llm_cfg)
    planner.carry_over_done(steps, [dict(o) for o in old_plan])

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO runs(project_id, started_at, files, lines, score, "
            "plan_source, counts_json, languages_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, now_iso(), result["stats"]["files"], result["stats"]["lines"],
             result["score"], plan_source, json.dumps(result["counts"]),
             json.dumps(result["stats"]["languages"])),
        )
        run_id = cur.lastrowid
        for f in result["findings"]:
            conn.execute(
                "INSERT INTO findings(project_id, run_id, rule, severity, path, "
                "line, title, detail) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, run_id, f["rule"], f["severity"], f["path"], f["line"],
                 f["title"], f["detail"]),
            )
        for pos, step in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO plan_items(project_id, run_id, position, title, "
                "detail, priority, done) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (pid, run_id, pos, step["title"], step["detail"],
                 step["priority"], step.get("done", 0)),
            )
        conn.execute("UPDATE projects SET score = ?, last_run_at = ? WHERE id = ?",
                     (result["score"], now_iso(), pid))
        conn.commit()
        return {"run_id": run_id, "score": result["score"],
                "counts": result["counts"], "stats": result["stats"],
                "plan": [{"id": None, "position": i, **s} for i, s in enumerate(steps, start=1)],
                "plan_source": plan_source}


# ── API: Findings ──────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/findings")
def api_findings(pid: int, severity: str = "", status: str = "offen"):
    with get_conn() as conn:
        _get_project(conn, pid)
        sql = ("SELECT * FROM findings WHERE project_id = ?")
        params: list = [pid]
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if status and status != "alle":
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY CASE severity WHEN 'hoch' THEN 0 WHEN 'mittel' THEN 1 " \
               "WHEN 'niedrig' THEN 2 ELSE 3 END, path, line"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


@app.patch("/api/findings/{fid}")
def api_finding_patch(fid: int, body: FindingPatch):
    if body.status not in ("offen", "erledigt", "ignoriert"):
        raise HTTPException(status_code=400, detail=f"Unbekannter Status: {body.status}")
    with get_conn() as conn:
        cur = conn.execute("UPDATE findings SET status = ? WHERE id = ?",
                           (body.status, fid))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Finding nicht gefunden.")
        return {"ok": True}


# ── API: Plan ──────────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/plan")
def api_plan(pid: int):
    with get_conn() as conn:
        _get_project(conn, pid)
        run = conn.execute(
            "SELECT id FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        if not run:
            return []
        rows = conn.execute(
            "SELECT * FROM plan_items WHERE project_id = ? AND run_id = ? ORDER BY position",
            (pid, run["id"]),
        ).fetchall()
        return [dict(r) for r in rows]


@app.patch("/api/plan/{item_id}")
def api_plan_patch(item_id: int, body: PlanPatch):
    with get_conn() as conn:
        cur = conn.execute("UPDATE plan_items SET done = ? WHERE id = ?",
                           (1 if body.done else 0, item_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Plan-Schritt nicht gefunden.")
        return {"ok": True}


# ── API: Verlauf ───────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/runs")
def api_runs(pid: int):
    with get_conn() as conn:
        _get_project(conn, pid)
        rows = conn.execute(
            "SELECT id, started_at, files, lines, score, plan_source, counts_json "
            "FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT 50", (pid,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["counts"] = json.loads(d.pop("counts_json"))
            out.append(d)
        return out


# ── API: Einstellungen ─────────────────────────────────────────────────────

@app.get("/api/settings")
def api_settings():
    with get_conn() as conn:
        return {
            "llm_base_url": get_setting(conn, "llm_base_url"),
            "llm_api_key": get_setting(conn, "llm_api_key"),
            "llm_model": get_setting(conn, "llm_model"),
            "exclude_patterns": get_setting(conn, "exclude_patterns"),
        }


@app.put("/api/settings")
def api_settings_put(body: SettingsPatch):
    with get_conn() as conn:
        for key in ("llm_base_url", "llm_api_key", "llm_model", "exclude_patterns"):
            value = getattr(body, key)
            if value is not None:
                set_setting(conn, key, value.strip())
        conn.commit()
        return api_settings()


@app.post("/api/settings/test-llm")
def api_settings_test_llm(body: LlmTest):
    result = test_connection(body.llm_base_url, body.llm_api_key, body.llm_model)
    return JSONResponse(result)


# ── SPA (React/Vite-Dist) ──────────────────────────────────────────────────

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="spa")
