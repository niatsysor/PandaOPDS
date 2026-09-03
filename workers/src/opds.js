import { extractListLanguage, parseDetailPage, parseGalleryTitleAuthors, parseListPage } from './parsers.js';
import { TOPLIST_TL, homeSectionHref, homeSectionRequiresAuth, homeSectionSpec, homeSectionUpstreamUrl, loadHomeConfig } from './home.js';
import { buildAtomFeed, buildOpenSearchDescription, buildOpds2Navigation, buildOpds2Publication, escapeXml, opds2Link, toHref } from './feed.js';
import { fetchText, origin, upstreamUrl, toplistUrl } from './upstream.js';
import { rewriteCommentContent } from './comments.js';
import {
  DEFAULT_FACETS,
  HttpError,
  LIST_TITLES,
  acqDetailEnv,
  buildBaseHeaders,
  cacheTtls,
  commentsEnabledEnv,
  isoNow,
  siteHost,
  tagStatusFilterEnv,
} from './config.js';
import {
  LIST_CACHE_VARIANT,
  cacheListCovers,
  detailCacheRequestUrl,
  detailCache,
  listCache,
  memCacheGetOrSet,
  setCachedCoverUrl,
} from './persistent.js';
import {
  applyMyTagsStyles,
  buildSubjects,
  filterTagsByStatus,
  getTagTranslator,
  prepareSearchQuery,
} from './tags.js';

export function parseFacets(env) {
  const raw = String(env.FACETS || '').trim();
  if (!raw) return DEFAULT_FACETS;
  const facets = [];
  for (const part of raw.split(',')) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const idx = trimmed.lastIndexOf(':');
    if (idx <= 0) continue;
    const name = trimmed.slice(0, idx).trim();
    const mask = Number.parseInt(trimmed.slice(idx + 1).trim(), 10);
    if (!name || !Number.isFinite(mask)) continue;
    facets.push([name, mask]);
  }
  return facets.length ? facets : DEFAULT_FACETS;
}

export function publicHref(path, env) {
  return toHref(path, env);
}

export async function fetchListPage(env, upstreamUrlString, kind) {
  const ttl = cacheTtls(env).list;
  return memCacheGetOrSet(listCache, upstreamUrlString, async () => {
    const { text } = await fetchText(upstreamUrlString, env);
    const result = parseListPage(text, upstreamUrlString, kind);
    await cacheListCovers(env, result.items);
    return result;
  }, ttl);
}

async function fetchHomeSectionPreview(env, section, translator = null) {
  const spec = homeSectionSpec(section);
  const search = spec.mode === 'search' ? await prepareSearchQuery(env, spec.query) : { query: null, params: {} };
  const upstream = new URL(homeSectionUpstreamUrl(section, origin(env), search.query));
  if (spec.mode === 'search') {
    for (const [key, value] of Object.entries(search.params || {})) {
      upstream.searchParams.set(key, value);
    }
  }
  upstream.searchParams.set('inline_set', 'dm_e');
  const ttl = cacheTtls(env).list;
  const parsed = await memCacheGetOrSet(listCache, upstream.toString(), async () => {
    const { text } = await fetchText(upstream, env);
    const result = parseListPage(text, upstream.toString(), spec.mode === 'latest' ? 'search' : spec.mode);
    await cacheListCovers(env, result.items);
    return result;
  }, ttl);
  return parsed;
}

