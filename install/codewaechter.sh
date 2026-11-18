#!/usr/bin/env bash
# =============================================================================
#  🛡️ CodeWächter – One-Click-Installer (Proxmox VE)
#  https://github.com/HatchetMan111/CodeWaechter-Proxmox
#
#  Erstellt einen unprivilegierten LXC-Container und installiert darin die
#  lokale Code-Review- & Planungs-App CodeWächter (Traycer-Alternative):
#    - FastAPI-Backend + React/Vite-Oberfläche (Build liegt im Repo –
#      kein Node.js nötig im Container)
#    - Statische Regel-Engine: Secrets, Debug-Reste, Injection-Risiken,
#      fehlende Tests … → Score 0-100
#    - Plan-Generator: priorisierte Implementierungs-Checkliste (P0/P1/P2)
#    - Optionaler lokaler LLM-Endpoint (OpenAI-kompatibel, z. B. Ollama)
#
#  Installation (auf dem Proxmox-Host als root):
#    bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/CodeWaechter-Proxmox/main/install/codewaechter.sh)"
#
#  Mit Optionen (direkt hinter dem schließenden Anführungszeichen):
#    ... " --id 210 --port 8030
#
#  Update / Deinstall / Status / Debug:
#    ... " --update  |  ... " --uninstall  |  ... " --status  |  ... " --debug
# =============================================================================

# shellcheck disable=SC1111  # deutsche Anführungszeichen sind Absicht
set -Eeuo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
APP_NAME="codewaechter"
REPO_OWNER="HatchetMan111"
REPO_NAME="CodeWaechter-Proxmox"
REPO_BRANCH="main"
CT_ID=""                    # leer = automatisch nächste freie ID ab ID_SCAN_START
ID_SCAN_START=200
CT_HOSTNAME="codewaechter"
CT_STORAGE="local-lvm"
CT_ROOTFS=8                 # GB – Debian + Python-venv + Analysedaten
CT_CORES=2
CT_MEMORY=2048              # MB – Regel-Engine ist leichtgewichtig
CT_SWAP=512
CT_BRIDGE="vmbr0"
CT_IP="dhcp"
CT_GATEWAY=""               # nur bei statischer IP nötig, z. B. 192.168.1.1
CT_PASSWORD=""              # leer = automatisch generieren
PORT=8030                   # Web-UI-Port im Container
UNINSTALL=false
UPDATE=false
STATUS_CHECK=false
FORCE_RECREATE=false
ASSUME_YES=false
TMP_DIR=""
LOG_FILE="/tmp/${APP_NAME}-installer-$(date +%Y%m%d-%H%M%S).log"
# Der fertige Einzeiler für Hinweise/Fehlerausgaben – exakt kopierbar (Optionen
# hinter dem schließenden Anführungszeichen; der Installer erkennt sie via $0-Fallback)
ONE_LINER='bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/CodeWaechter-Proxmox/main/install/codewaechter.sh)"'

# ── Farben & Logging ───────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

