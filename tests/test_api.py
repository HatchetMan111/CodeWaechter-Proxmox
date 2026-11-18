"""API-Tests: Routen direkt aufgerufen (kein HTTP-Client nötig)."""

import pytest

from conftest import make_zip_bytes, upload_file

from main import (FindingPatch, PlanPatch, ProjectCreate, SettingsPatch,
                  api_finding_patch, api_findings, api_plan, api_plan_patch,
                  api_project_analyze, api_project_create, api_project_delete,
                  api_project_detail, api_project_upload, api_projects,
                  api_runs, api_settings, api_settings_put, api_status)
from fastapi import HTTPException


def _create_path_project(clean_db, tmp_path, name="PfadProjekt"):
    body = ProjectCreate(name=name, source_type="path", source_ref=str(tmp_path))
    return api_project_create(body)


def test_status(clean_db):
    status = api_status()
    assert status["app"] == "CodeWächter"
    assert "version" in status


def test_create_path_project(clean_db, tmp_path):
    project = _create_path_project(clean_db, tmp_path)
    assert project["id"] >= 1
    assert project["local_path"] == str(tmp_path)
    assert project["source_type"] == "path"


def test_create_path_project_missing_dir(clean_db):
    body = ProjectCreate(name="gibtsnicht", source_type="path",
                         source_ref="/gibts/nicht/xyz")
    with pytest.raises(HTTPException) as exc:
        api_project_create(body)
    assert exc.value.status_code == 400
    # Fehlgeschlagene Anlage räumt die DB-Zeile auf
    assert api_projects() == []


def test_full_flow_path_project(clean_db, sample_project):
    body = ProjectCreate(name="Dirty", source_type="path", source_ref=str(sample_project))
    project = api_project_create(body)
    pid = project["id"]

    result = api_project_analyze(pid)
    assert result["score"] < 100
    assert result["counts"]["hoch"] >= 1
    assert result["plan_source"] == "regeln"
    assert result["plan"], "Plan wurde erzeugt"

    findings = api_findings(pid)
    assert findings, "Findings vorhanden"
    assert all(f["status"] == "offen" for f in findings)

    # Status ändern
    api_finding_patch(findings[0]["id"], FindingPatch(status="erledigt"))
    after = api_findings(pid)
    assert all(f["status"] == "erledigt" for f in after if f["id"] == findings[0]["id"])

    # Plan abrufen und abhaken
    plan = api_plan(pid)
    assert plan
    api_plan_patch(plan[0]["id"], PlanPatch(done=True))
    plan_after = api_plan(pid)
    assert plan_after[0]["done"] == 1

    # Verlauf
    runs = api_runs(pid)
    assert len(runs) == 1
    assert runs[0]["score"] == result["score"]

    detail = api_project_detail(pid)
    assert detail["score"] == result["score"]
    assert detail["letzter_lauf"]["counts"]["hoch"] >= 1


def test_analyze_rerun_carries_done(clean_db, sample_project):
    body = ProjectCreate(name="ReRun", source_type="path", source_ref=str(sample_project))
    pid = api_project_create(body)["id"]

    api_project_analyze(pid)
    plan = api_plan(pid)
    assert plan, "Erster Plan vorhanden"
    api_plan_patch(plan[0]["id"], PlanPatch(done=True))

    api_project_analyze(pid)
    plan2 = api_plan(pid)
    assert plan2[0]["done"] == 1, "Erledigt-Markierung überstand den Re-Run"

    runs = api_runs(pid)
    assert len(runs) == 2