async function buildHomeNavigationDocument(env, ctx) {
  const home = loadHomeConfig(env);
  const hasAuth = Boolean(env.IPB_MEMBER_ID && env.IPB_PASS_HASH);
  const translator = await getTagTranslator(env);
  const visibleSections = home.sections.filter((section) => !homeSectionRequiresAuth(section) || hasAuth);
  const previewSections = visibleSections.filter((section) => section.kind === 'publication' && section.count > 0);
  const previewResults = new Map();

  await Promise.all(previewSections.map(async (section) => {
    try {
      previewResults.set(section, await fetchHomeSectionPreview(env, section, translator));
    } catch (error) {
      console.warn(`Failed to fetch home section preview for ${section.title}:`, error);
    }
  }));

  const groupDefs = new Map(home.groups.map((group) => [group.id, group.title]));
  const groupsOut = [];
  const rootNav = [];
  const sectionGroups = new Map();
  const seenGroups = new Set();
  const hrefForHome = (path) => publicHref(path, env);

  for (const section of visibleSections) {
    const groupKey = String(section.group || '').trim();
    if (groupKey) {
      if (!sectionGroups.has(groupKey)) {
        sectionGroups.set(groupKey, []);
      }
      sectionGroups.get(groupKey).push(section);
    }
  }

  for (const section of visibleSections) {
    const groupKey = String(section.group || '').trim();
    if (!groupKey) {
      if (section.kind === 'navigation') {
        rootNav.push({ title: section.title, href: homeSectionHref(section, hrefForHome), rel: 'subsection', type: 'application/opds+json' });
      } else {
        const group = {
          metadata: {
            title: section.title,
            modified: isoNow(),
          },
          links: [{ rel: 'self', href: homeSectionHref(section, hrefForHome), type: 'application/opds+json' }],
        };
        const preview = previewResults.get(section);
        if (preview) {
          const publications = [];
          for (const item of preview.items.slice(0, section.count)) {
            publications.push(await itemToPublication(env, item, false, translator));
          }
          if (publications.length) group.publications = publications;
        }
        groupsOut.push(group);
      }
      continue;
    }

    if (seenGroups.has(groupKey)) continue;
    seenGroups.add(groupKey);
    const sections = sectionGroups.get(groupKey) || [];
    const title = groupDefs.get(groupKey) || sections[0]?.title || groupKey;
    const group = {
      metadata: {
        title,
        modified: isoNow(),
      },
      links: [{ rel: 'self', href: homeSectionHref(sections[0] || section, hrefForHome), type: 'application/opds+json' }],
    };
    const publications = [];
    const navigation = [];

    for (const entry of sections) {
      if (entry.kind === 'navigation') {
        navigation.push({ title: entry.title, href: homeSectionHref(entry, hrefForHome), rel: 'subsection', type: 'application/opds+json' });
        continue;
      }

      const preview = previewResults.get(entry);
      if (!preview) continue;
      for (const item of preview.items.slice(0, entry.count)) {
        publications.push(await itemToPublication(env, item, false, translator));
      }
    }

    if (publications.length) group.publications = publications;
    if (navigation.length) group.navigation = navigation;
    groupsOut.push(group);
  }

  return buildOpds2Navigation({
    title: 'PandaOPDS',
    updated: isoNow(),
    links: [
      { rel: 'self', href: publicHref('/opds/v2.0', env), type: 'application/opds+json' },
      { rel: 'search', href: publicHref('/opds/v2.0/gallery?query={searchTerms}', env), type: 'application/opds+json;profile=acquisition', templated: true },
    ],
    navigation: rootNav,
    groups: groupsOut,
  });
}

function resolveCategoryMask(env, category) {
  const want = String(category || '').trim().toLowerCase();
  if (!want) return null;
  for (const [name, mask] of parseFacets(env)) {
    if (name.toLowerCase() === want) return mask;
  }
  return null;
}

function buildCategoryFacets(env, currentCategory = '') {
  const current = String(currentCategory || '').trim().toLowerCase();
  const links = [
    { href: '/opds/v2.0/gallery', title: 'All' },
    ...parseFacets(env).map(([name]) => ({
      href: `/opds/v2.0/gallery?category=${encodeURIComponent(name)}`,
      title: name,
    })),
  ].map((link) => ({
    href: publicHref(link.href, env),
    title: link.title,
    active: link.title.toLowerCase() === current || (link.title === 'All' && !current),
  }));
  return [{ metadata: { title: 'Category' }, links }];
}

function listScopeFromQuery(query) {
  if (query === 'popular') return { kind: 'popular', path: '/popular' };
  if (query === 'watched') return { kind: 'watched', path: '/watched' };
  if (query === 'favorites') return { kind: 'favorites', path: '/favorites.php' };
  return { kind: 'search', path: '/' };
}

function entryToAtomLink(env, item, pageCount) {
  const links = [
    {
      rel: 'http://opds-spec.org/image/thumbnail',
      href: publicHref(`/image/${item.gid}/${item.token}/thumb`, env),
      type: 'image/jpeg',
    },
    {
      rel: 'http://opds-spec.org/acquisition',
      href: publicHref(`/opds/v1.2/gallery/${item.gid}/${item.token}/chapters`, env),
      type: 'application/atom+xml;profile=opds-catalog;kind=acquisition',
    },
    {
      rel: 'alternate',
      href: `${origin(env)}/g/${item.gid}/${item.token}/`,
      type: 'text/html',
      title: siteHost(env),
    },
  ];
  if (pageCount) {
    links.splice(2, 0, {
      rel: 'http://vaemendis.net/opds-pse/stream',
      href: publicHref(`/stream/${item.gid}/${item.token}/page/{pageNumber}`, env),
      type: 'image/jpeg',
      count: pageCount,
    });
  }
  return links;
}

