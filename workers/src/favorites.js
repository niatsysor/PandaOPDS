import { parseFavoriteCategories } from './parsers.js';
import { mytagsCacheKey } from './tags.js';
import {
  fetchText,
  origin,
  cookieHeader,
  ensureUpstreamSession,
  checkTextFailure,
  UA,
} from './upstream.js';
import {
  HttpError,
  buildBaseHeaders,
  intEnv,
} from './config.js';
import {
  listCache,
  persistentGet,
  persistentSet,
} from './persistent.js';

export const favoriteCategoriesState = new Map();

function favoritesBatchJitterMs(env) {
  return Math.max(0, intEnv(env.FAVORITES_BATCH_JITTER_MS, 0));
}

function sleep(ms) {
  if (ms <= 0) return Promise.resolve();
  return new Promise((r) => setTimeout(r, ms));
}

export function invalidateFavoritesListCache() {
  const prefix = '/favorites.php';
  const removed = [];
  for (const key of listCache.keys()) {
    if (String(key).includes(prefix)) {
      listCache.delete(key);
      removed.push(key);
    }
  }
  return removed;
}

export function requireFavoritesLogin(env) {
  if (!env.IPB_MEMBER_ID || !env.IPB_PASS_HASH) {
    throw new HttpError(403, 'favorites require IPB cookies (IPB_MEMBER_ID + IPB_PASS_HASH)');
  }
}

export function parseFavoritesPayload(payload) {
  const action = String(payload?.action || '').trim().toLowerCase();
  if (!['add', 'move', 'remove'].includes(action)) {
    throw new HttpError(400, "'action' must be one of add|move|remove");
  }
  const itemsRaw = payload?.items;
  let items;
  if (itemsRaw === undefined || itemsRaw === null) {
    const gid = payload?.gid;
    const token = payload?.token;
    if (gid === undefined || token === undefined || token === '') {
      throw new HttpError(400, "provide either 'items' [{gid, token}, ...] or a single 'gid' + 'token'");
    }
    items = [{ gid, token }];
  } else {
    if (!Array.isArray(itemsRaw) || itemsRaw.length === 0) {
      throw new HttpError(400, "'items' must be a non-empty list");
    }
    if (itemsRaw.length > 200) {
      throw new HttpError(400, `batch too large (${itemsRaw.length} > 200); split into chunks`);
    }
    items = itemsRaw;
  }
  const parsedItems = [];
  for (const entry of items) {
    if (!entry || typeof entry !== 'object') {
      throw new HttpError(400, "each item must be an object");
    }
    const gid = Number.parseInt(entry.gid, 10);
    const token = String(entry.token || '').trim();
    if (!Number.isFinite(gid) || !token) {
      throw new HttpError(400, 'each item needs gid + token');
    }
    parsedItems.push({ gid, token });
  }

  let favcat = null;
  if (action !== 'remove') {
    if (payload?.favcat === undefined || payload?.favcat === null || String(payload.favcat).trim() === '') {
      throw new HttpError(400, "'favcat' is required for add/move");
    }
    favcat = Number.parseInt(payload.favcat, 10);
    if (!Number.isFinite(favcat)) {
      throw new HttpError(400, `invalid favcat ${String(payload.favcat)}`);
    }
  }

  return {
    action,
    items: parsedItems,
    favcat,
    note: String(payload?.note || ''),
  };
}

export async function favoriteWrite(env, gid, token, action, favcat, note) {
  await ensureUpstreamSession(env);
  const form = new URLSearchParams();
  if (action === 'remove') {
    form.set('favcat', 'favdel');
    form.set('favnote', '');
    form.set('apply', 'Apply Changes');
    form.set('update', '1');
  } else {
    form.set('favcat', String(favcat));
    form.set('favnote', note || '');
    form.set('apply', action === 'add' ? 'Add to Favorites' : 'Apply Changes');
    form.set('update', '1');
  }

  const response = await fetch(`${origin(env)}/gallerypopups.php?gid=${gid}&t=${encodeURIComponent(token)}&act=addfav`, {
    method: 'POST',
    headers: {
      'user-agent': UA,
      'accept-language': 'en-US,en;q=0.9',
      'content-type': 'application/x-www-form-urlencoded',
      cookie: cookieHeader(env),
      referer: `${origin(env)}/g/${gid}/${token}/`,
    },
    body: form.toString(),
    redirect: 'follow',
  });
  if (response.status === 403) throw new HttpError(503, 'Cloudflare challenge from upstream');
  const text = await response.text();
  checkTextFailure(response, text, env);
  if (!response.ok) {
    throw new HttpError(502, `upstream returned ${response.status}`);
  }
  return text;
}

export async function handleFavoritesCategories(env) {
  requireFavoritesLogin(env);
  const key = mytagsCacheKey(env);
  const persistentKey = `favorite-categories:${key}`;
  let entry = favoriteCategoriesState.get(key);
  if (!entry) {
    const snapshot = await persistentGet(env, persistentKey);
    if (snapshot && typeof snapshot === 'object') {
      entry = {
        savedAt: Number.parseFloat(snapshot.savedAt) || 0,
        categories: Array.isArray(snapshot.categories) ? snapshot.categories : [],
        pending: null,
      };
      favoriteCategoriesState.set(key, entry);
    }
  }
  if (!entry) {
    entry = { savedAt: 0, categories: [], pending: null };
  }
  const stale = !entry.savedAt || (Date.now() / 1000 - entry.savedAt) >= 600;
  if (stale && !entry.pending) {
    entry.pending = (async () => {
      try {
        const { text } = await fetchText(`${origin(env)}/favorites.php?inline_set=dm_e`, env);
        const parsed = parseFavoriteCategories(text);
        if (Object.keys(parsed).length > 0) {
          entry.categories = parsed;
          entry.savedAt = Date.now() / 1000;
          await persistentSet(env, persistentKey, {
            savedAt: entry.savedAt,
            categories: entry.categories,
          });
        }
        return entry.categories;
      } catch {
        return entry.categories;
      } finally {
        entry.pending = null;
      }
    })();
    favoriteCategoriesState.set(key, entry);
  }
  if (entry.pending) await entry.pending;
  return new Response(JSON.stringify({ categories: entry.categories }, null, 2), buildBaseHeaders('application/json; charset=utf-8', {
    'cache-control': 'no-store',
  }));
}

export async function handleFavoritesWrite(env, request) {
  requireFavoritesLogin(env);
  let payload;
  try {
    payload = await request.json();
  } catch {
    throw new HttpError(400, 'invalid JSON body');
  }
  const parsed = parseFavoritesPayload(payload);
  const jitterMs = favoritesBatchJitterMs(env);
  const results = [];
  for (let index = 0; index < parsed.items.length; index += 1) {
    const item = parsed.items[index];
    try {
      await favoriteWrite(env, item.gid, item.token, parsed.action, parsed.favcat, parsed.note);
      results.push({ gid: item.gid, token: item.token, ok: true });
    } catch (error) {
      results.push({ gid: item.gid, token: item.token, ok: false, error: String(error?.message || error) });
    }
    if (jitterMs > 0 && index + 1 < parsed.items.length) {
      const variance = Math.floor(Math.random() * jitterMs);
      await sleep(jitterMs + variance);
    }
  }

  if (results.some((r) => r.ok)) {
    invalidateFavoritesListCache();
  }
  return new Response(JSON.stringify({
    action: parsed.action,
    ok: results.every((item) => item.ok),
    ok_count: results.filter((item) => item.ok).length,
    items: results,
  }, null, 2), buildBaseHeaders('application/json; charset=utf-8', {
    'cache-control': 'no-store',
  }));
}