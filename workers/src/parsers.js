import { parseHTML } from 'linkedom';

const EH_LANGUAGE_MAP = {
  "chinese": "zh",
  "chinese (simplified)": "zh-Hans",
  "chinese (traditional)": "zh-Hant",
  "english": "en",
  "japanese": "ja",
  "korean": "ko",
  "french": "fr",
  "german": "de",
  "spanish": "es",
  "italian": "it",
  "dutch": "nl",
  "portuguese": "pt",
  "portuguese (brazil)": "pt-BR",
  "russian": "ru",
  "vietnamese": "vi",
  "indonesian": "id",
  "thai": "th",
  "arabic": "ar",
  "polish": "pl",
  "turkish": "tr",
  "hungarian": "hu",
  "swedish": "sv",
  "czech": "cs",
  "greek": "el",
  "danish": "da",
  "finnish": "fi",
  "norwegian": "no",
  "ukrainian": "uk",
  "hebrew": "he",
  "hindi": "hi",
  "malay": "ms",
  "bengali": "bn",
  "filipino": "fil",
  "romanian": "ro",
  "catalan": "ca",
  "slovak": "sk",
  "bulgarian": "bg",
  "croatian": "hr",
  "serbian": "sr",
  "slovenian": "sl",
  "latin": "la",
  "persian": "fa",
  "mongolian": "mn",
  "nepali": "ne",
  "burmese": "my",
  "khmer": "km",
  "lao": "lo",
  "tamil": "ta",
  "telugu": "te",
  "punjabi": "pa",
  "gujarati": "gu",
  "urdu": "ur",
  "kazakh": "kk",
  "uzbek": "uz",
  "amharic": "am",
  "swahili": "sw",
  "icelandic": "is",
  "latvian": "lv",
  "lithuanian": "lt",
  "estonian": "et",
  "albanian": "sq",
  "macedonian": "mk",
  "georgian": "ka",
  "armenian": "hy",
  "azerbaijani": "az",
  "welsh": "cy",
  "irish": "ga",
  "esperanto": "eo",
  "afrikaans": "af",
};
const LANGUAGE_MARKERS = new Set(["translated", "rewrite", "raw"]);

export function extractListLanguage(tags) {
  for (const tag of tags || []) {
    if (tag.namespace === "language") {
      const key = String(tag.key || "").trim().toLowerCase();
      if (LANGUAGE_MARKERS.has(key)) continue;
      const mapped = EH_LANGUAGE_MAP[key];
      if (mapped) return mapped;
    }
  }
  return "";
}

function mapLanguageKey(key) {
  const k = String(key || "").trim().toLowerCase();
  if (LANGUAGE_MARKERS.has(k)) return "";
  return EH_LANGUAGE_MAP[k] || "";
}

export function extractDetailLanguage(labels) {
  const raw = labels.get("Language") || "";
  const normalized = String(raw || "").replace(/\s+TR\s*$/i, "").replace(/\s+/g, " ").trim().toLowerCase();
  return mapLanguageKey(normalized);
}

const LIST_ROW_SELECTORS = [
  '.itg.gld > div',
  '.itg.gltc tr',
  '.itg.glte tr',
  '.itg.gltm tr',
];
const COVER_SELECTORS = [
  '.gl3t > a > img',
  '.gl2c > .glthumb > div > img',
  '.gl1e > div > a > img',
  '.gl2m > .glthumb > div > img',
];
const LIST_VIEWS = ['thumbnail', 'compact', 'extended', 'minimal'];

const GALLERY_LINK_RE = /\/(?:g|mpv)\/(\d+)\/([0-9a-fA-F]+)\//;
const SHOWING_RE = /Showing\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+images/i;
const TAG_TITLE_RE = /^([^:]+):\s*(.+)$/;
const TAG_STATUS_CLASS_RE = /\b(gt|gtl|gtw)\b/i;
const BRACKET_RE = /\[.*?\]|\(.*?\)/g;
const IMAGE_509_URLS = new Set([
  'https://ehgt.org/g/509.gif',
  'https://exhentai.org/img/509.gif',
]);
const SIZE_UNITS = {
  K: 1024,
  M: 1024 ** 2,
  G: 1024 ** 3,
  T: 1024 ** 4,
};

