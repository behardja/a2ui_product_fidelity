// Minimal A2UI dev renderer (v0.9).
//
// Transport: raw A2A JSON-RPC (`message/send`) over fetch — no SDK. Rendering:
// the self-contained @a2ui/lit v0.9 renderer — a MessageProcessor seeded with the
// v0.9 basicCatalog drives an <a2ui-surface> (catalog + theme bundled, no manual
// context). Button clicks arrive via the processor's action handler, which we
// forward back to the agent as a userAction.

import { MessageProcessor } from '@a2ui/web_core/v0_9';
import { A2uiSurface, basicCatalog, Context } from '@a2ui/lit/v0_9';
import { ContextProvider } from '@lit/context';

void A2uiSurface; // ensure the <a2ui-surface> element module is evaluated/registered

const $ = (id) => document.getElementById(id);
const stage = $('stage');
const statusEl = $('status');

// The v0.9 Text widget turns `variant` into markdown (h2 -> "## text") and needs
// a markdown renderer to produce a heading; without one, the raw "##" shows.
// Provide a tiny async renderer (headings + bold), HTML-escaped for safety, and
// register it on the stage so every rendered <a2ui-surface> inherits it.
const escapeHtml = (s) =>
  String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
async function markdownRenderer(text) {
  let t = escapeHtml(text);
  t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  const h = t.match(/^(#{1,6})\s+([\s\S]*)$/);
  if (h) return `<h${h[1].length}>${h[2]}</h${h[1].length}>`;
  return t;
}
new ContextProvider(stage, { context: Context.markdown, initialValue: markdownRenderer });

const setStatus = (msg) => { statusEl.textContent = msg; };
const setBusy = (msg) => { statusEl.innerHTML = '<span class="spin"></span>' + msg; };
// Render a visible notice INTO the stage (so failures aren't hidden in the tiny
// status line). kind: 'info' | 'error'.
const showNotice = (title, detail, kind = 'info') => {
  const color = kind === 'error' ? '#D1453B' : '#17191C';
  stage.innerHTML =
    `<div class="empty"><div class="big" style="color:${color}">${title}</div>` +
    `<div style="margin-top:8px;font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap;text-align:left">${
      (detail || '').replace(/</g, '&lt;')
    }</div></div>`;
};
// Resolve the agent endpoint against the page URL so a relative value ("a2a")
// works both at the root and behind a path-prefixed proxy (/proxy/5173/).
const agentUrl = () =>
  new URL($('agentUrl').value.trim() || 'a2a', document.baseURI).toString();

// Current run config from the rail.
const cfg = () => ({
  threshold: parseFloat($('thr').value),
  maxRetries: parseInt($('retVal').textContent, 10),
});

// Optional creative direction for image generation (main-column field).
const creativePrompt = () => $('creativePrompt').value.trim();

// Selected image (generation) model.
const imageModel = () => $('imageModel').value;
const IMAGE_MODEL_INTENT = {
  'gemini-3.1-flash-lite-image': 'Fastest, lowest cost — near-real-time, high volume.',
  'gemini-3.1-flash-image': 'The generalist workhorse — best balance of quality, speed, and cost.',
  'gemini-3-pro-image': 'Highest quality — complex tasks, brand consistency, precise control.',
};
const updateImageModelHint = () => { $('imageModelHint').textContent = IMAGE_MODEL_INTENT[imageModel()] || ''; };
$('imageModel').addEventListener('change', updateImageModelHint);
updateImageModelHint();

let rpcId = 0;
// A2A conversation id — shared across a session so the agent keeps context
// (browse → evaluate). "New session" rotates it for a clean slate.
let sessionId = crypto.randomUUID();

// Estimated stage captions (A2A is non-streaming, so there is no live % — we show
// the real pipeline stages on a time estimate to keep the user oriented).
const EVAL_STEPS = [
  'Reading the reference image…',
  'Generating a candidate image (Nano Banana)…',
  'Scoring fidelity with Gecko…',
  'Assembling the Fidelity Report…',
];
const BROWSE_STEPS = ['Listing product images in the bucket…', 'Rendering the picker…'];

let progressTimer = null;
function startProgress(title, steps, estSeconds) {
  clearInterval(progressTimer);
  const started = Date.now();
  stage.innerHTML =
    '<div class="progress">' +
    '<div class="p-title"><span class="spin"></span><span id="pTitle"></span></div>' +
    '<div class="p-track"><div class="p-fill" id="pFill"></div></div>' +
    '<div class="p-step" id="pStep"></div>' +
    '<div class="p-note">Estimated progress — the agent runs server-side and returns when done.</div>' +
    '</div>';
  $('pTitle').textContent = title;
  const tick = () => {
    const fill = $('pFill'), step = $('pStep');
    if (!fill) return;
    const t = (Date.now() - started) / 1000;
    fill.style.width = Math.min(92, (t / estSeconds) * 92).toFixed(0) + '%';
    step.textContent = steps[Math.min(steps.length - 1, Math.floor(t / (estSeconds / steps.length)))];
  };
  tick();
  progressTimer = setInterval(tick, 500);
}
function stopProgress() { clearInterval(progressTimer); progressTimer = null; }

// Gray out the creative-direction field while a run is using it.
function setCreativeLocked(locked) {
  const el = $('creativePrompt');
  el.disabled = locked;
  el.classList.toggle('locked', locked);
}

async function sendMessage(parts, opts = {}) {
  const steps = opts.steps || EVAL_STEPS;
  const title = opts.title || 'Working…';
  const est = opts.est || 35;
  setStatus('');  // progress now lives in the stage panel, not the status line
  startProgress(title, steps, est);
  if (opts.lockCreative) setCreativeLocked(true);
  const body = {
    jsonrpc: '2.0',
    id: ++rpcId,
    method: 'message/send',
    params: {
      message: {
        kind: 'message',
        role: 'user',
        messageId: crypto.randomUUID(),
        contextId: sessionId,
        parts,
      },
    },
  };
  const url = agentUrl();
  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    stopProgress(); setCreativeLocked(false);
    showNotice('Could not reach the agent',
      'Fetch to ' + url + ' failed: ' + e.message +
      '\n\nIf you are on the raw external IP, the ~20s request likely got dropped ' +
      'mid-flight. See findings.md for reliable access options.', 'error');
    return;
  }
  const raw = await res.text();
  stopProgress();
  if (!res.ok) {
    setCreativeLocked(false);
    showNotice(`Agent HTTP ${res.status}`, `From ${url}:\n${raw.slice(0, 400)}`, 'error');
    return;
  }
  let json;
  try {
    json = JSON.parse(raw);
  } catch (e) {
    setCreativeLocked(false);
    showNotice(`Response was not JSON (HTTP ${res.status})`, `From ${url}:\n${raw.slice(0, 400)}`, 'error');
    return;
  }
  if (json.error) {
    setCreativeLocked(false);
    showNotice('Agent error', JSON.stringify(json.error, null, 1), 'error');
    return;
  }
  try {
    renderResult(json.result);   // on success the creative field stays locked (it's in use)
  } catch (e) {
    console.error('render failed:', e, json.result);
    setCreativeLocked(false);
    showNotice('Render failed', (e && e.message) + '\n(see browser console for the stack)', 'error');
  }
}

// Pull every DataPart across a Task/Message result and keep A2UI ones.
function collectA2uiMessages(result) {
  const msgs = [];
  const scan = (parts) => {
    for (const p of parts || []) {
      const mime = p?.metadata?.mimeType || p?.metadata?.mime_type;
      if (p?.kind === 'data' && p.data && (!mime || String(mime).includes('a2ui'))) {
        msgs.push(p.data);
      }
    }
  };
  if (!result) return msgs;
  if (result.kind === 'message') scan(result.parts);
  if (result.status?.message?.parts) scan(result.status.message.parts);
  for (const art of result.artifacts || []) scan(art.parts);
  return msgs;
}

function textOf(result) {
  const out = [];
  const scan = (parts) => {
    for (const p of parts || []) if (p?.kind === 'text' && p.text) out.push(p.text);
  };
  if (result?.kind === 'message') scan(result.parts);
  if (result?.status?.message?.parts) scan(result.status.message.parts);
  return out.join('\n');
}

// Split any message that merges several A2UI keys into separate single-key
// messages (LLMs sometimes emit {createSurface, updateComponents, updateDataModel}
// as one object; the processor requires one message per object). The `version`
// tag is preserved on each split message.
const A2UI_KEYS = ['createSurface', 'updateComponents', 'updateDataModel', 'deleteSurface'];
function expandA2ui(msgs) {
  const out = [];
  for (const m of msgs) {
    if (m && Array.isArray(m.messages)) { out.push(...expandA2ui(m.messages)); continue; } // unwrap envelope
    const present = A2UI_KEYS.filter((k) => m && k in m);
    if (present.length > 1) present.forEach((k) => out.push({ version: m.version, [k]: m[k] }));
    else out.push(m);
  }
  return out;
}

// Called by the MessageProcessor when a rendered Button fires its action. The
// context paths are already resolved against the surface's data model. Forward it
// to the agent as a userAction; for grid "select_reference" clicks, inject the
// rail's threshold/attempts (run_eval carries its own from the settings panel).
function actionHandler(action) {
  const name = action?.name ?? action?.event?.name;
  let context = action?.context ?? action?.event?.context ?? {};
  console.log('[a2ui] action:', name, context);
  if (!name) return;
  const isEval = name === 'select_reference' || name === 'run_eval';
  if (isEval) {
    // Inject the rail's config into any evaluation action (select_reference from
    // the grid, or run_eval from the settings widget).
    context = { ...context, imageModel: imageModel() };
    if (name === 'select_reference') {
      const c = cfg();
      context = { ...context, threshold: c.threshold, maxRetries: c.maxRetries };
      const p = creativePrompt();
      if (p) context.userPrompt = p;
    }
  }
  sendMessage([{ kind: 'data', data: { userAction: { name, context } } }],
    isEval ? { steps: EVAL_STEPS, title: 'Generating & evaluating…', est: 40, lockCreative: true } : {});
}

function renderResult(result) {
  const a2uiMessages = expandA2ui(collectA2uiMessages(result));
  const prose = textOf(result);
  console.log('[a2ui] collected messages:', a2uiMessages.length,
    a2uiMessages.map((m) => A2UI_KEYS.find((k) => k in m) || '?'));
  if (!a2uiMessages.length) {
    showNotice('No A2UI in the response', prose || '(no data parts found)', 'error');
    return;
  }
  if (!customElements.get('a2ui-surface')) {
    showNotice('Renderer not loaded', 'The <a2ui-surface> element is not registered — the @a2ui/lit/v0_9 import failed. Check the console.', 'error');
    return;
  }
  // Fresh processor per render so re-browsing the same surfaceId doesn't collide.
  let surface = null;
  const processor = new MessageProcessor([basicCatalog], actionHandler);
  processor.onSurfaceCreated((s) => { surface = s; });
  try {
    processor.processMessages(a2uiMessages);
  } catch (e) {
    console.error('processMessages failed:', e);
    showNotice('A2UI could not be processed', (e && e.message) || String(e), 'error');
    return;
  }
  if (!surface) {
    showNotice('No renderable surface', 'Messages parsed but no createSurface produced a surface.', 'error');
    return;
  }
  console.log('[a2ui] rendering surface', surface.id);
  const el = document.createElement('a2ui-surface');
  // Per-surface class (e.g. surface-fidelity-result) lets the shell scope CSS
  // vars (font size, image size) to a specific surface.
  if (surface.id) el.className = 'surface-' + String(surface.id).replace(/[^a-z0-9_-]/gi, '-');
  el.surface = surface;
  stage.replaceChildren(el);
  setStatus(prose || 'Rendered · ' + surface.id);
}

// ---- New session: reset the workflow to pick a fresh image ----
$('newSessionBtn').onclick = () => {
  sessionId = crypto.randomUUID();   // fresh agent conversation
  stopProgress();
  setCreativeLocked(false);
  $('creativePrompt').value = '';
  setMode('browse');
  stage.innerHTML =
    '<div class="empty"><div class="big">New session</div>' +
    'Browse a bucket or upload an image to run through the workflow.</div>';
  setStatus('');
};

// ---- Config rail interactions ----
$('thr').addEventListener('input', () => {
  $('thrOut').textContent = parseFloat($('thr').value).toFixed(2);
});
const stepRetries = (delta) => {
  const v = Math.min(5, Math.max(1, parseInt($('retVal').textContent, 10) + delta));
  $('retVal').textContent = String(v);
};
$('retMinus').onclick = () => stepRetries(-1);
$('retPlus').onclick = () => stepRetries(1);

// ---- Mode toggle ----
const setMode = (mode) => {
  const browse = mode === 'browse';
  $('tabBrowse').setAttribute('aria-selected', String(browse));
  $('tabUpload').setAttribute('aria-selected', String(!browse));
  $('modeBrowse').classList.toggle('active', browse);
  $('modeUpload').classList.toggle('active', !browse);
};
$('tabBrowse').onclick = () => setMode('browse');
$('tabUpload').onclick = () => setMode('upload');

// ---- Browse ----
$('browseBtn').onclick = () => {
  const prefix = $('prefix').value.trim();
  if (!prefix) return setStatus('Enter a gs:// prefix to browse.');
  sendMessage([{ kind: 'text', text: `List the images in ${prefix}` }],
    { steps: BROWSE_STEPS, title: 'Listing images…', est: 22 });
};
$('prefix').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('browseBtn').click(); });

