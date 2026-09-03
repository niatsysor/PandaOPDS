import { ensureCoverCacheRestored } from './persistent.js';
import {
  handleFavoritesCategories,
  handleFavoritesWrite,
} from './favorites.js';
import {
  handleOpdsV12Chapters,
  handleOpdsV12Gallery,
  handleOpdsV12Root,
  handleOpdsV12Search,
  handleOpdsV12Toplist,
  handleOpdsV20GalleryDetail,
  handleOpdsV20Gallery,
  handleOpdsV20Publication,
  handleOpdsV20Root,
  handleOpdsV20Search,
  handleOpdsV20Toplist,
} from './opds.js';
import { handleImageFetch, handleStream, handleThumb } from './streams.js';
import {
  HttpError,
  LIMITER_LEASE_TTL_SECONDS,
  buildBaseHeaders,
  htmlIntervalEnv,
  htmlMaxConcurrencyEnv,
  imageMaxConcurrencyEnv,
  parseEnvList,
  siteHost,
  thumbMaxConcurrencyEnv,
} from './config.js';

export class EhPersistentState {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === 'POST' && url.pathname === '/get') {
      const payload = await request.json();
      const key = String(payload?.key || '').trim();
      if (!key) {
        return new Response(JSON.stringify({ value: null }, null, 2), {
          headers: { 'content-type': 'application/json; charset=utf-8' },
        });
      }
      const value = await this.state.storage.get(key);
      return new Response(JSON.stringify({ value: value ?? null }, null, 2), {
        headers: { 'content-type': 'application/json; charset=utf-8' },
      });
    }
    if (request.method === 'POST' && url.pathname === '/set') {
      const payload = await request.json();
      const key = String(payload?.key || '').trim();
      if (!key) return new Response(null, { status: 400 });
      await this.state.storage.put(key, payload?.value ?? null);
      return new Response(null, { status: 204 });
    }
    if (request.method === 'POST' && url.pathname === '/delete') {
      const payload = await request.json();
      const key = String(payload?.key || '').trim();
      if (!key) return new Response(null, { status: 400 });
      await this.state.storage.delete(key);
      return new Response(null, { status: 204 });
    }
    if (request.method === 'GET' && url.pathname === '/status') {
      const entries = await this.state.storage.list();
      return new Response(JSON.stringify({ entries: entries.size }, null, 2), {
        headers: { 'content-type': 'application/json; charset=utf-8' },
      });
    }
    return new Response('not found', { status: 404 });
  }
}

