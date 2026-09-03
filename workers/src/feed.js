function escapeXml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

function indent(lines, level = 2) {
  const pad = ' '.repeat(level);
  return lines.map((line) => `${pad}${line}`).join('\n');
}

export function toHref(path, env) {
  if (env.PUBLIC_BASE_URL) {
    return new URL(path, env.PUBLIC_BASE_URL).toString();
  }
  return path;
}

export function atomLink({ rel, href, type, title, count }) {
  const attrs = [
    `rel="${escapeXml(rel)}"`,
    `href="${escapeXml(href)}"`,
  ];
  if (type) attrs.push(`type="${escapeXml(type)}"`);
  if (title) attrs.push(`title="${escapeXml(title)}"`);
  if (count !== undefined && count !== null) attrs.push(`pse:count="${escapeXml(count)}"`);
  return `<link ${attrs.join(' ')} />`;
}

export function buildOpenSearchDescription({ template, title = 'E-Hentai', description = 'Search E-Hentai galleries via PandaOPDS' }) {
  return `<?xml version="1.0" encoding="utf-8"?>\n` +
    `<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n` +
    indent([
      `<ShortName>${escapeXml(title)}</ShortName>`,
      `<Description>${escapeXml(description)}</Description>`,
      `<Url type="application/opds+json" template="${escapeXml(template)}" />`,
    ]) +
    `\n</OpenSearchDescription>`;
}

export function buildAtomFeed({
  title,
  id,
  updated,
  entries = [],
  links = [],
  facets = [],
  subtitle = '',
}) {
  const ns = 'http://www.w3.org/2005/Atom';
  const opdsNs = 'http://opds-spec.org/2010/catalog';
  const pseNs = 'http://vaemendis.net/opds-pse/ns';
  const linkXml = links.map((item) => indent([atomLink(item)], 2)).join('\n');
  const entryXml = entries
    .map((entry) => {
      const entryLinks = (entry.links || []).map((item) => indent([atomLink(item)], 6)).join('\n');
      const entryCategories = entry.category
        ? indent([
            `<category term="${escapeXml(entry.category)}" label="${escapeXml(entry.category)}" scheme="http://e-hentai.org" />`,
          ], 6)
        : '';
      const authorXml = entry.author
        ? indent([`<author><name>${escapeXml(entry.author)}</name></author>`], 6)
        : '';
      return [
        '  <entry>',
        `    <id>${escapeXml(entry.id)}</id>`,
        `    <title>${escapeXml(entry.title)}</title>`,
        `    <updated>${escapeXml(entry.updated || updated)}</updated>`,
        authorXml,
        entryCategories,
        entry.summary ? `    <summary>${escapeXml(entry.summary)}</summary>` : '',
        entryLinks,
        '  </entry>',
      ]
        .filter(Boolean)
        .join('\n');
    })
    .join('\n');

  const facetXml = facets.length
    ? indent(
        facets.map((facet) => {
          const [titleText, href, active] = facet;
          const attrs = [
            `rel="http://opds-spec.org/facet"`,
            `href="${escapeXml(href)}"`,
            `title="${escapeXml(titleText)}"`,
            `opds:facetGroup="period"`,
          ];
          if (active) attrs.push(`opds:activeFacet="true"`);
          return `<link ${attrs.join(' ')} />`;
        }),
        2,
      )
    : '';

  return [
    `<?xml version="1.0" encoding="utf-8"?>`,
    `<feed xmlns="${ns}" xmlns:opds="${opdsNs}" xmlns:pse="${pseNs}">`,
    `  <id>${escapeXml(id)}</id>`,
    `  <title>${escapeXml(title)}</title>`,
    `  <updated>${escapeXml(updated)}</updated>`,
    subtitle ? `  <subtitle>${escapeXml(subtitle)}</subtitle>` : '',
    linkXml,
    facetXml,
    entryXml,
    `</feed>`,
  ]
    .filter(Boolean)
    .join('\n');
}

export function buildOpds2Navigation({ title, updated, links = [], navigation = [], publications = [], groups = [] }) {
  return {
    metadata: {
      title,
      modified: updated,
    },
    links,
    navigation,
    publications,
    ...(groups.length ? { groups } : {}),
  };
}

export function buildOpds2Publication({
  title,
  identifier,
  updated,
  published,
  authors = [],
  language,
  subjects = [],
  pageCount,
  x = {},
  images = [],
  links = [],
  readingOrder = [],
}) {
  const metadata = {
    title,
    identifier,
    modified: updated,
    published: published || updated,
    authors,
    subject: subjects,
    ...x,
  };
  if (language) metadata.language = [language];
  if (pageCount) metadata.numberOfPages = pageCount;
  const publication = {
    context: [
      'https://readium.org/webpub-manifest/context.jsonld',
      { x: 'https://github.com/niatsysor/PandaOPDS/vocab#' },
    ],
    metadata,
    images,
    links,
  };
  if (readingOrder.length) {
    publication.readingOrder = readingOrder;
  }
  return publication;
}

export function opds2Link({ rel, href, type, title, templated = false, properties }) {
  const link = { rel, href, type };
  if (title) link.title = title;
  if (templated) link.templated = true;
  if (properties) link.properties = properties;
  return link;
}

export { escapeXml };
