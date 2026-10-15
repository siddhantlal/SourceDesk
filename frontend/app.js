import { createClient } from './api.js';
import { el, button, formatDate, showError, questionPayload, resultLabel, isFakeProvider, documentError } from './ui.js';

const $ = id => document.getElementById(id);
const state = { token: '', user: null, collections: [], collection: null, documents: [], questions: [], selected: new Set(), view: 'questions', authMode: 'login', health: null, fake: false, asking: false, uploading: false, loading: false };
let scope = new AbortController();
let sourceController = new AbortController();
let pollTimer;
let documentsGeneration = 0;
const blobUrls = new Set();
// Keep successful deletions authoritative even when a late read predates them.
const deletedDocuments = new Map();
function isDeletedDocument(id, collectionId = state.collection?.id) { return deletedDocuments.get(collectionId)?.has(id) || false; }
function redactQuestion(question, collectionId = state.collection?.id) {
  const redact = source => {
    if (!isDeletedDocument(source.document_id, collectionId)) return source;
    const { quote, ...reference } = source;
    return { ...reference, available: false, excerpt: null };
  };
  return { ...question, citations: question.citations?.map(redact), sources: question.sources?.map(redact) };
}
try { state.token = sessionStorage.getItem('sourcedesk.session') || ''; } catch { /* Memory-only sessions remain usable. */ }
const api = createClient({ getToken: () => state.token, onUnauthorized: () => endSession(true) });
function storeToken(token) { state.token = token; try { if (token) sessionStorage.setItem('sourcedesk.session', token); else sessionStorage.removeItem('sourcedesk.session'); } catch { /* Do not fall back to persistent storage. */ } }
function releaseBlobs() { for (const url of blobUrls) URL.revokeObjectURL(url); blobUrls.clear(); }
function notice(message) { $('workspace-status').textContent = message; }
function report(error, target = state.token ? $('workspace-error') : $('auth-error')) { showError(target, error); }
function on(id, event, action) { $(id).addEventListener(event, e => { try { Promise.resolve(action(e)).catch(error => report(error)); } catch (error) { report(error); } }); }
function resetSource() { sourceController.abort(); sourceController = new AbortController(); releaseBlobs(); renderSourceEmpty(); }
function cancelScope() { documentsGeneration++; clearTimeout(pollTimer); scope.abort(); scope = new AbortController(); resetSource(); state.asking = false; state.uploading = false; state.loading = false; $('question-pending').hidden = true; $('upload-status').hidden = true; $('pdf-input').disabled = false; $('dropzone').classList.remove('busy'); }
function updateProvider(warnings = []) { if (isFakeProvider(state.health, warnings)) state.fake = true; $('provider-banner').hidden = !state.fake; }
function endSession(expired = false) {
  storeToken(''); api.cancelAll(); cancelScope();
  deletedDocuments.clear();
  state.user = null; state.collection = null; state.collections = []; state.documents = []; state.questions = []; state.selected.clear();
  $('workspace').hidden = true; $('auth-screen').hidden = false; document.querySelector('.skip-link').href = '#main'; $('auth-form').reset(); $('password').type = 'password';
  $('auth-error').hidden = true; $('workspace-error').hidden = true;
  $('question-input').value = ''; $('question-history').replaceChildren(); $('document-list').replaceChildren(); $('collections').replaceChildren(); $('scope-options').replaceChildren(); $('account-email').textContent = '';
  $('collection-dialog').close(); $('confirm-dialog').close(); closeSidebar(); closeInspector();
  $('email').focus({ preventScroll: true });
  if (expired) report(new Error('Your session expired or could not be verified. Please sign in again.'), $('auth-error'));
}
async function allItems(path, signal) {
  const result = []; let offset = 0;
  while (true) {
    const data = await api.request(`${path}?limit=100&offset=${offset}`, { signal });
    if (!Array.isArray(data.items)) throw new Error('The service returned an unexpected response. Refresh and try again.');
    result.push(...data.items); offset += data.items.length;
    if (offset >= data.total || !data.items.length) return result;
    if (offset > 1000000) throw new Error('This collection is too large to load in one workspace.');
  }
}
function setAuthMode(mode) {
  state.authMode = mode; const register = mode === 'register';
  $('auth-title').textContent = register ? 'A fresh perspective.' : 'Welcome back.';
  $('auth-description').textContent = register ? 'Create your own space for grounded research.' : 'Sign in to pick up where you left off.';
  $('login-tab').classList.toggle('active', !register); $('register-tab').classList.toggle('active', register);
  $('login-tab').setAttribute('aria-pressed', String(!register)); $('register-tab').setAttribute('aria-pressed', String(register));
  $('password').minLength = register ? 12 : 1; $('password').autocomplete = register ? 'new-password' : 'current-password';
  $('password-help').textContent = register ? 'Use 12–128 characters. A longer, unique password is best.' : 'Use your SourceDesk account password.';
  $('auth-submit').replaceChildren(document.createTextNode(register ? 'Create account' : 'Sign in'), el('span', '', '↗'));
  $('auth-error').hidden = true;
}
async function authenticate(event) {
  event.preventDefault(); if ($('auth-submit').disabled) return;
  const email = $('email').value.trim(); const password = $('password').value;
  if (!$('auth-form').reportValidity()) return;
  $('auth-submit').disabled = true; $('login-tab').disabled = true; $('register-tab').disabled = true; $('auth-error').hidden = true;
  try {
    if (state.authMode === 'register') { await api.register(email, password); setAuthMode('login'); }
    const result = await api.login(email, password); storeToken(result.access_token); $('password').value = '';
    await enterWorkspace();
  } catch (error) { report(error, $('auth-error')); }
  finally { $('auth-submit').disabled = false; $('login-tab').disabled = false; $('register-tab').disabled = false; }
}
async function enterWorkspace() {
  const user = await api.request('/users/me'); if (!state.token) return;
  state.user = user; $('account-email').textContent = user.email; document.querySelector('.account-avatar').textContent = (user.email || 'R')[0].toUpperCase();
  $('auth-screen').hidden = true; $('workspace').hidden = false; document.querySelector('.skip-link').href = '#workbench'; $('workspace-error').hidden = true; $('auth-form').reset();
  try { await loadCollections(); } catch (error) { report(error); }
  if (state.token) $('workbench').focus({ preventScroll: true });
}
function renderCollections() {
  const target = $('collections'); target.replaceChildren();
  if (!state.collections.length) target.append(el('p', 'sidebar-empty', 'No collections yet. Start with a topic you want to explore.'));
  for (const collection of state.collections) {
    const item = button('', `collection-link${state.collection?.id === collection.id ? ' selected' : ''}`, () => { closeSidebar(); selectCollection(collection).catch(report); });
    item.append(el('span', 'collection-icon', '▤'), el('span', 'collection-label', collection.name));
    item.title = collection.name;
    if (state.collection?.id === collection.id) { item.setAttribute('aria-current', 'true'); item.append(el('span', 'selected-dot')); }
    target.append(item);
  }
}
async function loadCollections() {
  const token = state.token; $('reload-collections').disabled = true;
  try {
    const collections = await allItems('/collections'); if (!state.token || token !== state.token) return;
    state.collections = collections; renderCollections();
    const current = state.collections.find(item => item.id === state.collection?.id);
    if (!current) await selectCollection(state.collections[0] || null);
  } finally { $('reload-collections').disabled = false; }
}
function renderHeader() {
  const collection = state.collection;
  $('collection-title').textContent = collection?.name || 'Your next line of inquiry.';
  $('collection-description').textContent = collection ? collection.description || 'Documents, questions, and a clearer picture.' : 'Give your research a place to begin.';
  $('breadcrumb-name').textContent = collection?.name || 'GETTING STARTED';
  $('delete-collection').hidden = !collection; $('no-collection').hidden = Boolean(collection);
  $('question-count').textContent = String(state.questions.length); $('document-count').textContent = String(state.documents.length);
  $('questions-tab').disabled = !collection; $('library-tab').disabled = !collection;
  setView(state.view); renderCollections();
}
async function selectCollection(collection) {
  cancelScope(); const signal = scope.signal; const documentsTicket = ++documentsGeneration;
  state.collection = collection; state.documents = []; state.questions = []; state.selected.clear(); state.loading = Boolean(collection);
  $('question-input').value = ''; $('workspace-error').hidden = true; $('scope-popover').hidden = true; $('scope-button').setAttribute('aria-expanded', 'false');
  renderHeader(); renderDocuments(); renderQuestions(); renderScope();
  if (!collection) return;
  try {
    const [documents, questions] = await Promise.all([allItems(`/collections/${collection.id}/documents`, signal), allItems(`/collections/${collection.id}/questions`, signal)]);
    if (signal.aborted) return;
    if (documentsTicket === documentsGeneration) state.documents = documents.filter(doc => !isDeletedDocument(doc.id, collection.id));
    state.questions = questions.map(question => redactQuestion(question, collection.id)).sort((a, b) => new Date(a.created_at) - new Date(b.created_at)); state.loading = false;
    renderHeader(); renderDocuments(); renderQuestions(); renderScope(); schedulePoll();
    notice(`Opened ${collection.name}. ${state.documents.length} documents, ${questions.length} questions.`);
  } catch (error) { if (signal.aborted) return; state.loading = false; renderQuestions(); report(error); }
}
function setView(view) {
  state.view = view;
  for (const name of ['questions', 'library']) { const active = name === view; $(`${name}-tab`).setAttribute('aria-selected', String(active)); $(`${name}-tab`).tabIndex = active ? 0 : -1; $(`${name}-panel`).hidden = !state.collection || !active; }
}
function openCollectionDialog() { $('collection-form').reset(); $('collection-error').hidden = true; $('collection-dialog').showModal(); $('collection-name').focus(); }
async function createCollection(event) {
  event.preventDefault(); const submit = $('create-collection-submit'); if (submit.disabled) return;
  const name = $('collection-name').value.trim(); if (!name) { $('collection-name').setCustomValidity('Enter a collection name, not only spaces.'); $('collection-name').reportValidity(); return; }
  submit.disabled = true; $('collection-error').hidden = true;
  try {
    const collection = await api.request('/collections', { method: 'POST', body: { name, description: $('collection-notes').value.trim() } });
    if (!state.token) return; state.collections.unshift(collection); $('collection-dialog').close(); closeSidebar(); await selectCollection(collection); notice('Collection created.');
  } catch (error) { report(error, $('collection-error')); } finally { submit.disabled = false; }
}
function confirmDelete(title, description) {
  $('confirm-title').textContent = title; $('confirm-description').textContent = description; const dialog = $('confirm-dialog'); dialog.returnValue = ''; dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}
