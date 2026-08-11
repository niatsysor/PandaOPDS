"""Parser unit tests using mock HTML fixtures (no network, no cookies).

Fixture structures mirror the real E-Hentai markup documented in
example/JHenTai/lib/src/utils/eh_spider_parser.dart.
"""

import pytest

from app.eh.exceptions import ParseError
from app.eh.models import ImagePageInfo
from app.eh.parser import (
    parse_detail_page,
    parse_gdata_response,
    parse_image_page,
    parse_list_page,
)

# --------------------------------------------------------------------------
# list page (thumbnail view)
# --------------------------------------------------------------------------

THUMB_LIST_HTML = """
<html><body>
<div class="searchtext">Found 1,234,567 results.</div>
<table class="itg gld">
  <div>
    <div class="gl1t"><div class="gl3t"><a href="https://e-hentai.org/g/2165080/725f6a7a58/"><img src="/x/1.jpg" style="height:296px;width:250px"></a></div></div>
    <div class="gl5t">
      <div><div>1</div><div>66 pages</div></div>
      <div><div class="glink"><a href="https://e-hentai.org/g/2165080/725f6a7a58/">Test Gallery One</a></div></div>
      <div><span class="cs">Manga</span></div>
    </div>
  </div>
  <div>
    <div class="gl1t"><div class="gl3t"><a href="https://e-hentai.org/g/2165079/abc123/"><img src="/x/2.jpg" style="height:296px;width:250px"></a></div></div>
    <div class="gl5t">
      <div><div>1</div><div>10 pages</div></div>
      <div><div class="glink"><a href="https://e-hentai.org/g/2165079/abc123/">Second Gallery</a></div></div>
      <div><span class="cs">Doujinshi</span></div>
    </div>
  </div>
  <a href="https://e-hentai.org/?next=2165079" id="unext">Next</a>
  <a href="https://e-hentai.org/?prev=2165081" id="uprev">Prev</a>
</table>
</body></html>
"""


def test_parse_list_page_thumbnail_view():
    info = parse_list_page(THUMB_LIST_HTML)
    assert len(info.galleries) == 2
    g1, g2 = info.galleries
    assert g1.gid == 2165080
    assert g1.token == "725f6a7a58"
    assert g1.title == "Test Gallery One"
    assert g1.category == "Manga"
    assert g1.cover_url == "/x/1.jpg"
    assert g1.page_count == 66
    assert g2.title == "Second Gallery"
    assert g2.category == "Doujinshi"
    assert info.next_gid == 2165079
    assert info.prev_gid == 2165081
    assert info.total_count == 1234567


COMPACT_LIST_HTML = """
<html><body>
<table class="itg gltc"><tbody>
  <tr>
    <td class="gl2c"><div class="glthumb"><div><img src="/c/1.jpg" style="height:296px;width:250px"></div></div></td>
    <td class="gl3c glname"><a href="https://e-hentai.org/g/100/aaa/"><div class="glink">Compact Gallery</div></a></td>
    <td class="cn">Doujinshi</td>
    <td class="gl4c glhide"><div></div><div>5 pages</div></td>
  </tr>
  <tr><th>header row to skip</th></tr>
</tbody></table>
</body></html>
"""


def test_parse_list_page_compact_view():
    info = parse_list_page(COMPACT_LIST_HTML)
    assert len(info.galleries) == 1
    g = info.galleries[0]
    assert g.gid == 100
    assert g.token == "aaa"
    assert g.title == "Compact Gallery"
    assert g.category == "Doujinshi"
    assert g.cover_url == "/c/1.jpg"
    assert g.page_count == 5


# --------------------------------------------------------------------------
# detail page — new structure (datatags=1)
# --------------------------------------------------------------------------

DETAIL_NEW_HTML = """
<html><body>
<div class="gtb"><div class="gpc">Showing 1 - 20 of 40 images</div></div>
<div id="gdt" class="gdt">
  <a href="/mpv/2165080/725f6a7a58/">
    <div style="width: 100px; height: 150px; background: url(https://ehgt.org/t/aa/bb_1.jpg) 0px 0px no-repeat transparent;" data-orghash="0123456789abcdef0123456789abcdef01234567"></div>
  </a>
  <a href="/s/xyz123/2165080-2">
    <div style="width: 100px; height: 150px; background: url(https://ehgt.org/t/aa/bb_2.jpg) -50px 0px no-repeat transparent;" data-orghash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"></div>
  </a>
</div>
<table class="ptt"><tbody><tr><td><a href="?p=0">&#171;</a></td><td><a href="?p=0">1</a></td><td><a href="?p=1">2</a></td><td><a href="?p=1">&#187;</a></td></tr></tbody></table>
<div class="ptds"><a href="?p=0">1</a></div>
</body></html>
"""


def test_parse_detail_page_new_structure():
    info = parse_detail_page(DETAIL_NEW_HTML, "e-hentai.org", page_index=0)
    assert info.image_count == 40
    assert info.image_no_from == 0
    assert info.image_no_to == 19
    assert info.page_count == 2
    assert info.current_page_no == 1
    assert len(info.thumbnails) == 2

    t0, t1 = info.thumbnails
    # MPV href rewritten to /s/ using orghash[:10]
    assert t0.href == "/s/0123456789/2165080-1"
    assert t0.page_no == 1
    assert t0.thumb_url == "https://ehgt.org/t/aa/bb_1.jpg"
    assert t0.origin_image_hash == "0123456789abcdef0123456789abcdef01234567"
    # non-MPV href keeps its own page number
    assert t1.href == "/s/xyz123/2165080-2"
    assert t1.page_no == 2


