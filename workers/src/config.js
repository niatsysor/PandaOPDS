export const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';

export const CACHE_HINTS = {
  list: 600,
  detail: 3600,
  image: 604800,
};

export const LIST_TITLES = {
  popular: 'Popular',
  watched: 'Watched',
  favorites: 'Favorites',
};

export const DEFAULT_TTLS = {
  list: 600,
  detail: 3600,
  image: 604800,
};

export const DEFAULT_FACETS = [
  ['Doujinshi', 1021],
  ['Manga', 1019],
  ['Artist CG', 1015],
  ['Game CG', 1007],
  ['Western', 991],
  ['Non-H', 959],
  ['Image Set', 895],
  ['Cosplay', 767],
  ['Asian Porn', 511],
  ['Misc', 1022],
];

export const LIMITER_DEFAULTS = {
  htmlIntervalSeconds: 0.3,
  htmlMaxConcurrency: 5,
  imageMaxConcurrency: 5,
  thumbMaxConcurrency: 25,
};

export const LIMITER_LEASE_TTL_SECONDS = 300;
export const PERSISTENT_DEFAULT_NAME = 'global';
export const DEFAULT_RETRIES = 3;
export const CIRCUIT_COOLDOWN_SECONDS = 1800;

export const TAG_STATUS_KEEP = {
  strict: new Set(['gt']),
  balanced: new Set(['gt', 'gtl']),
  off: new Set(['gt', 'gtl', 'gtw']),
};

export const TAG_TRANSLATION_DEFAULT_URL = 'https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json';

export const KV_PERSISTED_PREFIXES = ['mytags:', 'tag-translation:', 'favorite-categories:'];

export function isoNow() {
  return new Date().toISOString();
}