async function deleteCollection() {
  const collection = state.collection; if (!collection || $('delete-collection').disabled) return;
  if (!await confirmDelete('Delete this collection?', `“${collection.name}” and its documents and saved questions will be permanently deleted. This cannot be undone.`)) return;
  $('delete-collection').disabled = true;
  try {
    await api.request(`/collections/${collection.id}`, { method: 'DELETE' });
    // A fresh owner-scoped list is the authority after deletion.
    state.collections = await allItems('/collections');
    if (state.collection?.id === collection.id) await selectCollection(state.collections[0] || null); else renderCollections();
    notice('Collection deleted.');
  } finally { $('delete-collection').disabled = false; }
}
function renderQuestions() {
  const target = $('question-history'); target.replaceChildren();
  if (state.loading) { const loading = el('div', 'source-loading'); loading.append(el('span', 'spinner'), el('span', '', 'Opening your research…')); target.append(loading); }
  else if (!state.questions.length) {
    const empty = el('div', 'question-empty'); empty.append(el('span', 'eyebrow', 'The question is a good place to start.'), el('h2', '', 'A little curiosity.\nA clearer picture.'), el('p', '', 'Ask about the documents in this collection. Each answer keeps its sources close, so you can go beyond the summary.'));
    const help = el('div', 'empty-instruction'); help.append(el('span', '', '01 —'), button(state.documents.length ? 'Check your documents, then ask below ↘' : 'Add your first PDF to begin ↗', 'text-button', () => setView('library'))); empty.append(help); target.append(empty);
  }
  for (const question of state.questions) {
    updateProvider(question.warnings || []);
    const article = el('article', 'question-entry'); article.dataset.questionId = question.id;
    const meta = el('div', 'question-meta'); meta.append(el('span', 'eyebrow', 'Your question'), el('span', '', '·'), el('time', '', formatDate(question.created_at))); article.append(meta, el('h2', '', question.question));
    const head = el('div', 'answer-head'); head.append(el('span', 'answer-monogram', 'S·'), el('strong', '', 'SourceDesk'), el('span', `status-pill ${question.status === 'answered' ? '' : question.status === 'insufficient_evidence' ? 'warning' : 'error'}`, resultLabel(question.status))); article.append(head, el('div', 'answer-text', question.answer));
    if (question.citations?.length) {
      const citations = el('div', 'citation-row'); citations.setAttribute('aria-label', 'Answer citations');
      for (const source of question.citations) {
        const pill = button('', `citation-pill${source.available === false ? ' unavailable' : ''}`, () => openSource(question.id, source).catch(report));
        pill.append(el('span', 'citation-id', source.source_id), el('span', '', `${source.filename || 'Document'} · p. ${source.page_number || '—'}${source.available === false ? ' · unavailable' : ''}`)); citations.append(pill);
      }
      article.append(citations);
    }
    if (question.warnings?.length) article.append(el('div', 'answer-warnings', question.warnings.join('\n')));
    const footer = el('div', 'answer-footer');
    const timing = Number(question.timings?.total_ms); footer.append(el('small', '', `${question.model || 'Model not specified'}${Number.isFinite(timing) ? ` · ${(timing / 1000).toFixed(1)}s` : ''}`), button(`Inspect ${question.sources?.length || 0} retrieved sources ↗`, 'text-button', () => openSources(question.id).catch(report))); article.append(footer); target.append(article);
  }
  $('question-count').textContent = String(state.questions.length); updateComposer();
}
function updateComposer() {
  const ready = state.documents.some(doc => doc.status === 'ready');
  $('ask-button').disabled = state.asking || state.loading || !state.collection;
  $('question-input').disabled = state.asking || state.loading || !state.collection;
  $('ask-button').replaceChildren(document.createTextNode(state.asking ? 'Working…' : 'Ask question'), el('span', '', '↑'));
  $('question-pending').hidden = !state.asking;
  $('composer-help').textContent = ready ? 'Answers are only as good as their evidence. Check the sources.' : 'No ready documents yet. Upload a PDF or wait for indexing.';
}
async function askQuestion(event) {
  event.preventDefault(); if (state.asking || !state.collection || state.loading) return;
  const payload = questionPayload($('question-input').value, state.selected, state.documents);
  const signal = scope.signal; const id = state.collection.id; state.asking = true; $('workspace-error').hidden = true; updateComposer();
  try {
    const question = redactQuestion(await api.request(`/collections/${id}/questions`, { method: 'POST', body: payload, signal, timeout: 180000 }), id);
    if (signal.aborted) return;
    state.questions.push(question); $('question-input').value = ''; renderQuestions(); notice(resultLabel(question.status));
    // Read back the saved result rather than relying only on POST output.
    const saved = redactQuestion(await api.request(`/questions/${question.id}`, { signal }), id);
    if (!signal.aborted) { const index = state.questions.findIndex(item => item.id === saved.id); if (index >= 0) state.questions[index] = saved; renderQuestions(); }
  } catch (error) { if (!signal.aborted) report(error); }
  finally { if (!signal.aborted) { state.asking = false; updateComposer(); $('question-input').focus({ preventScroll: true }); } }
}
function renderScope() {
  for (const id of state.selected) if (!state.documents.some(doc => doc.id === id && doc.status === 'ready')) state.selected.delete(id);
  const target = $('scope-options'); target.replaceChildren();
  if (!state.documents.length) target.append(el('p', '', 'Add a PDF in Documents to choose a scope.'));
  for (const doc of state.documents) {
    const label = el('label', 'scope-option'); const input = document.createElement('input'); input.type = 'checkbox'; input.value = doc.id; input.checked = state.selected.has(doc.id); input.disabled = doc.status !== 'ready';
    input.addEventListener('change', () => { if (input.checked) state.selected.add(doc.id); else state.selected.delete(doc.id); renderScope(); });
    label.append(input, el('span', '', doc.filename), el('small', '', doc.status)); target.append(label);
  }
  $('scope-button').textContent = state.selected.size ? `${state.selected.size} selected document${state.selected.size === 1 ? '' : 's'} ▾` : 'All ready documents ▾';
}

