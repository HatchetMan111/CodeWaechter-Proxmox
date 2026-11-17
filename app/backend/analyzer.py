#!/usr/bin/env python3
"""
CodeWächter – Statische Regel-Engine.

Läuft vollständig offline: Dateibaum einlesen, pro Sprache heuristische Regeln
anwenden, Findings mit Schweregrad produzieren und daraus einen Score 0-100
berechnen. Keine Cloud, keine externen Abhängigkeiten – nur Python-Stdlib.
"""

import ast
import fnmatch
import re
from pathlib import Path

# ── Konstanten ──────────────────────────────────────────────────────────────

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".env",
    "dist", "build", ".next", "target", ".idea", ".vscode", "coverage",
    ".tox", ".pytest_cache", ".mypy_cache", "site-packages", ".gradle",
    "vendor", ".cache", "Pods", ".terraform",
}

SCAN_MAX_BYTES = 512 * 1024          # Dateien > 512 KB inhaltlich überspringen
HUGE_FILE_BYTES = 1024 * 1024        # > 1 MB = eigenes Finding (mittel)
HUGE_FILE_LINES = 2000               # > 2000 Zeilen = eigenes Finding (niedrig)
LONG_FUNCTION_LINES = 80             # Python-Funktion > 80 Zeilen (niedrig)

MAX_FINDINGS_TOTAL = 400             # Hartes Limit pro Lauf
MAX_PER_RULE_PATH = 3                # Pro (Regel, Datei) – gegen Flut

SEVERITY_WEIGHT = {"hoch": 12, "mittel": 5, "niedrig": 1, "info": 0}

LANG_BY_EXT = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".sh": "shell", ".bash": "shell",
    ".go": "go", ".rs": "rust", ".java": "java", ".php": "php", ".rb": "ruby",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".md": "markdown", ".sql": "sql",
    ".xml": "xml", ".ini": "ini", ".cfg": "ini", ".txt": "text",
}

TEST_FILE_PATTERNS = [
    "test_*.py", "*_test.py", "conftest.py",
    "*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx",
    "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx",
]

PLACEHOLDER_VALUES = {
    "changeme", "dummy", "placeholder", "example", "todo", "fixme",
    "your-api-key", "your_api_key", "xxxx", "passwort", "password",
}

# ── Regex-Regeln (generisch über alle Sprachen) ─────────────────────────────

RE_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
RE_GH_TOKEN = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")
RE_GH_PAT = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")
RE_ANTHROPIC = re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")
RE_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
RE_DB_URL = re.compile(
    r"(?i)\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://[^\s@/]+:[^\s@]+@"
)
RE_SLACK_HOOK = re.compile(r"https://hooks\.slack\.com/services/T\w+/B\w+/\w+")
RE_ASSIGN_SECRET = re.compile(
    r"(?i)[\"']?(password|passwd|secret|token|api[_-]?key|access[_-]?key)[\"']?"
    r"\s*[=:]\s*[\"']([^\"']{8,})[\"']"
)
RE_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9\-_.=]{20,}")
RE_TODO = re.compile(r"(?i)\b(TODO|FIXME|HACK|XXX)\b")
RE_PY_DEBUG = re.compile(r"(?i)\b(breakpoint\s*\(\s*\)|pdb\.set_trace\s*\(\s*\)|import\s+pdb)")
RE_JS_CONSOLE = re.compile(r"\bconsole\.log\s*\(")
RE_JS_DEBUGGER = re.compile(r"^\s*debugger\s*;?\s*$", re.MULTILINE)
RE_JS_EVAL = re.compile(r"\beval\s*\(|new\s+Function\s*\(")
RE_SHELL_EVAL = re.compile(r"(?<![\w.-])eval\s+|\|\s*sudo\s+sh\b|curl[^|#]*\|\s*(ba)?sh\b")
RE_HARD_IP = re.compile(
    r"\b(?!127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.255)(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"
)
RE_SQL_FSTRING = re.compile(r"\b(?:execute|executemany|query)\s*\(\s*f[\"']", re.IGNORECASE)
RE_SQL_CONCAT = re.compile(
    r"(?i)\b(?:execute|executemany|query)\s*\(\s*[\"'][^\"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"']*[\"']\s*\+"
)
RE_ENV_ASSIGN = re.compile(r"(?i)\b(export\s+)?[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*\s*=")

