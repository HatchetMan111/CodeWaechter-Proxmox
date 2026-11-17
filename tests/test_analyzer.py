"""Analyzer-Tests: Regel-Engine auf synthetischen Projekten."""

from analyzer import analyze_project


def _rules(findings):
    return {f["rule"] for f in findings}


def test_dirty_project_core_rules(sample_project):
    result = analyze_project(sample_project)
    rules = _rules(result["findings"])

    # Secrets (hoch)
    assert "secret.aws-key" in rules
    assert "secret.zuweisung" in rules          # password = "sup3rs3cret42"
    assert "secret.github-token" in rules       # ghp_…
    # Python-AST-Regeln
    assert "code.eval-python" in rules
    assert "code.bare-except" in rules
    assert "code.shell-true" in rules
    assert "code.sql-fstring" in rules
    assert "code.lange-funktion" in rules
    assert "code.debug-python" in rules
    # JS-Regeln
    assert "code.debug-console" in rules
    assert "code.eval-js" in rules
    assert "code.debug-js" in rules
    # Shell
    assert "code.eval-shell" in rules
    # Projekt-Level
    assert "datei.node_modules" in rules
    assert "datei.env" in rules
    assert "projekt.keine-tests" in rules
    # README vorhanden → kein Finding
    assert "projekt.kein-readme" not in rules


def test_dirty_project_severity_and_score(sample_project):
    result = analyze_project(sample_project)
    assert result["counts"]["hoch"] >= 3
    assert result["counts"]["mittel"] >= 5
    assert result["score"] < 50  # mehrere hoch → kräftiger Abzug
    assert 0 <= result["score"] <= 100
    # 5 gescannte Dateien (node_modules zählt als ignoriertes Verzeichnis)
    assert result["stats"]["files"] == 5
    assert result["stats"]["ignored_dir_files"] == 1


def test_findings_have_paths_and_lines(sample_project):
    result = analyze_project(sample_project)
    main_py = [f for f in result["findings"] if f["path"].endswith("src/main.py")]
    assert main_py, "Findings für src/main.py erwartet"
    aws = [f for f in main_py if f["rule"] == "secret.aws-key"]
    assert aws and aws[0]["line"] == 3
    assert aws[0]["severity"] == "hoch"


def test_clean_project_scores_100(clean_project):
    result = analyze_project(clean_project)
    assert result["findings"] == []
    assert result["score"] == 100


def test_exclude_patterns(clean_project):
    result = analyze_project(clean_project, exclude_patterns=["src/*"])
    # src/ ausgeschlossen → nur tests/ + README bleiben
    assert result["stats"]["files"] == 2
    assert all(not f["path"].startswith("src/") for f in result["findings"])
    assert "projekt.keine-tests" not in _rules(result["findings"])


def test_sql_concat_rule(tmp_path):
    root = tmp_path / "sqlproj"
    (root / "tests").mkdir(parents=True)
    (root / "db.py").write_text(
        "def q(conn, uid):\n"
        "    conn.execute(\"SELECT * FROM t WHERE id = \" + uid)\n"
    )
    (root / "tests" / "test_dummy.py").write_text("def test_x():\n    assert True\n")
    (root / "README.md").write_text("# x\n")
    result = analyze_project(root)
    assert "code.sql-konkatenation" in _rules(result["findings"])


def test_placeholder_secrets_ignored(tmp_path):
    root = tmp_path / "placeproj"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(
        "API_KEY = \"YOUR_API_KEY_HERE\"\n"
        "password = \"changeme\"\n"
    )
    (root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    result = analyze_project(root)
    assert "secret.zuweisung" not in _rules(result["findings"])


def test_max_findings_per_rule_path(sample_project):
    result = analyze_project(sample_project)
    per = {}
    for f in result["findings"]:
        per[(f["rule"], f["path"])] = per.get((f["rule"], f["path"]), 0) + 1
    assert all(v <= 3 for v in per.values())