export class HttpError extends Error {
  constructor(status, message, headers = {}) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

export function parseEnvList(value) {
  return String(value || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
}

export function boolEnv(value) {
  return String(value || '').trim().toLowerCase() === 'true' || String(value || '').trim() === '1';
}

export function normalizedEnvString(value, fallback = '') {
  const raw = String(value ?? '').trim();
  return raw || fallback;
}

export function cookieTokenValue(value) {
  const raw = String(value || '').trim();
  if (!raw || raw.toLowerCase() === 'mystery') return '';
  return raw;
}

export function nwEnv(env) {
  return normalizedEnvString(env.NW, '1');
}

export function datatagsEnv(env) {
  return normalizedEnvString(env.DATATAGS, '1');
}

export function ehProfileEnv(env) {
  return normalizedEnvString(env.EH_PROFILE, 'PandaOPDS');
}

export function acqDetailEnv(env) {
  const raw = env.OPDS_ACQ_DETAIL;
  if (raw !== undefined && raw !== null && String(raw).trim() !== '') {
    return boolEnv(raw);
  }
  const legacy = String(env.OPDS_ACQ_MODE || '').trim().toLowerCase();
  return legacy === 'detail';
}

export function intEnv(value, fallback) {
  const parsed = Number.parseInt(String(value || ''), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function floatEnv(value, fallback) {
  const parsed = Number.parseFloat(String(value || ''));
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function boolOrDefaultEnv(value, fallback) {
  const raw = String(value || '').trim().toLowerCase();
  if (!raw) return fallback;
  if (['true', '1', 'yes', 'on'].includes(raw)) return true;
  if (['false', '0', 'no', 'off'].includes(raw)) return false;
  return fallback;
}

export function tagStatusFilterEnv(env) {
  const raw = String(env.TAG_STATUS_FILTER || 'balanced').trim().toLowerCase() || 'balanced';
  return TAG_STATUS_KEEP[raw] ? raw : 'balanced';
}

export function commentsEnabledEnv(env) {
  return boolOrDefaultEnv(env.COMMENTS_ENABLED, true);
}

export function tagTranslationEnabledEnv(env) {
  return boolOrDefaultEnv(env.TAG_TRANSLATION_ENABLED, false);
}

export function tagTranslationUrlEnv(env) {
  return String(env.TAG_TRANSLATION_URL || TAG_TRANSLATION_DEFAULT_URL).trim() || TAG_TRANSLATION_DEFAULT_URL;
}

export function tagTranslationIntervalEnv(env) {
  return intEnv(env.TAG_TRANSLATION_INTERVAL_SECONDS, 86400);
}

export function mytagsTtlEnv(env) {
  return intEnv(env.MYTAGS_TTL_SECONDS, 21600);
}

export function htmlIntervalEnv(env) {
  return Math.max(0, floatEnv(env.HTML_INTERVAL_SECONDS, LIMITER_DEFAULTS.htmlIntervalSeconds));
}

export function htmlMaxConcurrencyEnv(env) {
  return Math.max(1, intEnv(env.MAX_CONCURRENCY, LIMITER_DEFAULTS.htmlMaxConcurrency));
}

export function imageMaxConcurrencyEnv(env) {
  return Math.max(1, intEnv(env.IMAGE_MAX_CONCURRENCY, LIMITER_DEFAULTS.imageMaxConcurrency));
}

export function thumbMaxConcurrencyEnv(env) {
  return Math.max(1, intEnv(env.THUMB_MAX_CONCURRENCY, LIMITER_DEFAULTS.thumbMaxConcurrency));
}

export function imageSourceRetriesEnv(env) {
  return Math.max(0, intEnv(env.IMAGE_SOURCE_RETRIES, 2));
}

export function bannedCooldownEnv(env) {
  return Math.max(0, floatEnv(env.BANNED_COOLDOWN_SECONDS, 1800));
}

export function exceedCooldownEnv(env) {
  return Math.max(0, floatEnv(env.EXCEED_COOLDOWN_SECONDS, 300));
}

export function cacheTtls(env) {
  return {
    list: intEnv(env.LIST_CACHE_TTL_SECONDS, DEFAULT_TTLS.list),
    detail: intEnv(env.DETAIL_CACHE_TTL_SECONDS, DEFAULT_TTLS.detail),
    image: intEnv(env.IMAGE_CACHE_TTL_SECONDS, DEFAULT_TTLS.image),
  };
}

export function isFatalImageSourceError(error) {
  return error instanceof HttpError && [400, 404, 429, 503].includes(error.status);
}

export class CircuitBreaker {
  constructor(cooldownSeconds = CIRCUIT_COOLDOWN_SECONDS) {
    this.cooldownMs = cooldownSeconds * 1000;
    this.state = 'closed';
    this.reason = null;
    this.openedAt = null;
  }

  isOpen() {
    if (this.state !== 'open') return false;
    if (Date.now() - this.openedAt >= this.cooldownMs) {
      this.state = 'closed';
      this.reason = null;
      this.openedAt = null;
      return false;
    }
    return true;
  }

  trip(reason, cooldownSeconds = null) {
    this.state = 'open';
    this.reason = reason;
    this.openedAt = Date.now();
    this.cooldownMs = (cooldownSeconds || CIRCUIT_COOLDOWN_SECONDS) * 1000;
    console.error(`CIRCUIT BREAKER TRIPPED: ${reason} (cooldown ${this.cooldownMs / 1000}s)`);
  }

  check() {
    if (this.isOpen()) {
      const retryAfter = Math.max(0, Math.ceil((this.cooldownMs - (Date.now() - this.openedAt)) / 1000));
      throw new HttpError(503, `circuit open: ${this.reason}`, { 'Retry-After': String(retryAfter) });
    }
  }
}

export const circuitBreaker = new CircuitBreaker();

export function siteHost(env) {
  return String(env.EH_SITE || 'e-hentai').trim().toLowerCase() === 'exhentai' ? 'exhentai.org' : 'e-hentai.org';
}

export function origin(env) {
  return `https://${siteHost(env)}`;
}

export function apiOrigin(env) {
  return siteHost(env) === 'exhentai.org' ? 'https://exhentai.org' : 'https://api.e-hentai.org';
}

export function buildBaseHeaders(contentType, extra = {}) {
  return new Headers({
    'content-type': contentType,
    ...extra,
  });
}

export function makeCacheKey(request) {
  return new Request(request.url, { method: 'GET' });
}