#!/usr/bin/env bash
# =============================================================================
# CodeWaechter-Proxmox Mock-Harness (kein Proxmox-Host nötig)
#
# Läuft als unprivilegierter User: `unshare -rm` mappt den User auf root
# (EUID 0 → Root-Prüfung des Production-Scripts testbar) und isoliert die
# Schreibpfade per Mount-Namespace (tmpfs über /opt, /root, /usr/local/bin,
# /var/log, /etc/dhcp, /etc/systemd/system; bind-mount über /etc/resolv.conf).
# Alle pct/pveam/curl/systemctl/python3/useradd/...-Aufrufe werden über
# test/fakebin gemockt.
#
# Aufruf:   test/run-tests.sh            (alle Cases)
#           test/run-tests.sh <name> ... (nur bestimmte Cases)
# =============================================================================
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HARNESS_DIR")"
FAKEBIN="$HARNESS_DIR/fakebin"
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
WORK="/tmp/codewaechter-harness.$$"
INSTALLER="$REPO/install/codewaechter.sh"

PASS=0
FAIL=0
FAILED_CASES=()

# ── Assertions (inner- und host-seitig benutzt) ────────────────────────────
ok()   { echo "OK   $*" >> "$RESULTS_FILE"; }
fail() { echo "FAIL $*" >> "$RESULTS_FILE"; }

assert_eq() {
    local got="$1" want="$2" msg="$3"
    if [[ "$got" == "$want" ]]; then ok "$msg"; else fail "$msg (got=[$got] want=[$want])"; fi
}

assert_contains() {
    local hay="$1" needle="$2" msg="$3"
    if grep -qF -- "$needle" <<< "$hay"; then ok "$msg"; else fail "$msg (fehlt: [$needle])"; fi
}

assert_file() {
    if [[ -e "$1" ]]; then ok "$2"; else fail "$2 (Datei fehlt: $1)"; fi
}

assert_no_file() {
    if [[ ! -e "$1" ]]; then ok "$2"; else fail "$2 (sollte fehlen, existiert: $1)"; fi
}

# ── Installer-Lauf (innerhalb NS) ──────────────────────────────────────────
run_installer() {
    OUT=$(bash "$INSTALLER" "$@" < /dev/null 2>&1)
    RC=$?
}

# ── Presets (State-Seeding, läuft im NS) ───────────────────────────────────
seed_service_active() {
    mkdir -p /etc/systemd/system
    touch "$HARNESS_STATE/systemd/codewaechter.service.active"
}

seed_preset() {
    local preset="$1"
    mkdir -p "$HARNESS_STATE/cts" "$HARNESS_STATE/systemd"
    case "$preset" in
        fresh) ;;
        occupied200) printf 'running other-ct 10.0.0.200\n' > "$HARNESS_STATE/cts/200" ;;
        dup) printf 'running codewaechter 10.0.0.208\n' > "$HARNESS_STATE/cts/208" ;;
        existing-stopped)
            printf 'stopped codewaechter 10.0.0.205\n' > "$HARNESS_STATE/cts/205"
            mkdir -p /etc/systemd/system
            printf 'ExecStart=/opt/codewaechter/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8030\n' \
                > /etc/systemd/system/codewaechter.service
            ;;
        status-running)
            printf 'running codewaechter 10.0.0.42\n' > "$HARNESS_STATE/cts/206"
            mkdir -p /opt/codewaechter/data
            seed_service_active
            ;;
        status-stopped)
            printf 'stopped codewaechter 10.0.0.42\n' > "$HARNESS_STATE/cts/206"
            mkdir -p /opt/codewaechter/data
            seed_service_active
            ;;
        uninstall)
            printf 'running codewaechter 10.0.0.207\n' > "$HARNESS_STATE/cts/207"
            ;;
        *)
            echo "unbekanntes Preset: $preset" >&2
            exit 64
            ;;
    esac
}

