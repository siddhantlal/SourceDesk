import test from 'node:test';
import assert from 'node:assert/strict';

// One request vertical: real client encoding, response handling and session expiry.
test('authenticated requests preserve contracts and clear an expired session safely', async () => {
  const module = await import('../api.js').catch(() => ({}));
  assert.equal(typeof module.createClient, 'function', 'the request client is implemented');
  let token = 'session-only';
  const requests = [];
  let response = new Response(JSON.stringify({ access_token: 'issued' }), { status: 200 });
  const api = module.createClient({ getToken: () => token, onUnauthorized: () => { token = ''; }, fetcher: async (url, options) => { requests.push({ url, ...options }); return response; } });
  await api.login('reader@example.com', 'twelve-or-more');
  assert.equal(requests[0].url, '/api/v1/auth/login');
  assert.equal(new URLSearchParams(requests[0].body).get('username'), 'reader@example.com');
  response = new Response(JSON.stringify({ id: 'q1', status: 'answered' }), { status: 201 });
  await api.request('/collections/c1/questions', { method: 'POST', body: { text: 'Evidence?' } });
  assert.equal(requests[1].headers.get('Authorization'), 'Bearer session-only');
  assert.deepEqual(JSON.parse(requests[1].body), { text: 'Evidence?' });
  response = new Response(JSON.stringify({ error: { code: 'invalid_token', message: '<script>secret</script>', correlation_id: 'trace-123' } }), { status: 401 });
  await assert.rejects(api.request('/collections'), error => error.status === 401 && error.message.includes('sign in') && !error.message.includes('script') && error.correlationId === 'trace-123');
  assert.equal(token, '');
  response = new Response(null, { status: 204 });
  assert.equal(await api.request('/documents/d1', { method: 'DELETE' }), null);
});
