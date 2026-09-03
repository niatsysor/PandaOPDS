import { parseImagePage } from './parsers.js';
import {
  fetchBinary,
  fetchText,
  inspectImageResponse,
} from './upstream.js';
import {
  HttpError,
  cacheTtls,
  imageSourceRetriesEnv,
  isFatalImageSourceError,
  parseEnvList,
} from './config.js';
import {
  getCachedCoverUrl,
  memCacheGetOrSet,
  imgPageCache,
} from './persistent.js';
import { getDetailParsed } from './opds.js';
import { origin } from './config.js';

async function streamImage(env, ctx, gid, token, pageNo) {
  const ttls = cacheTtls(env);
  const base = Number.parseInt(String(env.PSE_PAGE_BASE || '1'), 10) || 1;
  if (pageNo < base) {
    throw new HttpError(400, `page must be >= ${base}`);
  }
  const groupIndex = Math.floor((pageNo - base) / 20);
  const pageIndex = (pageNo - base) % 20;
  const detailUrl = `${origin(env)}/g/${gid}/${token}/?p=${groupIndex}`;
  const sourceRetries = imageSourceRetriesEnv(env);
  let lastError = null;

  for (let sourceAttempt = 0; sourceAttempt <= sourceRetries; sourceAttempt += 1) {
    let imagePageUrl = '';
    let imagePage = null;
    try {
      const parsed = await getDetailParsed(env, ctx, gid, token, groupIndex);
      const pageUrl = parsed.pageUrls[pageIndex];
      if (!pageUrl) throw new HttpError(404, 'page not found');
      imagePageUrl = pageUrl.startsWith('http') ? pageUrl : new URL(pageUrl, detailUrl).toString();
      const pageCacheRequest = new Request(imagePageUrl, { method: 'GET' });
      const cached = await caches.default.match(pageCacheRequest);
      if (cached) {
        return cached;
      }

      const imgPageKey = `imgpage:${gid}:${token}:${pageNo}`;
      imagePage = await memCacheGetOrSet(imgPageCache, imgPageKey, async () => {
        const { text: imagePageText } = await fetchText(imagePageUrl, env);
        return parseImagePage(imagePageText);
      }, ttls.detail / 1000);
      if (imagePage.isQuotaGif) {
        throw new HttpError(429, 'upstream image quota exceeded');
      }
      if (!imagePage.src) {
        throw new HttpError(502, 'upstream image page missing img src');
      }

      try {
        const imageResponse = await fetchBinary(imagePage.src, env, {}, 'image');
        await inspectImageResponse(imageResponse);
        const response = new Response(imageResponse.body, imageResponse);
        response.headers.set('cache-control', `public, max-age=${ttls.image}`);
        ctx.waitUntil(caches.default.put(pageCacheRequest, response.clone()));
        return response;
      } catch (error) {
        lastError = error;
        if (isFatalImageSourceError(error)) throw error;

        if (imagePage.reloadKey && sourceAttempt === 0 && sourceRetries >= 1) {
          const retryUrl = `${imagePage.src}?nl=${imagePage.reloadKey}`;
          try {
            const retryResponse = await fetchBinary(retryUrl, env, {}, 'image');
            await inspectImageResponse(retryResponse);
            const response = new Response(retryResponse.body, retryResponse);
            response.headers.set('cache-control', `public, max-age=${ttls.image}`);
            ctx.waitUntil(caches.default.put(pageCacheRequest, response.clone()));
            return response;
          } catch (retryError) {
            lastError = retryError;
            if (isFatalImageSourceError(retryError)) throw retryError;
          }
        }
      }
    } catch (error) {
      lastError = error;
      if (isFatalImageSourceError(error)) throw error;
      if (sourceAttempt >= sourceRetries) break;
    }
  }

  if (lastError) throw lastError;
  throw new HttpError(502, 'image fetch failed');
}

async function proxyThumb(env, ctx, gid, token) {
  const ttls = cacheTtls(env);
  const cachedCover = await getCachedCoverUrl(gid, token);
  let thumbUrl = cachedCover || '';
  let parsed = null;
  if (!thumbUrl) {
    parsed = await getDetailParsed(env, ctx, gid, token, 0);
    thumbUrl = parsed.coverUrl || parsed.thumbnails?.[0]?.thumbUrl || parsed.pageUrls?.[0] || '';
  }
  if (!thumbUrl) {
    throw new HttpError(404, 'thumbnail unavailable');
  }
  const cacheRequest = new Request(thumbUrl.startsWith('http') ? thumbUrl : new URL(thumbUrl, `${origin(env)}/`).toString(), { method: 'GET' });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached;
  const response = await fetchBinary(cacheRequest.url, env, {}, 'thumb');
  const out = new Response(response.body, response);
  out.headers.set('cache-control', `public, max-age=${ttls.image}`);
  ctx.waitUntil(caches.default.put(cacheRequest, out.clone()));
  return out;
}

async function proxyCommentImage(env, ctx, url) {
  const ttls = cacheTtls(env);
  const parsed = new URL(url);
  const allowed = parseEnvList(env.IMAGE_PROXY_HOSTS || 'ehgt.org,s.exhentai.org');
  if (parsed.protocol !== 'https:' || !allowed.includes(parsed.hostname.toLowerCase())) {
    throw new HttpError(400, 'unsupported image url');
  }
  const cacheRequest = new Request(parsed.toString(), { method: 'GET' });
  const cached = await caches.default.match(cacheRequest);
  if (cached) return cached;
  const response = await fetchBinary(parsed.toString(), env, {}, 'thumb');
  const out = new Response(response.body, response);
  out.headers.set('cache-control', `public, max-age=${ttls.image}`);
  ctx.waitUntil(caches.default.put(cacheRequest, out.clone()));
  return out;
}

export async function handleStream(env, ctx, gid, token, pageNo) {
  return streamImage(env, ctx, gid, token, pageNo);
}

export async function handleThumb(env, ctx, gid, token) {
  return proxyThumb(env, ctx, gid, token);
}

export async function handleImageFetch(env, ctx, request) {
  const url = new URL(request.url).searchParams.get('url');
  if (!url) throw new HttpError(400, 'missing url');
  return proxyCommentImage(env, ctx, url);
}