# ── Cases (laufen im NS; non_root bewusst nicht) ───────────────────────────
case_fresh_install() {
    run_installer
    assert_eq "$RC" 0 "Install-Exit-Code 0"
    assert_contains "$OUT" "CodeWächter ist bereit!" "Summary erscheint"
    assert_contains "$OUT" "http://10.0.0.42:8030" "Summary zeigt CT-IP + Port"
    assert_contains "$OUT" "API antwortet (HTTP 200 auf Port 8030)" "API-HTTP-Check"
    assert_contains "$OUT" "Web-UI antwortet (HTTP 200 auf Port 8030)" "SPA-HTTP-Check"
    assert_contains "$OUT" "API-Identität bestätigt" "API-Inhalt geprüft (CodeWächter-JSON)"
    assert_contains "$OUT" "MockRootPW1234" "generiertes Root-PW in Summary"

    assert_file /etc/systemd/system/codewaechter.service "Unit installiert"
    local u
    u=$(cat /etc/systemd/system/codewaechter.service)
    assert_contains "$u" "--port 8030" "__PORT__ wurde ersetzt (8030)"
    assert_contains "$u" "Restart=always" "Restart-Policy (Reboot/Absturz-sicher)"
    assert_contains "$u" "After=network-online.target" "After=network-online.target"
    assert_contains "$u" "User=codewaechter" "Service läuft als dedizierter Benutzer"
    assert_contains "$u" "/opt/codewaechter/.venv/bin/python -m uvicorn main:app" "uvicorn über venv-python"

    assert_file /opt/codewaechter/src/backend/main.py "Backend im Container"
    assert_file /opt/codewaechter/src/backend/static/index.html "Committed Frontend-Build im Container"
    assert_file /opt/codewaechter/.venv/bin/python "venv angelegt (gemockt)"
    assert_file /opt/codewaechter/data/projects "Datenverzeichnis angelegt"
    assert_file /var/log/codewaechter-setup.log "Inner-Setup-Log geschrieben"

    assert_file "$HARNESS_STATE/systemd/codewaechter.service.active" "Service aktiv (gemockt)"
    assert_file "$HARNESS_STATE/systemd/codewaechter.service.enabled" "Service enabled (Autostart)"

    local ua
    ua=$(cat "$HARNESS_STATE/useradd-calls.log" 2>/dev/null || echo "")
    assert_contains "$ua" "useradd --system" "Service-Benutzer angelegt"

    local c
    c=$(cat "$HARNESS_STATE/pct-create-200.args" 2>/dev/null || echo "")
    assert_contains "$c" "--hostname codewaechter" "CT heißt 'codewaechter'"
    assert_contains "$c" "--onboot 1" "onboot=1 (Reboot-sicher)"
    assert_contains "$c" "--unprivileged 1" "unprivilegierter CT"
    assert_contains "$c" "nesting=1" "nesting=1"
    assert_contains "$c" "--memory 2048" "RAM 2048 MB"
    assert_contains "$c" "--cores 2" "2 vCPUs"
    assert_contains "$c" "--rootfs local-lvm:8" "Rootfs 8 GB auf local-lvm"
    assert_contains "$c" "local:vztmpl/debian-12-standard" "Debian-12-Template"

    if [[ -f "$HARNESS_STATE/cts/200" ]]; then ok "CT-ID 200 vergeben (erste freie ab 200)"; else fail "CT-ID 200 vergeben (erste freie ab 200)"; fi
}

case_id_skip() {
    run_installer
    assert_eq "$RC" 0 "Install-Exit-Code 0 (belegte ID 200)"
    assert_no_file "$HARNESS_STATE/pct-create-200.args" "belegte ID 200 nicht erneut vergeben"
    assert_file "$HARNESS_STATE/pct-create-201.args" "nächste freie ID 201 vergeben"
}

case_dns_late() {
    run_installer
    assert_eq "$RC" 0 "Install-Exit-Code 0 (DNS erst ab 11. Aufruf ok → Fallback)"
    assert_contains "$OUT" "DNS-Fallback aktiv" "DNS-Fallback-Pfad wurde durchlaufen"
    assert_file /etc/dhcp/dhclient-enter-hooks.d/no-dns "Fallback-Hook geschrieben"
    local r
    r=$(cat /etc/resolv.conf)
    assert_contains "$r" "nameserver 1.1.1.1" "resolv.conf mit Fallback-Nameservern"
}