def test_parse_detail_page_new_structure_page_2():
    # page_index=1 -> pages 21..40
    html = DETAIL_NEW_HTML.replace("Showing 1 - 20 of 40 images", "Showing 21 - 40 of 40 images")
    info = parse_detail_page(html, "e-hentai.org", page_index=1)
    assert info.image_no_from == 20
    assert info.image_no_to == 39
    assert info.thumbnails[0].page_no == 21


# --------------------------------------------------------------------------
# detail page — old structures
# --------------------------------------------------------------------------

DETAIL_OLD_SMALL_HTML = """
<html><body>
<div class="gtb"><div class="gpc">Showing 1 - 20 of 20 images</div></div>
<div id="gdt">
  <div class="gdtm"><div style="width: 100px; height: 150px; background: url(/t/small_1.jpg) -25px 0px no-repeat transparent;"><a href="/s/oldtok/2165080-1"></a></div></div>
  <div class="gdtm"><div style="width: 100px; height: 150px; background: url(/t/small_2.jpg) 0px 0px no-repeat transparent;"><a href="/s/oldtok2/2165080-2"></a></div></div>
</div>
</body></html>
"""


def test_parse_detail_page_old_small():
    info = parse_detail_page(DETAIL_OLD_SMALL_HTML, "e-hentai.org", page_index=0)
    assert len(info.thumbnails) == 2
    t0, t1 = info.thumbnails
    assert t0.href == "/s/oldtok/2165080-1"
    assert t0.page_no == 1
    assert t0.thumb_url == "https://e-hentai.org/t/small_1.jpg"
    assert t1.page_no == 2


DETAIL_OLD_LARGE_HTML = """
<html><body>
<div id="gdt">
  <div class="gdtl"><a href="/s/oldlarge/2165080-1"><img src="https://ehgt.org/t/large_1.jpg-296-400"></a></div>
</div>
</body></html>
"""


def test_parse_detail_page_old_large():
    info = parse_detail_page(DETAIL_OLD_LARGE_HTML, "e-hentai.org", page_index=0)
    assert len(info.thumbnails) == 1
    t = info.thumbnails[0]
    assert t.href == "/s/oldlarge/2165080-1"
    assert t.is_large is True
    assert t.thumb_url == "https://ehgt.org/t/large_1.jpg-296-400"


# --------------------------------------------------------------------------
# image page
# --------------------------------------------------------------------------

IMAGE_PAGE_HTML = """
<html><body>
<img id="img" src="https://ehgt.org/12/34/real_image.jpg" style="width: 1124px; height: 1600px;">
<div id="i6"><div><a href="https://e-hentai.org/fullimg.php?gid=2165080&page=1&f_shash=abcd1234">Original</a></div></div>
<div id="loadfail" onclick="return nl('WZG-474997')"></div>
</body></html>
"""


def test_parse_image_page():
    info = parse_image_page(IMAGE_PAGE_HTML)
    assert isinstance(info, ImagePageInfo)
    assert info.image_url == "https://ehgt.org/12/34/real_image.jpg"
    assert info.width == 1124
    assert info.height == 1600
    assert info.reload_key == "WZG-474997"
    assert info.is_509 is False


def test_parse_image_page_509():
    html = IMAGE_PAGE_HTML.replace(
        "https://ehgt.org/12/34/real_image.jpg", "https://ehgt.org/g/509.gif"
    )
    info = parse_image_page(html)
    assert info.is_509 is True


def test_parse_image_page_missing_img_raises():
    with pytest.raises(ParseError):
        parse_image_page("<html><body><p>no image</p></body></html>")


# --------------------------------------------------------------------------
# gdata
# --------------------------------------------------------------------------

GDATA_JSON = """
{
  "gmetadata": [
    {
      "gid": "2165080",
      "token": "725f6a7a58",
      "title": "Test Gallery",
      "title_jpn": "テスト",
      "category": "Manga",
      "thumb": "https://ehgt.org/t/thumb.jpg",
      "rating": "4.56",
      "tags": ["language:english", "artist:someone", "tagless"],
      "filecount": "66",
      "filesize": "12345678",
      "posted": "1700000000",
      "uploader": "uploader1",
      "torrentcount": "2",
      "expunged": false
    }
  ]
}
"""


def test_parse_gdata_response():
    metas = parse_gdata_response(GDATA_JSON)
    assert len(metas) == 1
    m = metas[0]
    assert m.gid == 2165080
    assert m.token == "725f6a7a58"
    assert m.title == "Test Gallery"
    assert m.category == "Manga"
    assert m.rating == 4.56
    assert m.filecount == 66
    assert m.filesize == 12345678
    assert m.posted == 1700000000
    assert m.language == "english"
    assert m.expunged is False
    assert m.tags["artist"][0].key == "someone"
    # tag without namespace goes to "temp"
    assert m.tags["temp"][0].key == "tagless"


def test_parse_gdata_response_empty_raises():
    with pytest.raises(ParseError):
        parse_gdata_response('{"gmetadata": []}')


def test_parse_gdata_response_skips_error_entries():
    body = '{"gmetadata": [{"gid": 1, "error": "Gallery not found"}, {"gid": "2", "token": "t", "title": "Ok", "tags": []}]}'
    metas = parse_gdata_response(body)
    assert len(metas) == 1
    assert metas[0].gid == 2