// ---- Upload (drag-drop + picker) ----
let pendingFile = null;
const drop = $('drop');
const showPreview = (f) => {
  pendingFile = f;
  const url = URL.createObjectURL(f);
  $('dropThumb').src = url;
  $('dropName').textContent = f.name;
  $('dropPreview').style.display = 'flex';
};
drop.addEventListener('click', (e) => { if (e.target.closest('#uploadBtn')) return; $('file').click(); });
$('file').addEventListener('change', () => { if ($('file').files[0]) showPreview($('file').files[0]); });
['dragenter', 'dragover'].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('drag'); }));
['dragleave', 'drop'].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('drag'); }));
drop.addEventListener('drop', (e) => {
  const f = e.dataTransfer?.files?.[0];
  if (f && f.type.startsWith('image/')) showPreview(f);
});
$('uploadBtn').onclick = (e) => {
  e.stopPropagation();
  if (!pendingFile) return setStatus('Choose an image first.');
  const c = cfg();
  const p = creativePrompt();
  const reader = new FileReader();
  reader.onload = () => {
    const b64 = String(reader.result).split(',')[1];
    let text = `Evaluate this uploaded image. Use threshold=${c.threshold} and max_retries=${c.maxRetries} and image_model='${imageModel()}'.`;
    if (p) text += ` Creative direction: ${p}`;
    sendMessage([
      { kind: 'file', file: { name: pendingFile.name, mimeType: pendingFile.type || 'image/png', bytes: b64 } },
      { kind: 'text', text },
    ], { steps: ['Uploading image…', ...EVAL_STEPS], title: 'Generating & evaluating…', est: 45, lockCreative: true });
  };
  reader.readAsDataURL(pendingFile);
};