async function itemToSubjects(env, item, translator = null) {
  const tags = filterTagsByStatus(item.tags, tagStatusFilterEnv(env));
  const subjects = [];
  for (const tag of tags) {
    const translated = translator ? translator.translateTag(tag.namespace, tag.key) : null;
    const subject = { name: translated || `${tag.namespace}:${tag.key}` };
    if (tag.style) subject['x:style'] = tag.style;
    subjects.push(subject);
  }
  return subjects;
}

async function itemToPublication(env, item, detail = false, translator = null) {
  const { title: cleanTitle, authors } = parseGalleryTitleAuthors(item.title, '', item.category);
  const subjects = await itemToSubjects(env, item, translator);
  const detailMode = acqDetailEnv(env);
  const direct = detail || !detailMode;
  const acquisitionHref = direct
    ? publicHref(`/stream/${item.gid}/${item.token}/page/{pageNumber}`, env)
    : publicHref(`/opds/v2.0/gallery/${item.gid}/${item.token}`, env);
  const acquisitionType = direct ? 'image/jpeg' : 'application/opds+json;profile=acquisition';
  const publication = buildOpds2Publication({
    title: cleanTitle || `${item.gid}`,
    identifier: `urn:ehentai:gallery:${item.gid}:${item.token}`,
    updated: item.published || new Date().toISOString(),
    published: item.published || new Date().toISOString(),
    authors: authors.length ? authors.map((name) => ({ name })) : [],
    language: extractListLanguage(item.tags),
    subjects,
    pageCount: item.pageCount || undefined,
    x: {
      'x:category': item.category || undefined,
      'x:rating': item.rating || undefined,
    },
    images: [{ href: publicHref(`/image/${item.gid}/${item.token}/thumb`, env), type: 'image/jpeg' }],
    links: [
      {
        rel: 'self',
        href: publicHref(`/opds/v2.0/gallery/${item.gid}/${item.token}/publication`, env),
        type: 'application/opds+json',
      },
      {
        rel: 'alternate',
        href: `${origin(env)}/g/${item.gid}/${item.token}/`,
        type: 'text/html',
        title: siteHost(env),
      },
      ...(item.pageCount
        ? [{
            rel: 'http://opds-spec.org/acquisition',
            href: acquisitionHref,
            type: acquisitionType,
            templated: direct,
            properties: { numberOfItems: item.pageCount },
          },
          {
            rel: 'http://vaemendis.net/opds-pse/stream',
            href: publicHref(`/stream/${item.gid}/${item.token}/page/{pageNumber}`, env),
            type: 'image/jpeg',
            templated: true,
            properties: { numberOfItems: item.pageCount },
          }]
        : []),
    ],
    readingOrder: detail && item.pageUrls ? item.pageUrls.map((pageHref, index) => ({
      href: publicHref(`/stream/${item.gid}/${item.token}/page/${index + 1}`, env),
      type: 'image/jpeg',
    })) : [],
  });
  return publication;
}

function atomEntry(env, item, detail = false) {
  const { title, authors } = parseGalleryTitleAuthors(item.title, '', item.category);
  const updated = item.published || new Date().toISOString();
  const author = authors.length ? authors[0] : '';
  const links = entryToAtomLink(env, item, item.pageCount || 0);
  const category = item.category ? `
      <category term="${escapeXml(item.category)}" label="${escapeXml(item.category)}" scheme="http://e-hentai.org" />` : '';
  return [
    '  <entry>',
    `    <id>urn:ehentai:gallery:${item.gid}:${item.token}</id>`,
    `    <title>${escapeXml(title || `${item.gid}`)}</title>`,
    `    <updated>${escapeXml(updated)}</updated>`,
    author ? `    <author><name>${escapeXml(author)}</name></author>` : '',
    category,
    links.map((link) => {
      const attrs = [
        `rel="${escapeXml(link.rel)}"`,
        `href="${escapeXml(link.href)}"`,
      ];
      if (link.type) attrs.push(`type="${escapeXml(link.type)}"`);
      if (link.title) attrs.push(`title="${escapeXml(link.title)}"`);
      if (link.count) attrs.push(`pse:count="${escapeXml(link.count)}"`);
      if (link.templated) attrs.push('templated="true"');
      if (link.properties && link.properties.numberOfItems) {
        attrs.push(`properties.numberOfItems="${escapeXml(link.properties.numberOfItems)}"`);
      }
      return `    <link ${attrs.join(' ')} />`;
    }).join('\n'),
    '  </entry>',
  ].filter(Boolean).join('\n');
}