case_dns_flaky() {
    run_installer
    assert_eq "$RC" 0 "Install-Exit-Code 0 (flaky DNS, 3-Erfolge-Fenster)"
    if grep -q "DNS-Fallback aktiv" <<< "$OUT"; then
        fail "flaky4 ohne dauerhaften Defekt löst KEINEN Fallback aus (Grube 11a)"
    else
        ok "flaky4 ohne dauerhaften Defekt löst KEINEN Fallback aus (Grube 11a)"
    fi
    assert_contains "$OUT" "DNS im Container funktioniert (3x bestätigt)" "3 konsekutive Erfolge erkannt"
}

case_dns_dead() {
    run_installer
    assert_eq "$RC" 1 "DNS tot → Installer bricht ab (Exit 1)"
    assert_contains "$OUT" "DNS im Container weiterhin ohne Funktion" "Diagnose-Message bei totalem DNS"
    assert_contains "$OUT" "getent hosts" "Debug-Hinweis enthält getent-Kommando"
}

case_update_stopped() {
    run_installer --update
    assert_eq "$RC" 0 "Update-Exit-Code 0 (CT war gestoppt)"
    local calls
    calls=$(cat "$HARNESS_STATE/pct-calls.log" 2>/dev/null || echo "")
    assert_contains "$calls" "pct start 205" "gestoppter CT wurde automatisch gestartet"
    assert_contains "$OUT" "Update abgeschlossen" "Update-Summary erscheint"
    assert_contains "$OUT" "Analysedaten" "Update behält Datenverzeichnis"
    assert_file /opt/codewaechter/src/backend/main.py "Backend nach Update frisch installiert"
    assert_file "$HARNESS_STATE/systemd/codewaechter.service.active" "Service nach Update aktiv"
    # Datenverzeichnis bleibt bestehen (Update löscht nur src/)
    assert_file /opt/codewaechter/data "Datenverzeichnis nach Update vorhanden"
}

case_status_running() {
    run_installer --status
    assert_eq "$RC" 0 "Status-Exit-Code 0"
    assert_contains "$OUT" "codewaechter.service läuft" "Service-Status"
    assert_contains "$OUT" "http://10.0.0.42:8030" "Web-UI-URL mit IP + Port"
    assert_contains "$OUT" "2 Projekt(e)" "Projektanzahl aus API"
    assert_contains "$OUT" "Version:   1.0.0" "App-Version im Status"
}

case_status_stopped() {
    run_installer --status
    assert_eq "$RC" 0 "Status-Exit-Code 0 (CT gestoppt)"
    assert_contains "$OUT" "Container ist gestoppt" "Stopp-Warnung erscheint"
    assert_contains "$OUT" "pct start 206" "Start-Hinweis mit CT-ID"
}

case_uninstall_yes() {
    run_installer --uninstall --yes
    assert_eq "$RC" 0 "Deinstall-Exit-Code 0"
    assert_no_file "$HARNESS_STATE/cts/207" "CT 207 gelöscht"
    assert_contains "$OUT" "entfernt" "Deinstall-Bestätigung"
}

case_uninstall_confirm() {
    run_installer --uninstall
    assert_eq "$RC" 1 "Deinstall ohne Bestätigung bricht ab"
    assert_file "$HARNESS_STATE/cts/207" "CT 207 NICHT gelöscht (Schutz funktioniert)"
    assert_contains "$OUT" "Keine Bestätigung erhalten" "Abbruch-Message"
    assert_contains "$OUT" "--uninstall --yes" "Erzwinge-Hinweis mit exaktem Befehl"
}

case_dup_hostname() {
    run_installer
    assert_eq "$RC" 1 "Duplikat-Hostname → Abbruch"
    assert_contains "$OUT" "existiert bereits" "Duplikat-Diagnose"
    assert_contains "$OUT" "--update" "Hinweis auf --update"
    assert_contains "$OUT" "--force" "Hinweis auf --force"
}

