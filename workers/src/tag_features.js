const DEFAULT_TAG_TRANSLATION_URL = 'https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json';

const EMOJI_RE = /[\u{1F000}-\u{1FFFF}\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]/gu;
const WS_RE = /\s+/g;
const NS_ALIAS_SUPPLEMENT = {
  char: 'character',
  circle: 'group',
  lang: 'language',
  series: 'parody',
};
const DOUBLE_QUOTE_RE = /[\u201c\u201d\u201e\u00ab\u00bb\uff02]/g;
const COLON_SPACE_QUOTE_RE = /:\s+"/g;
const TOKEN_RE = /[^\s"]+:"[^"]*"|"[^"]*"|\S+/g;
const PREFIXED_QUOTED_RE = /([^:\s"]+):"([^"]*)"/;
const RATING_RE = /^rating:([1-5])$/i;
const KEYWORDS = new Map([
  ['expunged', { f_sh: 'on' }],
  ['nohide:uploader', { f_sfu: 'on' }],
  ['nohide:language', { f_sfl: 'on' }],
  ['nohide:tags', { f_sft: 'on' }],
  ['nohide:all', { f_sfu: 'on', f_sfl: 'on', f_sft: 'on' }],
]);

function normalizeQuery(query) {
  return String(query || '')
    .replace(DOUBLE_QUOTE_RE, '"')
    .replace(COLON_SPACE_QUOTE_RE, ':"');
}

export function cleanTranslatedName(name) {
  return String(name || '')
    .replace(EMOJI_RE, '')
    .replace(WS_RE, ' ')
    .trim();
}

export function extractAdvParams(query) {
  if (!query) return { query, params: {} };
  const normalized = normalizeQuery(query);
  const params = {};
  const rest = [];
  let matched = false;
  for (const tok of normalized.match(TOKEN_RE) || []) {
    if (tok.startsWith('"')) {
      rest.push(tok);
      continue;
    }
    const key = tok.toLowerCase();
    const hit = KEYWORDS.get(key);
    if (hit) {
      Object.assign(params, hit);
      matched = true;
      continue;
    }
    const rating = key.match(RATING_RE);
    if (rating) {
      params.f_srdd = rating[1];
      matched = true;
      continue;
    }
    if (key.startsWith('rating:') || key.startsWith('nohide:') || key === 'expunged') {
      matched = true;
      continue;
    }
    rest.push(tok);
  }
  if (!matched) return { query, params: {} };
  if (Object.keys(params).length) params.advsearch = '1';
  return { query: rest.join(' '), params };
}

export function parseTagTranslationDb(payload) {
  const data = payload?.data;
  const buckets = Array.isArray(data)
    ? data.filter((bucket) => bucket && typeof bucket === 'object')
    : data && typeof data === 'object'
      ? Object.entries(data).map(([namespace, bucket]) => ({ namespace, ...bucket }))
      : [];
  const namespaces = {};
  const tags = {};
  const abbrs = {};
  for (const bucket of buckets) {
    const namespace = String(bucket.namespace || '').trim();
    if (!namespace || namespace === 'ns' || namespace === 'rows') continue;
    const frontMatters = bucket.frontMatters && typeof bucket.frontMatters === 'object' ? bucket.frontMatters : null;
    if (frontMatters) {
      const display = cleanTranslatedName(frontMatters.name);
      if (display) namespaces[namespace] = display;
      const abbr = String(frontMatters.abbr || '').trim();
      if (abbr) abbrs[abbr.toLowerCase()] = namespace;
    }
    const raw = bucket.data && typeof bucket.data === 'object'
      ? bucket.data
      : bucket.raw && typeof bucket.raw === 'object'
        ? bucket.raw
        : null;
    if (!raw) continue;
    for (const [key, entry] of Object.entries(raw)) {
      if (!entry || typeof entry !== 'object') continue;
      const display = cleanTranslatedName(entry.name);
      if (display) tags[`${namespace}:${key}`] = display;
    }
  }
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    const nsRaw = data.ns && typeof data.ns === 'object' ? data.ns.raw : null;
    if (nsRaw && typeof nsRaw === 'object') {
      for (const [namespace, entry] of Object.entries(nsRaw)) {
        if (!entry || typeof entry !== 'object') continue;
        const display = cleanTranslatedName(entry.name);
        if (display) namespaces[namespace] = display;
      }
    }
  }
  return { namespaces, tags, abbrs };
}

export class TagTranslator {
  constructor({ url = DEFAULT_TAG_TRANSLATION_URL, intervalSeconds = 86400 } = {}) {
    this.url = url || DEFAULT_TAG_TRANSLATION_URL;
    this.intervalSeconds = intervalSeconds;
    this._client = null;
    this._loaded = false;
    this._savedAt = 0;
    this._refreshLock = null;
    this.namespaces = {};
    this.abbrs = {};
    this.tags = {};
    this._forward = new Map();
    this._nsLookup = new Map();
    this._reversePrefixed = new Map();
    this._reverseBare = new Map();
    this.lastError = null;
  }

  get loaded() {
    return this._loaded;
  }

  get savedAt() {
    return this._savedAt;
  }

  stale() {
    if (!this._loaded) return true;
    if (this.intervalSeconds <= 0) return false;
    return (Date.now() / 1000 - this._savedAt) >= this.intervalSeconds;
  }

  _rebuild() {
    this._nsLookup = new Map();
    for (const [en, cn] of Object.entries(this.namespaces)) {
      this._nsLookup.set(String(en).toLowerCase(), en);
      if (cn) this._nsLookup.set(String(cn).toLowerCase(), en);
    }
    for (const [abbr, en] of Object.entries(this.abbrs)) {
      if (!this._nsLookup.has(String(abbr).toLowerCase())) {
        this._nsLookup.set(String(abbr).toLowerCase(), en);
      }
    }
    for (const [alias, en] of Object.entries(NS_ALIAS_SUPPLEMENT)) {
      if (!this._nsLookup.has(alias.toLowerCase())) {
        this._nsLookup.set(alias.toLowerCase(), en);
      }
    }
    this._forward = new Map();
    this._reversePrefixed = new Map();
    this._reverseBare = new Map();
    for (const [full, name] of Object.entries(this.tags).sort(([a], [b]) => a.localeCompare(b))) {
      const [ns, key] = full.split(':', 2);
      if (!ns || !key) continue;
      const displayNs = this.namespaces[ns] || ns;
      this._forward.set(`${ns.toLowerCase()}\u0000${key.toLowerCase()}`, [displayNs, name]);
      this._reversePrefixed.set(`${ns.toLowerCase()}\u0000${name.toLowerCase()}`, [ns, key]);
      const list = this._reverseBare.get(name.toLowerCase()) || [];
      list.push([ns, key]);
      this._reverseBare.set(name.toLowerCase(), list);
    }
  }

  install(namespaces, tags, abbrs = {}) {
    this.namespaces = Object.fromEntries(
      Object.entries(namespaces || {}).map(([k, v]) => [String(k), cleanTranslatedName(v) || String(k)])
    );
    this.tags = Object.fromEntries(
      Object.entries(tags || {}).map(([k, v]) => [String(k), cleanTranslatedName(v)]).filter(([, v]) => v)
    );
    this.abbrs = Object.fromEntries(
      Object.entries(abbrs || {}).map(([k, v]) => [String(k).toLowerCase(), String(v)]).filter(([, v]) => v)
    );
    this._savedAt = Date.now() / 1000;
    this._loaded = true;
    this._rebuild();
  }

  hydrate(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return false;
    this.namespaces = Object.fromEntries(
      Object.entries(snapshot.namespaces || {}).map(([k, v]) => [String(k), cleanTranslatedName(v) || String(k)])
    );
    this.tags = Object.fromEntries(
      Object.entries(snapshot.tags || {}).map(([k, v]) => [String(k), cleanTranslatedName(v)]).filter(([, v]) => v)
    );
    this.abbrs = Object.fromEntries(
      Object.entries(snapshot.abbrs || {}).map(([k, v]) => [String(k).toLowerCase(), String(v)]).filter(([, v]) => v)
    );
    this._savedAt = Number.parseFloat(snapshot.savedAt) || Date.now() / 1000;
    this._loaded = true;
    this._rebuild();
    return true;
  }

  snapshot() {
    return {
      url: this.url,
      intervalSeconds: this.intervalSeconds,
      savedAt: this._savedAt,
      namespaces: this.namespaces,
      tags: this.tags,
      abbrs: this.abbrs,
    };
  }

  async _getClient() {
    if (this._client) return this._client;
    this._client = globalThis.fetch;
    return this._client;
  }

  async refresh() {
    if (this._refreshLock) return this._refreshLock;
    this._refreshLock = (async () => {
      try {
        const client = await this._getClient();
        const response = await client(this.url, {
          headers: {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            'accept-language': 'en-US,en;q=0.9',
          },
        });
        if (!response.ok) throw new Error(`tag translation upstream returned ${response.status}`);
        const payload = await response.json();
        const parsed = parseTagTranslationDb(payload);
        if (!Object.keys(parsed.tags).length) {
          throw new Error('tag translation payload contained no tags');
        }
        this.install(parsed.namespaces, parsed.tags, parsed.abbrs);
        this.lastError = null;
        return true;
      } catch (error) {
        this.lastError = String(error?.message || error);
        return false;
      } finally {
        this._refreshLock = null;
      }
    })();
    return this._refreshLock;
  }

  async ensureLoaded() {
    if (!this.stale()) return true;
    return this.refresh();
  }

  translateTag(namespace, key) {
    const hit = this._forward.get(`${String(namespace).toLowerCase()}\u0000${String(key).toLowerCase()}`);
    if (!hit) return null;
    const [displayNs, name] = hit;
    return `${displayNs}:${name}`;
  }

  _format(namespace, key) {
    return String(key).includes(' ') ? `${namespace}:"${key}"` : `${namespace}:${key}`;
  }

  _translatePrefixed(prefix, rest) {
    const en = this._nsLookup.get(String(prefix).toLowerCase());
    if (!en || !rest) return null;
    const hit = this._reversePrefixed.get(`${en.toLowerCase()}\u0000${String(rest).toLowerCase()}`);
    if (!hit) return null;
    return this._format(hit[0], hit[1]);
  }

  translateToken(tok) {
    const prefixedQuoted = String(tok).match(PREFIXED_QUOTED_RE);
    if (prefixedQuoted) {
      return this._translatePrefixed(prefixedQuoted[1], prefixedQuoted[2]);
    }
    const text = String(tok);
    const sep = text.indexOf(':');
    if (sep > 0) {
      const prefix = text.slice(0, sep);
      const rest = text.slice(sep + 1).replace(/^"|"$/g, '');
      if (rest) {
        return this._translatePrefixed(prefix, rest);
      }
    }
    const cands = this._reverseBare.get(String(tok).toLowerCase());
    if (cands && cands.length === 1) {
      return this._format(cands[0][0], cands[0][1]);
    }
    return null;
  }

  translateQuery(query) {
    if (!query || !this.loaded) return query;
    const normalized = normalizeQuery(query);
    const toks = normalized.match(TOKEN_RE) || [];
    const out = [];
    let changed = false;
    let i = 0;
    while (i < toks.length) {
      const tok = toks[i];
      if (tok.startsWith('"')) {
        if (tok.length >= 2 && tok.endsWith('"')) {
          const inner = tok.slice(1, -1);
          if (inner.includes(':')) {
            const [prefix, ...restParts] = inner.split(':');
            const rep = this._translatePrefixed(prefix, restParts.join(':').trim());
            if (rep !== null) {
              out.push(rep);
              changed = true;
              i += 1;
              continue;
            }
          }
        }
        out.push(tok);
        i += 1;
        continue;
      }

      const rep = this.translateToken(tok);
      if (rep !== null) {
        out.push(rep);
        changed = true;
        i += 1;
        continue;
      }

      if (tok.includes(':')) {
        const [prefix, ...restParts] = tok.split(':');
        const rest = restParts.join(':');
        const en = this._nsLookup.get(prefix.toLowerCase());
        if (en && rest) {
          let best = null;
          let bestEnd = i;
          let candidate = rest;
          for (let j = i + 1; j < Math.min(toks.length, i + 6); j += 1) {
            const nextTok = toks[j];
            if (nextTok.includes(':') || nextTok.startsWith('"')) break;
            candidate = `${candidate} ${nextTok}`;
            if (this._reversePrefixed.has(`${en.toLowerCase()}\u0000${candidate.toLowerCase()}`)) {
              best = candidate;
              bestEnd = j;
            }
          }
          if (best !== null) {
            const rep2 = this._translatePrefixed(prefix, best);
            if (rep2 !== null) {
              out.push(rep2);
              changed = true;
              i = bestEnd + 1;
              continue;
            }
          }
        }
      }

      out.push(tok);
      i += 1;
    }
    return changed ? out.join(' ') : query;
  }
}
