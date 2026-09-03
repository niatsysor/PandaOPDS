import { parse as parseToml } from '@iarna/toml';

export const TOPLIST_TL = {
  yesterday: 15,
  month: 13,
  year: 12,
  alltime: 11,
};

export const DEFAULT_PUBLICATION_PREVIEW_COUNT = 10;

export const DEFAULT_HOME_CONFIG = {
  groups: [
    { id: 'rankings', title: '排行榜' },
    { id: 'browse', title: '浏览' },
  ],
  sections: [
    { group: 'rankings', kind: 'publication', title: '昨日最佳', type: 'preset', query: 'toplist:yesterday', count: 20 },
    { group: 'rankings', kind: 'navigation', title: '月度精选', type: 'preset', query: 'toplist:month', count: 0 },
    { group: 'rankings', kind: 'navigation', title: '年度佳作', type: 'preset', query: 'toplist:year', count: 0 },
    { group: 'browse', kind: 'publication', title: '本周热门', type: 'preset', query: 'popular', count: 20 },
    { group: 'browse', kind: 'navigation', title: '最新上传', type: 'preset', query: 'latest', count: 0 },
    { group: '', kind: 'publication', title: '中文同人', type: 'search', query: 'language:chinese', count: 20 },
    { group: '', kind: 'navigation', title: '历史总榜', type: 'preset', query: 'toplist:alltime', count: 0 },
    { group: '', kind: 'navigation', title: '我的收藏', type: 'preset', query: 'favorites', count: 0 },
    { group: '', kind: 'navigation', title: '日文原版', type: 'search', query: 'language:japanese', count: 0 },
  ],
};

const HOME_CONFIG_CACHE = { raw: '', config: DEFAULT_HOME_CONFIG };

function normalizeHomeSection(item) {
  if (!item || typeof item !== 'object') return null;
  const kind = String(item.kind || 'publication').trim().toLowerCase();
  const type = String(item.type || 'preset').trim().toLowerCase();
  const title = String(item.title || '').trim();
  const query = String(item.query || '').trim();
  const group = String(item.group || '').trim();
  const countRaw = item.count;
  const count = Number.isFinite(countRaw) ? Number(countRaw) : Number.parseInt(String(countRaw || ''), 10);
  if (!title || !query) return null;
  return {
    group,
    kind: kind === 'navigation' ? 'navigation' : 'publication',
    type: type === 'search' ? 'search' : 'preset',
    title,
    query,
    count: Number.isFinite(count) ? count : (kind === 'navigation' ? 0 : DEFAULT_PUBLICATION_PREVIEW_COUNT),
  };
}

export function parseHomeConfigToml(text) {
  const parsed = parseToml(String(text || ''));
  const groups = [];
  for (const group of Array.isArray(parsed.group) ? parsed.group : []) {
    if (!group || typeof group !== 'object') continue;
    const id = String(group.id || '').trim();
    const title = String(group.title || id || '').trim();
    if (!id || !title) continue;
    groups.push({ id, title });
  }

  const sections = [];
  for (const section of Array.isArray(parsed.section) ? parsed.section : []) {
    const normalized = normalizeHomeSection(section);
    if (normalized) sections.push(normalized);
  }

  return { groups, sections };
}

export function loadHomeConfig(env) {
  const raw = String(env.HOME_CONFIG_TOML || '').trim();
  if (!raw) return DEFAULT_HOME_CONFIG;
  if (HOME_CONFIG_CACHE.raw === raw && HOME_CONFIG_CACHE.config) return HOME_CONFIG_CACHE.config;
  try {
    const config = parseHomeConfigToml(raw);
    HOME_CONFIG_CACHE.raw = raw;
    HOME_CONFIG_CACHE.config = config;
    return config;
  } catch (error) {
    console.warn('Failed to parse HOME_CONFIG_TOML, falling back to defaults:', error);
    HOME_CONFIG_CACHE.raw = raw;
    HOME_CONFIG_CACHE.config = DEFAULT_HOME_CONFIG;
    return DEFAULT_HOME_CONFIG;
  }
}

export function homeSectionSpec(section) {
  if (!section || typeof section !== 'object') throw new Error('invalid home section');
  if (section.type === 'search') {
    return { mode: 'search', query: section.query };
  }
  switch (section.query) {
    case 'latest':
      return { mode: 'latest' };
    case 'popular':
      return { mode: 'popular' };
    case 'watched':
      return { mode: 'watched' };
    case 'favorites':
      return { mode: 'favorites' };
    case 'toplist:yesterday':
      return { mode: 'toplist', period: 'yesterday' };
    case 'toplist:month':
      return { mode: 'toplist', period: 'month' };
    case 'toplist:year':
      return { mode: 'toplist', period: 'year' };
    case 'toplist:alltime':
      return { mode: 'toplist', period: 'alltime' };
    default:
      throw new Error(`Unknown preset: ${section.query}`);
  }
}

export function homeSectionHref(section, publicHref) {
  const spec = homeSectionSpec(section);
  switch (spec.mode) {
    case 'latest':
      return publicHref('/opds/v2.0/gallery');
    case 'popular':
      return publicHref('/opds/v2.0/gallery?query=popular');
    case 'watched':
      return publicHref('/opds/v2.0/gallery?query=watched');
    case 'favorites':
      return publicHref('/opds/v2.0/gallery?query=favorites');
    case 'toplist':
      return publicHref(`/opds/v2.0/toplist?period=${encodeURIComponent(spec.period || 'yesterday')}`);
    case 'search':
      return publicHref(`/opds/v2.0/gallery?query=${encodeURIComponent(spec.query)}`);
    default:
      throw new Error(`Unknown home section mode: ${spec.mode}`);
  }
}

export function homeSectionRequiresAuth(section) {
  const spec = homeSectionSpec(section);
  return spec.mode === 'watched' || spec.mode === 'favorites';
}

export function homeSectionUpstreamUrl(section, origin, query = null) {
  const spec = homeSectionSpec(section);
  const url = new URL(spec.mode === 'toplist' ? 'https://e-hentai.org' : origin);
  switch (spec.mode) {
    case 'latest':
      url.pathname = '/';
      break;
    case 'popular':
      url.pathname = '/popular';
      break;
    case 'watched':
      url.pathname = '/watched';
      break;
    case 'favorites':
      url.pathname = '/favorites.php';
      break;
    case 'toplist':
      url.pathname = '/toplist.php';
      url.searchParams.set('tl', String(TOPLIST_TL[spec.period || 'yesterday'] || TOPLIST_TL.yesterday));
      break;
    case 'search':
      url.pathname = '/';
      if (query ?? spec.query) url.searchParams.set('f_search', query ?? spec.query);
      break;
    default:
      throw new Error(`Unknown home section mode: ${spec.mode}`);
  }
  return url.toString();
}