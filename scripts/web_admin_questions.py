"""
Web admin sekce — Hansovy otázky.
FastAPI router který se zaregistruje z web_admin.py.

Stránka: GET /questions/
API:
  GET  /questions/api?status=pending&target=all
  POST /questions/api/{qid}/answer    {answer, via}
  POST /questions/api/{qid}/dismiss
  POST /questions/api/{qid}/reassign  {target}

Použití (z web_admin.py):

    from scripts.web_admin_questions import (
        router as questions_router, init as questions_init)

    app = FastAPI()
    questions_init(config_dict, "data/hans_diary.db")
    app.include_router(questions_router)
"""

import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from scripts.hans_questions import (
    HansQuestionsStore,
    generate_hans_reaction,
    ANSWER_VIA_DASHBOARD,
)

_log = logging.getLogger("web_admin_questions")

# ─── Globální state ───────────────────────────────────────────────────────────
_CONFIG: Optional[dict] = None
_STORE: Optional[HansQuestionsStore] = None
_LOCK = threading.Lock()


def init(config: dict, db_path: str) -> None:
    """Volá se z web_admin.py při startu."""
    global _CONFIG, _STORE
    with _LOCK:
        _CONFIG = config
        _STORE = HansQuestionsStore(db_path, config)
    _log.info("web_admin_questions initialized (db=%s)", db_path)


def _require_store() -> HansQuestionsStore:
    if _STORE is None:
        raise HTTPException(500, "questions store not initialized — "
                                 "zavolej init(config, db_path) z web_admin.py")
    return _STORE


# ─── Router ───────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/questions", tags=["questions"])


class AnswerBody(BaseModel):
    answer: str
    via: str = ANSWER_VIA_DASHBOARD


class ReassignBody(BaseModel):
    target: str


@router.get("/api")
def list_questions(status: str = "pending",
                   target: str = "all",
                   limit: int = 200):
    store = _require_store()
    qs = store.list_questions(status=status, target=target, limit=limit)
    return {
        "questions": [q.to_dict() for q in qs],
        "stats": store.stats(),
        "count": len(qs),
    }


@router.post("/api/{qid}/answer")
def answer(qid: int, body: AnswerBody):
    store = _require_store()
    q = store.get_question(qid)
    if q is None:
        raise HTTPException(404, "question not found")
    if not body.answer.strip():
        raise HTTPException(400, "empty answer")

    store.answer_question(qid, body.answer, via=body.via)

    # Synchronní generování Hansovy reakce přes Ollama (max ~10s)
    reaction = None
    try:
        cfg = _CONFIG or {}
        ollama = cfg.get("openwebui_chat", {}).get(
            "base_url", "http://127.0.0.1:11434")
        model = (cfg.get("hans_dialog", {}).get("ollama_model")
                 or cfg.get("openwebui_chat", {}).get("model_name", "llama2"))
        reaction = generate_hans_reaction(
            question=q.question,
            answer=body.answer,
            ollama_url=ollama,
            model=model,
            target_person=q.target_person,
            config=cfg,
        )
        if reaction:
            store.set_reaction(qid, reaction)
    except Exception as e:
        _log.warning("Hans reaction failed: %s", e)

    return {"ok": True, "reaction": reaction}


@router.post("/api/{qid}/dismiss")
def dismiss(qid: int):
    _require_store().dismiss(qid)
    return {"ok": True}


@router.post("/api/{qid}/reassign")
def reassign(qid: int, body: ReassignBody):
    _require_store().reassign(qid, body.target)
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
def page():
    return _HTML