export class EhLimiter {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.nextLeaseId = 1;
    this.data = {
      inflight: { html: 0, image: 0, thumb: 0 },
      nextAt: { html: 0, image: 0, thumb: 0 },
      leases: new Map(),
      waiters: [],
      timer: null,
    };
  }

  limitsFor(kind) {
    if (kind === 'image') {
      return { max: imageMaxConcurrencyEnv(this.env), intervalMs: 0 };
    }
    if (kind === 'thumb') {
      return { max: thumbMaxConcurrencyEnv(this.env), intervalMs: 0 };
    }
    return { max: htmlMaxConcurrencyEnv(this.env), intervalMs: Math.round(htmlIntervalEnv(this.env) * 1000) };
  }

  reapExpired(now = Date.now()) {
    for (const [leaseId, lease] of this.data.leases) {
      if (lease.expiresAt > now) continue;
      this.data.leases.delete(leaseId);
      this.data.inflight[lease.kind] = Math.max(0, (this.data.inflight[lease.kind] || 0) - 1);
    }
  }

  canGrant(kind, now) {
    const limits = this.limitsFor(kind);
    return (this.data.inflight[kind] || 0) < limits.max && now >= (this.data.nextAt[kind] || 0);
  }

  grant(kind, now) {
    const limits = this.limitsFor(kind);
    const leaseId = `${kind}-${this.nextLeaseId++}-${Math.random().toString(16).slice(2)}`;
    this.data.inflight[kind] = (this.data.inflight[kind] || 0) + 1;
    this.data.nextAt[kind] = now + limits.intervalMs;
    this.data.leases.set(leaseId, {
      kind,
      expiresAt: now + LIMITER_LEASE_TTL_SECONDS * 1000,
    });
    return { leaseId, kind };
  }

  scheduleDrain() {
    if (this.data.timer) return;
    const now = Date.now();
    let delay = Number.POSITIVE_INFINITY;
    for (const waiter of this.data.waiters) {
      const limits = this.limitsFor(waiter.kind);
      if ((this.data.inflight[waiter.kind] || 0) >= limits.max) continue;
      const nextAt = this.data.nextAt[waiter.kind] || 0;
      if (nextAt > now) {
        delay = Math.min(delay, nextAt - now);
      }
    }
    if (!Number.isFinite(delay)) return;
    this.data.timer = setTimeout(() => {
      this.data.timer = null;
      this.drain();
    }, Math.max(0, delay));
  }

  drain() {
    const now = Date.now();
    this.reapExpired(now);
    let progressed = false;
    for (let index = 0; index < this.data.waiters.length; index += 1) {
      const waiter = this.data.waiters[index];
      if (!this.canGrant(waiter.kind, now)) continue;
      const lease = this.grant(waiter.kind, now);
      this.data.waiters.splice(index, 1);
      index -= 1;
      waiter.resolve(lease);
      progressed = true;
    }
    if (!progressed && this.data.waiters.length > 0) {
      this.scheduleDrain();
    }
  }

  acquire(kind) {
    const now = Date.now();
    this.reapExpired(now);
    if (this.canGrant(kind, now)) {
      return Promise.resolve(this.grant(kind, now));
    }
    return new Promise((resolve) => {
      this.data.waiters.push({ kind, resolve });
      this.scheduleDrain();
    });
  }

  release(leaseId) {
    if (!leaseId) return;
    const lease = this.data.leases.get(leaseId);
    if (!lease) return;
    this.data.leases.delete(leaseId);
    this.data.inflight[lease.kind] = Math.max(0, (this.data.inflight[lease.kind] || 0) - 1);
    this.drain();
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === 'POST' && url.pathname === '/acquire') {
      const payload = await request.json();
      const kind = ['html', 'image', 'thumb'].includes(payload?.kind) ? payload.kind : 'html';
      const lease = await this.acquire(kind);
      return new Response(JSON.stringify(lease), {
        headers: { 'content-type': 'application/json; charset=utf-8' },
      });
    }
    if (request.method === 'POST' && url.pathname === '/release') {
      const payload = await request.json();
      this.release(String(payload?.leaseId || '').trim());
      return new Response(null, { status: 204 });
    }
    if (request.method === 'GET' && url.pathname === '/status') {
      return new Response(JSON.stringify({
        inflight: this.data.inflight,
        nextAt: this.data.nextAt,
        queued: this.data.waiters.length,
      }, null, 2), {
        headers: { 'content-type': 'application/json; charset=utf-8' },
      });
    }
    return new Response('not found', { status: 404 });
  }
}

async function handleStatus(env, request) {
  return new Response(JSON.stringify({
    ok: true,
    site: siteHost(env),
    publicBaseUrl: env.PUBLIC_BASE_URL || null,
    hasCookies: Boolean(env.IPB_MEMBER_ID && env.IPB_PASS_HASH),
    workersMigration: true,
    path: new URL(request.url).pathname,
  }, null, 2), buildBaseHeaders('application/json; charset=utf-8', {
    'cache-control': 'no-store',
  }));
}

function unauthorized() {
  return new Response(JSON.stringify({ error: 'unauthorized' }, null, 2), {
    status: 401,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'www-authenticate': 'Basic realm="PandaOPDS"',
      'cache-control': 'no-store',
    },
  });
}

