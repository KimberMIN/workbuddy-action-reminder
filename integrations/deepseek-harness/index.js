import { randomUUID } from 'node:crypto';
import { loopbackOrigin, readLivePending } from './state.js';

export const inject = ['sessions', 'sessionQuery'];

export function validateConfig(config = {}) {
  const bridgeId = config.bridge_id ?? 'default';
  if (typeof bridgeId !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(bridgeId)) {
    throw new Error('bridge_id must contain 1–64 letters, digits, underscores or hyphens');
  }
  const token = config.token ?? '';
  if (typeof token !== 'string' || token.length > 4096 || /[\r\n]/.test(token)) throw new Error('Invalid bridge token');
  return {
    bridge_id: bridgeId,
    host_url: loopbackOrigin(config.host_url ?? 'http://127.0.0.1:19387', 'host_url'),
    daemon_url: loopbackOrigin(config.daemon_url ?? 'http://127.0.0.1:18789', 'daemon_url'),
    token,
  };
}

/** An injectable lifecycle owner for offline tests, with no Harness import dependency. */
export function startBridge(ctx, config, dependencies = {}) {
  const resolved = validateConfig(config);
  const send = dependencies.fetch ?? globalThis.fetch;
  const timers = dependencies.timers ?? globalThis;
  const bootId = (dependencies.uuid ?? randomUUID)();
  const cache = new Map();
  let stopped = false;
  let running = false;
  let dirty = false;
  let debounce;
  let active;
  let warned = false;
  let sequence = 0;
  function warn() {
    if (warned || stopped) return;
    warned = true;
    // Do not log request bodies, native errors, tokens, questions or file paths.
    ctx.logger?.warn?.('Agent Action Reminder: bridge unavailable or snapshot incomplete; retaining previous state and retrying.');
  }
  async function flush() {
    if (stopped) return;
    if (running) { dirty = true; return; }
    running = true;
    dirty = false;
    active = new AbortController();
    const timeout = timers.setTimeout(() => active?.abort(), 2800);
    try {
      const pending = await readLivePending(ctx, cache, active.signal);
      if (stopped) return;
      const response = await send(`${resolved.daemon_url}/harness/snapshot`, {
        method: 'POST', redirect: 'error', signal: active.signal,
        headers: { 'Content-Type': 'application/json',
          ...(resolved.token ? { Authorization: `Bearer ${resolved.token}` } : {}),
        },
        body: JSON.stringify({ bridge_id: resolved.bridge_id, boot_id: bootId,
          sequence: ++sequence, host_url: resolved.host_url, pending }),
      });
      if (!response.ok) throw new Error('Bridge rejected snapshot');
      // No need to consume/print data returned by a reminder daemon.
      await response.body?.cancel?.();
      warned = false;
    } catch { warn(); }
    finally {
      timers.clearTimeout(timeout);
      active = undefined;
      running = false;
      if (dirty && !stopped) schedule();
    }
  }
  function schedule() {
    if (stopped || debounce !== undefined) return;
    debounce = timers.setTimeout(() => { debounce = undefined; void flush(); }, 100);
  }
  const disposers = [
    ctx.on('session/event', (_session, event) => {
      // No access to arguments/results here; defer all read work beyond append observers.
      if (['approval/asked', 'approval/decided', 'tool/call', 'tool/result', 'tool/ptc-dispatch',
        'user/message', 'turn/end'].includes(event.type)) schedule();
    }),
    ctx.on('session/created', schedule),
    ctx.on('session/disposed', schedule),
  ];
  const heartbeat = timers.setInterval(() => { void flush(); }, 5000);
  heartbeat?.unref?.();
  schedule(); // First snapshot reconstructs existing live sessions, never assumes empty.
  return {
    flush,
    stop() {
      if (stopped) return;
      stopped = true;
      active?.abort();
      timers.clearInterval(heartbeat);
      if (debounce !== undefined) timers.clearTimeout(debounce);
      for (const dispose of disposers) dispose();
      cache.clear();
      // Deliberately no empty snapshot: disabling/reloading is not user resolution.
      // The daemon's lease pauses stale reminders until a complete baseline arrives.
    },
  };
}

export function apply(ctx, config) {
  // Cordis owns resource teardown on reload, disable or Host shutdown.
  ctx.effect(() => {
    const bridge = startBridge(ctx, config);
    return () => bridge.stop();
  }, 'agent-action-reminder: passive session observer');
}
