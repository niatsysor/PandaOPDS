import {
  DEFAULT_RETRIES,
  HttpError,
  UA,
  apiOrigin,
  bannedCooldownEnv,
  circuitBreaker,
  cookieTokenValue,
  datatagsEnv,
  ehProfileEnv,
  exceedCooldownEnv,
  nwEnv,
  origin,
  siteHost,
} from './config.js';

export const upstreamSessionState = new Map();
export { UA, apiOrigin, origin, siteHost };

export function sessionCacheKey(env) {
  return [
    siteHost(env),
    String(env.IPB_MEMBER_ID || ''),
    String(env.IPB_PASS_HASH || ''),
    nwEnv(env),
    datatagsEnv(env),
    ehProfileEnv(env),
  ].join('|');
}

export function sessionCookieKey(env) {
  return sessionCacheKey(env);
}

export function sessionIgneous(env) {
  const direct = cookieTokenValue(env.IGNEOUS);
  if (direct) return direct;
  const current = upstreamSessionState.get(sessionCookieKey(env));
  if (current?.cookies?.igneous) return cookieTokenValue(current.cookies.igneous);
  return cookieTokenValue(current?.igneous);
}

export function cookieHeader(env) {
  const cookies = new Map();
  cookies.set('nw', nwEnv(env));
  cookies.set('datatags', datatagsEnv(env));
  if (env.IPB_MEMBER_ID) cookies.set('ipb_member_id', env.IPB_MEMBER_ID);
  if (env.IPB_PASS_HASH) cookies.set('ipb_pass_hash', env.IPB_PASS_HASH);
  const key = sessionCookieKey(env);
  const current = upstreamSessionState.get(key);
  if (current?.cookies && typeof current.cookies === 'object') {
    for (const [name, value] of Object.entries(current.cookies)) {
      if (name && value !== undefined && value !== null) cookies.set(name, value);
    }
  }
  const igneous = sessionIgneous(env);
  if (igneous) cookies.set('igneous', igneous);
  return [...cookies.entries()].map(([name, value]) => `${name}=${value}`).join('; ');
}

export function upstreamUrl(env, path) {
  if (/^https?:\/\//i.test(path)) return path;
  return new URL(path, origin(env)).toString();
}

export function toplistUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  return new URL(path, 'https://e-hentai.org').toString();
}

export function upstreamApiUrl(env) {
  return `${apiOrigin(env)}/api.php`;
}

export function limiterNamespace(env) {
  return env.EH_LIMITER || null;
}

export function limiterBinding(env) {
  const ns = limiterNamespace(env);
  if (!ns) return null;
  return ns.get(ns.idFromName('global'));
}

export async function acquireUpstreamLease(env, kind) {
  const client = limiterBinding(env);
  if (!client) return null;
  const response = await client.fetch('https://limiter/acquire', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ kind }),
  });
  if (!response.ok) {
    throw new HttpError(503, 'upstream limiter unavailable');
  }
  const payload = await response.json();
  return payload?.leaseId || null;
}

export async function releaseUpstreamLease(env, leaseId) {
  if (!leaseId) return;
  const client = limiterBinding(env);
  if (!client) return;
  await client.fetch('https://limiter/release', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ leaseId }),
  });
}

export function releaseAfterResponse(response, release) {
  if (!response.body) {
    void release();
    return response;
  }

  let released = false;
  const releaseOnce = () => {
    if (released) return;
    released = true;
    void release();
  };

  const body = new ReadableStream({
    async start(controller) {
      const reader = response.body.getReader();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          controller.enqueue(value);
        }
        releaseOnce();
        controller.close();
      } catch (error) {
        releaseOnce();
        controller.error(error);
      }
    },
    cancel(reason) {
      releaseOnce();
      return response.body.cancel(reason);
    },
  });

  return new Response(body, response);
}

export function checkTextFailure(response, text, env = null) {
  const host = new URL(response.url).host;
  if (!host.endsWith('e-hentai.org') && !host.endsWith('exhentai.org') && !host.endsWith('ehgt.org')) return;
  const bannedCooldown = env ? bannedCooldownEnv(env) : null;
  const exceedCooldown = env ? exceedCooldownEnv(env) : null;
  if (response.status === 403) throw new HttpError(503, 'Cloudflare challenge from upstream');
  if (text === '') throw new HttpError(503, 'upstream login required');
  if (/^(Your IP address|This IP address)/.test(text)) {
    circuitBreaker.trip('upstream IP banned', bannedCooldown ?? undefined);
    throw new HttpError(503, 'upstream IP banned');
  }
  if (/^You have exceeded your image/i.test(text)) {
    circuitBreaker.trip('upstream image quota exceeded', exceedCooldown ?? undefined);
    throw new HttpError(429, 'upstream image quota exceeded');
  }
  if (/Page load has been aborted due to a fatal error/i.test(text)) throw new HttpError(503, 'upstream fatal error');
  if (/^Gallery not found/i.test(text)) throw new HttpError(404, 'gallery not found');
}