info()  { echo -e "${BLUE}ℹ${NC}  $1"; }
ok()    { echo -e "${GREEN}✔${NC}  $1"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $1"; }
err()   { echo -e "${RED}✘${NC}  $1" >&2; }
die()   { err "$1"; exit 1; }
header(){ echo -e "\n${CYAN}${BOLD}━━━ $1 ━━━${NC}"; }

# ── Vollständiges Log mitschneiden (Debugging-Anforderung) ─────────────────
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
exec > >(tee "$LOG_FILE") 2>&1

# ── Cleanup & Fehlerkette ──────────────────────────────────────────────────
cleanup() {
    # if statt [[ ]] && – sonst gibt cleanup() bei leerem TMP_DIR Exit-Code 1
    # zurück und überschreibt den eigentlichen Exit-Status des Scripts.
    if [[ -n "${TMP_DIR:-}" && -d "$TMP_DIR" ]]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

# Gibt bei Fehlern die KOMPLETTE Fehlerkette aus: Zeile, Befehl, Exit-Code,
# letzte Logzeilen sowie Container-Logs (Setup + Service), falls schon
# ein Container existiert.
on_error() {
    local ec=$1 line=$2 cmd=$3
    err "Installationsfehler – Exit-Code ${ec} in Zeile ${line}"
    err "Fehlgeschlagener Befehl: ${cmd}"
    if [[ -f "$LOG_FILE" ]]; then
        info "Letzte Logzeilen (${LOG_FILE}):"
        tail -n 20 "$LOG_FILE" | sed 's/^/    /' || true
    fi
    if [[ -n "${CT_ID:-}" ]] && pct status "$CT_ID" &>/dev/null; then
        info "Container-Logs (Setup + Service):"
        pct exec "$CT_ID" -- sh -c '
            tail -n 40 /var/log/codewaechter-setup.log 2>/dev/null
            echo "--- journalctl codewaechter ---"
            journalctl -u codewaechter --no-pager 2>/dev/null | tail -n 20
        ' 2>/dev/null | sed 's/^/    /' || true
        info "Container $CT_ID wurde NICHT gelöscht – Inspektion: pct enter $CT_ID"
        info "Aufräumen: pct stop $CT_ID && pct destroy $CT_ID --purge"
    fi
    info "Vollständiges Log: $LOG_FILE"
    info "Neu ausführen mit vollem bash -x-Trace (exakter Befehl):"
    echo "    ${ONE_LINER} --debug"
    exit "$ec"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

# ── Debug-Modus (voller bash -x-Trace) ─────────────────────────────────────
# (Vor parse_args, damit --debug selbst noch mitgetraced wird.)
if [[ "${1:-}" == "--debug" || "$0" == "--debug" ]]; then
    set -x
fi

# ── Hilfsfunktionen ────────────────────────────────────────────────────────
find_free_id() {
    # ID-Kollisionen auch mit VMs vermeiden (qm list; Community-Scripts-Stil).
    # Fehlt qm (Harness/Univ-Host), wird es einfach übersprungen.
    local id="$ID_SCAN_START" has_qm=false
    if command -v qm >/dev/null 2>&1; then
        has_qm=true
    fi
    while pct list 2>/dev/null | awk '{print $1}' | grep -qx "$id" \
        || { $has_qm && qm list 2>/dev/null | awk '{print $1}' | grep -qx "$id"; }; do
        id=$((id + 1))
    done
    echo "$id"
}

find_ct_id_by_hostname() {
    # Grube (pct list-Spalten variieren): Hostname ist IMMER das letzte Feld.
    pct list 2>/dev/null | awk -v h="$CT_HOSTNAME" '$NF==h {print $1; exit}'
}

get_ct_ip() {
    # Grube (pipefail): bei pct-exec-Fehlschlag stirbt die Substitution –
    # deshalb || true INNERHALB von $().
    local ip=""
    ip=$(pct exec "$1" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
    echo "$ip"
}

ensure_ct_running() {
    # Verwaltungs-Pfade (--update) müssen einen GESTOPPTEN CT selbst starten –
    # pct exec auf gestopptem CT → Exit 255.
    local id="$1"
    if pct status "$id" 2>/dev/null | grep -q "status: running"; then
        return 0
    fi
    info "Container ist gestoppt – starte ihn ..."
    pct start "$id"
    wait_for_ct_network "$id"
}

wait_for_ct_network() {
    # Nach pct create --start nicht blind sleep 3 (DHCP/DNS-Race) – aktiv pollen.
    local id="$1" tries=0 ip=""
    info "Warte auf Netzwerk im Container (DHCP) ..."
    while true; do
        ip=$(get_ct_ip "$id")
        if [[ -n "$ip" ]]; then
            ok "Container-IP: $ip"
            return 0
        fi
        tries=$((tries + 1))
        if [[ $tries -ge 60 ]]; then
            err "Container hat nach 60 s keine IP bekommen."
            info "Prüfe: pct exec $id -- ip a   und DHCP am Bridge-Interface ($CT_BRIDGE)"
            return 1
        fi
        sleep 1
    done
}

verify_ct_dns() {
    # IP allein reicht nicht – apt, pip (PyPI) und der Repo-Tarball von GitHub
    # brauchen Namensauflösung. DNS kann FLAKY sein: 3 Erfolge in Folge
    # verlangen; eine Störung → Fallback + Freeze.
    local id="$1" ok_count=0 tries=0
    info "Prüfe DNS im Container (3x hintereinander) ..."
    while true; do
        if pct exec "$id" -- getent hosts github.com pypi.org >/dev/null 2>&1; then
            ok_count=$((ok_count + 1))
            if [[ $ok_count -ge 3 ]]; then
                ok "DNS im Container funktioniert (3x bestätigt)"
                return 0
            fi
        else
            ok_count=0
        fi
        tries=$((tries + 1))
        if [[ $tries -ge 10 ]]; then
            break
        fi
        sleep 2
    done
    # Fallback: DNS des Hosts (falls nicht loopback) + öffentliche Resolver eintragen
    local host_ns ns_list
    host_ns=$(grep -h '^nameserver' /etc/resolv.conf 2>/dev/null | awk '{print $2}' | grep -v '^127\.' | head -1 || true)
    ns_list="1.1.1.1 8.8.8.8"
    if [[ -n "$host_ns" ]]; then
        ns_list="$host_ns $ns_list"
    fi
    warn "DNS im Container defekt – schreibe Fallback-Nameserver ($ns_list) nach /etc/resolv.conf"
    pct exec "$id" -- sh -c "printf 'nameserver %s\n' $ns_list > /etc/resolv.conf"
    # Dauerhaft: dhclient-Hook (verhindert das Überschreiben durch Lease-Renewals)
    pct exec "$id" -- bash -c 'mkdir -p /etc/dhcp/dhclient-enter-hooks.d && printf "make_resolv_conf() { :; }\n" > /etc/dhcp/dhclient-enter-hooks.d/no-dns'
    if pct exec "$id" -- getent hosts github.com pypi.org >/dev/null 2>&1; then
        ok "DNS-Fallback aktiv und gegen DHCP-Überschreiben geschützt – Auflösung funktioniert"
        return 0
    fi
    die "DNS im Container weiterhin ohne Funktion (auch mit Fallback).\n  Prüfe: pct exec $id -- getent hosts github.com pypi.org\n  Firewall/Router: ausgehende DNS-Anfragen (Port 53) am Bridge-Netz erlauben."
}

# ── Argumente parsen ───────────────────────────────────────────────────────
parse_args() {
    # Grube (bash -c): `bash -c "$(wget -qLO - URL)" --id 5` belegt das ERSTE
    # Argument nach dem Script-String mit $0, nicht $1 – die Option würde
    # still geschluckt. Fallback: startet $0 mit "-", ist es eine Option.
    local args=("$@")
    if [[ "$0" == -* ]]; then
        args=("$0" "$@")
    fi
    local i=0
    while [[ $i -lt ${#args[@]} ]]; do
        case "${args[$i]}" in
            --id)        CT_ID="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --hostname)  CT_HOSTNAME="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --storage)   CT_STORAGE="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --rootfs)    CT_ROOTFS="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --cores)     CT_CORES="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --memory)    CT_MEMORY="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --swap)      CT_SWAP="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --bridge)    CT_BRIDGE="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --ip)        CT_IP="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --gateway)   CT_GATEWAY="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --password)  CT_PASSWORD="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --port)      PORT="${args[$((i+1))]:-}"; i=$((i+2)) ;;
            --force)     FORCE_RECREATE=true; i=$((i+1)) ;;
            --yes|-y)    ASSUME_YES=true; i=$((i+1)) ;;
            --update)    UPDATE=true; i=$((i+1)) ;;
            --uninstall) UNINSTALL=true; i=$((i+1)) ;;
            --status)    STATUS_CHECK=true; i=$((i+1)) ;;
            --debug)     set -x; i=$((i+1)) ;;
            --help|-h)   print_help; exit 0 ;;
            *) die "Unbekannte Option: ${args[$i]}  (--help zeigt alle Optionen)" ;;
        esac
    done
}