function atomFeed(env, title, updated, items, extras = {}) {
  return buildAtomFeed({
    title,
    id: extras.id || `urn:ehentai:${title.toLowerCase().replace(/\s+/g, ':')}`,
    updated,
    entries: items.map((item) => {
      const parsed = parseGalleryTitleAuthors(item.title, '', item.category);
      return {
        id: `urn:ehentai:gallery:${item.gid}:${item.token}`,
        title: parsed.title,
        updated: item.published || updated,
        category: item.category,
        links: entryToAtomLink(env, item, item.pageCount || 0),
        author: parsed.authors.length ? parsed.authors[0] : undefined,
      };
    }),
    links: extras.links || [],
    facets: extras.facets || [],
    subtitle: extras.subtitle || '',
  });
}

async function loadListPage(env, ctx, { kind, path, url, page, next, upstreamQuery = null, upstreamParams = {} }) {
  const ttls = cacheTtls(env);
  const upstream = new URL(kind === 'toplist' ? toplistUrl(path) : upstreamUrl(env, path));
  const query = new URL(url).searchParams;
  const category = query.get('category') || '';
  for (const [key, value] of query.entries()) {
    if (key === 'query' || key === 'next' || key === 'page') continue;
    upstream.searchParams.set(key, value);
  }
  const searchQuery = upstreamQuery ?? query.get('query');
  if (kind === 'search' && searchQuery) {
    upstream.searchParams.set('f_search', searchQuery);
  }
  if (kind === 'search') {
    for (const [key, value] of Object.entries(upstreamParams || {})) {
      upstream.searchParams.set(key, value);
    }
  }
  if (next) upstream.searchParams.set('next', next);
  if (kind === 'toplist') {
    const period = query.get('period') || 'yesterday';
    upstream.pathname = '/toplist.php';
    upstream.searchParams.set('tl', String(TOPLIST_TL[period] || TOPLIST_TL.yesterday));
    if (page && page > 1) upstream.searchParams.set('p', String(page - 1));
  }
  upstream.searchParams.set('inline_set', 'dm_e');

  const cacheRequestUrl = new URL(upstream.toString());
  if (kind === 'search') {
    cacheRequestUrl.searchParams.set('__v', LIST_CACHE_VARIANT);
  }
  const cacheRequest = new Request(cacheRequestUrl.toString(), { method: 'GET' });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached;

  const parsed = await fetchListPage(env, upstream.toString(), kind);
  const updated = new Date().toISOString();
  const nextHref = parsed.nextCursor
    ? (kind === 'toplist'
        ? publicHref(`/opds/v1.2/toplist?period=${encodeURIComponent(query.get('period') || 'yesterday')}&page=${encodeURIComponent(parsed.nextCursor)}`, env)
        : publicHref(`/opds/v1.2/gallery?next=${encodeURIComponent(parsed.nextCursor)}${query.get('query') ? `&query=${encodeURIComponent(query.get('query'))}` : ''}`, env))
    : null;

  const atomItems = parsed.items.map((item) => ({
    ...item,
    pageCount: item.pageCount || undefined,
  }));
  const atomXml = buildAtomFeed({
    title: kind === 'toplist'
      ? `E-Hentai: Toplist ${(query.get('period') || 'yesterday').replace(/^(.)/, (m) => m.toUpperCase())}`
      : `E-Hentai: ${kind === 'search' ? 'Search' : (LIST_TITLES[kind] || 'Latest')}${category ? ` — ${category}` : ''}`,
    id: `urn:ehentai:${kind}:${query.get('query') || query.get('period') || 'latest'}`,
    updated,
    entries: atomItems.map((item) => ({
      id: `urn:ehentai:gallery:${item.gid}:${item.token}`,
      ...(() => {
        const parsedItem = parseGalleryTitleAuthors(item.title, '', item.category);
        return {
          title: parsedItem.title,
          author: parsedItem.authors.length ? parsedItem.authors[0] : undefined,
        };
      })(),
      updated: item.published || updated,
      category: item.category,
      links: entryToAtomLink(env, item, item.pageCount || 0),
    })),
    links: [
      {
        rel: 'self',
        href: publicHref(new URL(url).pathname + new URL(url).search, env),
        type: 'application/atom+xml;profile=opds-catalog;kind=acquisition',
      },
      ...(nextHref
        ? [{
            rel: 'next',
            href: nextHref,
            type: 'application/atom+xml;profile=opds-catalog;kind=acquisition',
          }]
        : []),
    ],
    facets: kind === 'toplist'
      ? Object.entries(TOPLIST_TL).map(([period, tl]) => [
          period.charAt(0).toUpperCase() + period.slice(1),
          publicHref(`/opds/v1.2/toplist?period=${period}`, env),
          period === (query.get('period') || 'yesterday'),
        ])
      : [],
  });

  const response = new Response(atomXml, buildBaseHeaders('application/atom+xml;profile=opds-catalog;kind=acquisition; charset=utf-8'));
  response.headers.set('cache-control', `public, max-age=${ttls.list}`);
  ctx.waitUntil(caches.default.put(cacheRequest, response.clone()));
  return response;
}