def test_rerun_scopes_findings_to_latest_run(clean_db, sample_project):
    """Re-Run darf Findings/Counts NICHT doppeln (Bug-Fix: alles bezieht sich
    auf den letzten Lauf)."""
    pid = api_project_create(
        ProjectCreate(name="Scope", source_type="path", source_ref=str(sample_project)))["id"]

    r1 = api_project_analyze(pid)
    n1 = len(api_findings(pid))
    assert n1 > 0

    # Einen alten Finding-Status ändern – darf den neuen Lauf nicht beeindrucken
    api_finding_patch(api_findings(pid)[0]["id"], FindingPatch(status="ignoriert"))

    r2 = api_project_analyze(pid)
    findings2 = api_findings(pid)
    assert len(findings2) == n1, "Re-Run doppelt die Findings nicht"
    assert all(f["run_id"] == r2["run_id"] for f in findings2)

    # Dashboard-Counts zählen nur den letzten Lauf
    p = [x for x in api_projects() if x["id"] == pid][0]
    assert p["offen_hoch"] == r2["counts"]["hoch"]
    assert p["offen_mittel"] == r2["counts"]["mittel"]


def test_upload_zip_flow(clean_db, sample_project):
    body = ProjectCreate(name="ZipProj", source_type="upload")
    project = api_project_create(body)
    pid = project["id"]

    zip_bytes = make_zip_bytes(sample_project)
    response = api_project_upload(pid, upload_file(zip_bytes, "proj-main.zip"))
    assert response["ok"] is True
    assert response["dateien"] >= 5

    # Gemeinsamer Stammordner wurde abgezogen → src/main.py direkt unter root
    from pathlib import Path
    assert (Path(response["ziel"]) / "src" / "main.py").exists()

    result = api_project_analyze(pid)
    assert result["score"] < 100

    api_project_delete(pid)
    assert all(p["id"] != pid for p in api_projects())
    assert not Path(response["ziel"]).exists()  # verwaltetes Verzeichnis mitgeräumt


def test_upload_rejects_traversal(clean_db, tmp_path):
    import io
    import zipfile

    project = api_project_create(
        ProjectCreate(name="EvilZip", source_type="upload"))["id"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("unter/ordner/ok.txt", "gut")
        zf.writestr("../escaped.txt", "boese")
    # Gemischte Wurzeln (kein gemeinsamer Stammordner) → ../-Eintrag muss
    # abgelehnt werden, nicht still abgestreift werden.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        api_project_upload(project, upload_file(buf.getvalue(), "evil.zip"))
    assert exc.value.status_code == 400


def test_upload_rejects_wrong_extension(clean_db):
    project = api_project_create(
        ProjectCreate(name="NoArch", source_type="upload"))["id"]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        api_project_upload(project, upload_file(b"nix", "datei.exe"))
    assert exc.value.status_code == 400


def test_analyze_empty_project_scores_100(clean_db):
    pid = api_project_create(ProjectCreate(name="Leer", source_type="upload"))["id"]
    result = api_project_analyze(pid)
    assert result["score"] == 100
    assert result["counts"]["hoch"] == 0


def test_analyze_missing_dir_fails(clean_db, tmp_path):
    pid = _create_path_project(clean_db, tmp_path, name="Weg")["id"]
    import db
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET local_path = '/gibts/nicht/xyz' WHERE id = ?", (pid,))
        conn.commit()
    with pytest.raises(HTTPException) as exc:
        api_project_analyze(pid)
    assert exc.value.status_code == 400


def test_clone_url_validation(clean_db):
    from main import _clone_repository
    from pathlib import Path
    with pytest.raises(HTTPException) as exc:
        _clone_repository("file:///etc/passwd", Path("/tmp/xyz-clone"))
    assert exc.value.status_code == 400


def test_settings_roundtrip(clean_db):
    settings = api_settings()
    assert "llm_base_url" in settings
    api_settings_put(SettingsPatch(llm_base_url="http://192.168.1.23:8000/v1",
                                   llm_model="qwen2.5"))
    after = api_settings()
    assert after["llm_base_url"] == "http://192.168.1.23:8000/v1"
    assert after["llm_model"] == "qwen2.5"
    assert after["llm_api_key"] == ""  # unverändert


def test_delete_path_project_keeps_external_dir(clean_db, tmp_path):
    project = _create_path_project(clean_db, tmp_path, name="Extern")
    pid = project["id"]
    api_project_delete(pid)
    assert tmp_path.exists(), "Externes Quellverzeichnis darf nicht gelöscht werden"