function renderDocuments() {
  const target = $('document-list'); target.replaceChildren();
  if (!state.documents.length) target.append(el('p', 'documents-empty', state.loading ? 'Loading documents…' : 'No documents here yet. Add a PDF above, and we’ll keep its processing status here.'));
  for (const doc of state.documents) {
    const row = el('div', 'document-row'); row.dataset.documentId = doc.id; const info = el('div', 'document-info'); info.append(el('strong', '', doc.filename));
    const meta = el('div', 'document-meta'); meta.append(el('span', `status-pill ${doc.status === 'failed' ? 'error' : doc.status === 'ready' ? '' : 'warning'}`, ({ queued: 'Queued', processing: 'Indexing', ready: 'Ready', failed: 'Failed' })[doc.status] || doc.status), el('span', '', `${doc.page_count} page${doc.page_count === 1 ? '' : 's'}`)); info.append(meta);
    if (doc.status === 'failed') info.append(el('p', 'error-box', documentError(doc)));
    const actions = el('div', 'document-actions');
    const download = button('↓', 'icon-button', () => guardedButton(download, () => downloadDocument(doc)).catch(report)); download.setAttribute('aria-label', `Download ${doc.filename}`); download.title = 'Download original PDF';
    const remove = button('×', 'icon-button danger', () => guardedButton(remove, () => deleteDocument(doc)).catch(report)); remove.setAttribute('aria-label', `Delete ${doc.filename}`); remove.title = 'Delete document'; actions.append(download, remove); row.append(el('span', 'pdf-stamp', 'PDF'), info, actions); target.append(row);
  }
  $('document-count').textContent = String(state.documents.length); updateComposer();
}
async function guardedButton(target, action) { if (target.disabled) return; target.disabled = true; try { await action(); } finally { target.disabled = false; } }
async function refreshDocuments() {
  if (!state.collection) return;
  const signal = scope.signal; const collectionId = state.collection.id; const ticket = ++documentsGeneration;
  try {
    const docs = await allItems(`/collections/${collectionId}/documents`, signal);
    if (signal.aborted || ticket !== documentsGeneration) return;
    state.documents = docs.filter(doc => !isDeletedDocument(doc.id, collectionId)); renderDocuments(); renderScope(); schedulePoll();
  } catch (error) { if (!signal.aborted && ticket === documentsGeneration) throw error; }
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (state.collection && state.token && state.documents.some(doc => !['ready', 'failed'].includes(doc.status))) {
    pollTimer = setTimeout(() => { refreshDocuments().catch(error => { if (error.name !== 'AbortError') { report(error); notice('Status updates paused. Use Refresh in Documents to resume.'); } }); }, 2500);
  }
}
async function uploadFiles(files) {
  if (state.uploading || !state.collection || !files.length) return;
  const signal = scope.signal; const collectionId = state.collection.id; const selectedFiles = [...files];
  state.uploading = true; $('pdf-input').disabled = true; $('dropzone').classList.add('busy'); setView('library');
  const target = $('upload-status'); target.hidden = false; target.replaceChildren();
  try {
    for (const file of selectedFiles) {
      if (signal.aborted) return;
      const line = el('div', '', `Uploading ${file.name}…`); const progress = document.createElement('progress'); progress.max = 100; progress.value = 0; progress.setAttribute('aria-label', `Upload progress for ${file.name}`); target.append(line, progress);
      try {
        if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') throw new Error('Choose a PDF file. Other file types are not supported.');
        if (!file.size) throw new Error('This file is empty. Choose a readable PDF.');
        const doc = await api.upload(collectionId, file, { signal, onProgress: value => { if (value === null) progress.removeAttribute('value'); else progress.value = value; line.textContent = value === 100 ? `${file.name} — validating on the server…` : `${file.name} — uploading${value === null ? '…' : ` ${value}%`}`; } });
        if (signal.aborted) return;
        line.textContent = `${doc.filename} — accepted. ${doc.status === 'ready' ? 'Ready to search.' : 'Waiting for indexing.'}`; progress.remove();
        await refreshDocuments();
      } catch (error) { progress.remove(); if (signal.aborted) return; showError(line, error); line.prepend(el('strong', '', `${file.name}: `)); line.classList.add('error-box'); }
    }
  } finally { if (!signal.aborted) { state.uploading = false; $('pdf-input').disabled = false; $('pdf-input').value = ''; $('dropzone').classList.remove('busy'); schedulePoll(); } }
}
async function deleteDocument(doc) {
  const signal = scope.signal; const collectionId = state.collection.id;
  if (!await confirmDelete('Delete this document?', `“${doc.filename}” will be permanently deleted. Saved answers remain, but this document’s source excerpts will become unavailable.`) || signal.aborted) return;
  await api.request(`/documents/${doc.id}`, { method: 'DELETE', signal });
  if (!deletedDocuments.has(collectionId)) deletedDocuments.set(collectionId, new Set());
  deletedDocuments.get(collectionId).add(doc.id);
  if (signal.aborted) return;
  documentsGeneration++;
  // Never leave private excerpts or usable blob links waiting on a refetch.
  state.documents = state.documents.filter(item => item.id !== doc.id); state.selected.delete(doc.id);
  resetSource(); state.questions = state.questions.map(question => redactQuestion(question, collectionId));
  renderDocuments(); renderScope(); renderQuestions(); notice('Document deleted. Its saved sources are unavailable.');
  await refreshDocuments(); if (signal.aborted) return;
  const questions = await allItems(`/collections/${collectionId}/questions`, signal);
  if (signal.aborted) return; state.questions = questions.map(question => redactQuestion(question, collectionId)).sort((a, b) => new Date(a.created_at) - new Date(b.created_at)); renderQuestions(); notice('Document deleted. Saved sources have been refreshed.');
}
function makeBlobUrl(blob) { const url = URL.createObjectURL(blob); blobUrls.add(url); return url; }
async function downloadDocument(doc) {
  const signal = scope.signal; const collectionId = state.collection?.id; const blob = await api.request(`/documents/${doc.id}/download`, { blob: true, signal, timeout: 60000 }); if (signal.aborted || isDeletedDocument(doc.id, collectionId)) return;
  const url = makeBlobUrl(blob); const link = document.createElement('a'); link.href = url; link.download = doc.filename || 'document.pdf'; document.body.append(link); link.click(); link.remove();
  setTimeout(() => { URL.revokeObjectURL(url); blobUrls.delete(url); }, 60000);
}
function renderSourceEmpty() {
  const target = $('source-content'); target.replaceChildren(); const empty = el('div', 'source-empty'); empty.append(el('div', 'notebook-lines'), el('h2', '', 'A clear line back\nto the source.'), el('p', '', 'Select a citation in an answer to see the passage behind it, in its original context.'), el('p', 'small-note', 'The answer is a starting point.\nThe source is where you look closer.')); target.append(empty);
}
function showInspector() { closeSidebar(false); $('inspector').classList.add('open'); $('sources-toggle').setAttribute('aria-expanded', 'true'); if (matchMedia('(max-width:1190px)').matches) { $('close-inspector').focus({ preventScroll: true }); $('inspector').setAttribute('role', 'dialog'); $('inspector').setAttribute('aria-modal', 'true'); } }
function closeInspector(restoreFocus = true) { const wasOpen = $('inspector').classList.contains('open'); $('inspector').classList.remove('open'); $('inspector').removeAttribute('role'); $('inspector').removeAttribute('aria-modal'); $('sources-toggle').setAttribute('aria-expanded', 'false'); if (wasOpen && restoreFocus && state.token) $('sources-toggle').focus({ preventScroll: true }); }
function beginSource() { resetSource(); showInspector(); $('source-content').replaceChildren(); const loading = el('div', 'source-loading'); loading.append(el('span', 'spinner'), el('span', '', 'Checking saved sources…')); $('source-content').append(loading); return sourceController.signal; }
async function freshQuestion(id, signal) { const collectionId = state.collection?.id; const question = redactQuestion(await api.request(`/questions/${id}`, { signal }), collectionId); if (!signal.aborted) { const index = state.questions.findIndex(q => q.id === id); if (index >= 0) state.questions[index] = question; renderQuestions(); } return question; }
async function openSources(id) {
  const signal = beginSource();
  try {
    const question = await freshQuestion(id, signal); if (signal.aborted) return;
    const target = $('source-content'); target.replaceChildren(el('span', 'source-breadcrumb', 'RETRIEVED CONTEXT'), el('h2', '', 'Behind the answer.'), el('p', 'muted', 'Retrieved passages are context, not proof that every claim is supported. Open each source to read closely.'));
    if (!question.sources?.length) target.append(el('p', 'source-unavailable', 'No eligible passages were retrieved for this question.'));
    for (const source of question.sources || []) { const card = button('', 'source-card', () => openSource(id, source).catch(report)); card.append(el('strong', '', `${source.source_id} · ${source.filename || 'Document'}`), el('small', '', `Page ${source.page_number || '—'}${source.available === false ? ' · Source unavailable' : ''}`)); target.append(card); }
  } catch (error) { if (!signal.aborted) sourceError(error); }
}
function sourceError(error) { const box = el('div', 'error-box source-error'); showError(box, error); $('source-content').replaceChildren(el('h2', '', 'Source unavailable.'), box); }
async function openSource(questionId, reference) {
  const signal = beginSource();
  try {
    const question = await freshQuestion(questionId, signal); if (signal.aborted) return;
    const source = [...(question.citations || []), ...(question.sources || [])].find(item => item.source_id === reference.source_id && item.chunk_id === reference.chunk_id);
    if (!source) throw new Error('This saved source is no longer available.');
    const target = $('source-content'); target.replaceChildren(el('span', 'source-breadcrumb', `SOURCE ${source.source_id}`), el('h2', 'source-title', source.filename || 'Original document'));
    const page = el('div', 'source-page'); page.append(el('span', '', `Page ${source.page_number || '—'}`), el('span', '', '·'), el('span', '', source.available === false ? 'Unavailable' : 'Extracted passage')); target.append(page);
    if (source.available === false) { const unavailable = el('div', 'source-unavailable'); unavailable.append(el('strong', '', 'This source is no longer available.'), el('p', '', 'The document may have been deleted. Its excerpt and quote have been removed; the saved answer is retained for your history.')); target.append(unavailable); return; }
    if (source.quote) target.append(el('h3', 'source-section-heading', 'Quoted in the answer'), el('blockquote', 'source-quote', source.quote));
    target.append(el('h3', 'source-section-heading', 'Exact retrieved excerpt'), el('div', 'source-excerpt', source.excerpt || 'No excerpt was returned for this passage.'));
    const actions = el('div', 'source-actions'); const original = button('Get original PDF ↗', 'button subtle', () => guardedButton(original, async () => {
      const blob = await api.request(`/documents/${source.document_id}/download`, { signal, blob: true, timeout: 60000 }); if (signal.aborted) return;
      const url = makeBlobUrl(blob); const open = el('a', 'button primary', `Open PDF · page ${source.page_number}`); open.href = `${url}#page=${Number(source.page_number) || 1}`; open.target = '_blank'; open.rel = 'noopener noreferrer'; const download = el('a', 'button subtle', 'Download PDF ↓'); download.href = url; download.download = source.filename || 'document.pdf'; actions.replaceChildren(open, download);
    }).catch(error => { if (!signal.aborted) { const box = el('div', 'error-box'); showError(box, error); actions.after(box); } })); actions.append(original); target.append(actions, el('p', 'source-disclaimer', 'Page numbers refer to the original PDF, not printed page labels. Citation structure is not a guarantee of semantic support.'));
    const details = el('details', 'page-details'); const summary = el('summary', '', 'Read the full extracted page'); const text = el('div', 'page-text', 'Loading page text…'); details.append(summary, text); target.append(details);
    let loaded = false;
    details.addEventListener('toggle', () => { if (!details.open || loaded) return; loaded = true; api.request(`/documents/${source.document_id}/pages/${source.page_number}`, { signal }).then(data => { if (!signal.aborted) text.textContent = data.text || 'No text was extracted from this page.'; }).catch(error => { if (!signal.aborted) showError(text, error); }); });
    target.append(button('← All retrieved sources', 'text-button', () => openSources(questionId).catch(report)));
  } catch (error) { if (!signal.aborted) sourceError(error); }
}
function closeSidebar(restoreFocus = true) { const wasOpen = $('sidebar').classList.contains('open'); $('sidebar').classList.remove('open'); $('sidebar').removeAttribute('role'); $('sidebar').removeAttribute('aria-modal'); $('pane-scrim').hidden = true; $('menu-toggle').setAttribute('aria-expanded', 'false'); if (wasOpen && restoreFocus && state.token && matchMedia('(max-width:750px)').matches) $('menu-toggle').focus({ preventScroll: true }); }
function toggleSidebar() {
  if ($('sidebar').classList.contains('open')) { closeSidebar(); return; }
  closeInspector(false); $('sidebar').classList.add('open'); $('pane-scrim').hidden = false; $('menu-toggle').setAttribute('aria-expanded', 'true');
  if (matchMedia('(max-width:750px)').matches) { $('sidebar').setAttribute('role', 'dialog'); $('sidebar').setAttribute('aria-modal', 'true'); }
  $('new-collection').focus({ preventScroll: true });
}
function offcanvasPane() {
  // Native modal dialogs own keyboard handling and focus until they close.
  if (!state.token || $('workspace').hidden || document.querySelector('dialog[open]')) return null;
  if (matchMedia('(max-width:750px)').matches && $('sidebar').classList.contains('open')) return $('sidebar');
  if (matchMedia('(max-width:1190px)').matches && $('inspector').classList.contains('open')) return $('inspector');
  return null;
}
function paneFocusables(pane) { return [...pane.querySelectorAll('button, a[href], input, select, textarea, summary, [tabindex]')].filter(node => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden'); }
// Forms use native validation first; every asynchronous entry point catches errors.
on('auth-form', 'submit', authenticate); on('login-tab', 'click', () => setAuthMode('login')); on('register-tab', 'click', () => setAuthMode('register'));
on('signout', 'click', () => endSession()); on('new-collection', 'click', openCollectionDialog); on('first-collection', 'click', openCollectionDialog);
on('collection-form', 'submit', createCollection); on('collection-name', 'input', () => $('collection-name').setCustomValidity('')); on('close-collection-dialog', 'click', () => $('collection-dialog').close());
on('delete-collection', 'click', deleteCollection); on('reload-collections', 'click', loadCollections);
on('questions-tab', 'click', () => setView('questions')); on('library-tab', 'click', () => setView('library'));
for (const id of ['questions-tab', 'library-tab']) on(id, 'keydown', event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const view = event.key === 'Home' ? 'questions' : event.key === 'End' ? 'library' : state.view === 'questions' ? 'library' : 'questions'; setView(view); $(`${view}-tab`).focus(); } });
on('question-form', 'submit', askQuestion); on('question-input', 'keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('question-form').requestSubmit(); } });
on('scope-button', 'click', () => { const open = $('scope-popover').hidden; $('scope-popover').hidden = !open; $('scope-button').setAttribute('aria-expanded', String(open)); });
on('scope-all', 'click', () => { state.selected.clear(); renderScope(); });
on('refresh-documents', 'click', () => guardedButton($('refresh-documents'), refreshDocuments)); on('pdf-input', 'change', event => uploadFiles(event.target.files));
for (const event of ['dragenter', 'dragover']) on('dropzone', event, e => { e.preventDefault(); if (!state.uploading) $('dropzone').classList.add('dragging'); });
on('dropzone', 'dragleave', () => $('dropzone').classList.remove('dragging')); on('dropzone', 'drop', event => { event.preventDefault(); $('dropzone').classList.remove('dragging'); return uploadFiles(event.dataTransfer.files); });
on('menu-toggle', 'click', toggleSidebar); on('pane-scrim', 'click', closeSidebar); on('close-inspector', 'click', closeInspector); on('sources-toggle', 'click', () => { if ($('inspector').classList.contains('open')) closeInspector(); else showInspector(); });
document.addEventListener('keydown', event => {
  if (document.querySelector('dialog[open]')) return;
  const pane = offcanvasPane();
  if (event.key === 'Escape') {
    if (pane) { event.preventDefault(); if (pane === $('sidebar')) closeSidebar(); else closeInspector(); }
    $('scope-popover').hidden = true; $('scope-button').setAttribute('aria-expanded', 'false');
  }
  if (event.key === 'Tab' && pane) {
    const focusable = paneFocusables(pane); const first = focusable[0]; const last = focusable.at(-1);
    if (!pane.contains(document.activeElement) || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) { event.preventDefault(); (event.shiftKey ? last : first)?.focus(); }
  }
});
document.addEventListener('focusin', event => { const pane = offcanvasPane(); if (pane && !pane.contains(event.target)) paneFocusables(pane)[0]?.focus({ preventScroll: true }); });
matchMedia('(max-width:750px)').addEventListener('change', event => { if (!event.matches) closeSidebar(false); });
document.addEventListener('click', event => { if (!$('scope-popover').contains(event.target) && !$('scope-button').contains(event.target)) { $('scope-popover').hidden = true; $('scope-button').setAttribute('aria-expanded', 'false'); } });
window.addEventListener('pagehide', () => { releaseBlobs(); clearTimeout(pollTimer); });
setAuthMode('login'); renderSourceEmpty();
api.request('/health', { auth: false, timeout: 8000 }).then(health => { state.health = health; updateProvider(); }).catch(() => { /* Health is advisory; authentication reports actionable failures. */ });
if (state.token) enterWorkspace().catch(error => { if (state.token) { $('auth-screen').hidden = false; $('workspace').hidden = true; report(error, $('auth-error')); } });