case_port_option() {
    run_installer --port 8099
    assert_eq "$RC" 0 "Install-Exit-Code 0 (--port 8099)"
    local u
    u=$(cat /etc/systemd/system/codewaechter.service)
    assert_contains "$u" "--port 8099" "Port-Option in Unit übernommen"
    assert_contains "$OUT" "http://10.0.0.42:8099" "Summary zeigt Custom-Port"
}

case_help() {
    run_installer --help
    assert_eq "$RC" 0 "Help-Exit-Code 0"
    assert_contains "$OUT" "--port" "Help listet --port"
    assert_contains "$OUT" "--update" "Help listet --update"
}

case_zero_fallback() {
    # bash -c "$(wget ...)" --status: $0 ist "--status", kein $1 – der
    # Installer muss die Option trotzdem erkennen ($0-Fallback).
    OUT=$(bash -c "$(cat "$INSTALLER")" --status < /dev/null 2>&1)
    RC=$?
    assert_eq "$RC" 0 "Exit-Code 0 ($0-Fallback)"
    assert_contains "$OUT" "Status CodeWächter" "Option wurde als --status erkannt"
    assert_contains "$OUT" "codewaechter.service läuft" "Status-Output vollständig"
}

case_error_chain() {
    run_installer
    assert_eq "$RC" 9 "pct create-Fehler → Exit-Code 9 durchgereicht"
    assert_contains "$OUT" "Installationsfehler – Exit-Code 9" "on_error: Exit-Code genannt"
    assert_contains "$OUT" "Fehlgeschlagener Befehl: pct create" "on_error: fehlgeschlagener Befehl genannt"
    assert_contains "$OUT" "Letzte Logzeilen" "on_error: Log-Auszug"
    assert_contains "$OUT" "--debug" "on_error: bash -x-Hinweis mit exaktem Befehl"
}

case_non_root() {
    # Läuft bewusst AUSSERHALB des unshare-NS (dort wäre man root):
    OUT=$(bash "$INSTALLER" < /dev/null 2>&1)
    RC=$?
    assert_eq "$RC" 1 "nicht-root → Exit 1"
    assert_contains "$OUT" "Bitte als root ausführen" "Root-Prüfung greift"
}

# ── NS-Runner (host-seitig) ────────────────────────────────────────────────
declare -A PRESETS=(
    [fresh_install]=fresh [id_skip]=occupied200 [dns_late]=fresh [dns_flaky]=fresh
    [dns_dead]=fresh [update_stopped]=existing-stopped [status_running]=status-running
    [status_stopped]=status-stopped [uninstall_yes]=uninstall [uninstall_confirm]=uninstall
    [dup_hostname]=dup [port_option]=fresh [help]=fresh
    [zero_fallback]=status-running [error_chain]=fresh
)
declare -A DNS_MODES=(
    [dns_late]=late [dns_flaky]=flaky4 [dns_dead]=dead
)
declare -A FAIL_CREATE=(
    [error_chain]=1
)

run_case() {
    local name="$1"
    local preset="${PRESETS[$name]:-fresh}"
    local state="$WORK/state-$name"
    rm -rf "$state"
    mkdir -p "$state"
    RESULTS_FILE="$WORK/results-$name.log"
    : > "$RESULTS_FILE"
    export HARNESS_STATE="$state" HARNESS_SRC_TARBALL="$WORK/src.tar.gz" \
        RESULTS_FILE FAKEBIN_PATH="$FAKEBIN" \
        HARNESS_DNS_MODE="${DNS_MODES[$name]:-ok}" HARNESS_FAIL_CREATE="${FAIL_CREATE[$name]:-0}"

    if [[ "$name" == "non_root" ]]; then
        # Läuft bewusst ohne unshare (EUID bleibt unprivilegiert)
        "case_non_root"
    else
        unshare -rm bash "$SELF" --ns-setup --inner "$name" "$preset" < /dev/null
    fi

    # Ergebnis auswerten
    local o x
    o=$(grep -c '^OK' "$RESULTS_FILE" || true)
    x=$(grep -c '^FAIL' "$RESULTS_FILE" || true)
    if [[ "$x" -eq 0 ]]; then
        PASS=$((PASS + 1))
        echo "  ✔ $name  ($o Assertions)"
    else
        FAIL=$((FAIL + 1))
        FAILED_CASES+=("$name")
        echo "  ✘ $name  ($x von $((o + x)) Assertions fehlgeschlagen)"
        grep '^FAIL' "$RESULTS_FILE" | sed 's/^/      /'
    fi
}