export async function rawFetchText(requestUrl, env, init = {}) {
  const lease = await acquireUpstreamLease(env, 'html');
  circuitBreaker.check();
  try {
    let attempt = 0;
    while (true) {
      attempt += 1;
      try {
        const response = await fetch(upstreamUrl(env, requestUrl), {
          ...init,
          headers: {
            'user-agent': UA,
            'accept-language': 'en-US,en;q=0.9',
            ...(init.headers || {}),
            cookie: cookieHeader(env),
          },
          redirect: init.redirect || 'follow',
        });
        const text = await response.text();
        captureSessionCookies(env, response);
        checkTextFailure(response, text, env);
        return { response, text };
      } catch (error) {
        const isNetworkError = error instanceof TypeError;
        const isRetryableHttp = error instanceof HttpError && (
          error.message.includes('Cloudflare challenge') ||
          error.message.includes('upstream fatal error') ||
          error.message.includes('upstream login required')
        );
        if ((isNetworkError || isRetryableHttp) && attempt <= (init.retries ?? DEFAULT_RETRIES)) {
          const backoffMs = Math.min(500 * attempt, 3000);
          await new Promise(r => setTimeout(r, backoffMs));
          continue;
        }
        throw error;
      }
    }
  } finally {
    await releaseUpstreamLease(env, lease);
  }
}

export async function rawFetchBinary(requestUrl, env, init = {}, kind = 'image') {
  const lease = await acquireUpstreamLease(env, kind);
  circuitBreaker.check();
  let attempt = 0;
  while (true) {
    attempt += 1;
    try {
      const response = await fetch(upstreamUrl(env, requestUrl), {
        ...init,
        headers: {
          'user-agent': UA,
          'accept-language': 'en-US,en;q=0.9',
          ...(init.headers || {}),
          cookie: cookieHeader(env),
          ...(kind === 'image' || kind === 'thumb' ? { referer: `${origin(env)}/` } : {}),
        },
        redirect: init.redirect || 'follow',
      });
      if (response.status === 404) throw new HttpError(404, 'image not found');
      if (response.status === 403) throw new HttpError(503, 'Cloudflare challenge from upstream');
      if (response.status === 429) {
        circuitBreaker.trip('upstream image quota exceeded', exceedCooldownEnv(env));
        throw new HttpError(429, 'upstream image quota exceeded');
      }
      if (!response.ok && response.status !== 206) {
        throw new HttpError(502, `upstream returned ${response.status}`);
      }
      return releaseAfterResponse(response, () => releaseUpstreamLease(env, lease));
    } catch (error) {
      const isNetworkError = error instanceof TypeError;
      const isCloudflare = error instanceof HttpError && error.message.includes('Cloudflare');
      if ((isNetworkError || isCloudflare) && attempt <= (init.retries ?? DEFAULT_RETRIES)) {
        const backoffMs = Math.min(500 * attempt, 3000);
        await new Promise(r => setTimeout(r, backoffMs));
        continue;
      }
      await releaseUpstreamLease(env, lease);
      throw error;
    }
  }
}

export function collectSetCookieHeaders(headers) {
  if (!headers) return [];
  if (typeof headers.getSetCookie === 'function') {
    const values = headers.getSetCookie();
    if (Array.isArray(values) && values.length) return values;
  }
  const single = headers.get?.('set-cookie') || headers.get?.('Set-Cookie') || '';
  return single ? [single] : [];
}

export function extractCookieValue(setCookieHeaders, cookieName) {
  const needle = `${String(cookieName || '').trim().toLowerCase()}=`;
  for (const header of Array.isArray(setCookieHeaders) ? setCookieHeaders : [setCookieHeaders]) {
    const raw = String(header || '');
    if (!raw) continue;
    for (const segment of raw.split(/,(?=\s*[^=;,\s]+=)/g)) {
      const trimmed = String(segment || '').trim();
      if (!trimmed) continue;
      const lower = trimmed.toLowerCase();
      if (!lower.startsWith(needle)) continue;
      const match = trimmed.match(/^[^=]+=([^;]*)/);
      if (match) return String(match[1] || '').trim();
    }
  }
  return '';
}

