import test from 'node:test';
import assert from 'node:assert/strict';
import { foldPending, readLivePending, loopbackOrigin } from '../state.js';
import { startBridge, validateConfig, apply } from '../index.js';

const events = (...items) => items.map(([type, data = {}], seq) => ({ type, data, seq, time: 1 }));
const start = ['turn/start'];
const ask = (callId = 'q1', question = 'Choose a mode?') => ['tool/call', {
  name: 'ask_user_question', callId, arguments: JSON.stringify({ questions: [{ id: 'choice', question }] }),
}];
const result = (callId = 'q1', body = { answers: [] }, error) => ['tool/result', {
  message: { toolCallId: callId, content: [{ type: 'text', text: JSON.stringify(body) }] },
  ...(error ? { error } : {}),
}];
const header = timed => ['request/header', { header: { tools: [{ name: 'ask_user_question',
  parameters: { properties: timed ? { timeout: {} } : {} } }] } }];
const approval = id => ['approval/asked', { id, toolName: 'bash', reason: 'Needs write access' }];

test('approval opens only inside an open turn and every decision closes it', () => {
  assert.equal(foldPending('s', events(approval('a'))).length, 0);
  const pending = foldPending('s', events(start, approval('a')));
  assert.equal(pending[0].interaction_id, 'approval:a');
  assert.equal(pending[0].kind, 'permission');
  for (const outcome of ['allowed-once', 'rejected', 'cancelled', 'unavailable']) {
    assert.deepEqual(foldPending('s', events(start, approval('a'), ['approval/decided', { id: 'a', outcome }])), []);
  }
  assert.deepEqual(foldPending('s', events(start, approval('a'), ['turn/end'])), []);
});

test('permission policy never produces no pending notification after paired events', () => {
  assert.deepEqual(foldPending('s', events(start, approval('a'), ['approval/decided', { id: 'a', outcome: 'rejected' }])), []);
});

test('concurrent permissions/questions retain independent and namespaced identities', () => {
  const pending = foldPending('s', events(start, approval('same'), ask('same'), approval('b'),
    ['approval/decided', { id: 'b', outcome: 'cancelled' }]));
  assert.deepEqual(pending.map(x => x.interaction_id), ['approval:same', 'question:same']);
});

test('legacy question blocks until result, including error/abort; turn closure clears it', () => {
  assert.equal(foldPending('s', events(start, header(false), ask())).length, 1);
  for (const tail of [result(), result('q1', {}, { code: 'ASK_ABORTED' }), ['turn/end']]) {
    assert.deepEqual(foldPending('s', events(start, header(false), ask(), tail)), []);
  }
});

test('timed pending/unknown remains after turn ends; only admitted reply closes it', () => {
  for (const completion of [result('q1', { pending: true }), result('q1', {}, { code: 'TOOL_OUTCOME_UNKNOWN' })]) {
    const prefix = [start, header(true), ask(), completion, ['turn/end']];
    assert.equal(foldPending('s', events(...prefix)).length, 1);
    assert.equal(foldPending('s', events(...prefix, ['inbox/queued', { callId: 'q1' }])).length, 1);
    assert.deepEqual(foldPending('s', events(...prefix, ['user/message', {
      source: { kind: 'user-question-reply', callId: 'q1' }, content: [],
    }])), []);
  }
});

test('timed real errors remove question; PTC pending result uses subCallId', () => {
  assert.deepEqual(foldPending('s', events(start, header(true), ask(), result('q1', {}, { code: 'ASK_CANCELLED' }))), []);
  const pending = foldPending('s', events(start, ['tool/ptc-dispatch', {
    name: 'ask_user_question', subCallId: 'ptc', arguments: { questions: [{ id: 'x', question: 'Continue?' }] },
    content: [{ type: 'text', text: '{"pending":true}' }],
  }], ['turn/end']));
  assert.equal(pending[0].interaction_id, 'question:ptc');
});

test('does not classify normal prose, request_secret, unknown aliases, malformed arguments', () => {
  assert.deepEqual(foldPending('s', events(start,
    ['assistant/message', { content: 'Can you approve this?' }],
    ['tool/call', { name: 'request_secret', callId: 'secret', arguments: '{"name":"TOKEN"}' }],
    ['tool/call', { name: 'ask_user', callId: 'alias', arguments: '{"question":"Choose"}' }],
    ['tool/call', { name: 'ask_user_question', callId: 'broken', arguments: 'not JSON' }],
  )), []);
});

test('ignores inherited fork prefix and bounds summaries', () => {
  const log = events(start, approval('parent'), ask('parent'), start, approval('own'));
  assert.deepEqual(foldPending('child', log, 3).map(x => x.interaction_id), ['approval:own']);
  assert.equal(foldPending('s', events(start, ask('q', 'x'.repeat(10000))))[0].question.length, 4096);
});

function harness(initial = events(start, approval('a'))) {
  const session = { id: 's', seq: initial.length };
  const listeners = new Map();
  let live = [session];
  let log = initial;
  let reads = 0;
  let disposals = 0;
  let warnings = 0;
  const ctx = {
    sessions: { list: () => [...live], get: id => live.find(s => s.id === id) },
    sessionQuery: { async observeSession() {
      reads++;
      return { source: 'live', cursor: log.length - 1, events: log, inheritedEventCount: 0,
        [Symbol.dispose]() { disposals++; } };
    } },
    on(name, handler) { assert.ok(!listeners.has(name)); listeners.set(name, handler); return () => listeners.delete(name); },
    logger: { warn() { warnings++; } },
  };
  return { ctx, session, listeners, get reads() { return reads; }, get disposals() { return disposals; },
    get warnings() { return warnings; }, setLive(value) { live = value; },
    setLog(value) { log = value; session.seq = log.length; },
  };
}
function fakeTimers() {
  let next = 0;
  const timeouts = new Map(), intervals = new Map();
  return { timeouts, intervals,
    setTimeout(fn) { const id = ++next; timeouts.set(id, fn); return id; },
    clearTimeout(id) { timeouts.delete(id); },
    setInterval(fn) { const id = ++next; intervals.set(id, fn); return id; },
    clearInterval(id) { intervals.delete(id); },
  };
}