CASES=(
    fresh_install
    id_skip
    dns_late
    dns_flaky
    dns_dead
    update_stopped
    status_running
    status_stopped
    uninstall_yes
    uninstall_confirm
    dup_hostname
    port_option
    help
    zero_fallback
    error_chain
    non_root
)

cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT

# ── Inner-Dispatch: läuft INNERHALB des unshare-Mount-Namespace ────────────
# (Nach allen Funktionsdefinitionen, damit seed/case verfügbar sind.)
MODE="${1:-host}"
if [[ "$MODE" == "--ns-setup" || "$MODE" == "--inner" ]]; then
    if [[ "$MODE" == "--ns-setup" ]]; then
        shift
        mount --make-rprivate /
        mkdir -p /opt /root /usr/local/bin /var/log /etc/dhcp /etc/systemd/system
        mount -t tmpfs tmpfs /opt
        mount -t tmpfs tmpfs /root
        mount -t tmpfs tmpfs /usr/local/bin
        mount -t tmpfs tmpfs /var/log
        mount -t tmpfs tmpfs /etc/dhcp
        mount -t tmpfs tmpfs /etc/systemd/system
        RESOLV_SCRATCH="/tmp/codewaechter-harness-resolv.$$"
        : > "$RESOLV_SCRATCH"
        mount --bind "$RESOLV_SCRATCH" /etc/resolv.conf
    fi
    shift  # --inner Flag ablegen
    CASE_NAME="${1:?Case-Name fehlt}"
    PRESET="${2:?Preset fehlt}"
    shift 2
    export PATH="$FAKEBIN:$PATH"
    seed_preset "$PRESET"
    "case_$CASE_NAME"
    exit 0
fi

# ── Main (host-seitig) ─────────────────────────────────────────────────────
mkdir -p "$WORK"

echo "🧪 CodeWaechter-Proxmox Mock-Harness"
echo "   Installer: $INSTALLER"

# Tarball-Fixture bauen (simuliert den codeload-Download des Repos):
# app/backend (inkl. committed static/) + systemd-Unit.
# --owner=0/--group=0: GitHub-Tarballs gehören uid 0 – im User-Namespace
# wäre ein chown auf phil-uid (1000) unmöglich ("Invalid argument", Grube 16).
PKGROOT="$WORK/pkgroot/CodeWaechter-Proxmox-main"
mkdir -p "$PKGROOT"
cp -r "$REPO/app/backend" "$PKGROOT/app-backend-tmp"
mkdir -p "$PKGROOT/app"
mv "$PKGROOT/app-backend-tmp" "$PKGROOT/app/backend"
rm -rf "$PKGROOT/app/backend/__pycache__"
cp -r "$REPO/systemd" "$PKGROOT/systemd"
tar --owner=0 --group=0 -czf "$WORK/src.tar.gz" -C "$WORK/pkgroot" CodeWaechter-Proxmox-main

echo ""
SELECTED=("${CASES[@]}")
if [[ $# -gt 0 ]]; then
    SELECTED=("$@")
fi
for name in "${SELECTED[@]}"; do
    run_case "$name"
done

echo ""
echo "════════════════════════════════════════"
if [[ $FAIL -eq 0 ]]; then
    echo "✅ ALLE ${PASS} TESTS BESTANDEN"
    echo "════════════════════════════════════════"
    exit 0
else
    echo "❌ ${FAIL} von $((PASS + FAIL)) Tests fehlgeschlagen:"
    for c in "${FAILED_CASES[@]}"; do
        echo "   ✘ $c"
    done
    echo "════════════════════════════════════════"
    exit 1
fi
