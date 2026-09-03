import {
  KV_PERSISTED_PREFIXES,
  PERSISTENT_DEFAULT_NAME,
  cacheTtls,
} from './config.js';

export const coverCache = new Map();
let coverCacheRestored = false;

export const listCache = new Map();
export const detailCache = new Map();
export const imgPageCache = new Map();

export function persistentNamespace(env) {
  return env.EH_STATE || null;
}

export function persistentBinding(env) {
  const ns = persistentNamespace(env);
  if (!ns) return null;
  return ns.get(ns.idFromName(PERSISTENT_DEFAULT_NAME));
}

export function kvPersistenceBinding(env) {
  return env.EH_CACHE || null;
}

export function usesKvPersistence(key) {
  return KV_PERSISTED_PREFIXES.some((prefix) => String(key || '').startsWith(prefix));
}

export async function persistentGet(env, key) {
  if (usesKvPersistence(key)) {
    const kv = kvPersistenceBinding(env);
    if (!kv) return null;
    const value = await kv.get(key);
    if (value == null) return null;
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }
  const client = persistentBinding(env);
  if (!client) return null;
  const response = await client.fetch('https://state/get', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ key }),
  });
  if (!response.ok) return null;
  const payload = await response.json();
  return payload?.value ?? null;
}

export async function persistentSet(env, key, value) {
  if (usesKvPersistence(key)) {
    const kv = kvPersistenceBinding(env);
    if (!kv) return;
    await kv.put(key, JSON.stringify(value));
    return;
  }
  const client = persistentBinding(env);
  if (!client) return;
  await client.fetch('https://state/set', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ key, value }),
  });
}

export async function persistentDelete(env, key) {
  if (usesKvPersistence(key)) {
    const kv = kvPersistenceBinding(env);
    if (!kv) return;
    await kv.delete(key);
    return;
  }
  const client = persistentBinding(env);
  if (!client) return;
  await client.fetch('https://state/delete', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ key }),
  });
}

export function coverCacheKey(gid, token) {
  return `cover:${gid}:${token}`;
}

export async function getCachedCoverUrl(gid, token) {
  const entry = coverCache.get(coverCacheKey(gid, token));
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) {
    coverCache.delete(coverCacheKey(gid, token));
    return null;
  }
  return entry.url;
}

export async function setCachedCoverUrl(env, gid, token, url, ttlSeconds) {
  const key = coverCacheKey(gid, token);
  const expiresAt = Date.now() + ttlSeconds * 1000;
  coverCache.set(key, { url, expiresAt });
  const persistentKey = `cover:${gid}:${token}`;
  await persistentSet(env, persistentKey, { url, expiresAt });
}

export async function restoreCoverCache(env) {
  const client = persistentBinding(env);
  if (!client) return;
  try {
    const response = await client.fetch('https://state/list', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ prefix: 'cover:' }),
    });
    if (!response.ok) return;
    const data = await response.json();
    const entries = Array.isArray(data?.keys) ? data.keys : [];
    const now = Date.now();
    for (const entry of entries) {
      const key = String(entry?.key || '');
      const persisted = await persistentGet(env, key);
      if (persisted && typeof persisted === 'object' && persisted.url && persisted.expiresAt) {
        if (now < persisted.expiresAt) {
          coverCache.set(key, { url: persisted.url, expiresAt: persisted.expiresAt });
        }
      }
    }
  } catch {
    // best-effort restore; non-fatal
  }
}

export function ensureCoverCacheRestored(env) {
  if (coverCacheRestored) return;
  coverCacheRestored = true;
  return restoreCoverCache(env);
}

export function memCacheGet(cache, key) {
  const entry = cache.get(key);
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) {
    cache.delete(key);
    return null;
  }
  if (entry.pending) return entry.pending;
  return entry.value;
}

export function memCacheSet(cache, key, value, ttlSeconds) {
  const expiresAt = Date.now() + ttlSeconds * 1000;
  cache.set(key, { value, expiresAt, pending: null });
}

export function memCacheGetOrSet(cache, key, factory, ttlSeconds) {
  const existing = memCacheGet(cache, key);
  if (existing) return existing;

  let entry = cache.get(key);
  if (entry?.pending) return entry.pending;

  const pending = (async () => {
    try {
      const value = await factory();
      memCacheSet(cache, key, value, ttlSeconds);
      return value;
    } catch (error) {
      cache.delete(key);
      throw error;
    }
  })();

  cache.set(key, { value: null, expiresAt: Date.now() + ttlSeconds * 1000, pending });
  return pending;
}

export async function cacheListCovers(env, items) {
  const ttl = cacheTtls(env).detail;
  for (const item of items) {
    const url = item.coverUrl || item.thumbnails?.[0]?.thumbUrl || item.pageUrls?.[0];
    if (url) {
      await setCachedCoverUrl(env, item.gid, item.token, url, ttl / 1000);
    }
  }
}

export const DETAIL_CACHE_VARIANT = 'detail-tags-v3';
export const LIST_CACHE_VARIANT = 'search-v2';

export function detailCacheRequestUrl(detailUrl) {
  const url = new URL(detailUrl);
  url.searchParams.set('__v', DETAIL_CACHE_VARIANT);
  return url.toString();
}