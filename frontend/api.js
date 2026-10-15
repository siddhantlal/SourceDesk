const messages = {
  invalid_credentials: 'The email or password is incorrect.', email_exists: 'An account with this email already exists. Try signing in.',
  duplicate_document: 'This PDF is already in this collection.', invalid_pdf: 'This file is not a readable PDF.',
  encrypted_pdf: 'Password-protected PDFs are not supported.', file_too_large: 'This PDF exceeds the server’s upload size limit.',
  too_many_pages: 'This PDF exceeds the server’s page limit.', not_found: 'This item is no longer available. Refresh the collection.',
  file_unavailable: 'The original PDF is unavailable.', validation_error: 'Check your input and try again.',
  provider_unavailable: 'The model provider is unavailable. Please try again later.', rate_limited: 'Too many requests. Please wait and try again.'
};
export class ApiError extends Error {
  constructor(message, status = 0, correlationId = '') { super(message); this.name = 'ApiError'; this.status = status; this.correlationId = correlationId; }
}
export function createClient({ getToken = () => '', onUnauthorized = () => {}, fetcher = (...args) => fetch(...args) } = {}) {
  const active = new Set();
  function failure(data, status, headers) {
    if (status === 401) onUnauthorized();
    const info = data?.error || data || {};
    const correlation = info.correlation_id || data?.correlation_id || headers?.get?.('X-Correlation-ID') || '';
    const message = status === 401 ? (info.code === 'invalid_credentials' ? messages.invalid_credentials : 'Your session could not be verified. Please sign in again.') : messages[info.code] || (status === 422 ? 'Check your input. It does not meet the server’s requirements.' : status >= 500 ? 'The service could not complete this request. Please try again.' : 'The request could not be completed. Please try again.');
    return new ApiError(message, status, String(correlation).slice(0, 128));
  }
  async function request(path, { method = 'GET', body, signal, timeout = 30000, auth = true, blob = false } = {}) {
    const controller = new AbortController();
    active.add(controller);
    const abort = () => controller.abort(signal?.reason);
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    const headers = new Headers({ Accept: blob ? 'application/pdf' : 'application/json' });
    if (auth && getToken()) headers.set('Authorization', `Bearer ${getToken()}`);
    if (body instanceof URLSearchParams) headers.set('Content-Type', 'application/x-www-form-urlencoded');
    else if (body !== undefined && !(body instanceof FormData)) { headers.set('Content-Type', 'application/json'); body = JSON.stringify(body); }
    try {
      const response = await fetcher(path.startsWith('/health') ? path : `/api/v1${path}`, { method, headers, body, signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw failure(data, response.status, response.headers); }
      if (response.status === 204) return null;
      return blob ? await response.blob() : await response.json();
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (timedOut) throw new ApiError('The request timed out. Check history before retrying a question; it may still have been saved.');
      if (controller.signal.aborted) throw new DOMException('Request cancelled', 'AbortError');
      throw new ApiError('Unable to reach SourceDesk. Check your connection and try again.');
    } finally { clearTimeout(timer); active.delete(controller); signal?.removeEventListener('abort', abort); }
  }
  function upload(collectionId, file, { signal, onProgress = () => {} } = {}) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const entry = { abort: () => xhr.abort() }; active.add(entry);
      const abort = () => xhr.abort();
      const cleanup = () => { active.delete(entry); signal?.removeEventListener('abort', abort); };
      xhr.open('POST', `/api/v1/collections/${encodeURIComponent(collectionId)}/documents`);
      xhr.timeout = 120000;
      if (getToken()) xhr.setRequestHeader('Authorization', `Bearer ${getToken()}`);
      xhr.upload.onprogress = event => onProgress(event.lengthComputable ? Math.round(event.loaded / event.total * 100) : null);
      xhr.onload = () => { cleanup(); let data; try { data = JSON.parse(xhr.responseText); } catch { data = {}; } if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(failure(data, xhr.status, { get: key => xhr.getResponseHeader(key) })); };
      xhr.onerror = () => { cleanup(); reject(new ApiError('Upload interrupted. Check your connection and try again.')); };
      xhr.ontimeout = () => { cleanup(); reject(new ApiError('Upload timed out. Refresh documents before trying again.')); };
      xhr.onabort = () => { cleanup(); reject(new DOMException('Upload cancelled', 'AbortError')); };
      if (signal?.aborted) { cleanup(); reject(new DOMException('Upload cancelled', 'AbortError')); return; }
      signal?.addEventListener('abort', abort, { once: true });
      const form = new FormData(); form.append('file', file); xhr.send(form);
    });
  }
  return { request, upload, cancelAll: () => { for (const item of active) item.abort(); active.clear(); },
    login: (email, password) => request('/auth/login', { method: 'POST', body: new URLSearchParams({ username: email, password }), auth: false }),
    register: (email, password) => request('/auth/register', { method: 'POST', body: { email, password }, auth: false }) };
}