print_help() {
    cat <<EOF
🛡️ CodeWächter – Proxmox-Installer (lokale Code-Review- & Planungs-App im LXC)

Installation:   bash -c "\$(wget -qLO - https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/main/install/codewaechter.sh)"

Optionen (hinter dem schließenden Anführungszeichen anhängen, z. B. ... " --id 210):
  --id ID          Container-ID (Default: nächste freie ab 200)
  --hostname NAME  Container-Hostname (Default: codewaechter)
  --storage NAME   Storage für rootfs (Default: local-lvm)
  --rootfs GB      Disk-Größe (Default: 8)
  --cores N        vCPUs (Default: 2)
  --memory MB      RAM (Default: 2048)
  --swap MB        Swap (Default: 512)
  --bridge BR      Netzwerk-Bridge (Default: vmbr0)
  --ip CIDR        Statt DHCP, z. B. 192.168.1.50/24 (+ --gateway 192.168.1.1)
  --gateway GW     Gateway für statische IP
  --password PW    Root-Passwort des CT (Default: automatisch generiert)
  --port PORT      Web-UI-Port im Container (Default: 8030)
  --force          Vorhandenen Container mit gleichem Hostname löschen und neu anlegen
  --yes / -y       Rückfragen überspringen
  --update         App im bestehenden Container erneuern (Analysedaten bleiben)
  --uninstall      Container stoppen und löschen (inkl. Analysedaten!)
  --status         Status anzeigen
  --debug          Vollständiges bash -x-Trace

  Modus:  Install (Default) / --update / --uninstall / --status / --debug
EOF
}

# ── In-Container-Setup-Script (idempotent) ─────────────────────────────────
# QUOTETER Heredoc ('INNEREOF'): KEINE Expansion im Host – Werte kommen als
# Umgebungsvariablen via `pct exec -- env …` (Grube: Doppel-Escaping).
write_inner_script() {
    cat > "$TMP_DIR/setup-inner.sh" <<'INNEREOF'
#!/usr/bin/env bash
# =============================================================================
# CodeWächter In-Container-Setup (idempotent – safe für Update & Re-Run)
# Installiert FastAPI-Backend + committed React-Build als systemd-Service.
# =============================================================================
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C   # verhindert perl/locale-Warnungen im frischen Debian-CT

echo ">>> Basis-Pakete (python3, venv, pip, curl, git) ..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates git >/dev/null

echo ">>> Installer-Tarball laden (App + systemd-Unit aus diesem Repo) ..."
curl -fsSL --retry 3 "https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REPO_BRANCH}" -o /tmp/codewaechter.tar.gz
rm -rf /tmp/codewaechter-src && mkdir -p /tmp/codewaechter-src
tar -xzf /tmp/codewaechter.tar.gz -C /tmp/codewaechter-src --strip-components=1
if [[ ! -f /tmp/codewaechter-src/app/backend/main.py ]]; then
    echo "FEHLER: app/backend/main.py fehlt im Tarball – Repo-Struktur geändert?"
    echo "  Tarball-Inhalt:"
    tar -tzf /tmp/codewaechter.tar.gz | head -20
    exit 8
fi
if [[ ! -f /tmp/codewaechter-src/app/backend/static/index.html ]]; then
    echo "FEHLER: app/backend/static/index.html fehlt im Tarball –"
    echo "  der Frontend-Build muss committed sein (npm run build im Repo)."
    exit 9
fi

echo ">>> Service-Benutzer anlegen (idempotent) ..."
if ! id -u codewaechter >/dev/null 2>&1; then
    useradd --system --home-dir /opt/codewaechter --shell /usr/sbin/nologin codewaechter
fi

echo ">>> App-Quelle nach /opt/codewaechter/src (Backend inkl. committed Frontend-Build) ..."
mkdir -p /opt/codewaechter
rm -rf /opt/codewaechter/src
mkdir -p /opt/codewaechter/src/backend
cp -a /tmp/codewaechter-src/app/backend/. /opt/codewaechter/src/backend/
install -m 644 /tmp/codewaechter-src/systemd/codewaechter.service /etc/systemd/system/codewaechter.service
sed -i "s/__PORT__/${PORT}/g" /etc/systemd/system/codewaechter.service

echo ">>> Python-virtualenv + Abhängigkeiten (FastAPI, uvicorn, requests, multipart) ..."
if [[ ! -x /opt/codewaechter/.venv/bin/python ]]; then
    python3 -m venv /opt/codewaechter/.venv
fi
/opt/codewaechter/.venv/bin/pip install --quiet --upgrade pip
/opt/codewaechter/.venv/bin/pip install --quiet -r /opt/codewaechter/src/backend/requirements.txt
/opt/codewaechter/.venv/bin/python -c "import fastapi, uvicorn, requests, multipart" \
    || { echo "FEHLER: Python-Abhängigkeiten unvollständig (pip-Output oben ansehen)."; exit 10; }

echo ">>> Datenverzeichnis anlegen (Update-sicher: liegt außerhalb von src/) ..."
mkdir -p /opt/codewaechter/data/projects
chown -R codewaechter:codewaechter /opt/codewaechter

# Aufräumen erst NACH allen installs
rm -rf /tmp/codewaechter-src /tmp/codewaechter.tar.gz

echo ">>> Service aktivieren ..."
systemctl daemon-reload
systemctl enable codewaechter.service >/dev/null 2>&1
systemctl restart codewaechter.service

echo ">>> Setup abgeschlossen. Log: /var/log/codewaechter-setup.log"
INNEREOF
}

run_inner_setup() {
    local ct_id="$1"
    header "⚙️  Installation im Container $ct_id (FastAPI + Frontend-Build + systemd)"
    write_inner_script
    pct push "$ct_id" "$TMP_DIR/setup-inner.sh" /root/setup-inner.sh
    # ALLES läuft im inner-Script (das nackte Debian-Template hat kein curl).
    # Grube (tee-Pipe): ohne exit ${PIPESTATUS[0]} frisst die Pipe den inner-
    # Exit-Code (Fehler im inner liefen als "erfolgreich" durch!).
    pct exec "$ct_id" -- env REPO_OWNER="$REPO_OWNER" REPO_NAME="$REPO_NAME" \
        REPO_BRANCH="$REPO_BRANCH" PORT="$PORT" \
        bash -c "bash /root/setup-inner.sh 2>&1 | tee /var/log/codewaechter-setup.log; exit \${PIPESTATUS[0]}"
}

# ── Verifikation ───────────────────────────────────────────────────────────
verify_installation() {
    local ct_id="$1" tries=0 code="" body=""

    header "✅ Verifikation"

    # Service aktiv?
    while true; do
        if pct exec "$ct_id" -- systemctl is-active --quiet codewaechter.service 2>/dev/null; then
            ok "Service codewaechter.service läuft"
            break
        fi
        tries=$((tries + 1))
        if [[ $tries -ge 30 ]]; then
            die "Service codewaechter.service läuft nicht.\n  Debug: pct exec $ct_id -- journalctl -u codewaechter -e"
        fi
        sleep 1
    done

    # API antwortet? (Grube: curl -w gibt bei Verbindungsfehler bereits 000
    # aus – NIE zusätzlich || echo 000 anhängen.)
    tries=0
    while true; do
        code=$(pct exec "$ct_id" -- curl -s -o /dev/null -w '%{http_code}' \
               --max-time 5 "http://127.0.0.1:${PORT}/api/status" || true)
        if [[ "$code" == "200" ]]; then
            ok "API antwortet (HTTP 200 auf Port $PORT)"
            break
        fi
        tries=$((tries + 1))
        if [[ $tries -ge 30 ]]; then
            die "API antwortet nicht (letzter HTTP-Code: ${code:-unbekannt}).\n  000 = uvicorn tot, 404 = falscher Port.\n  Debug: pct exec $ct_id -- journalctl -u codewaechter -e"
        fi
        sleep 1
    done

    # Web-UI (SPA) antwortet?
    tries=0
    while true; do
        code=$(pct exec "$ct_id" -- curl -s -o /dev/null -w '%{http_code}' \
               --max-time 5 "http://127.0.0.1:${PORT}/" || true)
        if [[ "$code" == "200" ]]; then
            ok "Web-UI antwortet (HTTP 200 auf Port $PORT)"
            break
        fi
        tries=$((tries + 1))
        if [[ $tries -ge 30 ]]; then
            die "Web-UI (static/index.html) antwortet nicht (letzter HTTP-Code: ${code:-unbekannt}).\n  Debug: pct exec $ct_id -- ls -la /opt/codewaechter/src/backend/static/"
        fi
        sleep 1
    done

    # Rückgabe enthält die App? (JSON-Inhalt gegen falschen Service absichern)
    body=$(pct exec "$ct_id" -- curl -s --max-time 5 "http://127.0.0.1:${PORT}/api/status" || true)
    if [[ "$body" != *"CodeWächter"* ]]; then
        die "API liefert unerwartete Antwort (kein CodeWächter-JSON).\n  Antwort: ${body:0:200}"
    fi
    ok "API-Identität bestätigt: ${body:0:120}"
}

# ── Zusammenfassung ────────────────────────────────────────────────────────
print_summary() {
    header "🛡️  CodeWächter ist bereit!"

    local ct_ip
    ct_ip=$(get_ct_ip "$CT_ID")
    if [[ -z "$ct_ip" ]]; then
        ct_ip="<IP-ADRESSE>"
    fi

    echo ""
    echo -e "  ${GREEN}${BOLD}════════════════════════════════════════════════════════════${NC}"
    echo -e "  ${GREEN}  🌐 Web-UI:   http://${ct_ip}:${PORT}${NC}"
    echo -e "  ${GREEN}  📊 API:      http://${ct_ip}:${PORT}/api/status${NC}"
    echo -e "  ${GREEN}  📁 Daten:    /opt/codewaechter/data  (im Container)${NC}"
    if [[ -n "$CT_PASSWORD" ]]; then
        echo -e "  ${GREEN}  🔒 Root-PW:  ${CT_PASSWORD}${NC}"
    fi
    echo -e "  ${GREEN}${BOLD}════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${CYAN}Erste Schritte:${NC}"
    echo "  1. Web-UI öffnen → „＋ Neues Projekt“"
    echo "     - 📁 Pfad im Container (z. B. /root/repos/mein-projekt)"
    echo "     - 🌐 Git-URL (wird flach geklont, depth 1)"
    echo "     - 📦 Archiv-Upload (.zip/.tar.gz, max. 200 MB)"
    echo "  2. „▶ Analyse“ → Findings + Score 0-100 + priorisierter Plan (P0/P1/P2)"
    echo "  3. Optional: ⚙️ Einstellungen → LLM-Endpoint (OpenAI-kompatibel, z. B."
    echo "     Ollama oder ZCodeAI-Server im LAN) für frei formulierte Pläne."
    echo ""
    echo -e "  ${CYAN}Nützliche Befehle (exakt kopierbar):${NC}"
    echo "  Status:    ${ONE_LINER} --status"
    echo "  Update:    ${ONE_LINER} --update"
    echo "  Deinstall: ${ONE_LINER} --uninstall"
    echo "  Logs:      pct exec ${CT_ID} -- journalctl -u codewaechter -f"
    echo ""
    warn "Hinweis: die App läuft vollständig lokal – Analyse offline. Nur der"
    warn "optionale LLM-Endpoint und Git-Klone kontaktieren Rechner im LAN/Internet."
}

# ── Install ────────────────────────────────────────────────────────────────
do_install() {
    header "🛡️ Installiere CodeWächter (LXC, Port ${PORT})"

    if [[ $EUID -ne 0 ]]; then
        die "Bitte als root ausführen (auf dem Proxmox-Host)."
    fi
    if ! command -v pct >/dev/null 2>&1; then
        die "pct nicht gefunden – das Script muss auf einem Proxmox-VE-Host laufen."
    fi

    # Bereits vorhanden? ALLE Treffer listen (doppelte Hostnamen = Warnfall)
    local all_ids existing
    all_ids=$(pct list 2>/dev/null | awk -v h="$CT_HOSTNAME" '$NF==h {print $1}' | tr '\n' ' ' | sed 's/ $//')
    if [[ -n "$all_ids" ]]; then
        existing=${all_ids%% *}
        if [[ "$all_ids" == *" "* ]]; then
            warn "MEHRERE Container mit Hostname '$CT_HOSTNAME': $all_ids"
            warn "--force löscht nur $existing – überzählige vorher manuell räumen:"
            warn "  pct stop <ID> && pct destroy <ID> --purge   (für jede doppelte ID)"
        fi
        if $FORCE_RECREATE; then
            warn "Container $existing ($CT_HOSTNAME) existiert – --force: lösche und lege neu an (nächste freie ID)."
            warn "Analysedaten (/opt/codewaechter/data) in diesem Container gehen verloren!"
            pct stop "$existing" || true
            pct destroy "$existing" --purge
        else
            die "Container $all_ids mit Hostname '$CT_HOSTNAME' existiert bereits.\n  Update (dasselbe CT, Daten bleiben):  ${ONE_LINER} --update\n  Neu aufsetzen (löscht CT, wählt nächste freie ID):  ${ONE_LINER} --force\n  Doppelte manuell räumen:  pct stop <ID> && pct destroy <ID> --purge"
        fi
    fi

    # Debian-Template sicherstellen
    header "🧱 Container-Template"
    local template=""
    template=$(pveam list local 2>/dev/null | awk '{print $1}' | grep -o 'debian-12-standard[^ ]*' | sort -rV | head -1 || true)
    if [[ -z "$template" ]]; then
        info "Kein Debian-12-Template vorhanden – lade es herunter ..."
        pveam update
        template=$(pveam available --section system 2>/dev/null | grep -o 'debian-12-standard[^ ]*' | sort -rV | head -1 || true)
        if [[ -z "$template" ]]; then
            die "Kein debian-12-standard-Template gefunden (pveam available leer?)."
        fi
        pveam download local "$template"
    fi
    ok "Template: $template"

    # ID, Root-Passwort
    if [[ -z "$CT_ID" ]]; then
        CT_ID=$(find_free_id)
        info "Nächste freie CT-ID: $CT_ID"
    fi
    if [[ -z "$CT_PASSWORD" ]]; then
        CT_PASSWORD=$(openssl rand -base64 12 2>/dev/null || true)
        if [[ -z "$CT_PASSWORD" ]]; then
            CT_PASSWORD="cw-$(date +%s)"
        fi
    fi

    # Container anlegen
    header "🚀 Erzeuge Container $CT_ID ($CT_HOSTNAME)"
    local net0="name=eth0,bridge=${CT_BRIDGE},ip=${CT_IP},firewall=0"
    if [[ -n "$CT_GATEWAY" ]]; then
        net0+=",gateway=${CT_GATEWAY}"
    fi
    pct create "$CT_ID" "local:vztmpl/${template}" \
        --hostname "$CT_HOSTNAME" \
        --cores "$CT_CORES" \
        --memory "$CT_MEMORY" \
        --swap "$CT_SWAP" \
        --rootfs "${CT_STORAGE}:${CT_ROOTFS}" \
        --net0 "$net0" \
        --unprivileged 1 \
        --features nesting=1 \
        --onboot 1 \
        --start 1 \
        --password "$CT_PASSWORD"
    ok "Container $CT_ID erstellt und gestartet"

    wait_for_ct_network "$CT_ID"
    verify_ct_dns "$CT_ID"

    run_inner_setup "$CT_ID"
    verify_installation "$CT_ID"
    print_summary
}

# ── Update ─────────────────────────────────────────────────────────────────
do_update() {
    header "🔄 Update CodeWächter"

    local ct_id
    ct_id=$(find_ct_id_by_hostname || true)
    if [[ -z "$ct_id" ]]; then
        die "Kein Container mit Hostname '$CT_HOSTNAME' gefunden.\n  Erst installieren:  ${ONE_LINER}"
    fi
    CT_ID="$ct_id"

    ensure_ct_running "$ct_id"
    verify_ct_dns "$ct_id"

    run_inner_setup "$ct_id"
    verify_installation "$ct_id"

    ok "Update abgeschlossen – Analysedaten (/opt/codewaechter/data) blieben erhalten."
    print_summary
}

# ── Uninstall ──────────────────────────────────────────────────────────────
do_uninstall() {
    header "🗑️  Deinstalliere CodeWächter"

    local ct_id
    ct_id=$(find_ct_id_by_hostname || true)
    if [[ -z "$ct_id" ]]; then
        warn "Kein Container mit Hostname '$CT_HOSTNAME' gefunden – nichts zu tun."
        return 0
    fi
    CT_ID="$ct_id"

    warn "Container $ct_id ($CT_HOSTNAME) wird GESTOPPT und GELÖSCHT."
    warn "Darin: die App und alle Analysedaten (/opt/codewaechter/data)!"
    if ! $ASSUME_YES; then
        local answer=""
        if ! read -r -t 60 -p "Wirklich löschen? (ja/NEIN): " answer; then
            die "Keine Bestätigung erhalten (nicht-interaktiv?) – nichts gelöscht.\n  Erzwingen:  ${ONE_LINER} --uninstall --yes"
        fi
        if [[ "$answer" != "ja" ]]; then
            info "Abgebrochen – nichts gelöscht."
            return 0
        fi
    fi
    pct stop "$ct_id" || true
    pct destroy "$ct_id" --purge
    ok "Container $ct_id entfernt."
}

# ── Status ─────────────────────────────────────────────────────────────────
do_status() {
    header "📊 Status CodeWächter"

    local ct_id
    ct_id=$(find_ct_id_by_hostname || true)
    if [[ -z "$ct_id" ]]; then
        warn "Kein Container mit Hostname '$CT_HOSTNAME' gefunden."
        info "Installieren:  ${ONE_LINER}"
        return 0
    fi
    CT_ID="$ct_id"

    pct list 2>/dev/null | awk -v id="$ct_id" '$1==id {print "Container:  " $1 " (" $2 ") – " $3 " – " $4 " – " $5}'
    # --status startet einen gestoppten Container NICHT – nur melden.
    if ! pct status "$ct_id" 2>/dev/null | grep -q "status: running"; then
        warn "Container ist gestoppt – Web-UI nicht erreichbar."
        info "Starten:  pct start $ct_id   |   Update inkl. Start:  ${ONE_LINER} --update"
        return 0
    fi

    if pct exec "$ct_id" -- systemctl is-active --quiet codewaechter.service 2>/dev/null; then
        ok "Service:   codewaechter.service läuft"
    else
        err "Service:   codewaechter.service läuft NICHT"
        info "Logs:      pct exec $ct_id -- journalctl -u codewaechter -e"
    fi

    local ip code projects
    ip=$(get_ct_ip "$ct_id")
    if [[ -n "$ip" ]]; then
        code=$(pct exec "$ct_id" -- curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/api/status" || true)
        if [[ "$code" == "200" ]]; then
            projects=$(pct exec "$ct_id" -- curl -s --max-time 5 "http://127.0.0.1:${PORT}/api/status" | grep -o '"projects":[0-9]*' | cut -d: -f2 || true)
            ok "Web-UI:    http://${ip}:${PORT}  (${projects:-?} Projekt(e))"
        else
            warn "Web-UI:    HTTP-Code ${code:-unbekannt} von http://${ip}:${PORT}"
        fi
    else
        warn "Container hat keine IP (läuft er?)."
    fi

    local app_ver
    app_ver=$(pct exec "$ct_id" -- sh -c "curl -s --max-time 5 http://127.0.0.1:${PORT}/api/status 2>/dev/null | grep -o '\"version\":\"[^\"]*\"' | cut -d'\"' -f4" 2>/dev/null || true)
    info "Version:   ${app_ver:-unbekannt}"
}

# ── Main ───────────────────────────────────────────────────────────────────
parse_args "$@"

TMP_DIR=$(mktemp -d)

if $UNINSTALL; then
    do_uninstall
elif $UPDATE; then
    do_update
elif $STATUS_CHECK; then
    do_status
else
    do_install
fi
