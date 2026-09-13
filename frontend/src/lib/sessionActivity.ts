/**
 * Cross-tab session activity tracker.
 *
 * The idle-timeout hook and the proactive-refresh scheduler both need a
 * single "when did the user last interact?" timestamp that is shared
 * across every tab of the same origin. Otherwise Tab A can log you out
 * for inactivity while Tab B has you actively typing, or Tab B can
 * silently refresh your session while Tab A shows the warning modal.
 *
 * BroadcastChannel is the primary sync mechanism (Chrome/Firefox/Edge
 * and Safari >= 15.4). ``storage`` events are the fallback path for
 * older Safari — a bit noisier but adequate. Only ``markActivity('local')``
 * broadcasts; remote messages update the shared timestamp without
 * re-broadcasting to avoid ping-pong loops.
 */

const ACTIVITY_KEY = 'ansiauth.lastActivity';
const CHANNEL_NAME = 'ansiauth-session';
const BROADCAST_THROTTLE_MS = 1_000;

let _lastActivity = Date.now();
let _lastBroadcast = 0;
let _bc: BroadcastChannel | null = null;

type RemoteLogoutHandler = () => void;
const _remoteLogoutHandlers = new Set<RemoteLogoutHandler>();

type ActivityMessage =
  | { type: 'activity'; ts: number }
  | { type: 'logout' };

function readStoredActivity(): number {
  if (typeof window === 'undefined') return 0;
  try {
    const raw = window.localStorage.getItem(ACTIVITY_KEY);
    return raw ? Number(raw) : 0;
  } catch {
    return 0;
  }
}

/**
 * Update the "user is here" timestamp. ``source='local'`` means an event
 * fired in this tab and should be broadcast to siblings; ``source='remote'``
 * means we received it from a sibling and must not re-broadcast.
 *
 * The broadcast (localStorage write + BroadcastChannel post) is throttled
 * to once per second so ``mousemove`` on a busy page can't saturate the
 * storage/channel — the in-memory ``_lastActivity`` still updates on
 * every event, which is all the poller cares about.
 */
export function markActivity(source: 'local' | 'remote' = 'local', ts?: number): void {
  const now = ts ?? Date.now();
  if (now <= _lastActivity) return;
  _lastActivity = now;

  if (source !== 'local' || typeof window === 'undefined') return;
  if (now - _lastBroadcast < BROADCAST_THROTTLE_MS) return;
  _lastBroadcast = now;

  try {
    window.localStorage.setItem(ACTIVITY_KEY, String(now));
  } catch {
    /* private mode / quota — non-fatal, the channel still delivers */
  }
  _bc?.postMessage({ type: 'activity', ts: now } satisfies ActivityMessage);
}

export function getLastActivity(): number {
  return _lastActivity;
}

/**
 * Wire up cross-tab listeners. Idempotent: calling it twice reuses the
 * existing channel. Returns a cleanup function that closes the channel
 * and removes the storage listener.
 */
export function initSessionActivity(): () => void {
  if (typeof window === 'undefined') return () => {};

  // Adopt the highest timestamp already known — if another tab was
  // active while this one was in bfcache / backgrounded, the storage
  // value is fresher than our in-memory one.
  const stored = readStoredActivity();
  if (stored > _lastActivity) _lastActivity = stored;

  const onMessage = (ev: MessageEvent<ActivityMessage>) => {
    if (ev.data?.type === 'activity') {
      markActivity('remote', ev.data.ts);
    } else if (ev.data?.type === 'logout') {
      _remoteLogoutHandlers.forEach((h) => h());
    }
  };
  const onStorage = (ev: StorageEvent) => {
    if (ev.key === ACTIVITY_KEY && ev.newValue) {
      markActivity('remote', Number(ev.newValue));
    }
  };

  if ('BroadcastChannel' in window && !_bc) {
    _bc = new BroadcastChannel(CHANNEL_NAME);
    _bc.addEventListener('message', onMessage);
  }
  window.addEventListener('storage', onStorage);

  return () => {
    if (_bc) {
      _bc.removeEventListener('message', onMessage);
      _bc.close();
      _bc = null;
    }
    window.removeEventListener('storage', onStorage);
  };
}

/**
 * Tell sibling tabs the user has logged out (idle timeout, explicit
 * logout, or step-up cancellation) so they can drop their state without
 * waiting for their own idle check to fire.
 */
export function broadcastLogout(): void {
  if (typeof window === 'undefined') return;
  _bc?.postMessage({ type: 'logout' } satisfies ActivityMessage);
}

export function onRemoteLogout(handler: RemoteLogoutHandler): () => void {
  _remoteLogoutHandlers.add(handler);
  return () => {
    _remoteLogoutHandlers.delete(handler);
  };
}