async function loadDetail(env, ctx, gid, token, groupIndex = 0) {
  const ttls = cacheTtls(env);
  const detailUrl = `${origin(env)}/g/${gid}/${token}/?p=${groupIndex}`;
  const cacheRequest = new Request(detailCacheRequestUrl(detailUrl), { method: 'GET' });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached;
  const { text } = await fetchText(detailUrl, env);
  const parsed = parseDetailPage(text, detailUrl, gid, token, groupIndex);
  const response = new Response(JSON.stringify(parsed), buildBaseHeaders('application/json; charset=utf-8'));
  response.headers.set('cache-control', `public, max-age=${ttls.detail}`);
  ctx.waitUntil(caches.default.put(cacheRequest, response.clone()));
  return response;
}

export async function getDetailParsed(env, ctx, gid, token, groupIndex = 0) {
  const ttls = cacheTtls(env);
  const detailUrl = `${origin(env)}/g/${gid}/${token}/?p=${groupIndex}`;
  const cacheRequest = new Request(detailCacheRequestUrl(detailUrl), { method: 'GET' });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached.json();

  const detailKey = `${gid}:${token}:${groupIndex}`;
  const parsed = await memCacheGetOrSet(detailCache, detailKey, async () => {
    const { text } = await fetchText(detailUrl, env);
    const result = parseDetailPage(text, detailUrl, gid, token, groupIndex);
    if (result.coverUrl) {
      await setCachedCoverUrl(env, gid, token, result.coverUrl, ttls.detail / 1000);
    }
    return result;
  }, ttls.detail / 1000);

  const response = new Response(JSON.stringify(parsed), buildBaseHeaders('application/json; charset=utf-8'));
  response.headers.set('cache-control', `public, max-age=${ttls.detail}`);
  ctx.waitUntil(caches.default.put(cacheRequest, response.clone()));
  return parsed;
}

export async function handleOpdsV12Root(env, ctx, request) {
  const hasAuth = Boolean(env.IPB_MEMBER_ID && env.IPB_PASS_HASH);
  const nav = [
    ['Latest', '/opds/v1.2/gallery'],
    ...(hasAuth ? [['Watched', '/opds/v1.2/gallery?query=watched'], ['Favorites', '/opds/v1.2/gallery?query=favorites']] : []),
    ['Popular', '/opds/v1.2/gallery?query=popular'],
    ['Toplist', '/opds/v1.2/toplist?period=yesterday'],
    ['Search', '/opds/v1.2/search.xml'],
  ];
  const updated = new Date().toISOString();
  const links = nav.map(([title, href]) => `  <entry>\n    <id>${escapeXml(`urn:ehentai:nav:${title.toLowerCase()}`)}</id>\n    <title>${escapeXml(title)}</title>\n    <updated>${escapeXml(updated)}</updated>\n    <link rel="subsection" href="${escapeXml(publicHref(href, env))}" type="application/atom+xml;profile=opds-catalog;kind=acquisition" />\n  </entry>`).join('\n');
  const xml = `<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog" xmlns:pse="http://vaemendis.net/opds-pse/ns">\n  <id>urn:ehentai:root</id>\n  <title>PandaOPDS</title>\n  <updated>${escapeXml(updated)}</updated>\n  <link rel="self" href="${escapeXml(publicHref('/opds/v1.2', env))}" type="application/atom+xml;profile=opds-catalog;kind=navigation" />\n${links}\n</feed>`;
  return new Response(xml, buildBaseHeaders('application/atom+xml;profile=opds-catalog;kind=navigation; charset=utf-8', {
    'cache-control': 'public, max-age=300',
  }));
}

export async function handleOpdsV12Search(env) {
  return new Response(buildOpenSearchDescription({
    template: `${publicHref('/opds/v1.2/gallery?query={searchTerms}', env)}`,
    title: 'E-Hentai',
    description: 'Search E-Hentai galleries via PandaOPDS',
  }), buildBaseHeaders('application/opensearchdescription+xml; charset=utf-8', {
    'cache-control': 'public, max-age=3600',
  }));
}