export function cleanText(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function parseTagStyle(styleText = '') {
  const style = String(styleText || '');
  const out = {};
  const color = style.match(/color:\s*([^;]+)/i);
  if (color) out.color = cleanText(color[1]);
  const border = style.match(/border-color:\s*([^;]+)/i);
  if (border) out.borderColor = cleanText(border[1]);
  const background = style.match(/background:\s*([^;]+)/i);
  if (background) {
    out.background = cleanText(background[1]).replace(/\s*!important$/i, '');
  }
  return Object.keys(out).length ? out : null;
}

function parseTag(el) {
  const title = cleanText(el.getAttribute('title'));
  if (!title) return null;
  const match = title.match(TAG_TITLE_RE);
  const namespace = match ? match[1].trim().toLowerCase() : 'temp';
  const key = match ? match[2].trim() : title;
  const style = parseTagStyle(el.getAttribute('style'));
  const tag = {
    namespace,
    key,
    title: title,
  };
  if (style) tag.style = style;
  const className = String(el.getAttribute('class') || '');
  if (TAG_STATUS_CLASS_RE.test(className)) {
    tag.status = className.match(TAG_STATUS_CLASS_RE)[1].toLowerCase();
  } else {
    tag.status = 'gt';
  }
  return tag;
}

function parsePageCount(row) {
  const extDivs = row.querySelectorAll('.gl3e > div');
  if (extDivs.length > 4) {
    const m = String(extDivs[4].textContent || '').match(/(\d+)/);
    if (m) return Number(m[1]);
  }
  const cmpDivs = row.querySelectorAll('.gl4c.glhide > div');
  if (cmpDivs.length > 1) {
    const m = String(cmpDivs[1].textContent || '').match(/(\d+)/);
    if (m) return Number(m[1]);
  }
  const thumbDivs = row.querySelectorAll('.gl5t > div:nth-child(1) > div');
  if (thumbDivs.length > 1) {
    const m = String(thumbDivs[1].textContent || '').match(/(\d+)/);
    if (m) return Number(m[1]);
  }
  const match = String(row.textContent || '').match(/(\d+)\s+pages?/i);
  return match ? Number(match[1]) : undefined;
}

function parseSizeBytes(text) {
  const match = String(text || '').match(/([\d.]+)\s*([KMGTP]?)[iI]?B/i);
  if (!match) return 0;
  const value = Number.parseFloat(match[1]);
  if (!Number.isFinite(value)) return 0;
  const unit = match[2].toUpperCase();
  return Math.trunc(value * (SIZE_UNITS[unit] || 1));
}

function parseRating(row) {
  const ratingEl = row.querySelector('.ir[style]');
  if (!ratingEl) return 0;
  const style = ratingEl.getAttribute('style') || '';
  const offsets = [...style.matchAll(/-?\d+px/g)].map((m) => Number.parseInt(m[0], 10));
  if (offsets.length < 2) return 0;
  const x = offsets[0];
  const y = offsets[1];
  const rating = 5 - (-x) / 16 - (y === -21 ? 0.5 : 0);
  return Math.max(0, Math.round(rating * 100) / 100);
}

function parsePublishTime(text) {
  const match = String(text || '').match(/\b\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\b/);
  return match ? match[0] : '';
}

function parseFavcat(row, view) {
  const selectors = {
    thumbnail: '.gl5t > div > div[id]',
    compact: '.gl2c > div:nth-child(2) > [id]',
    extended: '.gl3e > div[id]',
    minimal: '.gl2m > div:nth-child(2)',
  };
  const sel = selectors[view];
  if (!sel) return null;
  const el = row.querySelector(sel);
  if (!el) return null;
  const title = String(el.getAttribute('title') || '').trim();
  if (!title) return null;
  return title;
}

function parseFavcatMap(doc) {
  const out = new Map();
  for (const el of doc.querySelectorAll('div.nosel div.fp[onclick]')) {
    const onclick = String(el.getAttribute('onclick') || '');
    const match = onclick.match(/favcat=(\d+)/i);
    if (!match) continue;
    const id = Number.parseInt(match[1], 10);
    const name = cleanText(el.querySelector('div:nth-child(3)')?.textContent || '').trim();
    if (name) out.set(id, name);
  }
  return out;
}

function parseCoverUrl(row, baseUrl, view = 'compact') {
  const selectors = COVER_SELECTORS;
  for (const selector of selectors) {
    const img = row.querySelector(selector);
    if (!img) continue;
    const raw = img.getAttribute('data-src') || img.getAttribute('src') || '';
    if (!raw) continue;
    try {
      return new URL(raw, baseUrl).toString();
    } catch {
      return raw;
    }
  }
  return '';
}

function parseCoverUrlFromDetail(doc, baseUrl) {
  const div = doc.querySelector('#gd1 > div');
  if (!div) return '';
  const style = String(div.getAttribute('style') || '');
  const match = style.match(/url\(["']?(.+?)["']?\)/i);
  if (!match) return '';
  let url = match[1];
  if (url.startsWith('/')) {
    try {
      url = new URL(url, baseUrl).toString();
    } catch {
      const host = baseUrl ? new URL(baseUrl).host : '';
      url = `https://${host}${url}`;
    }
  }
  return url;
}

function parseNextCursor(doc, baseUrl, kind) {
  if (kind === 'toplist') {
    const tr = doc.querySelector('.ptt tr');
    if (!tr) return null;
    const tds = Array.from(tr.children);
    if (!tds.length) return null;
    const lastTd = tds[tds.length - 1];
    const a = lastTd.querySelector('a[href]');
    if (!a) return null;
    const href = a.getAttribute('href') || '';
    const pMatch = href.match(/[?&](?:p|page)=(\d+)/);
    return pMatch ? String(Number.parseInt(pMatch[1], 10) + 1) : null;
  }

  const nextLink = doc.querySelector('a#unext, a[href*="next="]');
  if (!nextLink) return null;
  const href = nextLink.getAttribute('href') || '';
  if (!href) return null;
  try {
    const url = new URL(href, baseUrl);
    return url.searchParams.get('next') || url.searchParams.get('prev');
  } catch {
    const match = href.match(/[?&](?:next|prev)=([^&]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }
}

function parseListTags(row, view = 'compact') {
  const out = [];
  let divs = [];
  if (view === 'extended') {
    divs = row.querySelectorAll('.gl2e > div > a > div > div:nth-child(2) > table > tr > td > div[title]');
  } else if (view === 'compact') {
    divs = row.querySelectorAll('div.gt[title], div.gtl[title], div.gtw[title]');
  }
  for (const el of divs) {
    const className = String(el.getAttribute('class') || '');
    if (/\bst\b/.test(className)) continue;
    const tag = parseTag(el);
    if (tag) out.push(tag);
  }
  return out;
}

export function parseListPage(html, baseUrl, kind = 'search') {
  const { document } = parseHTML(html);
  const items = [];
  const seen = new Set();
  
  const favcatMap = parseFavcatMap(document);
  const favcatNameToId = new Map();
  for (const [id, name] of favcatMap) {
    favcatNameToId.set(name, id);
  }
  
  for (let vi = 0; vi < LIST_ROW_SELECTORS.length; vi++) {
    const selector = LIST_ROW_SELECTORS[vi];
    const view = LIST_VIEWS[vi];
    const rows = document.querySelectorAll(selector);
    if (!rows.length) continue;
    
    for (const row of rows) {
      if (row.children.length === 1 || row.querySelector('th')) continue;
      
      const link = row.querySelector('a[href*="/g/"], a[href*="/mpv/"]');
      if (!link) continue;
      const href = link.getAttribute('href') || '';
      const match = href.match(GALLERY_LINK_RE);
      if (!match) continue;
      const gid = Number.parseInt(match[1], 10);
      const token = match[2];
      const key = `${gid}:${token}`;
      if (seen.has(key)) continue;
      seen.add(key);
      
      const title = cleanText(row.querySelector('.glink')?.textContent || link.textContent || row.textContent || '');
      const category = cleanText(
        row.querySelector('.cs')?.textContent ||
        row.querySelector('.cn')?.textContent ||
        row.querySelector('.gl1m.glcat > div')?.textContent || ''
      );
      const pageCount = parsePageCount(row);
      const rating = parseRating(row);
      const published = parsePublishTime(row.textContent);
      const coverUrl = parseCoverUrl(row, baseUrl, view);
      const tags = parseListTags(row, view);
      const isExpunged = Boolean(row.querySelector('.glink s'));
      const favcatTitle = parseFavcat(row, view);
      const favcat = favcatTitle ? (favcatNameToId.get(favcatTitle.trim()) ?? null) : null;
      items.push({
        gid,
        token,
        title,
        category,
        pageCount: pageCount || undefined,
        rating,
        published,
        coverUrl,
        tags,
        isExpunged,
        favcat,
      });
    }
    if (items.length) break;
  }

  return {
    items,
    nextCursor: parseNextCursor(document, baseUrl, kind),
    rawTitle: cleanText(document.querySelector('title')?.textContent || ''),
  };
}

function parseLabelValueMap(doc) {
  const out = new Map();
  for (const tr of doc.querySelectorAll('#gdd table tr')) {
    const label = cleanText(tr.querySelector('.gdt1')?.textContent || '').replace(/:$/, '');
    const value = cleanText(tr.querySelector('.gdt2')?.textContent || '');
    if (label) out.set(label, value);
  }
  return out;
}

function parseDetailThumbnails(doc, pageBaseUrl, gid, token, pageIndex = 0) {
  const gdt = doc.querySelector('#gdt');
  if (!gdt) return [];
  const out = [];
  const isNewStructure = Boolean(String(gdt.getAttribute('class') || '').trim());

  if (isNewStructure) {
    let pageNo = pageIndex * 20 + 1;
    for (const anchor of gdt.querySelectorAll('a[href]')) {
      const href = anchor.getAttribute('href') || '';
      const div = anchor.querySelector('div[style]');
      const style = div?.getAttribute('style') || '';
      const urlMatch = style.match(/url\(["']?(.+?)["']?\)/i);
      if (!urlMatch) {
        pageNo += 1;
        continue;
      }
      const thumbUrl = urlMatch[1].startsWith('/') ? new URL(urlMatch[1], pageBaseUrl).toString() : urlMatch[1];
      const originImageHash = cleanText(div?.getAttribute('data-orghash') || '') || null;
      const mpv = href.match(/\/mpv\/(\d+)\/([0-9a-fA-F]+)\//);
      let pageHref = href;
      if (mpv && originImageHash) {
        pageHref = `/s/${originImageHash.slice(0, 10)}/${mpv[1]}-${pageNo}`;
      }
      out.push({
        href: pageHref,
        thumbUrl,
        pageNo,
        isLarge: !/\)\s*-?\d+px/i.test(style),
        originImageHash,
      });
      pageNo += 1;
    }
    return out;
  }

  const smallThumbs = gdt.querySelectorAll('.gdtm');
  if (smallThumbs.length) {
    let pageNo = pageIndex * 20 + 1;
    for (const el of smallThumbs) {
      const div = [...el.children].find((child) => child.tagName === 'DIV');
      const anchor = el.querySelector('div > a');
      const href = anchor?.getAttribute('href') || '';
      const style = div?.getAttribute('style') || '';
      const urlMatch = style.match(/url\(["']?(.+?)["']?\)/i);
      if (!urlMatch) {
        pageNo += 1;
        continue;
      }
      const thumbUrl = urlMatch[1].startsWith('/') ? new URL(urlMatch[1], pageBaseUrl).toString() : urlMatch[1];
      out.push({ href, thumbUrl, pageNo, isLarge: false, originImageHash: null });
      pageNo += 1;
    }
    return out;
  }

  const largeThumbs = gdt.querySelectorAll('.gdtl');
  if (largeThumbs.length) {
    let pageNo = pageIndex * 20 + 1;
    for (const el of largeThumbs) {
      const anchor = el.querySelector('a[href]');
      const img = el.querySelector('a > img[src]');
      const href = anchor?.getAttribute('href') || '';
      const thumbUrl = img?.getAttribute('src') || '';
      out.push({
        href,
        thumbUrl: thumbUrl.startsWith('/') ? new URL(thumbUrl, pageBaseUrl).toString() : thumbUrl,
        pageNo,
        isLarge: true,
        originImageHash: null,
      });
      pageNo += 1;
    }
  }

  return out;
}

function parseComments(doc) {
  const out = [];
  const blocks = doc.querySelectorAll('#cdiv .c1');
  let index = 1;
  for (const block of blocks) {
    const c3 = block.querySelector('.c2 > .c3') || block.querySelector('.c3');
    const body = block.querySelector('.c6') || block;
    const idAttr = cleanText(body.getAttribute?.('id') || '');
    const idMatch = idAttr.match(/comment_(\d+)/i);
    const username = cleanText(c3?.querySelector('a')?.textContent || '');
    let userId = null;
    for (const anchor of c3 ? c3.querySelectorAll('a') : []) {
      const showUserHref = anchor.getAttribute('href') || '';
      const userIdMatch = showUserHref.match(/showuser=(\d+)/i);
      if (userIdMatch) {
        userId = Number.parseInt(userIdMatch[1], 10);
        break;
      }
    }
    const c3Text = cleanText(c3?.textContent || '');
    const timeMatch = c3Text.match(/Posted\s+on\s+(.+?)\s+by:/i);
    const lastEditTime = cleanText(block.querySelector('.c8 > strong')?.textContent || '');
    const item = {
      id: idMatch ? Number.parseInt(idMatch[1], 10) : 0,
      username,
      time: timeMatch ? timeMatch[1] : '',
      content: body.outerHTML || body.innerHTML || '',
    };
    if (userId !== null) item.userId = userId;
    if (lastEditTime) item.lastEditTime = lastEditTime;
    out.push(item);
    index += 1;
  }
  return out;
}

function parseDetailTags(doc) {
  const out = [];
  const tagDivs = doc.querySelectorAll('#taglist table tr > td:nth-child(2) > div[id]');
  if (tagDivs.length) {
    for (const el of tagDivs) {
      const tagId = cleanText(el.getAttribute('id'));
      if (!tagId) continue;
      const parts = tagId.split(':', 1);
      let namespace = 'temp';
      let key = tagId;
      if (tagId.startsWith('td_') && tagId.includes(':')) {
        namespace = tagId.slice(3, tagId.indexOf(':')).trim().toLowerCase() || 'temp';
        key = tagId.slice(tagId.indexOf(':') + 1).replace(/_/g, ' ').trim();
      } else if (parts.length === 2) {
        namespace = parts[0].replace(/^td_/, '').trim().toLowerCase() || 'temp';
        key = parts[1].replace(/_/g, ' ').trim();
      } else {
        key = tagId.replace(/^td_/, '').replace(/_/g, ' ').trim();
      }
      if (!key) continue;
      const tag = {
        namespace,
        key,
        status: TAG_STATUS_CLASS_RE.test(el.getAttribute('class') || '')
          ? (el.getAttribute('class') || '').match(TAG_STATUS_CLASS_RE)[1].toLowerCase()
          : 'gt',
      };
      const style = parseTagStyle(el.getAttribute('style'));
      if (style) tag.style = style;
      out.push(tag);
    }
    return out;
  }

  for (const el of doc.querySelectorAll('#taglist div[title]')) {
    const tag = parseTag(el);
    if (tag) out.push(tag);
  }
  return out;
}

function parseAuthorNames(text) {
  return [text];
}

export function parseGalleryTitleAuthors(title, titleJpn = '', category = '') {
  const parseTitleAuthors = (rawTitle) => {
    const raw = String(rawTitle || '');
    const cleanRaw = cleanText(raw.replace(BRACKET_RE, ''));
    const pos = cleanRaw ? raw.indexOf(cleanRaw) : -1;
    if (pos <= 0 || !cleanRaw) {
      return { title: cleanRaw || cleanText(raw), authors: [] };
    }

    const before = raw.slice(0, pos);
    const match = before.match(/\[([^\]]+)\]\s*$/);
    if (!match) {
      return { title: cleanRaw, authors: [] };
    }

    const authorText = cleanText(match[1]);
    if (!authorText) {
      return { title: cleanRaw, authors: [] };
    }

    return { title: cleanRaw, authors: parseAuthorNames(authorText) };
  };

  if (titleJpn && cleanText(String(titleJpn).replace(BRACKET_RE, ''))) {
    const parsedJpn = parseTitleAuthors(titleJpn);
    if (parsedJpn.title) {
      return parsedJpn;
    }
  }

  return parseTitleAuthors(title);
}

export function parseDetailPage(html, baseUrl, gid, token, pageIndex = 0) {
  const { document } = parseHTML(html);
  const labels = parseLabelValueMap(document);
  const title = cleanText(document.querySelector('#gn')?.textContent || labels.get('Title') || '');
  const titleJpn = cleanText(document.querySelector('#gj')?.textContent || labels.get('Japanese Title') || '');
  const category = cleanText(labels.get('Category') || labels.get('Category:') || '');
  const uploader = cleanText(
    labels.get('Uploader') || labels.get('Uploaded by') || labels.get('Posted by') || '',
  );
  const ratingText = cleanText(
    document.querySelector('#rating_count, #rating_label, .ir')?.textContent || labels.get('Average') || '',
  );
  const rating = Number.parseFloat((ratingText.match(/\d+(?:\.\d+)?/) || [''])[0] || '0') || 0;
  const fileCount = Number.parseInt((labels.get('Length') || labels.get('Pages') || '').match(/\d+/)?.[0] || '0', 10) || 0;
  const filesizeText = cleanText(labels.get('File Size') || '');
  const publishTime = cleanText(labels.get('Posted') || labels.get('Posted on') || '');
  const filesizeBytes = parseSizeBytes(labels.get('File Size') || '');
  const tags = parseDetailTags(document);
  const comments = parseComments(document);
  const expunged = [...labels.values()].some((value) => /\bexpunged\b/i.test(value)) || /\bexpunged\b/i.test(cleanText(document.textContent || ''));
  const coverUrl = parseCoverUrlFromDetail(document, baseUrl);
  const thumbnails = parseDetailThumbnails(document, baseUrl, gid, token, pageIndex);
  const pageUrls = thumbnails.map((thumb) => thumb.href);

  return {
    title,
    titleJpn,
    category,
    uploader,
    rating,
    fileCount,
    filesizeText,
    publishTime,
    language: extractDetailLanguage(labels),
    filesizeBytes,
    expunged,
    coverUrl,
    tags,
    comments,
    thumbnails,
    pageUrls,
    rawTitle: cleanText(document.querySelector('title')?.textContent || ''),
  };
}

export function parseImagePage(html) {
  const { document } = parseHTML(html);
  const src = cleanText(document.querySelector('#img')?.getAttribute('src') || '');
  const onclick = cleanText(document.querySelector('#loadfail')?.getAttribute('onclick') || '');
  const reloadKeyMatch = onclick.match(/nl\('([^']+)'\)/i);
  return {
    src,
    reloadKey: reloadKeyMatch ? reloadKeyMatch[1] : '',
    isQuotaGif: IMAGE_509_URLS.has(src),
  };
}

const MYTAGS_TITLE_RE = /^([^:]+):\s*(.+)$/;

function parseMyTagStyle(styleText = '') {
  const style = String(styleText || '');
  const out = {};
  const color = style.match(/color:\s*([^;]+)/i);
  if (color) out.color = cleanText(color[1]);
  const border = style.match(/border-color:\s*([^;]+)/i);
  if (border) out.borderColor = cleanText(border[1]);
  const background = style.match(/background:\s*([^;]+)/i);
  if (background) {
    out.background = cleanText(background[1]).replace(/\s*!important$/i, '');
  }
  return Object.keys(out).length ? out : null;
}

export function parseMyTags(html) {
  const { document } = parseHTML(html || '');
  const out = {};
  for (const div of document.querySelectorAll('div[id^=tagpreview_][title]')) {
    const key = cleanText(div.getAttribute('title') || '').toLowerCase().replace(/\s+/g, ' ');
    if (!key) continue;
    let normalized = key;
    if (normalized.includes(':')) {
      const match = normalized.match(MYTAGS_TITLE_RE);
      if (match) {
        const ns = match[1].trim().toLowerCase();
        const rest = match[2].trim();
        const abbr = ns === 'f' ? 'female' : ns === 'm' ? 'male' : ns === 'x' ? 'mixed' : ns;
        normalized = `${abbr}:${rest}`;
      }
    } else {
      normalized = `*:${normalized}`;
    }
    const style = parseMyTagStyle(div.getAttribute('style'));
    if (style) out[normalized] = style;
  }
  return out;
}

const FAVCAT_ID_RE = /favcat=(\d+)/i;

export function parseFavoriteCategories(html) {
  const { document } = parseHTML(html || '');
  const out = {};
  for (const fp of document.querySelectorAll('div.nosel div.fp[onclick]')) {
    const onclick = fp.getAttribute('onclick') || '';
    const match = onclick.match(FAVCAT_ID_RE);
    if (!match) continue;
    const children = [...fp.children].filter((child) => child.tagName === 'DIV');
    let name = '';
    if (children.length >= 3) {
      name = cleanText(children[2].textContent || '');
    } else if (children.length >= 2) {
      name = cleanText(children[1].textContent || '');
    }
    if (!name) name = cleanText(fp.textContent || '');
    const id = Number.parseInt(match[1], 10);
    if (Number.isFinite(id) && name) out[id] = name;
  }
  return out;
}
