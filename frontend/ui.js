export function questionPayload(text, selected, documents) {
  text = text.trim();
  if (!text || text.length > 2000) throw new Error('Enter a question between 1 and 2,000 characters.');
  const payload = { text };
  if (selected.size) {
    const ids = [...selected];
    if (ids.length > 100 || ids.some(id => !documents.some(doc => doc.id === id && doc.status === 'ready'))) throw new Error('Select up to 100 ready documents, or choose all documents.');
    payload.document_ids = ids;
  }
  return payload;
}
export function resultLabel(status) {
  return ({ answered: 'Answered', insufficient_evidence: 'Insufficient evidence', provider_unavailable: 'Provider unavailable', internal_error: 'Processing error', generation_error: 'Citation verification failed', invalid_citations: 'Citation verification failed', refused: 'Unable to answer' })[status] || String(status || 'Unknown status').replaceAll('_', ' ');
}
export function isFakeProvider(health, warnings = []) { return health?.fake_provider === true || health?.provider === 'fake' || warnings.some(item => /fake/i.test(String(item))); }
export function documentError(doc) {
  const messages = {
    insufficient_text: 'No searchable text was found. Scanned or image-only PDFs require OCR, which is not supported in this version.',
    no_extractable_text: 'No searchable text was found. Scanned or image-only PDFs require OCR, which is not supported in this version.',
    extraction_limit: 'This PDF exceeds the safe extraction limits. Try a simpler or smaller text-based PDF.',
    provider_unavailable: 'The embedding provider was unavailable. The document could not be indexed.',
    provider_timeout: 'The embedding provider timed out. The document could not be indexed after bounded retries.',
    extraction_failed: 'Text extraction failed. Try exporting a new text-based PDF.'
  };
  return messages[doc.error_code] || 'This document could not be indexed. Try another text-based PDF or contact your administrator.';
}
export function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
export function button(text, className = 'button subtle', action) { const node = el('button', className, text); node.type = 'button'; if (action) node.addEventListener('click', action); return node; }
export function formatDate(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date); }
export function showError(target, error) { if (error?.name === 'AbortError') return; target.hidden = false; target.replaceChildren(el('span', '', error?.message || 'Something went wrong. Please try again.')); if (error?.correlationId) target.append(el('small', 'correlation', `Reference: ${error.correlationId}`)); }