// ---- Access-path warning: raw external IP drops slow agent POSTs ----
// (Confirmed on this project: page GETs arrive, the ~20-40s browse POST never
// does. The authenticated Workbench proxy is the reliable path.)
(() => {
  const h = location.hostname;
  const safe = h === 'localhost' || h === '127.0.0.1' || h.endsWith('.googleusercontent.com');
  if (!safe) {
    statusEl.innerHTML =
      '<span style="color:#D1453B;font-weight:600">⚠ You are on the raw external IP (' + h + ').</span> ' +
      'On some networks the ~20–40s agent request is silently dropped and Browse hangs forever. ' +
      'Prefer the Workbench proxy link printed by server.py (https://…/proxy/5173/).';
  }
})();

// ---- Connection dot: ping the agent card on load ----
async function pingAgent() {
  const conn = $('conn'); const txt = $('connText');
  // The A2A message endpoint is POST-only; the card is served at /.well-known/…
  // under the same (possibly proxied) a2a path, which Vite rewrites to the agent.
  const cardUrl = agentUrl().replace(/\/$/, '') + '/.well-known/agent-card.json';
  try {
    const r = await fetch(cardUrl);
    if (r.ok) { conn.className = 'conn ok'; txt.textContent = 'agent connected'; return; }
    conn.className = 'conn bad'; txt.textContent = `agent HTTP ${r.status}`;
  } catch {
    conn.className = 'conn bad'; txt.textContent = 'agent unreachable';
  }
}
pingAgent();