export async function handleOpdsV12Gallery(env, ctx, request) {
  const url = new URL(request.url);
  const query = url.searchParams.get('query') || '';
  const next = url.searchParams.get('next') || '';
  const page = Number.parseInt(url.searchParams.get('page') || '1', 10) || 1;
  const scope = listScopeFromQuery(query);
  const upstream = scope.path;
  const search = query && scope.kind === 'search' ? await prepareSearchQuery(env, query) : { query, params: {} };
  const rewrittenQuery = search.query;
  return loadListPage(env, ctx, {
    kind: scope.kind,
    path: upstream,
    url: `${url.origin}${url.pathname}${rewrittenQuery && scope.kind === 'search' ? `?query=${encodeURIComponent(rewrittenQuery)}` : url.search}`,
    page,
    next,
    upstreamQuery: rewrittenQuery,
    upstreamParams: search.params,
  });
}

export async function handleOpdsV12Toplist(env, ctx, request) {
  return loadListPage(env, ctx, {
    kind: 'toplist',
    path: '/toplist.php',
    url: request.url,
    page: Number.parseInt(new URL(request.url).searchParams.get('page') || '1', 10) || 1,
  });
}

export async function handleOpdsV12Chapters(env, ctx, request, gid, token) {
  const parsed = await getDetailParsed(env, ctx, gid, token, 0);
  const { title, authors } = parseGalleryTitleAuthors(parsed.title, parsed.titleJpn, parsed.category);
  const updated = parsed.publishTime || new Date().toISOString();
  const xml = buildAtomFeed({
    title: `E-Hentai: ${title}`,
    id: `urn:ehentai:gallery:${gid}:${token}`,
    updated,
    entries: [
      {
        id: `urn:ehentai:gallery:${gid}:${token}:chapter`,
        title,
        updated,
        author: authors.length ? authors[0] : undefined,
        category: parsed.category || '',
        links: [
          {
            rel: 'http://opds-spec.org/image/thumbnail',
            href: publicHref(`/image/${gid}/${token}/thumb`, env),
            type: 'image/jpeg',
          },
          {
            rel: 'http://vaemendis.net/opds-pse/stream',
            href: publicHref(`/stream/${gid}/${token}/page/{pageNumber}`, env),
            type: 'image/jpeg',
            count: parsed.fileCount || parsed.pageUrls.length,
          },
        ],
      },
    ],
    links: [
      { rel: 'self', href: publicHref(`/opds/v1.2/gallery/${gid}/${token}/chapters`, env), type: 'application/atom+xml;profile=opds-catalog;kind=acquisition' },
    ],
  });
  return new Response(xml, buildBaseHeaders('application/atom+xml;profile=opds-catalog;kind=acquisition; charset=utf-8', {
    'cache-control': 'public, max-age=3600',
  }));
}

export async function handleOpdsV20Root(env) {
  return new Response(JSON.stringify(await buildHomeNavigationDocument(env), null, 2), buildBaseHeaders('application/opds+json; charset=utf-8', {
    'cache-control': 'public, max-age=300',
  }));
}

export async function handleOpdsV20Search(env) {
  return new Response(buildOpenSearchDescription({
    template: publicHref('/opds/v2.0/gallery?query={searchTerms}', env),
    title: 'E-Hentai',
    description: 'Search E-Hentai galleries via PandaOPDS',
  }), buildBaseHeaders('application/opensearchdescription+xml; charset=utf-8', {
    'cache-control': 'public, max-age=3600',
  }));
}

