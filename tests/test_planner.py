"""Planner-Tests: deterministische Pläne, JSON-Extraktion, Fortschritt."""

from planner import build_plan, build_rule_plan, carry_over_done, _extract_json_array


def _finding(rule, severity, path="src/a.py", line=1):
    from analyzer import _title
    return {"rule": rule, "severity": severity, "path": path, "line": line,
            "title": _title(rule), "detail": ""}


def test_rule_plan_priorities():
    findings = [
        _finding("secret.aws-key", "hoch"),
        _finding("code.todo", "info"),
        _finding("code.bare-except", "mittel"),
    ]
    steps = build_rule_plan(findings)
    priorities = [s["priority"] for s in steps]
    assert priorities[0] == "P0"                      # hoch zuerst
    assert priorities[-1] == "P2"                     # Re-Run-Schritt zuletzt
    assert any("Secrets" in s["title"] for s in steps)
    assert any("Analyse erneut ausführen" in s["title"] for s in steps)


def test_rule_plan_groups_by_rule():
    findings = [_finding("code.todo", "info", path=p) for p in ("a.py", "b.py", "c.py")]
    steps = build_rule_plan(findings)
    todo_steps = [s for s in steps if "TODO" in s["title"]]
    assert len(todo_steps) == 1
    assert "3 Fundstellen" in todo_steps[0]["title"]


def test_build_plan_without_llm_is_rules():
    steps, source = build_plan([_finding("code.todo", "info")], "Testprojekt", None)
    assert source == "regeln"
    assert steps


def test_build_plan_llm_fallback_on_garbage(monkeypatch):
    import planner

    def broken_chat(**kwargs):
        raise RuntimeError("Endpoint tot")
    monkeypatch.setattr(planner, "chat_completion", broken_chat)
    steps, source = build_plan([_finding("secret.aws-key", "hoch")], "P",
                               {"base_url": "http://x", "model": "m"})
    assert source == "regeln-fallback"  # LLM war konfiguriert und ist ausgefallen
    assert any("AWS" in s["title"] for s in steps)


def test_build_plan_llm_fallback_on_unparseable(monkeypatch):
    import planner

    def nonsense_chat(**kwargs):
        return "Ich kann heute kein JSON liefern, sorry."
    monkeypatch.setattr(planner, "chat_completion", nonsense_chat)
    _, source = build_plan([_finding("code.todo", "info")], "P",
                           {"base_url": "http://x", "model": "m"})
    assert source == "regeln-fallback"


def test_build_plan_llm_success(monkeypatch):
    import json as _json

    import planner

    def fake_chat(**kwargs):
        return "```json\n" + _json.dumps([
            {"title": "Key rotieren", "detail": "AWS-Konsole", "priority": "P0"},
            {"title": "Env-Variable nutzen", "detail": "", "priority": "P1"},
        ]) + "\n```"
    monkeypatch.setattr(planner, "chat_completion", fake_chat)
    steps, source = build_plan([_finding("secret.aws-key", "hoch")], "P",
                               {"base_url": "http://x", "model": "m"})
    assert source == "llm"
    assert steps[0]["title"] == "Key rotieren"
    assert steps[0]["priority"] == "P0"
    # Re-Run-Schritt wird angehängt
    assert "Analyse erneut" in steps[-1]["title"]


def test_extract_json_array_tolerant():
    fenced = 'Hier der Plan:\n```json\n[{"title": "A", "priority": "P0"}]\n```\nViel Erfolg!'
    result = _extract_json_array(fenced)
    assert result and result[0]["title"] == "A" and result[0]["priority"] == "P0"
    plain = '[{"title":"B"}]'
    assert _extract_json_array(plain)[0]["title"] == "B"
    assert _extract_json_array("kein JSON hier") is None
    assert _extract_json_array('[]') is None  # leer → None


def test_carry_over_done():
    old = [
        {"title": "Secrets entfernen und rotieren: AWS-Zugangsschlüssel im Code (1 Fundstelle)", "priority": "P0", "done": 1},
        {"title": "anderer Schritt", "priority": "P1", "done": 0},
    ]
    new = [
        {"title": "Secrets entfernen und rotieren: AWS-Zugangsschlüssel im Code (1 Fundstelle)", "priority": "P0", "done": 0},
        {"title": "anderer Schritt", "priority": "P1", "done": 0},
        {"title": "Analyse erneut ausführen (Re-Run) und Ergebnis prüfen", "priority": "P2", "done": 0},
    ]
    carry_over_done(new, old)
    assert new[0]["done"] == 1
    assert new[1]["done"] == 0
