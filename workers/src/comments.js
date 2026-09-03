import { toHref } from './feed.js';
import { parseEnvList } from './config.js';

export function rewriteCommentGalleryLinks(content, env) {
  return String(content || '').replace(
    /(href=["'])(https?:\/\/(?:e-hentai|exhentai)\.org\/(?:g|mpv)\/(\d+)\/([0-9a-fA-F]+)\/[^"']*)(["'])/gi,
    (_match, open, _url, gid, token, close) => `${open}${toHref(`/opds/v2.0/gallery/${gid}/${token}`, env)}${close}`,
  );
}

export function commentImageProxyHosts(env) {
  return parseEnvList(env.IMAGE_PROXY_HOSTS || 'ehgt.org,s.exhentai.org');
}

export function rewriteCommentCovers(content, env) {
  const hosts = commentImageProxyHosts(env);
  if (!hosts.length) return String(content || '');
  const hostPattern = hosts.map((host) => host.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  let out = String(content || '');
  out = out.replace(
    new RegExp(`(src=["'])(https:\/\/(?:${hostPattern})\/[^"']+)(["'])`, 'gi'),
    (_match, open, url, close) => `${open}${toHref(`/image/fetch?url=${encodeURIComponent(url)}`, env)}${close}`,
  );
  out = out.replace(
    new RegExp(`(url\(["']?)(https:\/\/(?:${hostPattern})\/[^"')]+)(["']?\))`, 'gi'),
    (_match, open, url, close) => `${open}${toHref(`/image/fetch?url=${encodeURIComponent(url)}`, env)}${close}`,
  );
  return out;
}

export function rewriteCommentContent(content, env) {
  let out = String(content || '');
  out = rewriteCommentGalleryLinks(out, env);
  out = rewriteCommentCovers(out, env);
  return out;
}