test('startup reconstructs live pending, caches unchanged sessions, disposes leases', async () => {
  const h = harness(); const cache = new Map();
  assert.equal((await readLivePending(h.ctx, cache)).length, 1);
  assert.equal(h.disposals, 1);
  await readLivePending(h.ctx, cache);
  assert.equal(h.reads, 1);
  h.setLog(events(start, approval('a'), ['approval/decided', { id: 'a', outcome: 'rejected' }]));
  assert.deepEqual(await readLivePending(h.ctx, cache), []);
  h.setLive([]);
  assert.deepEqual(await readLivePending(h.ctx, cache), []);
  assert.equal(cache.size, 0);
});

test('rejects prepared/cold observations and membership races, disposing observation', async () => {
  const h = harness(); let disposed = false;
  h.ctx.sessionQuery.observeSession = async () => ({ source: 'prepared', [Symbol.dispose]() { disposed = true; } });
  await assert.rejects(readLivePending(h.ctx, new Map()));
  assert.equal(disposed, true);
  const h2 = harness(); const original = h2.ctx.sessionQuery.observeSession;
  h2.ctx.sessionQuery.observeSession = async () => { const value = await original(); h2.setLive([]); return value; };
  await assert.rejects(readLivePending(h2.ctx, new Map()));
  assert.equal(h2.disposals, 1);
});

test('partial reconstruction and overflow never send an empty or truncated baseline', async () => {
  const h = harness(); const sent = []; const timers = fakeTimers();
  h.ctx.sessionQuery.observeSession = async () => { throw new Error('sensitive user data must not be logged'); };
  const bridge = startBridge(h.ctx, {}, { timers, fetch: async (...args) => { sent.push(args); return { ok: true }; } });
  await bridge.flush(); await bridge.flush();
  assert.equal(sent.length, 0); assert.equal(h.warnings, 1); bridge.stop();
  const overflow = harness(events(start, ...Array.from({ length: 513 }, (_, i) => approval(`a${i}`))));
  await assert.rejects(readLivePending(overflow.ctx, new Map()), /512/);
});

test('snapshot protocol, optional bearer, no Origin, no redirects, no shutdown empty snapshot', async () => {
  const h = harness(); const sent = []; const timers = fakeTimers();
  const bridge = startBridge(h.ctx, { token: 'local-bridge-token' }, {
    timers, uuid: () => 'boot-one', fetch: async (url, options) => { sent.push({ url, options }); return { ok: true }; },
  });
  assert.deepEqual([...h.listeners.keys()], ['session/event', 'session/created', 'session/disposed']);
  await bridge.flush();
  assert.equal(sent[0].url, 'http://127.0.0.1:18789/harness/snapshot');
  assert.equal(sent[0].options.headers.Authorization, 'Bearer local-bridge-token');
  assert.equal(sent[0].options.headers.Origin, undefined);
  assert.equal(sent[0].options.redirect, 'error');
  const body = JSON.parse(sent[0].options.body);
  assert.equal(body.bridge_id, 'default'); assert.equal(body.boot_id, 'boot-one');
  assert.equal(body.pending[0].interaction_id, 'approval:a');
  bridge.stop(); bridge.stop(); await bridge.flush();
  assert.equal(sent.length, 1); assert.equal(h.listeners.size, 0);
  assert.equal(timers.timeouts.size, 0); assert.equal(timers.intervals.size, 0);
});

test('no overlapping outbound requests and latest state reconciles on next flush', async () => {
  const h = harness(); const timers = fakeTimers(); let release; let count = 0;
  const bridge = startBridge(h.ctx, {}, { timers, fetch: () => {
    count++; return new Promise(resolve => { release = () => resolve({ ok: true }); });
  } });
  const first = bridge.flush(); await new Promise(resolve => setImmediate(resolve));
  await bridge.flush(); assert.equal(count, 1);
  release(); await first; bridge.stop();
});

test('stop aborts in-flight transport and the plugin effect returns a disposer', async () => {
  const h = harness(); const timers = fakeTimers(); let signal;
  const bridge = startBridge(h.ctx, {}, { timers, fetch: (_url, opts) => {
    signal = opts.signal;
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  } });
  const pending = bridge.flush(); await new Promise(resolve => setImmediate(resolve));
  bridge.stop(); await pending; assert.equal(signal.aborted, true);
  const h2 = harness(); let dispose;
  h2.ctx.effect = callback => { dispose = callback(); };
  apply(h2.ctx, {}); assert.equal(typeof dispose, 'function'); dispose();
});

test('loopback-only configuration rejects URL injection and unsafe tokens', () => {
  assert.equal(validateConfig().host_url, 'http://127.0.0.1:19387');
  for (const bad of ['http://evil.test:8080', 'http://localhost:8080', 'http://127.0.0.1:8080/',
    'http://127.0.0.1:8080/?token=SECRET', 'http://user@127.0.0.1:8080', 'http://127.0.0.1:65536',
    'https://127.0.0.1:8080', 'http://127.0.0.1:8080#x']) assert.throws(() => loopbackOrigin(bad, 'origin'));
  assert.throws(() => validateConfig({ token: 'bad\r\nHeader: value' }));
  assert.throws(() => validateConfig({ bridge_id: '../other' }));
});