export async function probeSessionUrl(requestUrl, env, maxRedirects = 5) {
  circuitBreaker.check();
  const lease = await acquireUpstreamLease(env, 'html');
  try {
    let nextUrl = upstreamUrl(env, requestUrl);
    let response = null;
    for (let index = 0; index < maxRedirects; index += 1) {
      response = await fetch(nextUrl, {
        headers: {
          'user-agent': UA,
          'accept-language': 'en-US,en;q=0.9',
          cookie: cookieHeader(env),
        },
        redirect: 'manual',
      });
      const setCookies = collectSetCookieHeaders(response.headers);
      captureSessionCookies(env, response);
      const location = response.headers.get('location') || response.headers.get('Location') || '';
      if (!response.status || response.status < 300 || response.status >= 400 || !location) break;
      nextUrl = new URL(location, nextUrl).toString();
    }
    captureSessionCookies(env, response);
    return response;
  } finally {
    await releaseUpstreamLease(env, lease);
  }
}

export function captureSessionCookies(env, response) {
  const setCookieHeaders = collectSetCookieHeaders(response?.headers);
  if (!setCookieHeaders.length) return false;
  const key = sessionCookieKey(env);
  const current = upstreamSessionState.get(key) || { ready: false, pending: null, igneous: '', cookies: {} };
  const cookies = { ...current.cookies };
  for (const header of setCookieHeaders) {
    const pair = String(header || '').split(';')[0] || '';
    const idx = pair.indexOf('=');
    if (idx > 0) {
      const name = pair.slice(0, idx).trim().toLowerCase();
      const value = pair.slice(idx + 1).trim();
      if (name) cookies[name] = value;
    }
  }
  const igneous = cookies['igneous'] || current.igneous || '';
  upstreamSessionState.set(key, { ...current, cookies, igneous });
  return true;
}

export async function ensureUpstreamSession(env) {
  const key = sessionCookieKey(env);
  const current = upstreamSessionState.get(key);
  if (current?.ready) return;
  if (current?.pending) return current.pending;

  const pending = (async () => {
    try {
      if (siteHost(env) === 'exhentai.org') {
        await rawFetchText('https://e-hentai.org/', env);
      }
      const sessionProbe = await probeSessionUrl('/', env);
      const profileName = ehProfileEnv(env);
      if (profileName) {
        await ensureUconfigProfile(env, profileName);
      }
      const sessionState = upstreamSessionState.get(key) || {};
      upstreamSessionState.set(key, { ...sessionState, ready: true, pending: null });
    } catch (error) {
      upstreamSessionState.delete(key);
      throw error;
    }
  })();

  upstreamSessionState.set(key, { ready: false, pending });
  return pending;
}

export async function ensureUconfigProfile(env, profileName) {
  try {
    const { text } = await rawFetchText('/uconfig.php', env);
    const escaped = profileName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = text.match(new RegExp(`<option\\s+value="(\\d+)"[^>]*>\\s*${escaped}\\s*</option>`, 'i'));
    if (match) {
      await rawFetchText('/uconfig.php', env, {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ profile_set: match[1] }).toString(),
        referer: `${origin(env)}/uconfig.php`,
      });
      return;
    }

    await rawFetchText('/uconfig.php', env, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        profile_action: 'create',
        profile_name: profileName,
        profile_set: '616',
      }).toString(),
      referer: `${origin(env)}/uconfig.php`,
    });
  } catch (error) {
    console.warn(`uconfig profile ${profileName} failed; list pages will still use inline_set override:`, error);
  }
}

export async function fetchText(requestUrl, env, init = {}) {
  await ensureUpstreamSession(env);
  const { response, text } = await rawFetchText(requestUrl, env, init);
  return { response, text };
}

export async function fetchBinary(requestUrl, env, init = {}, kind = 'image') {
  await ensureUpstreamSession(env);
  return rawFetchBinary(requestUrl, env, init, kind);
}

export async function inspectImageResponse(imageResponse) {
  const contentType = String(imageResponse.headers.get('content-type') || imageResponse.headers.get('Content-Type') || '').toLowerCase();
  if (contentType && !contentType.includes('text/html') && !contentType.includes('application/octet-stream') && !contentType.includes('text/plain')) {
    return;
  }
  const snippet = (await imageResponse.clone().text()).slice(0, 2048);
  if (!snippet.trim()) {
    throw new HttpError(502, 'upstream image returned blank body');
  }
  if (/Invalid token|Invalid request|An error has occurred/i.test(snippet) || /<html/i.test(snippet)) {
    throw new HttpError(502, 'upstream image returned HTML error page');
  }
}