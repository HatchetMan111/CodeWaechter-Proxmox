# 🛡️ CodeWächter – Lokale Code-Review- & Planungs-App

**Traycer-Alternative für den eigenen Proxmox-Host:** CodeWächter analysiert lokale Repos (statisch, offline), vergibt einen Qualitäts-Score 0–100 und erzeugt daraus eine **priorisierte Implementierungs-Checkliste (P0/P1/P2)** – wie Traycers Implementierungspläne, aber vollkommen lokal. Optional wird ein OpenAI-kompatibler LLM-Endpoint im LAN (Ollama, ZCodeAI-Server …) für frei formulierte Pläne genutzt – ohne Endpoint funktioniert alles rein regelbasiert.

## Einzeiler-Installation (Proxmox-Host, als root)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/CodeWaechter-Proxmox/main/install/codewaechter.sh)"
```

Das Script (im Stil der [Proxmox VE Community Scripts](https://community-scripts.github.io/ProxmoxVE)):

- erstellt automatisch einen **unprivilegierten LXC-Container** (Debian 12, `nesting=1`, `onboot=1`, 2 vCPU, 2 GB RAM, 8 GB Disk – alles per Option änderbar; CT-ID wird automatisch frei gewählt, Default-Scan ab 200 – kollisionsfrei auch gegen bestehende **VMs** (`qm list`))
- installiert FastAPI + uvicorn in einem venv und serviert die React-Oberfläche aus dem mitgelieferten Build (**kein Node.js im Container**)
- richtet den systemd-Service `codewaechter.service` ein (`Restart=always`, `After=network-online.target`)
- verifiziert die Installation selbst (Service aktiv, API 200, Web-UI 200, API-Identität) und gibt die finale URL aus

Mit Optionen (hinter dem schließenden Anführungszeichen anhängen):

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/CodeWaechter-Proxmox/main/install/codewaechter.sh)" --id 210 --port 8030
```

Alle Optionen: `--id --hostname --storage --rootfs --cores --memory --swap --bridge --ip --gateway --password --port --force --yes --update --uninstall --status --debug` (oder `--help`).

## Was die App kann

| Bereich | Beschreibung |
|---|---|
| **Projekte** | 📁 Pfad im Container · 🌐 Git-URL (flacher Klon) · 📦 Archiv-Upload (.zip/.tar.gz, max. 200 MB) |
| **Regel-Engine (offline)** | Secrets (AWS/GitHub/Anthropic-Keys, private Keys, DB-URLs mit Passwort, hardcodierte Passwörter), Debug-Reste (`breakpoint()`, `console.log`, `debugger`), `eval`/`exec`, bare `except`, `shell=True`, SQL per f-String/Konkatenation, eingecheckte Artefakte (`node_modules`, `__pycache__`, `.env`, Schlüsseldateien), fehlende Tests/README, Riesen-Dateien, harte IPs |
| **Score** | 0–100, abgeleitet aus den Schweregraden (hoch −12, mittel −5, niedrig −1) |
| **Plan-Generator** | Findings → priorisierte Checkliste (P0 Sicherheit → P1 demnächst → P2 Hygiene) mit Tipps pro Regel; Fortschritt bleibt über Re-Runs erhalten |
| **LLM (optional)** | ⚙️ Einstellungen → OpenAI-kompatible Base-URL + Modell (z. B. `http://<lan-ip>:8000/v1` + `qwen2.5-coder`); Verbindungstest eingebaut; bei Ausfall greift automatisch der Regel-Plan – die UI zeigt das ehrlich als „Regel-Engine (LLM-Fallback)" an |
| **Verlauf** | Score- und Findings-Historie je Projekt; Findings, Counts und Plan zeigen immer den **letzten Lauf** (Re-Runs zählen nichts doppelt) |

## Update / Deinstall / Status

```bash
# Update (dasselbe CT, Analysedaten in /opt/codewaechter/data bleiben erhalten):
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/CodeWaechter-Proxmox/main/install/codewaechter.sh)" --update

# Status:
... --status

# Deinstall (Container + Daten löschen, fragt nach):
... --uninstall
```

## Debugging

Bei jedem Fehler gibt der Installer die **komplette Fehlerkette** aus (Exit-Code, Zeile, fehlgeschlagener Befehl, Log-Auszug, Container-Logs). Vollständiges Trace:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/CodeWaechter-Proxmox/main/install/codewaechter.sh)" --debug
```

Das komplette Install-Log liegt unter `/tmp/codewaechter-installer-<zeitstempel>.log`, das Setup-Log im Container unter `/var/log/codewaechter-setup.log`, Service-Logs via `pct exec <CT-ID> -- journalctl -u codewaechter -f`.

## Entwicklung

```
app/backend/    FastAPI (main.py, db.py, analyzer.py, planner.py, llm.py)
                + static/ = gebautes Frontend (committed, kein Node im CT)
app/frontend/   React 18 + Vite (npm run build → backend/static)
install/        codewaechter.sh – Installer (set -Eeuo pipefail)
systemd/        codewaechter.service (Unit-Vorlage mit __PORT__)
tests/          pytest-Backend-Tests (31)
test/           Mock-Harness (unshare -rm + fakebin, 16 Cases) – ohne Proxmox-Host lauffähig
```

Tests lokal ausführen (ohne Proxmox):

```bash
python3 -m pytest tests/          # 31 Backend-Tests
test/run-tests.sh                 # 16 Harness-Cases (84 Assertions)
```

Frontend neu bauen (Node lokal, z. B. `~/.local/node/bin`):

```bash
cd app/frontend && npm install && npm run build   # → app/backend/static
```

## Sicherheitshinweise

- Die Web-UI hat keine Auth – Port 8030 nicht ohne VPN/Firewall ins Internet exposen.
- Analyse läuft offline; Netzkontakte nur: Git-Klone, Archiv-Upload und optional der konfigurierte LLM-Endpoint.
- Findings mit erkannten Secrets werden nur LOKAL in der SQLite-DB (`/opt/codewaechter/data`) gespeichert.