function basicAuthOk(request, env) {
  const username = env.AUTH_USERNAME || '';
  const password = env.AUTH_PASSWORD || '';
  if (!username || !password) return true;
  const exemptPaths = new Set(parseEnvList(env.AUTH_EXEMPT_PATHS));
  const exemptPrefixes = parseEnvList(env.AUTH_EXEMPT_PREFIXES);
  const path = new URL(request.url).pathname;
  if (path === '/health' || exemptPaths.has(path.toLowerCase())) return true;
  if (exemptPrefixes.some((prefix) => path.toLowerCase().startsWith(prefix))) return true;
  const auth = request.headers.get('authorization') || '';
  if (!auth.toLowerCase().startsWith('basic ')) return false;
  const raw = atob(auth.slice(6));
  const idx = raw.indexOf(':');
  if (idx < 0) return false;
  const u = raw.slice(0, idx);
  const p = raw.slice(idx + 1);
  return u === username && p === password;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!basicAuthOk(request, env)) {
      return unauthorized();
    }
    await ensureCoverCacheRestored(env);

    try {
      if (url.pathname === '/' || url.pathname === '/health') {
        return handleStatus(env, request);
      }
      if (url.pathname === '/opds/v1.2') return handleOpdsV12Root(env, ctx, request);
      if (url.pathname === '/opds/v1.2/search.xml') return handleOpdsV12Search(env);
      if (url.pathname === '/opds/v1.2/gallery') return handleOpdsV12Gallery(env, ctx, request);
      if (url.pathname === '/opds/v1.2/toplist') return handleOpdsV12Toplist(env, ctx, request);
      const v12Chapter = url.pathname.match(/^\/opds\/v1\.2\/gallery\/(\d+)\/([0-9a-fA-F]+)\/chapters$/);
      if (v12Chapter) return handleOpdsV12Chapters(env, ctx, request, Number.parseInt(v12Chapter[1], 10), v12Chapter[2]);
      if (url.pathname === '/opds/v2.0') return handleOpdsV20Root(env);
      if (url.pathname === '/opds/v2.0/search.xml') return handleOpdsV20Search(env);
      if (url.pathname === '/opds/v2.0/gallery') return handleOpdsV20Gallery(env, ctx, request);
      if (url.pathname === '/opds/v2.0/toplist') return handleOpdsV20Toplist(env, ctx, request);
      if (url.pathname === '/api/favorites/categories') return handleFavoritesCategories(env);
      if (url.pathname === '/api/favorites' && request.method === 'POST') return handleFavoritesWrite(env, request);
      const v20Publication = url.pathname.match(/^\/opds\/v2\.0\/gallery\/(\d+)\/([0-9a-fA-F]+)\/publication$/);
      if (v20Publication) return handleOpdsV20Publication(env, ctx, Number.parseInt(v20Publication[1], 10), v20Publication[2]);
      const v20Gallery = url.pathname.match(/^\/opds\/v2\.0\/gallery\/(\d+)\/([0-9a-fA-F]+)$/);
      if (v20Gallery) return handleOpdsV20GalleryDetail(env, ctx, Number.parseInt(v20Gallery[1], 10), v20Gallery[2]);
      const stream = url.pathname.match(/^\/stream\/(\d+)\/([0-9a-fA-F]+)\/page\/(\d+)$/);
      if (stream) return handleStream(env, ctx, Number.parseInt(stream[1], 10), stream[2], Number.parseInt(stream[3], 10));
      const thumb = url.pathname.match(/^\/image\/(\d+)\/([0-9a-fA-F]+)\/thumb$/);
      if (thumb) return handleThumb(env, ctx, Number.parseInt(thumb[1], 10), thumb[2]);
      if (url.pathname === '/image/fetch') return handleImageFetch(env, ctx, request);
      return new Response(JSON.stringify({ error: 'not_found', path: url.pathname }, null, 2), {
        status: 404,
        headers: {
          'content-type': 'application/json; charset=utf-8',
          'cache-control': 'no-store',
        },
      });
    } catch (error) {
      if (error instanceof HttpError) {
        return new Response(JSON.stringify({ error: error.message }, null, 2), {
          status: error.status,
          headers: {
            'content-type': 'application/json; charset=utf-8',
            'cache-control': 'no-store',
            ...error.headers,
          },
        });
      }
      return new Response(JSON.stringify({ error: 'internal_error', detail: String(error?.message || error) }, null, 2), {
        status: 500,
        headers: {
          'content-type': 'application/json; charset=utf-8',
          'cache-control': 'no-store',
        },
      });
    }
  },
};