RULE_META = {
    "secret.aws-key":        ("hoch",   "AWS-Zugangsschlüssel im Code"),
    "secret.github-token":   ("hoch",   "GitHub-Token im Code"),
    "secret.anthropic-key":  ("hoch",   "Anthropic-API-Key im Code"),
    "secret.private-key":    ("hoch",   "Privater Schlüssel im Code"),
    "secret.db-url":         ("hoch",   "Datenbank-URL mit Passwort im Code"),
    "secret.webhook":        ("hoch",   "Webhook-URL mit Token im Code"),
    "secret.zuweisung":      ("hoch",   "Hardcodierte Zugangsdaten-Zuweisung"),
    "secret.bearer":         ("mittel", "Bearer-Token hardcodiert"),
    "datei.privater-schluessel": ("hoch", "Schlüsseldatei eingecheckt"),
    "datei.env":             ("mittel", "Dotfile mit potenziellen Secrets eingecheckt"),
    "datei.keyfile":         ("mittel", "Zertifikats-/Schlüsseldatei eingecheckt"),
    "datei.node_modules":    ("hoch",   "node_modules eingecheckt"),
    "datei.venv":            ("mittel", "Virtuelle Python-Umgebung eingecheckt"),
    "datei.pycache":         ("mittel", "Python-Bytecode eingecheckt"),
    "datei.dsstore":         ("niedrig",".DS_Store eingecheckt"),
    "datei.riesig":          ("mittel", "Sehr große Datei im Repository"),
    "datei.riesig-zeilen":   ("niedrig", "Sehr lange Datei (viele Zeilen)"),
    "code.debug-python":     ("mittel", "Python-Debugger-Rest"),
    "code.debug-console":    ("niedrig","console.log im Code"),
    "code.debug-js":         ("mittel", "JS-debugger-Anweisung"),
    "code.eval-python":      ("mittel", "eval()/exec() in Python"),
    "code.eval-js":          ("mittel", "eval()/new Function() in JavaScript"),
    "code.eval-shell":       ("mittel", "eval / curl|sh in Shell-Script"),
    "code.shell-true":       ("mittel", "subprocess mit shell=True"),
    "code.bare-except":      ("mittel", "bare except (verschluckt Fehler)"),
    "code.except-pass":      ("mittel", "except Exception: pass (Fehler wird ignoriert)"),
    "code.sql-fstring":      ("mittel", "SQL als f-string ausgeführt (Injection-Risiko)"),
    "code.sql-konkatenation":("mittel", "SQL durch String-Konkatenation gebaut (Injection-Risiko)"),
    "code.lange-funktion":   ("niedrig","Sehr lange Funktion"),
    "code.harte-ip":         ("niedrig","Hardcodierte IP-Adresse"),
    "code.todo":             ("info",   "TODO/FIXME-Marker"),
    "projekt.keine-tests":   ("mittel", "Keine Tests im Projekt gefunden"),
    "projekt.kein-readme":   ("niedrig","Keine README-Datei"),
}


def _severity(rule: str) -> str:
    return RULE_META.get(rule, ("mittel", rule))[0]


def _title(rule: str) -> str:
    return RULE_META.get(rule, (rule, rule.replace(".", " ")))[1]


def _add(findings: list[dict], rule: str, path: str, line: int,
         detail: str, counters: dict) -> None:
    key = (rule, path)
    if counters.get(key, 0) >= MAX_PER_RULE_PATH:
        return
    if len(findings) >= MAX_FINDINGS_TOTAL:
        return
    counters[key] = counters.get(key, 0) + 1
    findings.append({
        "rule": rule,
        "severity": _severity(rule),
        "path": path,
        "line": line,
        "title": _title(rule),
        "detail": detail,
    })


def _is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return (
        v in PLACEHOLDER_VALUES
        or v.startswith(("your", "<", "xxx", "...", "$"))
        or "{{" in v
    )


