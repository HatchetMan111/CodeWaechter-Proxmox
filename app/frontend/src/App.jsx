import React, { useCallback, useEffect, useRef, useState } from 'react'

// ── API-Helfer ──────────────────────────────────────────────────────────────
async function api(path, options = {}) {
  const res = await fetch(path, options)
  let data = null
  try { data = await res.json() } catch { /* leere Antwort ist okay */ }
  if (!res.ok) {
    const detail = data && data.detail ? String(data.detail) : `HTTP ${res.status}`
    throw new Error(detail)
  }
  return data
}

const SEV_ORDER = ['hoch', 'mittel', 'niedrig', 'info']
const SEV_LABEL = { hoch: 'Hoch', mittel: 'Mittel', niedrig: 'Niedrig', info: 'Info' }

// plan_source → Anzeigetext (regeln-fallback = LLM war konfiguriert, ist aber
// ausgefallen → ehrlich anzeigen, statt still die Regel-Engine zu nutzen)
function planSourceLabel(source) {
  if (source === 'llm') return '🤖 LLM'
  if (source === 'regeln-fallback') return '⚙️ Regel-Engine (LLM-Fallback ⚠️)'
  return '⚙️ Regel-Engine'
}
function planSourceShort(source) {
  if (source === 'llm') return 'LLM'
  if (source === 'regeln-fallback') return 'Regel-Engine (LLM-Fallback)'
  return 'Regel-Engine'
}

function toastText(err) {
  return err && err.message ? err.message : String(err)
}