# ─── HTML stránka ─────────────────────────────────────────────────────────────
_HTML = r"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Hansovy otázky</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; margin: 0;
         background: #f4f1ec; color: #2a2520; }
  header { background: #2a2520; color: #f4f1ec; padding: 1rem 2rem; }
  header h1 { margin: 0; font-weight: 400; font-size: 1.4rem; }
  header .nav { margin-top: 0.4rem; }
  header .nav a { color: #c9b896; margin-right: 1rem; text-decoration: none;
                  font-size: 0.9rem; }
  header .nav a:hover { color: #fff; }
  main { max-width: 920px; margin: 1rem auto; padding: 0 1rem; }
  .tabs { display: flex; gap: 0.4rem; margin-bottom: 1rem; flex-wrap: wrap; }
  .tab { padding: 0.55rem 1.1rem; cursor: pointer; background: #e8e0d3;
         border: none; border-radius: 4px; font-size: 0.95rem;
         color: #2a2520; }
  .tab.active { background: #2a2520; color: #f4f1ec; }
  .badge { display: inline-block; background: #c9b896; color: #2a2520;
           border-radius: 10px; padding: 1px 7px; font-size: 0.75em;
           margin-left: 5px; }
  .tab.active .badge { background: #c9b896; color: #2a2520; }
  .filters { margin-bottom: 1rem; display: flex; gap: 0.6rem;
             align-items: center; flex-wrap: wrap; }
  .filters select, .filters button {
    padding: 0.4rem 0.7rem; border-radius: 4px;
    border: 1px solid #ccc; font-size: 0.9rem; }
  .card { background: #fff; border-radius: 6px; padding: 1rem; margin-bottom: 0.8rem;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .card .meta { color: #888; font-size: 0.83rem; margin-bottom: 0.4rem; }
  .card .meta .target { color: #2a2520; font-weight: 600; }
  .card .question { font-size: 1.05rem; margin-bottom: 0.4rem; line-height: 1.4; }
  .card .context { color: #777; font-style: italic; font-size: 0.88rem;
                   margin-bottom: 0.6rem; }
  .card textarea { width: 100%; min-height: 60px; padding: 0.5rem;
                   border: 1px solid #ccc; border-radius: 4px;
                   font-family: inherit; font-size: 0.95rem;
                   resize: vertical; }
  .actions { display: flex; gap: 0.5rem; margin-top: 0.5rem;
             align-items: center; flex-wrap: wrap; }
  .actions button, .actions select {
    padding: 0.45rem 0.85rem; border: none; border-radius: 4px;
    cursor: pointer; font-size: 0.9rem; }
  .btn-primary { background: #2a2520; color: white; }
  .btn-secondary { background: #c9b896; color: #2a2520; }
  .btn-danger { background: #b14a3a; color: white; }
  .actions select { background: #fff; border: 1px solid #ccc; cursor: default; }
  .answer-block { background: #ecf0e8; padding: 0.6rem 0.8rem; border-radius: 4px;
                  margin-top: 0.5rem; font-size: 0.95rem; }
  .answer-block .label { color: #666; font-size: 0.82rem;
                         text-transform: uppercase; letter-spacing: 0.5px; }
  .reaction { margin-top: 0.5rem; font-style: italic; color: #555;
              border-left: 3px solid #c9b896; padding-left: 0.6rem; }
  .empty { text-align: center; padding: 2.5rem; color: #888; }
  .toast { position: fixed; bottom: 1.5rem; right: 1.5rem;
           background: #2a2520; color: #f4f1ec; padding: 0.8rem 1.2rem;
           border-radius: 4px; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
           opacity: 0; transition: opacity 0.3s; max-width: 420px;
           font-size: 0.9rem; line-height: 1.4; }
  .toast.show { opacity: 1; }
  .toast .reaction-label { color: #c9b896; font-size: 0.78rem;
                           text-transform: uppercase; letter-spacing: 0.5px;
                           margin-bottom: 0.3rem; }
</style>
</head>
<body>
<header>
  <h1>Hansovy otázky</h1>
  <div class="nav">
    <a href="/">← Dashboard</a>
    <a href="javascript:reload()">Obnovit</a>
  </div>
</header>
<main>
  <div class="tabs">
    <button class="tab active" data-status="pending">Čeká na odpověď
      <span class="badge" id="badge-pending">0</span></button>
    <button class="tab" data-status="answered">Zodpovězené
      <span class="badge" id="badge-answered">0</span></button>
    <button class="tab" data-status="expired">Promlčené
      <span class="badge" id="badge-expired">0</span></button>
    <button class="tab" data-status="dismissed">Zrušené
      <span class="badge" id="badge-dismissed">0</span></button>
  </div>
  <div class="filters">
    <label>Pro:
      <select id="filter-target"><option value="all">Všichni</option></select>
    </label>
  </div>
  <div id="list"></div>
</main>
<div class="toast" id="toast"></div>

<script>
let currentStatus = 'pending';
let knownTargets = ['anyone'];

const escapeHtml = s => (s == null ? '' : String(s)).replace(
  /[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])
);

const cap = s => (s && typeof s === 'string')
  ? s.charAt(0).toUpperCase() + s.slice(1) : s;

const fmtAge = ts => {
  if (!ts) return '';
  const sec = Math.max(1, Date.now()/1000 - ts);
  if (sec < 60)    return 'právě teď';
  if (sec < 3600)  return Math.floor(sec/60) + ' min';
  if (sec < 86400) return Math.floor(sec/3600) + ' h';
  return Math.floor(sec/86400) + ' dní';
};

const sourceLabel = q => {
  const map = {reading:'četba', observation:'pozorování',
               kodi:'Kodi', thought:'myšlenka', news:'zprávy',
               url:'odkaz', self_question:'úvaha'};
  const t = map[q.source_type] || q.source_type;
  return q.source_ref ? `${t}: ${escapeHtml(q.source_ref)}` : t;
};

document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    currentStatus = t.dataset.status;
    reload();
  };
});

document.getElementById('filter-target').onchange = reload;

async function reload() {
  const target = document.getElementById('filter-target').value;
  let data;
  try {
    const r = await fetch(`/questions/api?status=${currentStatus}&target=${target}`);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    data = await r.json();
  } catch (e) {
    document.getElementById('list').innerHTML =
      `<div class="empty">Chyba načtení: ${escapeHtml(e.message)}</div>`;
    return;
  }
  const s = data.stats || {};
  document.getElementById('badge-pending').textContent   = s.pending   || 0;
  document.getElementById('badge-answered').textContent  = s.answered  || 0;
  document.getElementById('badge-expired').textContent   = s.expired   || 0;
  document.getElementById('badge-dismissed').textContent = s.dismissed || 0;

  const list = document.getElementById('list');
  if (!data.questions || !data.questions.length) {
    list.innerHTML = '<div class="empty">Žádné otázky v této kategorii.</div>';
    return;
  }
  list.innerHTML = data.questions.map(renderCard).join('');
}

function renderCard(q) {
  const target = q.target_person === 'anyone'
    ? 'pro kohokoliv' : 'pro ' + cap(q.target_person);
  const askedVoice = q.status === 'asked_voice'
    ? ' · <em>zeptal se hlasem</em>' : '';

  let body;
  if (q.status === 'pending' || q.status === 'asked_voice') {
    const opts = ['anyone', ...knownTargets.filter(t => t !== 'anyone')]
      .map(t => `<option value="${t}" ${t===q.target_person?'selected':''}>` +
                `${t==='anyone'?'kdokoliv':cap(t)}</option>`).join('');
    body = `
      <textarea id="ans-${q.id}" placeholder="Vaše odpověď..."></textarea>
      <div class="actions">
        <button class="btn-primary" onclick="submit(${q.id})">Odeslat</button>
        <button class="btn-danger" onclick="dismiss(${q.id})">Zrušit otázku</button>
        <label>Přesměrovat:
          <select onchange="reassign(${q.id}, this.value)">${opts}</select>
        </label>
      </div>`;
  } else if (q.status === 'answered') {
    const via = q.answer_via || 'dashboard';
    body = `
      <div class="answer-block">
        <div class="label">Odpověděl${via==='voice'?' (hlasem)':''}: ${cap(q.target_person)}</div>
        ${escapeHtml(q.answer)}
        ${q.hans_reaction
          ? `<div class="reaction">Hans: ${escapeHtml(q.hans_reaction)}</div>`
          : ''}
      </div>`;
  } else {
    body = '';
  }

  return `
    <div class="card">
      <div class="meta">
        <span class="target">${escapeHtml(target)}</span> ·
        před ${fmtAge(q.ts_asked)}${askedVoice} ·
        zdroj: ${sourceLabel(q)}
      </div>
      <div class="question">${escapeHtml(q.question)}</div>
      ${q.context ? `<div class="context">${escapeHtml(q.context)}</div>` : ''}
      ${body}
    </div>`;
}

function showToast(html, ms = 6000) {
  const t = document.getElementById('toast');
  t.innerHTML = html;
  t.classList.add('show');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => t.classList.remove('show'), ms);
}

async function submit(qid) {
  const ta = document.getElementById('ans-' + qid);
  const ans = ta.value.trim();
  if (!ans) { ta.focus(); return; }
  ta.disabled = true;
  try {
    const r = await fetch(`/questions/api/${qid}/answer`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({answer: ans, via: 'dashboard'}),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (data.reaction) {
      showToast(`<div class="reaction-label">Hansova reakce</div>${escapeHtml(data.reaction)}`);
    } else {
      showToast('Odpověď uložena.');
    }
    reload();
  } catch (e) {
    alert('Chyba: ' + e.message);
    ta.disabled = false;
  }
}

async function dismiss(qid) {
  if (!confirm('Zrušit tuto otázku? Hans se přestane ptát.')) return;
  await fetch(`/questions/api/${qid}/dismiss`, {method: 'POST'});
  reload();
}

async function reassign(qid, target) {
  await fetch(`/questions/api/${qid}/reassign`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({target}),
  });
  reload();
}

async function init() {
  // Sesbírej cílové osoby ze všech otázek (lehký dotaz)
  try {
    const r = await fetch('/questions/api?status=all&limit=500');
    const data = await r.json();
    const set = new Set();
    (data.questions || []).forEach(q => {
      if (q.target_person && q.target_person !== 'anyone') set.add(q.target_person);
    });
    knownTargets = ['anyone', ...Array.from(set).sort()];
    const sel = document.getElementById('filter-target');
    Array.from(set).sort().forEach(t => {
      const opt = document.createElement('option');
      opt.value = t; opt.textContent = cap(t);
      sel.appendChild(opt);
    });
  } catch (e) { /* ignore */ }
  reload();
  setInterval(reload, 30000);
}

init();
</script>
</body>
</html>
"""