def _iter_source_files(root: Path, exclude_patterns: list[str]):
    """Liefert ALLE Dateien (auch in ignorierten Verzeichnissen – die braucht
    die Projekt-Level-Regel „eingecheckte Artefakte“). Der Aufrufer entscheidet,
    welche Dateien inhaltlich gescannt werden."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pat.strip()) for pat in exclude_patterns if pat.strip()):
            continue
        yield rel, path


def _in_ignored_dir(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part in IGNORE_DIRS for part in parts[:-1])


def _scan_content(rel: str, text: str, lang: str, findings: list[dict],
                  counters: dict) -> None:
    lines = text.splitlines()

    for i, line in enumerate(lines, start=1):
        # Secrets (hoch/mittel)
        if RE_AWS_KEY.search(line):
            _add(findings, "secret.aws-key", rel, i, f"Zeile {i}: AWS-Key-Muster (AKIA…)", counters)
        if RE_GH_TOKEN.search(line) or RE_GH_PAT.search(line):
            _add(findings, "secret.github-token", rel, i, f"Zeile {i}: GitHub-Token-Muster", counters)
        if RE_ANTHROPIC.search(line):
            _add(findings, "secret.anthropic-key", rel, i, f"Zeile {i}: sk-ant-…-Key", counters)
        if RE_PRIVATE_KEY.search(line):
            _add(findings, "secret.private-key", rel, i, "Block „PRIVATE KEY“ beginnt hier", counters)
        if RE_DB_URL.search(line):
            _add(findings, "secret.db-url", rel, i, "Verbindungs-URL enthält Nutzer:Passwort", counters)
        if RE_SLACK_HOOK.search(line):
            _add(findings, "secret.webhook", rel, i, "Slack-Webhook-URL mit Token", counters)
        if RE_BEARER.search(line):
            _add(findings, "secret.bearer", rel, i, "Hardcodierter Bearer-Token", counters)
        m = RE_ASSIGN_SECRET.search(line)
        if m and not _is_placeholder(m.group(2)) and not line.strip().startswith(("#", "//")):
            _add(findings, "secret.zuweisung", rel, i,
                 f"„{m.group(1)}“ wird als Literal zugewiesen (Zeile {i})", counters)

        # Debug-Reste
        if lang == "python" and RE_PY_DEBUG.search(line):
            _add(findings, "code.debug-python", rel, i, f"Zeile {i}: {line.strip()[:80]}", counters)
        if lang in ("javascript", "typescript"):
            if RE_JS_CONSOLE.search(line):
                _add(findings, "code.debug-console", rel, i,
                     f"Zeile {i}: {line.strip()[:80]}", counters)
            if RE_JS_DEBUGGER.match(line):
                _add(findings, "code.debug-js", rel, i, f"Zeile {i}: debugger", counters)
        if lang == "shell" and RE_SHELL_EVAL.search(line):
            _add(findings, "code.eval-shell", rel, i, f"Zeile {i}: {line.strip()[:80]}", counters)

        # SQL per f-String / Konkatenation
        if RE_SQL_FSTRING.search(line) or RE_SQL_CONCAT.search(line):
            _add(findings, "code.sql-fstring" if RE_SQL_FSTRING.search(line)
                 else "code.sql-konkatenation", rel, i, f"Zeile {i}: {line.strip()[:80]}",
                 counters)

        # TODO/FIXME (nur in Textkommentar-nahen Zeilen zählen – sonst False Hits
        # in Strings sind okay, Marker sind Marker)
        if RE_TODO.search(line) and lang not in ("json",):
            _add(findings, "code.todo", rel, i, f"Zeile {i}: {line.strip()[:80]}", counters)

        # Hardcodierte IPs (Code-Dateien, ohne Loopback/Default)
        if lang not in ("markdown", "json", "yaml", "toml", "ini", "text"):
            m = RE_HARD_IP.search(line)
            if m:
                octets = m.group(1).split(".")
                if all(int(o) <= 255 for o in octets):
                    _add(findings, "code.harte-ip", rel, i,
                         f"IP {m.group(1)} in Zeile {i}", counters)

    # eval in JS/TS (zeilenbasiert reicht hier)
    if lang in ("javascript", "typescript"):
        for i, line in enumerate(lines, start=1):
            if RE_JS_EVAL.search(line):
                _add(findings, "code.eval-js", rel, i, f"Zeile {i}: {line.strip()[:80]}", counters)


def _scan_python_ast(rel: str, text: str, findings: list[dict], counters: dict) -> None:
    """Python-AST-Regeln: eval/exec, bare except, except-pass, shell=True,
    lange Funktionen. Parse-Fehler werden still übersprungen (Syntax kaputt
    = Analyse hier ende; kein Crash des Laufs)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in ("eval", "exec"):
                _add(findings, "code.eval-python", rel, node.lineno,
                     f"„{name}()“ in Zeile {node.lineno}", counters)
            if name == "run" and isinstance(fn, ast.Attribute) and fn.attr == "run":
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is True:
                        _add(findings, "code.shell-true", rel, node.lineno,
                             f"subprocess mit shell=True (Zeile {node.lineno})", counters)
        elif isinstance(node, ast.ExceptHandler):
            if node.type is None:
                _add(findings, "code.bare-except", rel, node.lineno,
                     f"bare `except:` in Zeile {node.lineno}", counters)
            elif isinstance(node.type, ast.Name) and node.type.id == "Exception" \
                    and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                _add(findings, "code.except-pass", rel, node.lineno,
                     f"„except Exception: pass“ in Zeile {node.lineno}", counters)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            if end - node.lineno > LONG_FUNCTION_LINES:
                _add(findings, "code.lange-funktion", rel, node.lineno,
                     f"„{node.name}“ umfasst {end - node.lineno + 1} Zeilen", counters)


