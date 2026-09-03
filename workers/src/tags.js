import { TagTranslator, extractAdvParams } from './tag_features.js';
import { parseMyTags } from './parsers.js';
import { fetchText } from './upstream.js';
import {
  TAG_STATUS_KEEP,
  mytagsTtlEnv,
  siteHost,
  tagStatusFilterEnv,
  tagTranslationEnabledEnv,
  tagTranslationIntervalEnv,
  tagTranslationUrlEnv,
} from './config.js';
import {
  persistentGet,
  persistentSet,
} from './persistent.js';

let tagTranslatorState = null;
let tagTranslatorKey = '';
export const mytagsState = new Map();

export async function getTagTranslator(env) {
  if (!tagTranslationEnabledEnv(env)) return null;
  const key = `${tagTranslationUrlEnv(env)}|${tagTranslationIntervalEnv(env)}`;
  if (!tagTranslatorState || tagTranslatorKey !== key) {
    tagTranslatorState = new TagTranslator({
      url: tagTranslationUrlEnv(env),
      intervalSeconds: tagTranslationIntervalEnv(env),
    });
    tagTranslatorKey = key;
    const snapshot = await persistentGet(env, `tag-translation:${tagTranslationUrlEnv(env)}`);
    if (snapshot && snapshot.url === tagTranslationUrlEnv(env)) {
      tagTranslatorState.hydrate(snapshot);
    }
  }
  if (tagTranslatorState.stale()) {
    const ok = await tagTranslatorState.ensureLoaded();
    if (ok) {
      await persistentSet(env, `tag-translation:${tagTranslationUrlEnv(env)}`, tagTranslatorState.snapshot());
    }
  }
  return tagTranslatorState;
}

export async function translateTagName(env, tag, translator = null) {
  if (!translator) return `${tag.namespace}:${tag.key}`;
  const translated = translator.translateTag(tag.namespace, tag.key);
  return translated || `${tag.namespace}:${tag.key}`;
}

export async function buildSubjects(env, tags, translator = null) {
  const out = [];
  for (const tag of tags || []) {
    const subject = { name: await translateTagName(env, tag, translator) };
    if (tag.style) subject['x:style'] = tag.style;
    out.push(subject);
  }
  return out;
}

export function filterTagsByStatus(tags, level) {
  const keep = TAG_STATUS_KEEP[level] || TAG_STATUS_KEEP.balanced;
  return (tags || []).filter((tag) => keep.has(tag.status || 'gt'));
}

export function mytagsCacheKey(env) {
  return `${siteHost(env)}:${String(env.IPB_MEMBER_ID || '')}:${String(env.IPB_PASS_HASH || '')}`;
}

export async function applyMyTagsStyles(env, tags) {
  if (!env.IPB_MEMBER_ID || !env.IPB_PASS_HASH) return tags;
  const ttl = mytagsTtlEnv(env);
  const key = mytagsCacheKey(env);
  let entry = mytagsState.get(key);
  if (!entry) {
    const snapshot = await persistentGet(env, `mytags:${key}`);
    if (snapshot && typeof snapshot === 'object') {
      entry = {
        savedAt: Number.parseFloat(snapshot.savedAt) || 0,
        styles: snapshot.styles && typeof snapshot.styles === 'object' ? snapshot.styles : {},
        pending: null,
      };
      mytagsState.set(key, entry);
    }
  }
  if (!entry) {
    entry = { savedAt: 0, styles: {}, pending: null };
  }
  const stale = !entry.savedAt || ttl <= 0 || (Date.now() / 1000 - entry.savedAt) >= ttl;
  if (stale && !entry.pending) {
    entry.pending = (async () => {
      try {
        const { text } = await fetchText('/mytags', env);
        entry.styles = parseMyTags(text);
        entry.savedAt = Date.now() / 1000;
        await persistentSet(env, `mytags:${key}`, {
          savedAt: entry.savedAt,
          styles: entry.styles,
        });
        return entry.styles;
      } catch {
        return entry.styles;
      } finally {
        entry.pending = null;
      }
    })();
    mytagsState.set(key, entry);
  }
  if (entry.pending) {
    await entry.pending;
  }
  mytagsState.set(key, entry);
  const styles = entry.styles || {};
  if (!Object.keys(styles).length) return tags;
  return tags.map((tag) => {
    if (tag.style) return tag;
    const lookup = styles[`${tag.namespace}:${tag.key}`.toLowerCase()] || styles[`*:${String(tag.key || '').toLowerCase()}`];
    if (lookup) return { ...tag, style: lookup };
    return tag;
  });
}

export async function rewriteSearchQuery(env, query) {
  const translator = await getTagTranslator(env);
  return translator ? translator.translateQuery(query) : query;
}

export async function prepareSearchQuery(env, query) {
  const { query: strippedQuery, params } = extractAdvParams(query);
  const translatedQuery = await rewriteSearchQuery(env, strippedQuery);
  return { query: translatedQuery, params };
}