export async function handleOpdsV20Gallery(env, ctx, request) {
  const url = new URL(request.url);
  const query = url.searchParams.get('query') || '';
  const next = url.searchParams.get('next') || '';
  const page = Number.parseInt(url.searchParams.get('page') || '1', 10) || 1;
  const category = url.searchParams.get('category') || '';
  const scope = listScopeFromQuery(query);
  const upstream = scope.path;
  const listUrl = new URL(scope.kind === 'toplist' ? toplistUrl(upstream) : upstreamUrl(env, upstream));
  const search = query && scope.kind === 'search' ? await prepareSearchQuery(env, query) : { query, params: {} };
  const rewrittenQuery = search.query;
  if (rewrittenQuery && scope.kind === 'search') {
    listUrl.searchParams.set('f_search', rewrittenQuery);
  }
  if (scope.kind === 'search') {
    for (const [key, value] of Object.entries(search.params || {})) {
      listUrl.searchParams.set(key, value);
    }
  }
  if (category && scope.kind === 'search') {
    const fCats = resolveCategoryMask(env, category);
    if (fCats !== null) {
      listUrl.searchParams.set('f_cats', String(fCats));
    }
  }
  if (next) listUrl.searchParams.set('next', next);
  if (scope.kind === 'toplist') {
    const period = url.searchParams.get('period') || 'yesterday';
    listUrl.pathname = '/toplist.php';
    listUrl.searchParams.set('tl', String(TOPLIST_TL[period] || TOPLIST_TL.yesterday));
    if (page > 1) listUrl.searchParams.set('p', String(page - 1));
  }
  listUrl.searchParams.set('inline_set', 'dm_e');
  const parsed = await fetchListPage(env, listUrl.toString(), scope.kind);
  const translator = await getTagTranslator(env);
  const publications = await Promise.all(parsed.items.map((item) => itemToPublication(env, item, false, translator)));
  const nextHref = parsed.nextCursor
    ? publicHref(`/opds/v2.0/gallery?next=${encodeURIComponent(parsed.nextCursor)}${query ? `&query=${encodeURIComponent(query)}` : ''}${category ? `&category=${encodeURIComponent(category)}` : ''}`, env)
    : null;
  const body = JSON.stringify({
    metadata: { title: `E-Hentai: ${scope.kind === 'search' ? 'Search' : (LIST_TITLES[scope.kind] || 'Latest')}${category ? ` — ${category}` : ''}`, modified: new Date().toISOString() },
    links: [
      opds2Link({ rel: 'self', href: publicHref(new URL(request.url).pathname + new URL(request.url).search, env), type: 'application/opds+json' }),
      opds2Link({ rel: 'search', href: publicHref('/opds/v2.0/gallery?query={searchTerms}', env), type: 'application/opds+json', templated: true }),
      ...(nextHref ? [opds2Link({ rel: 'next', href: nextHref, type: 'application/opds+json' })] : []),
    ],
    publications,
    ...(scope.kind === 'search' && query && query !== 'popular' && query !== 'watched' && query !== 'favorites'
      ? { facets: buildCategoryFacets(env, category) }
      : {}),
    navigation: [],
  }, null, 2);
  return new Response(body, buildBaseHeaders('application/opds+json; charset=utf-8', {
    'cache-control': 'public, max-age=300',
  }));
}

export async function handleOpdsV20Toplist(env, ctx, request) {
  const url = new URL(request.url);
  const period = url.searchParams.get('period') || 'yesterday';
  const page = Number.parseInt(url.searchParams.get('page') || '1', 10) || 1;
  if (!Object.prototype.hasOwnProperty.call(TOPLIST_TL, period)) {
    throw new HttpError(400, `unknown period ${period}`);
  }

  const upstream = new URL(toplistUrl('/toplist.php'));
  upstream.searchParams.set('tl', String(TOPLIST_TL[period]));
  if (page > 1) upstream.searchParams.set('p', String(page - 1));
  upstream.searchParams.set('inline_set', 'dm_e');

  const cacheRequest = new Request(upstream.toString(), { method: 'GET' });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached;

  const parsed = await fetchListPage(env, upstream.toString(), 'toplist');
  const translator = await getTagTranslator(env);
  const publications = await Promise.all(parsed.items.map((item) => itemToPublication(env, item, false, translator)));
  const nextHref = parsed.nextCursor
    ? publicHref(`/opds/v2.0/toplist?period=${encodeURIComponent(period)}&page=${encodeURIComponent(parsed.nextCursor)}`, env)
    : null;
  const facets = [{
    metadata: { title: 'Period' },
    links: Object.entries(TOPLIST_TL).map(([p, tl]) => ({
      href: publicHref(`/opds/v2.0/toplist?period=${encodeURIComponent(p)}`, env),
      title: p.charAt(0).toUpperCase() + p.slice(1),
      active: p === period,
    })),
  }];
  const body = JSON.stringify(buildOpds2Navigation({
    title: `E-Hentai: Toplist ${period.charAt(0).toUpperCase() + period.slice(1)}`,
    updated: new Date().toISOString(),
    links: [
      opds2Link({ rel: 'self', href: publicHref(new URL(request.url).pathname + new URL(request.url).search, env), type: 'application/opds+json' }),
      opds2Link({ rel: 'search', href: publicHref('/opds/v2.0/gallery?query={searchTerms}', env), type: 'application/opds+json', templated: true }),
      ...(nextHref ? [opds2Link({ rel: 'next', href: nextHref, type: 'application/opds+json' })] : []),
    ],
    publications,
    groups: [],
  }), null, 2);
  const doc = JSON.parse(body);
  doc.facets = facets;
  const response = new Response(JSON.stringify(doc, null, 2), buildBaseHeaders('application/opds+json; charset=utf-8', {
    'cache-control': 'public, max-age=300',
  }));
  ctx.waitUntil(caches.default.put(cacheRequest, response.clone()));
  return response;
}