def _project_level_rules(root: Path, rels: list[str], findings: list[dict],
                         counters: dict) -> None:
    """Regeln, die den ganzen Projektbaum betreffen."""
    has_tests = any(
        fnmatch.fnmatch(Path(r).name, pat) or r.startswith(("tests/", "test/", "__tests__/"))
        for r in rels for pat in TEST_FILE_PATTERNS
    )
    if rels and not has_tests:
        _add(findings, "projekt.keine-tests", "", 0,
             "Keine Dateien nach Test-Muster (test_*.py, *.spec.* …) gefunden", counters)

    has_readme = any(Path(r).name.lower().startswith("readme") for r in rels)
    if rels and not has_readme:
        _add(findings, "projekt.kein-readme", "", 0,
             "Keine README-Datei im Projektstamm", counters)

    # Eingecheckte Verzeichnisse/Dateien (zustandsbasiert, nicht pro Datei)
    node_modules = [r for r in rels if "/node_modules/" in f"/{r}" or r.startswith("node_modules/")]
    if node_modules:
        _add(findings, "datei.node_modules", node_modules[0], 0,
             f"node_modules eingecheckt ({len(node_modules)} Dateien) – .gitignore prüfen",
             counters)
    venvs = [r for r in rels if r.startswith((".venv/", "venv/"))]
    if venvs:
        _add(findings, "datei.venv", venvs[0], 0,
             f"venv eingecheckt ({len(venvs)} Dateien) – .gitignore prüfen", counters)
    caches = [r for r in rels if "__pycache__/" in r or r.endswith(".pyc")]
    if caches:
        _add(findings, "datei.pycache", caches[0], 0,
             f"Python-Bytecode eingecheckt ({len(caches)} Dateien)", counters)
    ds = [r for r in rels if Path(r).name == ".DS_Store"]
    if ds:
        _add(findings, "datei.dsstore", ds[0], 0,
             ".DS_Store (macOS-Müll) eingecheckt", counters)
    envs = [r for r in rels if Path(r).name in (".env", ".env.local", ".env.production")]
    if envs:
        _add(findings, "datei.env", envs[0], 0,
             ".env-Datei eingecheckt – wenn sie Secrets enthält: rotieren + .gitignore",
             counters)
    keyfiles = [r for r in rels if Path(r).name in ("id_rsa", "id_ed25519", "id_ecdsa")]
    keyfiles += [r for r in rels if Path(r).suffix.lower() in (".pem", ".p12", ".pfx", ".key")]
    if keyfiles:
        _add(findings, "datei.privater-schluessel", keyfiles[0], 0,
             f"Schlüssel-/Zertifikatsdatei eingecheckt: {', '.join(keyfiles[:3])}", counters)


def analyze_project(root: Path, exclude_patterns: list[str] | None = None) -> dict:
    """Analysiert den Projektbaum und liefert findings, Stats und Score."""
    exclude_patterns = exclude_patterns or []
    findings: list[dict] = []
    counters: dict = {}
    stats = {"files": 0, "lines": 0, "languages": {}, "skipped_binary": 0}

    rels: list[str] = []
    for rel, path in _iter_source_files(root, exclude_patterns):
        rels.append(rel)
        if _in_ignored_dir(rel):
            # In ignorierten Verzeichnissen (node_modules …) wird nur die
            # Projekt-Level-Regel fündig – kein Content-Scan, kein Score-Abzug.
            stats["ignored_dir_files"] = stats.get("ignored_dir_files", 0) + 1
            continue
        size = path.stat().st_size
        if size > HUGE_FILE_BYTES:
            _add(findings, "datei.riesig", rel, 0,
                 f"{size // 1024} KB – sollte nicht ins Repository", counters)
            continue

        ext = path.suffix.lower()
        lang = LANG_BY_EXT.get(ext)
        stats["files"] += 1
        if lang:
            stats["languages"][lang] = stats["languages"].get(lang, 0) + 1

        if size > SCAN_MAX_BYTES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            stats["skipped_binary"] += 1
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except Exception:
                stats["skipped_binary"] += 1
                continue

        lines = text.count("\n") + 1
        stats["lines"] += lines
        if lines > HUGE_FILE_LINES:
            _add(findings, "datei.riesig-zeilen", rel, 0,
                 f"{lines} Zeilen – zur Aufteilung prüfen", counters)

        if lang:
            _scan_content(rel, text, lang, findings, counters)
        if lang == "python":
            _scan_python_ast(rel, text, findings, counters)

    _project_level_rules(root, rels, findings, counters)

    counts = {"hoch": 0, "mittel": 0, "niedrig": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] += 1
    score = max(0, 100 - 12 * counts["hoch"] - 5 * counts["mittel"] - counts["niedrig"])

    return {"findings": findings, "stats": stats, "score": score, "counts": counts}