// ── Kleine Bausteine ────────────────────────────────────────────────────────
function ScoreRing({ score, size = 64 }) {
  const r = (size - 8) / 2
  const c = 2 * Math.PI * r
  const color = score >= 80 ? 'var(--ok)' : score >= 50 ? 'var(--warn)' : 'var(--err)'
  return (
    <svg width={size} height={size} className="score-ring" title={`Score ${score}/100`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--ring-bg)" strokeWidth="6" />
      <circle
        cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth="6"
        strokeLinecap="round"
        strokeDasharray={`${(c * score) / 100} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text x="50%" y="54%" textAnchor="middle" className="score-text" fill={color}>
        {score}
      </text>
    </svg>
  )
}

function SevChip({ sev, count, onClick, active }) {
  if (!count) return null
  return (
    <button className={`chip chip-${sev} ${active ? 'chip-active' : ''}`} onClick={onClick}>
      {SEV_LABEL[sev]}: {count}
    </button>
  )
}

function PriorityBadge({ prio }) {
  return <span className={`prio prio-${prio}`}>{prio}</span>
}

// ── Toast ───────────────────────────────────────────────────────────────────
function Toast({ toast }) {
  if (!toast) return null
  return (
    <div className={`toast toast-${toast.kind}`}>
      <pre>{toast.text}</pre>
    </div>
  )
}

// ── Neues Projekt (Modal) ───────────────────────────────────────────────────
function NewProjectModal({ onClose, onCreated, showToast }) {
  const [tab, setTab] = useState('path')
  const [name, setName] = useState('')
  const [sourceRef, setSourceRef] = useState('')
  const [file, setFile] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    try {
      if (tab === 'upload' && !file) {
        throw new Error('Bitte zuerst eine .zip-/.tar.gz-Datei auswählen.')
      }
      const body = { name, source_type: tab, source_ref: sourceRef, description: '' }
      const project = await api('/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (tab === 'upload') {
        try {
          const fd = new FormData()
          fd.append('file', file)
          const up = await api(`/api/projects/${project.id}/upload`, { method: 'POST', body: fd })
          showToast(`ok`, `Archiv entpackt: ${up.dateien} Dateien`)
        } catch (err) {
          // Upload fehlgeschlagen → Projekt wieder aufräumen (kein Waisen-Datensatz)
          try { await api(`/api/projects/${project.id}`, { method: 'DELETE' }) } catch { /* ignorieren */ }
          throw err
        }
      }
      onCreated(project)
    } catch (err) {
      showToast('err', toastText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>🛡️ Neues Projekt anlegen</h2>
        <div className="tabs">
          {[['path', '📁 Lokaler Pfad'], ['git', '🌐 Git-URL'], ['upload', '📦 Archiv-Upload']].map(([key, label]) => (
            <button key={key} className={`tab ${tab === key ? 'tab-active' : ''}`} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </div>

        <label className="field">
          <span>Name (optional)</span>
          <input value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="z. B. mein-webservice" />
        </label>

        {tab === 'path' && (
          <label className="field">
            <span>Pfad im Container</span>
            <input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)}
                   placeholder="/root/repos/mein-webservice" />
          </label>
        )}
        {tab === 'git' && (
          <label className="field">
            <span>Git-URL (https://… oder git@host:path)</span>
            <input value={sourceRef} onChange={(e) => setSourceRef(e.target.value)}
                   placeholder="https://github.com/HatchetMan111/CodeWaechter-Proxmox" />
          </label>
        )}
        {tab === 'upload' && (
          <label className="field">
            <span>Archiv (.zip / .tar.gz / .tgz / .tar, max. 200 MB)</span>
            <input type="file" accept=".zip,.tar.gz,.tgz,.tar"
                   onChange={(e) => setFile(e.target.files[0] || null)} />
          </label>
        )}

        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Abbrechen</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy ? '…' : 'Anlegen'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Einstellungen (Modal) ───────────────────────────────────────────────────
function SettingsModal({ onClose, showToast }) {
  const [s, setS] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api('/api/settings').then(setS).catch((e) => showToast('err', toastText(e)))
  }, [showToast])

  if (!s) return null

  const set = (key) => (e) => setS({ ...s, [key]: e.target.value })

  const save = async () => {
    setBusy(true)
    try {
      const saved = await api('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(s),
      })
      setS(saved)
      showToast('ok', 'Einstellungen gespeichert')
    } catch (err) {
      showToast('err', toastText(err))
    } finally {
      setBusy(false)
    }
  }

  const testLlm = async () => {
    setBusy(true)
    try {
      await api('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(s),
      })
      const res = await api('/api/settings/test-llm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ llm_base_url: s.llm_base_url, llm_api_key: s.llm_api_key, llm_model: s.llm_model }),
      })
      showToast(res.ok ? 'ok' : 'err', res.message)
    } catch (err) {
      showToast('err', toastText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>⚙️ Einstellungen</h2>
        <label className="field">
          <span>LLM-Base-URL (OpenAI-kompatibel, optional – z. B. Ollama oder ZCodeAI-Server im LAN)</span>
          <input value={s.llm_base_url} onChange={set('llm_base_url')}
                 placeholder="http://192.168.1.23:8000/v1" />
        </label>
        <label className="field">
          <span>LLM-API-Key (optional)</span>
          <input value={s.llm_api_key} onChange={set('llm_api_key')} type="password" placeholder="leer lassen, wenn nicht nötig" />
        </label>
        <label className="field">
          <span>LLM-Modell</span>
          <input value={s.llm_model} onChange={set('llm_model')} placeholder="z. B. qwen2.5-coder" />
        </label>
        <label className="field">
          <span>Ausschluss-Muster (Kommagetrennt, z. B. vendor/*,docs/*)</span>
          <input value={s.exclude_patterns} onChange={set('exclude_patterns')} placeholder="leer = alles analysieren" />
        </label>
        <p className="hint">Ohne LLM-Endpoint erzeugt CodeWächter Pläne aus der Regel-Engine – vollkommen offline.</p>
        <div className="modal-actions">
          <button className="btn" onClick={testLlm} disabled={busy}>🔌 Verbindung testen</button>
          <button className="btn" onClick={onClose}>Schließen</button>
          <button className="btn btn-primary" onClick={save} disabled={busy}>Speichern</button>
        </div>
      </div>
    </div>
  )
}

// ── Dashboard ───────────────────────────────────────────────────────────────
function Dashboard({ onOpen, onNew, onSettings, showToast }) {
  const [projects, setProjects] = useState(null)
  const [status, setStatus] = useState(null)

  const reload = useCallback(() => {
    api('/api/projects').then(setProjects).catch((e) => showToast('err', toastText(e)))
    api('/api/status').then(setStatus).catch(() => {})
  }, [showToast])

  useEffect(() => { reload() }, [reload])

  const analyze = async (p) => {
    showToast('info', `Analysiere „${p.name}" …`)
    try {
      const r = await api(`/api/projects/${p.id}/analyze`, { method: 'POST' })
      showToast('ok', `„${p.name}": Score ${r.score}/100 · ${r.counts.hoch} hoch · ${r.counts.mittel} mittel · Plan: ${planSourceShort(r.plan_source)}`)
      reload()
    } catch (err) {
      showToast('err', toastText(err))
    }
  }

  const remove = async (p) => {
    if (!window.confirm(`Projekt „${p.name}" inkl. Analyseergebnissen löschen?`)) return
    try {
      await api(`/api/projects/${p.id}`, { method: 'DELETE' })
      reload()
    } catch (err) {
      showToast('err', toastText(err))
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>🛡️ CodeWächter</h1>
          <p className="subtitle">
            Lokale Code-Review &amp; Implementierungspläne – wie Traycer, aber auf deinem Proxmox-Host.
            {status && <span> · v{status.version} · {status.projects} Projekt(e)</span>}
          </p>
        </div>
        <div className="header-actions">
          <button className="btn" onClick={onSettings}>⚙️ Einstellungen</button>
          <button className="btn btn-primary" onClick={onNew}>＋ Neues Projekt</button>
        </div>
      </header>

      {projects === null && <p className="hint">Lade Projekte …</p>}
      {projects !== null && projects.length === 0 && (
        <div className="empty-state">
          <p>Noch keine Projekte.</p>
          <p className="hint">Lege dein erstes Projekt an: Pfad im Container, Git-URL oder Archiv-Upload.</p>
        </div>
      )}

      <div className="card-grid">
        {(projects || []).map((p) => (
          <div key={p.id} className="card project-card">
            <div className="card-top">
              <ScoreRing score={p.score ?? 0} />
              <div className="card-title">
                <h3 onClick={() => onOpen(p.id)} className="link">{p.name}</h3>
                <span className={`src-badge src-${p.source_type}`}>
                  {p.source_type === 'git' ? '🌐 Git' : p.source_type === 'upload' ? '📦 Upload' : '📁 Pfad'}
                </span>
                {p.last_run_at && <span className="hint">Letzter Lauf: {new Date(p.last_run_at).toLocaleString('de-DE')}</span>}
                {!p.last_run_at && <span className="hint">Noch nicht analysiert</span>}
              </div>
            </div>
            <div className="chips">
              <SevChip sev="hoch" count={p.offen_hoch} />
              <SevChip sev="mittel" count={p.offen_mittel} />
              <SevChip sev="niedrig" count={p.offen_niedrig} />
              <SevChip sev="info" count={p.offen_info} />
            </div>
            {p.plan_gesamt > 0 && (
              <div className="plan-progress">
                <div className="plan-progress-bar">
                  <div style={{ width: `${(p.plan_erledigt / p.plan_gesamt) * 100}%` }} />
                </div>
                <span className="hint">Plan: {p.plan_erledigt}/{p.plan_gesamt} erledigt</span>
              </div>
            )}
            <div className="card-actions">
              <button className="btn" onClick={() => analyze(p)}>▶ Analyse</button>
              <button className="btn btn-primary" onClick={() => onOpen(p.id)}>Öffnen</button>
              <button className="btn btn-danger" onClick={() => remove(p)}>🗑</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Projekt-Detail ──────────────────────────────────────────────────────────
function ProjectView({ id, onBack, showToast }) {
  const [detail, setDetail] = useState(null)
  const [tab, setTab] = useState('findings')
  const [findings, setFindings] = useState([])
  const [sevFilter, setSevFilter] = useState('')
  const [plan, setPlan] = useState([])
  const [runs, setRuns] = useState([])
  const [analyzing, setAnalyzing] = useState(false)

  const reloadAll = useCallback(async () => {
    try {
      const d = await api(`/api/projects/${id}`)
      setDetail(d)
      const f = await api(`/api/projects/${id}/findings?status=alle&severity=${sevFilter}`)
      setFindings(f)
      setPlan(await api(`/api/projects/${id}/plan`))
      setRuns(await api(`/api/projects/${id}/runs`))
    } catch (err) {
      showToast('err', toastText(err))
    }
  }, [id, sevFilter, showToast])

  useEffect(() => { reloadAll() }, [reloadAll])

  const analyze = async () => {
    setAnalyzing(true)
    try {
      const r = await api(`/api/projects/${id}/analyze`, { method: 'POST' })
      showToast('ok', `Analyse fertig: Score ${r.score}/100 · Plan aus ${r.plan_source === 'llm' ? 'LLM' : 'Regel-Engine'}`)
      await reloadAll()
    } catch (err) {
      showToast('err', toastText(err))
    } finally {
      setAnalyzing(false)
    }
  }

  const patchFinding = async (fid, status) => {
    try {
      await api(`/api/findings/${fid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      })
      setFindings((fs) => fs.map((f) => (f.id === fid ? { ...f, status } : f)))
    } catch (err) {
      showToast('err', toastText(err))
    }
  }

  const patchPlan = async (itemId, done) => {
    try {
      await api(`/api/plan/${itemId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ done }),
      })
      setPlan((ps) => ps.map((p) => (p.id === itemId ? { ...p, done: done ? 1 : 0 } : p)))
    } catch (err) {
      showToast('err', toastText(err))
    }
  }

  if (!detail) return <div className="page"><p className="hint">Lade Projekt …</p></div>

  const counts = detail.letzter_lauf ? detail.letzter_lauf.counts : { hoch: 0, mittel: 0, niedrig: 0, info: 0 }
  const planDone = plan.filter((p) => p.done).length
  const offen = findings.filter((f) => f.status === 'offen')

  return (
    <div className="page">
      <header className="page-header">
        <div className="project-head">
          <button className="btn" onClick={onBack}>← Übersicht</button>
          <ScoreRing score={detail.score ?? 0} size={72} />
          <div>
            <h1>{detail.name}</h1>
            <p className="subtitle">
              {detail.local_path || detail.source_ref}
              {detail.letzter_lauf && (
                <> · {detail.letzter_lauf.files} Dateien · {detail.letzter_lauf.lines} Zeilen ·
                  {' '}Plan aus {planSourceLabel(detail.letzter_lauf.plan_source)}</>
              )}
            </p>
          </div>
        </div>
        <button className="btn btn-primary" onClick={analyze} disabled={analyzing}>
          {analyzing ? '⏳ Analysiere …' : '▶ Analyse starten'}
        </button>
      </header>

      <div className="chips chips-row">
        {SEV_ORDER.map((sev) => (
          <SevChip key={sev} sev={sev} count={offen.filter((f) => f.severity === sev).length}
                   active={sevFilter === sev}
                   onClick={() => setSevFilter(sevFilter === sev ? '' : sev)} />
        ))}
        <span className="chip chip-plan">Plan: {planDone}/{plan.length}</span>
      </div>

      <div className="tabs">
        {[['findings', `🔍 Findings (${offen.length})`], ['plan', `📋 Plan (${planDone}/${plan.length})`],
          ['verlauf', `📈 Verlauf (${runs.length})`]].map(([key, label]) => (
          <button key={key} className={`tab ${tab === key ? 'tab-active' : ''}`} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'findings' && (
        <div className="finding-list">
          {findings.filter((f) => f.status === 'offen').length === 0 && (
            <p className="empty-state">Keine offenen Findings{detail.score === 100 ? ' – sauber! 🎉' : ''}.</p>
          )}
          {findings.filter((f) => f.status === 'offen').map((f) => (
            <div key={f.id} className={`finding finding-${f.severity}`}>
              <div className="finding-head">
                <span className={`chip chip-${f.severity}`}>{SEV_LABEL[f.severity]}</span>
                <strong>{f.title}</strong>
                <span className="finding-loc link"
                      title="In Datei suchen">{f.path ? `${f.path}${f.line ? `:${f.line}` : ''}` : 'projektweit'}</span>
              </div>
              {f.detail && <p className="finding-detail">{f.detail}</p>}
              <div className="finding-actions">
                <button className="btn btn-small" onClick={() => patchFinding(f.id, 'erledigt')}>✔ Erledigt</button>
                <button className="btn btn-small" onClick={() => patchFinding(f.id, 'ignoriert')}>🙈 Ignorieren</button>
              </div>
            </div>
          ))}
          <details className="resolved-block">
            <summary>Erledigte / ignorierte Findings ({findings.filter((f) => f.status !== 'offen').length})</summary>
            {findings.filter((f) => f.status !== 'offen').map((f) => (
              <div key={f.id} className={`finding finding-${f.severity} finding-resolved`}>
                <div className="finding-head">
                  <span className={`chip chip-${f.severity}`}>{SEV_LABEL[f.severity]}</span>
                  <strong>{f.title}</strong>
                  <span className="finding-loc">{f.path || 'projektweit'}</span>
                  <span className="hint">{f.status}</span>
                </div>
                <div className="finding-actions">
                  <button className="btn btn-small" onClick={() => patchFinding(f.id, 'offen')}>↩ Wieder öffnen</button>
                </div>
              </div>
            ))}
          </details>
        </div>
      )}

      {tab === 'plan' && (
        <div className="plan-list">
          {plan.length === 0 && <p className="empty-state">Noch kein Plan – erst eine Analyse starten.</p>}
          {plan.map((item) => (
            <label key={item.id} className={`plan-item ${item.done ? 'plan-done' : ''}`}>
              <input type="checkbox" checked={!!item.done} onChange={(e) => patchPlan(item.id, e.target.checked)} />
              <div>
                <div className="plan-title"><PriorityBadge prio={item.priority} /> {item.title}</div>
                {item.detail && <p className="finding-detail">{item.detail}</p>}
              </div>
            </label>
          ))}
        </div>
      )}

      {tab === 'verlauf' && (
        <table className="runs-table">
          <thead>
            <tr><th>Lauf</th><th>Zeitpunkt</th><th>Score</th><th>Dateien</th><th>Zeilen</th><th>Hoch</th><th>Mittel</th><th>Niedrig</th><th>Info</th><th>Plan</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td>#{r.id}</td>
                <td>{new Date(r.started_at).toLocaleString('de-DE')}</td>
                <td><strong>{r.score}</strong></td>
                <td>{r.files}</td>
                <td>{r.lines}</td>
                <td className={`num-${r.counts.hoch ? 'bad' : 'ok'}`}>{r.counts.hoch}</td>
                <td>{r.counts.mittel}</td>
                <td>{r.counts.niedrig}</td>
                <td>{r.counts.info}</td>
                <td title={r.plan_source}>{r.plan_source === 'llm' ? '🤖' : r.plan_source === 'regeln-fallback' ? '⚠️⚙️' : '⚙️'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ── Root ────────────────────────────────────────────────────────────────────
export default function App() {
  const [view, setView] = useState({ name: 'dashboard' })
  const [modal, setModal] = useState(null)
  const [toast, setToast] = useState(null)
  const toastTimer = useRef(null)

  const showToast = useCallback((kind, text) => {
    setToast({ kind, text })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), kind === 'err' ? 10000 : 4000)
  }, [])

  return (
    <>
      {view.name === 'dashboard' && (
        <Dashboard
          onOpen={(id) => setView({ name: 'project', id })}
          onNew={() => setModal('new')}
          onSettings={() => setModal('settings')}
          showToast={showToast}
        />
      )}
      {view.name === 'project' && (
        <ProjectView key={view.id} id={view.id} onBack={() => setView({ name: 'dashboard' })} showToast={showToast} />
      )}
      {modal === 'new' && (
        <NewProjectModal
          onClose={() => setModal(null)}
          showToast={showToast}
          onCreated={(p) => {
            setModal(null)
            showToast('ok', `Projekt „${p.name}" angelegt`)
            setView({ name: 'project', id: p.id })
          }}
        />
      )}
      {modal === 'settings' && <SettingsModal onClose={() => setModal(null)} showToast={showToast} />}
      <Toast toast={toast} />
    </>
  )
}
