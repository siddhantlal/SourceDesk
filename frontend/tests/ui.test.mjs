import test from 'node:test';
import assert from 'node:assert/strict';

test('question scope only narrows ready documents and result states stay distinct', async () => {
  const ui = await import('../ui.js').catch(() => ({}));
  assert.equal(typeof ui.questionPayload, 'function', 'question scope helper is implemented');
  const documents = [{ id: 'ready', status: 'ready' }, { id: 'queued', status: 'queued' }];
  assert.deepEqual(ui.questionPayload(' Evidence? ', new Set(), documents), { text: 'Evidence?' });
  assert.deepEqual(ui.questionPayload('Evidence?', new Set(['ready']), documents), { text: 'Evidence?', document_ids: ['ready'] });
  assert.throws(() => ui.questionPayload('Evidence?', new Set(['queued']), documents), /ready/i);
  assert.throws(() => ui.questionPayload('   ', new Set(), documents), /question/i);
  assert.equal(ui.resultLabel('answered'), 'Answered');
  assert.equal(ui.resultLabel('insufficient_evidence'), 'Insufficient evidence');
  assert.equal(ui.resultLabel('provider_unavailable'), 'Provider unavailable');
  assert.equal(ui.resultLabel('internal_error'), 'Processing error');
  assert.equal(ui.isFakeProvider({ fake_provider: true }, []), true);
  assert.equal(ui.isFakeProvider({}, ['FAKE deterministic providers; not a real-model quality result.']), true);
  assert.equal(ui.isFakeProvider({ fake_provider: false }, []), false);
  assert.equal(typeof ui.documentError, 'function');
  assert.match(ui.documentError({ error_code: 'insufficient_text' }), /OCR/i);
  assert.match(ui.documentError({ error_code: 'extraction_limit' }), /limits/i);
  assert.doesNotMatch(ui.documentError({ error_code: 'unknown', error_message: '<script>private provider payload</script>' }), /script|private provider/i);
});
