#!/usr/bin/env python3
"""
CodeWächter – Plan-Generator.

Das Traycer-Kernstück: macht aus den Findings eines Analyse-Laufs eine
priorisierte Implementierungs-Checkliste (P0 = sofort, P1 = demnächst,
P2 = Hygiene). Ohne LLM deterministisch aus Regel-Gruppen; mit konfiguriertem
OpenAI-kompatiblem Endpoint (Ollama, ZCodeAI-Server …) optional frei
formuliert – bei LLM-Fehler greift der deterministische Plan (nie ein Crash).
"""

import json
import re
from collections import defaultdict

from llm import chat_completion

PRIORITY_BY_SEVERITY = {"hoch": "P0", "mittel": "P1", "niedrig": "P2", "info": "P2"}

KATEGORIE_TITEL = {
    "secret.": "Secrets entfernen und rotieren",
    "datei.": "Eingecheckte Dateien aufräumen",
    "code.": "Code-Qualität verbessern",
    "projekt.": "Projekt-Grundausstattung",
}

FALLBACK_TIPPS = {
    "secret.aws-key": "Key in der AWS-Konsole deaktivieren/rotieren, dann aus dem Code entfernen (Umgebungskonfiguration nutzen).",
    "secret.github-token": "Token auf GitHub widerrufen (Settings → Developer settings), dann aus dem Code entfernen.",
    "secret.anthropic-key": "Key widerrufen und neu erzeugen; Code liest ihn künftig aus einer Umgebungsvariable.",
    "secret.private-key": "Schlüssel als kompromittiert behandeln: neu erzeugen, im Code durch Pfad-/Env-Referenz ersetzen.",
    "secret.db-url": "Zugangsdaten in eine Konfigurationsdatei/Env-Variable auslagern; Passwort wechseln.",
    "secret.webhook": "Webhook in Slack neu erzeugen und aus dem Code entfernen.",
    "secret.zuweisung": "Literal durch Umgebungsvariable ersetzen (z. B. os.environ), Defaultwerte nur ohne Geheimcharakter.",
    "secret.bearer": "Token aus dem Code entfernen und über eine Umgebungsvariable/Config bereitstellen.",
    "datei.privater-schluessel": "Datei aus dem Repo entfernen (git rm --cached), .gitignore ergänzen, Schlüssel bei Bedarf rotieren.",
    "datei.env": "Datei aus dem Repo entfernen und in .gitignore aufnehmen; eine .env.example ohne echte Werte committen.",
    "datei.keyfile": "Datei aus dem Repo entfernen und in .gitignore aufnehmen.",
    "datei.node_modules": "node_modules aus dem Repo entfernen und in .gitignore eintragen (git rm -r --cached node_modules).",
    "datei.venv": "venv aus dem Repo entfernen und in .gitignore eintragen.",
    "datei.pycache": "__pycache__ und *.pyc aus dem Repo entfernen, .gitignore ergänzen.",
    "datei.dsstore": ".DS_Store entfernen und in .gitignore eintragen (z. B. via `git rm --cached`).",
    "datei.riesig": "Datei prüfen: gehört sie ins Repo? Falls ja, komprimieren oder splitten.",
    "datei.riesig-zeilen": "Datei in kleinere Module aufteilen oder generiertes Material kennzeichnen.",
    "code.debug-python": "Debugger-Aufrufe entfernen; falls gewünscht, über Logging bzw. Umgebungsvariable steuern.",
    "code.debug-console": "console.log-Aufrufe entfernen oder über ein Log-Level-Flag steuern.",
    "code.debug-js": "debugger-Anweisung entfernen.",
    "code.eval-python": "eval()/exec() durch sichere Alternativen ersetzen (Parsing, Mapping-Dicts, ast.literal_eval).",
    "code.eval-js": "eval()/new Function() durch sichere Alternativen ersetzen (JSON.parse, Lookup-Tabellen).",
    "code.eval-shell": "eval bzw. curl|sh durch explizite Befehle ersetzen; Eingaben nicht ungeprüft an die Shell geben.",
    "code.shell-true": "shell=True vermeiden: Argumentliste übergeben (shell=False) oder Eingaben strikt validieren.",
    "code.bare-except": "Konkrete Exception-Klassen fangen; übergeordnete Fehler nicht still verschlucken.",
    "code.except-pass": "Fehler zumindest loggen oder gezielt behandeln statt `pass`.",
    "code.sql-fstring": "Parameterisierte Abfragen nutzen (z. B. execute(sql, params)) statt f-Strings.",
    "code.sql-konkatenation": "Parameterisierte Abfragen nutzen statt String-Konkatenation.",
    "code.lange-funktion": "Funktion in kleinere, testbare Teile zerlegen.",
    "code.harte-ip": "IP in Konfiguration/Env-Variable auslagern (Portabilität und Testbarkeit).",
    "code.todo": "Todos sichten: umsetzen, als Issue erfassen oder entfernen.",
    "projekt.keine-tests": "Testgerüst aufbauen (mind. Smoke-Tests für Kernpfade) und in CI einbinden.",
    "projekt.kein-readme": "README mit Zweck, Start und Konfiguration ergänzen.",
}


