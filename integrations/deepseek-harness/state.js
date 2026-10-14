// Passive reconstruction only: never register an approval/question answerer.
const MAX_TEXT = 4096;
const text = value => typeof value === 'string' ? value.slice(0, MAX_TEXT) : '';
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const id = value => typeof value === 'string' && value.length > 0 && value.length <= 256;
function parse(value) {
  if (object(value)) return value;
  if (typeof value !== 'string') return null;
  try { const result = JSON.parse(value); return object(result) ? result : null; } catch { return null; }
}
function resultBody(content) {
  return parse(Array.isArray(content) ? content.find(block => block.type === 'text')?.text : null);
}
function questionText(args) {
  const body = parse(args);
  if (!Array.isArray(body?.questions) || !body.questions.length) return null;
  if (!body.questions.every(q => object(q) && typeof q.id === 'string' && typeof q.question === 'string')) return null;
  return text(body.questions.map(q => q.question).join('\n'));
}

/** Rebuild only the session-owned prefix. No natural-language heuristics. */
export function foldPending(sessionId, events, inheritedEventCount = 0) {
  if (!id(sessionId)) throw new Error('Invalid session identity');
  const approvals = new Map();
  const questions = new Map();
  let timed = false;
  let openTurn = false;
  for (const event of events) {
    if (event.seq < inheritedEventCount) continue;
    const data = event.data ?? {};
    switch (event.type) {
      case 'turn/start': openTurn = true; break;
      case 'request/header': {
        const tools = data.header?.tools;
        timed = Array.isArray(tools) && tools.some(tool => tool?.name === 'ask_user_question'
          && object(tool.parameters?.properties) && Object.hasOwn(tool.parameters.properties, 'timeout'));
        break;
      }
      case 'approval/asked':
        if (openTurn && id(data.id)) approvals.set(data.id, {
          session_id: sessionId, interaction_id: `approval:${data.id}`, kind: 'permission',
          tool_name: text(data.toolName), message: text(data.reason),
        });
        break;
      case 'approval/decided':
        // Every decision closes it: allowed-once, rejected, cancelled, unavailable.
        approvals.delete(data.id);
        break;
      case 'tool/call': {
        if (data.name !== 'ask_user_question' || !id(data.callId)) break;
        const question = questionText(data.arguments);
        if (question === null) break;
        questions.set(data.callId, { timed, continued: false, item: {
          session_id: sessionId, interaction_id: `question:${data.callId}`, kind: 'question',
          tool_name: 'ask_user_question', question,
        } });
        break;
      }
      case 'tool/result': {
        const callId = data.message?.toolCallId;
        const previous = questions.get(callId);
        if (!previous) break;
        const pending = resultBody(data.message?.content)?.pending === true;
        const unknown = data.error?.code === 'TOOL_OUTCOME_UNKNOWN';
        if (previous.timed && (pending || unknown)) previous.continued = true;
        else questions.delete(callId);
        break;
      }
      case 'tool/ptc-dispatch': {
        if (data.name !== 'ask_user_question' || data.isError || !id(data.subCallId)
          || resultBody(data.content)?.pending !== true) break;
        const question = questionText(data.arguments);
        if (question !== null) questions.set(data.subCallId, { timed: true, continued: true, item: {
          session_id: sessionId, interaction_id: `question:${data.subCallId}`, kind: 'question',
          tool_name: 'ask_user_question', question,
        } });
        break;
      }
      case 'user/message':
        if (data.source?.kind === 'user-question-reply') questions.delete(data.source.callId);
        break;
      case 'turn/end':
        openTurn = false;
        approvals.clear();
        for (const [callId, entry] of questions) if (!entry.continued) questions.delete(callId);
        break;
    }
  }
  return [...approvals.values(), ...Array.from(questions.values(), entry => entry.item)];
}

/** Strict loopback URL: a trusted local bridge, never arbitrary egress. */
export function loopbackOrigin(value, label) {
  if (typeof value !== 'string' || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(value)) {
    throw new Error(`${label} must be http://127.0.0.1:PORT with no path, credentials, query or fragment`);
  }
  const url = new URL(value);
  if (Number(url.port || '80') > 65535) throw new Error(`${label} port is invalid`);
  return value;
}

/** Complete live-only baseline; a partial/failed read MUST NOT clear daemon state. */
export async function readLivePending(ctx, cache, signal) {
  const live = ctx.sessions.list();
  const present = new Set(live);
  const pending = [];
  for (const session of live) {
    signal?.throwIfAborted();
    let cached = cache.get(session);
    if (!cached || cached.seq !== session.seq) {
      let observation;
      try {
        observation = await ctx.sessionQuery.observeSession(session.id, { projectionMode: 'none', signal });
        // Never turn a detached/persisted crash tail into a live confirmation.
        if (observation.source !== 'live' || ctx.sessions.get(session.id) !== session) {
          throw new Error('Live session changed during reconstruction');
        }
        cached = { seq: observation.cursor + 1, pending: foldPending(
          session.id, observation.events, observation.inheritedEventCount,
        ) };
        cache.set(session, cached);
      } finally { observation?.[Symbol.dispose]?.(); }
    }
    pending.push(...cached.pending);
    if (pending.length > 512) throw new Error('Pending snapshot exceeds 512 entries; refusing partial baseline');
  }
  for (const session of cache.keys()) if (!present.has(session)) cache.delete(session);
  // Creation/disposal during async reads would otherwise make the baseline incomplete.
  const after = ctx.sessions.list();
  if (after.length !== live.length || after.some(session => !present.has(session))) {
    throw new Error('Live session membership changed during reconstruction');
  }
  return pending;
}
