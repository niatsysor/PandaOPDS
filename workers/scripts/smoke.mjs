import assert from 'node:assert/strict';
import { buildOpenSearchDescription, buildOpds2Navigation, buildOpds2Publication } from '../src/feed.js';
import { parseDetailPage, parseImagePage, parseListPage, parseMyTags } from '../src/parsers.js';
import { TagTranslator, parseTagTranslationDb } from '../src/tag_features.js';

const listHtml = `
<html>
  <head><title>Test</title></head>
  <body>
    <div class="itg glte">
      <tr>
        <td><a href="/g/123/abc123/">Sample Gallery</a></td>
        <td><div class="ir" style="background-position:-32px -21px"></div></td>
        <td><div title="female:big breasts" class="gt">big breasts</div></td>
        <td>2026-08-12 13:11</td>
        <td>20 pages</td>
      </tr>
    </div>
    <a id="unext" href="/\?next=999"></a>
  </body>
</html>`;

const list = parseListPage(listHtml, 'https://e-hentai.org/', 'search');
assert.equal(list.items.length, 1);
assert.equal(list.items[0].gid, 123);
assert.equal(list.items[0].token, 'abc123');
assert.equal(list.items[0].pageCount, 20);

const detailHtml = `
<html>
  <head><title>Detail</title></head>
  <body>
    <div id="gn">Sample Gallery</div>
    <div id="gj">サンプル</div>
    <div id="gdd"><table>
      <tr><td class="gdt1">Category:</td><td class="gdt2">Doujinshi</td></tr>
      <tr><td class="gdt1">Posted:</td><td class="gdt2">2026-08-12 13:11</td></tr>
      <tr><td class="gdt1">File Size:</td><td class="gdt2">12 MB</td></tr>
      <tr><td class="gdt1">Length:</td><td class="gdt2">20 pages</td></tr>
    </table></div>
    <div id="gdt" class="gdt">
      <a href="/mpv/123/abc123/"><div style="background:url(https://ehgt.org/t/aa/bb/test.jpg) 0 0 no-repeat" data-orghash="0123456789abcdef0123456789abcdef01234567"></div></a>
    </div>
    <div id="taglist"><div title="female:big breasts" class="gt">big breasts</div></div>
    <div id="cdiv"><div class="c1" id="c1"><div class="c6"><a href="/showuser.php?showuser=7">Alice</a> Posted on 12 August 2026, 13:11 by: Alice</div></div></div>
  </body>
</html>`;

const detail = parseDetailPage(detailHtml, 'https://e-hentai.org/g/123/abc123/?p=0', 123, 'abc123', 0);
assert.equal(detail.title, 'Sample Gallery');
assert.equal(detail.titleJpn, 'サンプル');
assert.equal(detail.category, 'Doujinshi');
assert.equal(detail.filesizeBytes, 12 * 1024 * 1024);
assert.equal(detail.pageUrls.length, 1);
assert.equal(detail.pageUrls[0], '/s/0123456789/123-1');
assert.equal(detail.thumbnails[0].thumbUrl, 'https://ehgt.org/t/aa/bb/test.jpg');

const imagePageHtml = `<html><body><img id="img" src="https://example.com/image.jpg"><div id="loadfail" onclick="return nl('abc')"></div></body></html>`;
const imagePage = parseImagePage(imagePageHtml);
assert.equal(imagePage.src, 'https://example.com/image.jpg');
assert.equal(imagePage.reloadKey, 'abc');

const myTagsHtml = `
<html><body>
  <div id="tagpreview_1" title="f:big breasts" style="color:#fff;border-color:#000;background:red !important">big breasts</div>
  <div id="tagpreview_2" title="hidden gem" style="background:blue">hidden gem</div>
</body></html>`;
const myTags = parseMyTags(myTagsHtml);
assert.equal(myTags['female:big breasts'].color, '#fff');
assert.equal(myTags['*:hidden gem'].background, 'blue');

const translatedDb = parseTagTranslationDb({
  data: [
    {
      namespace: 'female',
      frontMatters: { name: '女性', abbr: 'f' },
      data: { 'big breasts': { name: '巨乳' } },
    },
  ],
});
const translator = new TagTranslator({ intervalSeconds: 0 });
translator.install(translatedDb.namespaces, translatedDb.tags, translatedDb.abbrs);
assert.equal(translator.translateTag('female', 'big breasts'), '女性:巨乳');
assert.equal(translator.translateQuery('女性:巨乳'), 'female:"big breasts"');
assert.equal(translator.translateQuery('巨乳'), 'female:"big breasts"');

const openSearch = buildOpenSearchDescription({ template: '/opds/v1.2/gallery?query={searchTerms}' });
assert.match(openSearch, /OpenSearchDescription/);

const publication = buildOpds2Publication({
  title: 'Sample',
  identifier: 'urn:test:1',
  updated: '2026-01-01T00:00:00Z',
  published: '2026-01-01T00:00:00Z',
  subjects: [{ name: 'female:big breasts' }],
  images: [],
  links: [],
});
assert.equal(publication.metadata.title, 'Sample');
assert.equal(publication.metadata.identifier, 'urn:test:1');

const navigation = buildOpds2Navigation({
  title: 'PandaOPDS',
  updated: '2026-01-01T00:00:00Z',
  navigation: [{ title: 'Latest', href: '/opds/v2.0/gallery' }],
  groups: [{ metadata: { title: 'Browse' }, links: [] }],
});
assert.equal(navigation.groups.length, 1);
assert.equal(navigation.navigation.length, 1);

console.log('smoke ok');