def _kategorie_for(rule: str) -> str:
    prefix = rule.rsplit(".", 1)[0] + "."
    return KATEGORIE_TITEL.get(prefix, "Nacharbeiten")


def _short_files(paths: list[str], limit: int = 5) -> str:
    shown = paths[:limit]
    suffix = f" … (+{len(paths) - limit} weitere)" if len(paths) > limit else ""
    return ", ".join(shown) + suffix


def build_rule_plan(findings: list[dict]) -> list[dict]:
    """Deterministischer Plan: eine Gruppe pro Regel, priorisiert nach Schweregrad."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        groups[f["rule"]].append(f)

    def severity_rank(rule: str) -> int:
        sev = groups[rule][0]["severity"]
        return {"hoch": 0, "mittel": 1, "niedrig": 2, "info": 3}.get(sev, 1)

    steps: list[dict] = []
    for rule in sorted(groups, key=lambda r: (severity_rank(r), r)):
        items = groups[rule]
        sev = items[0]["severity"]
        prio = PRIORITY_BY_SEVERITY.get(sev, "P1")
        paths = sorted({f["path"] for f in items if f["path"]})
        lines = sorted({f["line"] for f in items if f.get("line")})
        detail = FALLBACK_TIPPS.get(rule, "")
        if paths:
            loc = _short_files(paths)
            if lines:
                loc += f" (Zeilen {', '.join(str(l) for l in lines[:5])} …)"
            detail = f"Betroffen: {loc}. {detail}".strip()
        steps.append({
            "title": f"{_kategorie_for(rule)}: {_title_of(items)} ({len(items)} Fundstelle{'n' if len(items) != 1 else ''})",
            "detail": detail,
            "priority": prio,
        })
    steps.append({
        "title": "Analyse erneut ausführen (Re-Run) und Ergebnis prüfen",
        "detail": "Nach den Umsetzungen den CodeWächter-Lauf wiederholen – Score und offene Findings kontrollieren.",
        "priority": "P2",
    })
    return steps


def _title_of(items: list[dict]) -> str:
    return items[0]["title"]


def _extract_json_array(text: str) -> list[dict] | None:
    """Nimmt den LLM-Output und zieht das JSON-Schritte-Array heraus
    (tolerant gegenüber ```json-Code fences und Prosa drumherum)."""
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    start = candidate.find("[")
    end = candidate.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    steps = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        prio = str(entry.get("priority") or "P1").upper()
        steps.append({
            "title": title[:200],
            "detail": str(entry.get("detail") or "").strip()[:2000],
            "priority": prio if prio in ("P0", "P1", "P2") else "P1",
        })
    return steps or None


def build_plan(findings: list[dict], project_name: str,
               llm_cfg: dict | None = None) -> tuple[list[dict], str]:
    """Erzeugt den Plan. Rückgabe: (Schritte, Quelle) mit Quelle in
    {'regeln', 'llm'}. Mit LLM-Konfiguration wird zuerst der LLM-Plan versucht;
    schlägt der fehl (offline, falsches Format, Timeout), folgt der
    deterministische Plan – der LLM-Fehler wird als Schritt-Detail nicht,
    aber in run.plan_source bemerkbar; der Rückgabewert 'regeln-fallback'
    signalisiert es dem Aufrufer für den Log."""
    if llm_cfg and llm_cfg.get("base_url") and llm_cfg.get("model"):
        try:
            text = chat_completion(
                base_url=llm_cfg["base_url"],
                api_key=llm_cfg.get("api_key") or "",
                model=llm_cfg["model"],
                messages=[
                    {"role": "system", "content":
                        "Du bist ein erfahrener Code-Reviewer. Erstelle aus den "
                        "gegebenen Findings einen präzisen, priorisierten "
                        "Implementierungsplan AUF DEUTSCH. Antworte NUR mit einem "
                        "JSON-Array: [{\"title\": \"…\", \"detail\": \"…\", "
                        "\"priority\": \"P0|P1|P2\"}, …]. P0 = sofort (Sicherheit), "
                        "P1 = demnächst, P2 = Hygiene. Maximal 12 Schritte."},
                    {"role": "user", "content":
                        f"Projekt: {project_name}\n"
                        f"Findings (JSON):\n{json.dumps(findings[:60], ensure_ascii=False, indent=1)}"},
                ],
                timeout=90,
            )
            steps = _extract_json_array(text)
            if steps:
                steps.append({
                    "title": "Analyse erneut ausführen (Re-Run) und Ergebnis prüfen",
                    "detail": "Nach den Umsetzungen den CodeWächter-Lauf wiederholen.",
                    "priority": "P2",
                })
                return steps, "llm"
        except Exception:
            pass  # Fallback unten – die App darf nie am LLM hängen
    return build_rule_plan(findings), "regeln"


def carry_over_done(new_steps: list[dict], old_items: list[dict]) -> None:
    """Erledigt-Markierungen von Titel+Priorität auf den neuen Plan übertragen,
    damit ein Re-Run den Fortschritt nicht wegwirft."""
    done_keys = {(i["title"], i["priority"]) for i in old_items if i.get("done")}
    for step in new_steps:
        if (step["title"], step["priority"]) in done_keys:
            step["done"] = 1