async function handleOpdsV20GalleryEntry(env, ctx, gid, token, detailDocument = false) {
  const parsed = await getDetailParsed(env, ctx, gid, token, 0);
  const { title, authors } = parseGalleryTitleAuthors(parsed.title, parsed.titleJpn, parsed.category);
  const translator = await getTagTranslator(env);
  let tags = filterTagsByStatus(parsed.tags, tagStatusFilterEnv(env));
  tags = await applyMyTagsStyles(env, tags);
  const subjects = await buildSubjects(env, tags, translator);
  const detailMode = acqDetailEnv(env);
  const direct = detailDocument || !detailMode;
  const acquisitionHref = direct
    ? publicHref(`/stream/${gid}/${token}/page/{pageNumber}`, env)
    : publicHref(`/opds/v2.0/gallery/${gid}/${token}`, env);
  const acquisitionType = direct ? 'image/jpeg' : 'application/opds+json;profile=acquisition';
  const publication = buildOpds2Publication({
    title,
    identifier: `urn:ehentai:gallery:${gid}:${token}`,
    updated: parsed.publishTime || new Date().toISOString(),
    published: parsed.publishTime || new Date().toISOString(),
    authors: authors.length ? authors.map((name) => ({ name })) : (parsed.uploader ? [{ name: parsed.uploader }] : []),
    language: parsed.language || undefined,
    subjects,
    pageCount: parsed.fileCount || parsed.pageUrls.length || undefined,
    x: {
      'x:category': parsed.category || undefined,
      'x:titleJpn': parsed.titleJpn || undefined,
      'x:uploader': parsed.uploader || undefined,
      'x:rating': parsed.rating || undefined,
      'x:sizeBytes': parsed.filesizeBytes || undefined,
      'x:expunged': parsed.expunged || undefined,
    },
    images: [{ href: publicHref(`/image/${gid}/${token}/thumb`, env), type: 'image/jpeg' }],
    links: [
      opds2Link({ rel: 'self', href: publicHref(`/opds/v2.0/gallery/${gid}/${token}/publication`, env), type: 'application/opds+json' }),
      opds2Link({ rel: 'alternate', href: `${origin(env)}/g/${gid}/${token}/`, type: 'text/html' }),
      ...(parsed.fileCount || parsed.pageUrls.length
        ? [
            opds2Link({ rel: 'http://opds-spec.org/acquisition', href: acquisitionHref, type: acquisitionType, templated: direct, properties: { numberOfItems: parsed.fileCount || parsed.pageUrls.length } }),
            opds2Link({ rel: 'http://vaemendis.net/opds-pse/stream', href: publicHref(`/stream/${gid}/${token}/page/{pageNumber}`, env), type: 'image/jpeg', templated: true, properties: { numberOfItems: parsed.fileCount || parsed.pageUrls.length } }),
          ]
        : []),
    ],
    readingOrder: parsed.pageUrls.map((_, index) => ({ href: publicHref(`/stream/${gid}/${token}/page/${index + 1}`, env), type: 'image/jpeg' })),
  });

  if (commentsEnabledEnv(env) && parsed.comments.length) {
    publication.metadata['x:reviews'] = parsed.comments.map((comment) => ({
      id: comment.id,
      username: comment.username,
      userId: comment.userId,
      time: comment.time,
      lastEditTime: comment.lastEditTime,
      content: rewriteCommentContent(comment.content, env),
    }));
  }

  return publication;
}

export async function handleOpdsV20GalleryDetail(env, ctx, gid, token) {
  const publication = await handleOpdsV20GalleryEntry(env, ctx, gid, token, true);
  const doc = {
    metadata: {
      title: publication.metadata.title,
      identifier: publication.metadata.identifier,
      modified: publication.metadata.modified,
    },
    links: [
      opds2Link({ rel: 'self', href: publicHref(`/opds/v2.0/gallery/${gid}/${token}`, env), type: 'application/opds+json;profile=acquisition' }),
      opds2Link({ rel: 'search', href: publicHref('/opds/v2.0/gallery?query={searchTerms}', env), type: 'application/opds+json', templated: true }),
    ],
    publications: [publication],
  };
  return new Response(JSON.stringify(doc, null, 2), buildBaseHeaders('application/opds+json; charset=utf-8', {
    'cache-control': `public, max-age=${cacheTtls(env).detail}`,
  }));
}

export async function handleOpdsV20Publication(env, ctx, gid, token) {
  const publication = await handleOpdsV20GalleryEntry(env, ctx, gid, token, true);
  return new Response(JSON.stringify(publication, null, 2), buildBaseHeaders('application/opds+json; charset=utf-8', {
    'cache-control': `public, max-age=${cacheTtls(env).detail}`,
  }));
}