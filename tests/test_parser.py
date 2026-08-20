"""Parser unit tests using mock HTML fixtures (no network, no cookies).

Fixture structures mirror the real E-Hentai markup.
"""

import pytest

from app.eh.exceptions import ParseError
from app.eh.models import GalleryTag, ImagePageInfo
from app.eh.parser import (
    apply_status_filter,
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
    assert info.next_gid == "2165079"
    assert info.prev_gid == "2165081"
    assert info.total_count == 1234567


FAVORITES_NAV_HTML = """
<html><body>
<table class="itg glte">
  <tr>
    <td class="gl1e"><a href="https://e-hentai.org/g/2753175/abc123/"><img src="/x/1.jpg"></a></td>
    <td class="gl2e"><div class="gl3e"><div class="cn">Manga</div>
      <div id="posted_2753175" title="Common">2026-08-11 12:00</div>
      <div>42 pages</div>
      <a href="https://e-hentai.org/g/2753175/abc123/"><div class="gl4e"><div class="glink">Old Favorite</div></div></a>
    </div></td>
  </tr>
</table>
<div class="searchnav"><a id="unext" href="?next=2753175-1786365950">Next</a></div>
</body></html>
"""


def test_parse_list_page_dashed_cursor():
    """Favorites sorted by favorited time emit a composite `gid-favtime`
    cursor (`?next=2753175-1786365950`) instead of a plain gid. It must
    survive parsing as an opaque string — never int()-coerced (would raise
    ValueError and 500 the favorites feed)."""
    info = parse_list_page(FAVORITES_NAV_HTML)
    assert info.galleries[0].gid == 2753175
    assert info.next_gid == "2753175-1786365950"
    assert info.prev_gid is None


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
    assert m.language == "en"
    assert m.expunged is False
    assert m.tags["artist"][0].key == "someone"
    # tag without namespace goes to "temp"
    assert m.tags["temp"][0].key == "tagless"


def test_language_mapping_to_bcp47():
    """EH language tags map to BCP 47 (RFC 5646); markers/unknowns dropped."""
    from app.eh.languages import map_language

    assert map_language("chinese") == "zh"
    assert map_language("chinese (simplified)") == "zh-Hans"
    assert map_language("chinese (traditional)") == "zh-Hant"
    assert map_language("japanese") == "ja"
    assert map_language("english") == "en"
    assert map_language("Korean") == "ko"  # case-insensitive
    assert map_language("translated") is None   # marker pseudo-tag
    assert map_language("rewrite") is None      # marker pseudo-tag
    assert map_language("raw") is None          # marker pseudo-tag
    assert map_language("klingon") is None      # unknown → dropped
    assert map_language("") is None


def test_parse_gdata_response_empty_raises():
    with pytest.raises(ParseError):
        parse_gdata_response('{"gmetadata": []}')


def test_parse_gdata_response_skips_error_entries():
    body = '{"gmetadata": [{"gid": 1, "error": "Gallery not found"}, {"gid": "2", "token": "t", "title": "Ok", "tags": []}]}'
    metas = parse_gdata_response(body)
    assert len(metas) == 1
    assert metas[0].gid == 2


# --------------------------------------------------------------------------
# tag parsing (featured / highlighted tags with inline styles)
# --------------------------------------------------------------------------

COMPACT_TAGS_HTML = """
<html><body>
<table class="itg gltc"><tbody>
  <tr>
    <td class="gl2c"><div class="glthumb"><div><img src="/c/1.jpg"></div></div></td>
    <td class="gl3c glname">
      <a href="https://e-hentai.org/g/100/aaa/"><div class="glink">Tag Gallery</div></a>
      <div class="gt" title="parody:zenless zone zero">zenless zone zero</div>
      <div class="gtl" title="character:ellen joe">ellen joe</div>
      <div class="gt" style="color:#f1f1f1;border-color:#048751;background:radial-gradient(#048751,#24A771) !important" title="female:netorare">f:netorare</div>
    </td>
    <td class="cn">Misc</td>
    <td class="gl4c glhide"><div></div><div>5 pages</div></td>
  </tr>
</tbody></table>
</body></html>
"""


def test_parse_list_page_compact_tags():
    info = parse_list_page(COMPACT_TAGS_HTML)
    assert len(info.galleries) == 1
    g = info.galleries[0]
    assert [str(t) for t in g.tags] == [
        "parody:zenless zone zero",
        "character:ellen joe",
        "female:netorare",
    ]
    # gtl -> skepticism
    assert g.tags[1].status == "skepticism"
    # plain tag: no style
    assert g.tags[0].style is None
    # featured tag: inline style extracted, !important stripped
    featured = g.tags[2]
    assert featured.status == "confidence"
    assert featured.style is not None
    assert featured.style.color == "#f1f1f1"
    assert featured.style.border_color == "#048751"
    assert featured.style.background == "radial-gradient(#048751,#24A771)"


# --------------------------------------------------------------------------
# extended view tag parsing (cover rows; full tag set in the nested table)
# --------------------------------------------------------------------------

EXTENDED_TAGS_HTML = """
<html><body>
<table class="itg glte">
  <!-- Cover row: gallery info + nested tag table (full tag set, mirroring
       tests/fixtures/list_page_extended.html) -->
  <tr>
    <td class="gl1e" style="width:250px"><div><a href="https://e-hentai.org/g/100/aaa/"><img src="/x/cover.jpg"></a></div></td>
    <td class="gl2e">
      <div>
        <div class="gl3e">
          <div class="cn">Manga</div>
          <div onclick="popUp('https://e-hentai.org/gallerypopups.php?gid=100&amp;t=aaa')" id="posted_100">2026-08-12 00:00</div>
          <div class="ir" style="background-position:0px -21px;opacity:1"></div>
          <div><a href="https://e-hentai.org/uploader/u1">u1</a></div>
          <div>42 pages</div>
          <div class="gldown"></div>
        </div>
        <a href="https://e-hentai.org/g/100/aaa/">
          <div class="gl4e glname" style="min-height:100px">
            <div class="glink">Extended Gallery</div>
            <div>
              <table>
                <tr><td class="tc">language:</td><td><div class="gt" title="language:chinese">chinese</div></td></tr>
                <tr><td class="tc">female:</td><td><div class="gt" title="female:ahegao" style="color:#f1f1f1;border-color:#048751;background:radial-gradient(#048751,#24A771) !important">ahegao</div></td></tr>
                <tr><td class="tc">parody:</td><td><div class="gt" title="parody:original">original</div><div class="gtl" title="parody:fate grand order" style="color:#fff;border-color:#f00;background:radial-gradient(#f00,#a00) !important">fate grand order</div></td></tr>
              </table>
            </div>
          </div>
        </a>
      </div>
    </td>
  </tr>
  <!-- Another cover row (empty nested table: no tags) -->
  <tr>
    <td class="gl1e" style="width:250px"><div><a href="https://e-hentai.org/g/200/bbb/"><img src="/x/cover2.jpg"></a></div></td>
    <td class="gl2e">
      <div>
        <div class="gl3e"><div class="cn">Doujinshi</div><div>2026-08-12 00:01</div><div class="ir"></div><div></div><div>5 pages</div><div class="gldown"></div></div>
        <a href="https://e-hentai.org/g/200/bbb/">
          <div class="gl4e glname" style="min-height:50px">
            <div class="glink">Second Gallery</div>
            <div><table></table></div>
          </div>
        </a>
      </div>
    </td>
  </tr>
</table>
</body></html>
"""


def test_parse_list_page_extended_tags():
    """Extended view: cover-row nested table carries the full tag set."""
    info = parse_list_page(EXTENDED_TAGS_HTML)
    assert len(info.galleries) == 2

    g1 = info.galleries[0]
    assert g1.gid == 100
    assert g1.title == "Extended Gallery"
    assert g1.category == "Manga"
    assert g1.page_count == 42  # extended page-count div index (5th child)
    # Tags from the cover row's nested tag table
    assert len(g1.tags) == 4
    tag_strs = [str(t) for t in g1.tags]
    assert "language:chinese" in tag_strs
    assert "female:ahegao" in tag_strs
    assert "parody:original" in tag_strs
    assert "parody:fate grand order" in tag_strs

    # Featured tags carry style
    ahegao = next(t for t in g1.tags if t.key == "ahegao")
    assert ahegao.style is not None
    assert ahegao.style.color == "#f1f1f1"
    assert ahegao.style.background == "radial-gradient(#048751,#24A771)"

    # Featured tag inside the nested table carries style
    fate = next(t for t in g1.tags if t.key == "fate grand order")
    assert fate.style is not None
    assert fate.style.color == "#fff"
    assert fate.status == "skepticism"  # gtl

    # Plain tag: no style
    orig = next(t for t in g1.tags if t.key == "original")
    assert orig.style is None

    # Second gallery (empty nested table: no tags, page count from 5th div)
    g2 = info.galleries[1]
    assert g2.gid == 200
    assert g2.title == "Second Gallery"
    assert g2.page_count == 5
    assert g2.tags == []


def test_parse_list_page_extended_extra_fields():
    """Extended rows expose rating (`.ir` sprite), publish time and language."""
    info = parse_list_page(EXTENDED_TAGS_HTML)
    g1, g2 = info.galleries
    # `background-position:0px -21px` → 5 - 0 - 0.5 = 4.5
    assert g1.rating == 4.5
    assert g1.publish_time == "2026-08-12 00:00"
    assert g1.language == "zh"  # language:chinese → BCP 47 zh
    # row without a posted element / rating sprite / tags → field defaults
    assert g2.rating == 0.0
    assert g2.publish_time == ""
    assert g2.language == ""


def test_parse_list_page_compact_rating_and_publish_time():
    """Compact rows also expose rating/publish time."""
    html = """
    <html><body>
    <table class="itg gltc"><tbody>
      <tr>
        <td class="gl2c">
          <div class="glthumb"><div><img src="/c/1.jpg"></div></div>
          <div><div id="posted_100">2026-08-11 22:00</div></div>
          <div class="ir" style="background-position:-16px -1px;opacity:1"></div>
        </td>
        <td class="gl3c glname"><a href="https://e-hentai.org/g/100/aaa/"><div class="glink">Compact Gallery</div></a></td>
        <td class="cn">Doujinshi</td>
        <td class="gl4c glhide"><div></div><div>5 pages</div></td>
      </tr>
    </tbody></table>
    </body></html>
    """
    info = parse_list_page(html)
    assert len(info.galleries) == 1
    g = info.galleries[0]
    assert g.publish_time == "2026-08-11 22:00"
    assert g.rating == 4.0  # -16px → 5 - 1 = 4


def test_parse_publish_time_iso():
    from app.eh.parser import parse_publish_time_iso

    assert parse_publish_time_iso("2026-08-12 13:11") == "2026-08-12T13:11:00Z"
    assert parse_publish_time_iso("12 August 2024, 02:31") == "2024-08-12T02:31:00Z"
    assert parse_publish_time_iso("") == ""
    assert parse_publish_time_iso("not a date") == ""


def test_parse_real_extended_fixture():
    """Regression: real extended-view HTML fixture has styled tags.

    NOTE: the extended-view fixture is saved by the integration test as
    ``list_page.html`` (renamed from ``list_page_extended.html``). It is the
    ``itg glte`` Extended layout — the one the server forces via
    ``inline_set=dm_e`` — so this is the critical-path tag parsing.
    """
    from pathlib import Path

    fx = Path(__file__).parent / "fixtures"
    fixture = fx / "list_page.html"
    if not fixture.exists():
        pytest.skip("real HTML fixture not present")
    info = parse_list_page(fixture.read_text(encoding="utf-8"))
    assert len(info.galleries) >= 1, "should parse galleries from real fixture"
    # At least one gallery should have tags
    total_tags = sum(len(g.tags) for g in info.galleries)
    assert total_tags > 0, "extended view should carry tags"
    # At least some tags should have highlighted styles
    styled = sum(1 for g in info.galleries for t in g.tags if t.style is not None)
    assert styled > 0, f"expected some styled tags, got {styled} out of {total_tags}"


DETAIL_TAGS_HTML = """
<html><body>
<div class="gtb"><div class="gpc">Showing 1 - 20 of 40 images</div></div>
<div id="gdt" class="gdt">
  <a href="/s/xyz123/2165080-1"><div style="width: 100px; height: 150px; background: url(https://ehgt.org/t/aa/bb_1.jpg);"></div></a>
</div>
<div id="taglist"><table>
  <tr><td class="tc">parody:</td><td><div id="td_parody:zenless_zone_zero" class="gtl" style="opacity:1.0"><a id="ta_parody:zenless_zone_zero" href="https://exhentai.org/tag/parody:zenless+zone+zero">zenless zone zero</a></div></td></tr>
  <tr><td class="tc">female:</td><td><div id="td_female:netorare" class="gt" style="color:#f1f1f1;border-color:#048751;background:radial-gradient(#048751,#24A771) !important"><a id="ta_female:netorare" href="#">netorare</a></div></td></tr>
  <tr><td class="tc"></td><td><div id="td_custom_tag" class="gt"><a id="ta_custom_tag" href="#">custom tag</a></div></td></tr>
</table></div>
</body></html>
"""


def test_parse_detail_page_tags():
    info = parse_detail_page(DETAIL_TAGS_HTML, "e-hentai.org", 0)
    assert [str(t) for t in info.tags] == [
        "parody:zenless zone zero",
        "female:netorare",
        "temp:custom tag",
    ]
    assert info.tags[0].status == "skepticism"  # gtl
    assert info.tags[1].status == "confidence"  # gt
    assert info.tags[1].style is not None
    assert info.tags[1].style.background == "radial-gradient(#048751,#24A771)"
    # id without namespace -> temp
    assert info.tags[2].namespace == "temp"
    # thumbnails still parsed alongside tags
    assert len(info.thumbnails) == 1


DETAIL_META_HTML = """
<html><body>
<div class="gtb"><div class="gpc">Showing 1 - 20 of 893 images</div></div>
<div id="gd1"><div style="width:250px; height:188px; background:transparent url(https://ehgt.org/w/02/566/x.webp) 0 0 no-repeat"></div></div>
<div id="gn">[Author] Clean Title [Chinese]</div>
<div id="gj">[著者] クリーンタイトル [中国翻訳]</div>
<div id="gdc"><span class="cs">Doujinshi</span></div>
<div id="gdd"><table>
  <tr><td class="gdt1">Posted:</td><td class="gdt2">2026-08-12 13:11</td></tr>
  <tr><td class="gdt1">Language:</td><td class="gdt2">Chinese TR</td></tr>
  <tr><td class="gdt1">File Size:</td><td class="gdt2">12.34 MB</td></tr>
  <tr><td class="gdt1">Length:</td><td class="gdt2">893 pages</td></tr>
</table></div>
<div id="gdn"><a href="#">uploader1</a></div>
<div id="rating_image" class="ir" style="background-position:-32px -21px"></div>
<div id="gd5">Report Gallery Archive Download Torrent Download (2)</div>
<div id="gdt" class="gdt">
  <a href="/s/xyz123/2165080-1"><div style="width: 100px; height: 150px; background: url(https://ehgt.org/t/aa/bb_1.jpg);"></div></a>
</div>
</body></html>
"""


def test_parse_detail_page_metadata():
    """Detail page carries gdata-equivalent metadata (#gn/#gdd/#gdn/#grt2)."""
    info = parse_detail_page(DETAIL_META_HTML, "e-hentai.org", 0)
    assert info.title == "[Author] Clean Title [Chinese]"
    assert info.title_jpn == "[著者] クリーンタイトル [中国翻訳]"
    assert info.category == "Doujinshi"
    assert info.cover_url == "https://ehgt.org/w/02/566/x.webp"
    assert info.rating == 2.5  # -32px -21px → 5 - 2 - 0.5
    assert info.uploader == "uploader1"
    assert info.publish_time == "2026-08-12 13:11"
    assert info.language == "zh"  # "Chinese TR" → mapped to BCP 47 zh
    assert info.filesize_text == "12.34 MB"
    assert info.image_count == 893
    assert info.torrent_count == 2
    assert info.expunged is False
    # thumbnails and tags still parsed alongside
    assert len(info.thumbnails) == 1


def test_parse_detail_page_metadata_expunged():
    html = DETAIL_META_HTML.replace(
        '<tr><td class="gdt1">Language:</td><td class="gdt2">Chinese TR</td></tr>',
        '<tr><td class="gdt1">Expunged:</td><td class="gdt2">Expunged</td></tr>',
    )
    info = parse_detail_page(html, "e-hentai.org", 0)
    assert info.expunged is True


# --------------------------------------------------------------------------
# detail page — comments (#cdiv)
# --------------------------------------------------------------------------

DETAIL_COMMENTS_HTML = """
<html><body>
<div class="gtb"><div class="gpc">Showing 1 - 20 of 40 images</div></div>
<div id="gdt" class="gdt">
  <a href="/s/xyz123/2165080-1"><div style="width: 100px; height: 150px; background: url(https://ehgt.org/t/aa/bb_1.jpg);"></div></a>
</div>
<div id="cdiv" class="gm">
  <a name="c0"></a>
  <div class="c1">
    <div class="c2">
      <div class="c3">Posted on 12 August 2026, 13:11 by: &nbsp; <a href="https://e-hentai.org/uploader/gvc051126">gvc051126</a>&nbsp; &nbsp; <a href="https://forums.e-hentai.org/index.php?showuser=685825">PM</a></div>
      <div class="c4 nosel"><a name="ulcomment"></a>Uploader Comment</div>
      <div class="c"></div>
    </div>
    <div class="c6" id="comment_0">尝试用BallonsTranslator翻译+修图。<br />原图收集自：<br /><a href="https://e-hentai.org/g/867387/3a0d9903d3/">source link</a></div>
    <div class="c7" id="cvotes_0" style="display:none"></div>
  </div>
  <div class="c1">
    <div class="c2">
      <div class="c3">Posted on 1 March 2022, 22:05 by: &nbsp; <a href="https://e-hentai.org/uploader/someuser">someuser</a>&nbsp; &nbsp; <a href="https://forums.e-hentai.org/index.php?showuser=12345">PM</a></div>
    </div>
    <div class="c6" id="comment_1">Edited content<br>second line</div>
    <div class="c8"><strong>10 March 2022, 03:49</strong></div>
  </div>
</div>
</body></html>
"""


def test_parse_detail_comments():
    info = parse_detail_page(DETAIL_COMMENTS_HTML, "e-hentai.org", 0)
    assert len(info.comments) == 2

    c0 = info.comments[0]
    assert c0.id == 0
    assert c0.username == "gvc051126"
    assert c0.user_id == 685825
    assert c0.time == "2026-08-12 13:11"
    assert c0.last_edit_time == ""
    # raw HTML preserved
    assert 'id="comment_0"' in c0.content_html
    assert "尝试用BallonsTranslator翻译+修图" in c0.content_html
    assert 'href="https://e-hentai.org/g/867387/3a0d9903d3/"' in c0.content_html

    c1 = info.comments[1]
    assert c1.id == 1
    assert c1.username == "someuser"
    assert c1.user_id == 12345
    assert c1.time == "2022-03-01 22:05"
    assert c1.last_edit_time == "10 March 2022, 03:49"
    assert "<br>" in c1.content_html


def test_parse_detail_comments_missing_cdiv():
    """Detail pages without a comment block yield an empty list (no throw)."""
    info = parse_detail_page(DETAIL_META_HTML, "e-hentai.org", 0)
    assert info.comments == []


def test_parse_detail_comments_malformed_time():
    """An unrecognised posted-time format never breaks the detail parse."""
    html = DETAIL_COMMENTS_HTML.replace(
        "Posted on 12 August 2026, 13:11 by:",
        "Posted on somedate by:",
    )
    info = parse_detail_page(html, "e-hentai.org", 0)
    assert info.comments[0].time == ""
    assert info.comments[0].username == "gvc051126"


def test_parse_size_text():
    from app.eh.parser import _parse_size_text

    assert _parse_size_text("189.3 MiB") == 198495436
    assert _parse_size_text("12.34 MB") == 12939427
    assert _parse_size_text("1.5 GB") == 1610612736
    assert _parse_size_text("1024 B") == 1024
    assert _parse_size_text("890.1 KiB") == 911462
    assert _parse_size_text("") == 0
    assert _parse_size_text("garbage") == 0


def test_parse_real_fixture_tags():
    """Offline regression on the saved real HTML fixtures.

    Real captures are variable: some galleries / detail pages are genuinely
    tagless (no tag table in the HTML at all). Assert structure parsing
    works, and verify tag fields whenever a tag table IS present.
    """
    from pathlib import Path

    fx = Path(__file__).parent / "fixtures"
    if not (fx / "list_page.html").exists():
        pytest.skip("real HTML fixtures not present")
    info = parse_list_page((fx / "list_page.html").read_text(encoding="utf-8"))
    assert info.galleries, "fixture should contain at least one gallery"
    tagged = [g for g in info.galleries if g.tags]
    if tagged:  # some real galleries genuinely carry no tags
        t = tagged[0].tags[0]
        assert t.namespace and t.key
    detail = parse_detail_page(
        (fx / "detail_page.html").read_text(encoding="utf-8"), "e-hentai.org", 0
    )
    if detail.tags:  # #taglist table is absent on tagless galleries
        assert detail.tags[0].status in ("confidence", "skepticism", "incorrect")


# --------------------------------------------------------------------------
# title parser — parse_title_authors
# --------------------------------------------------------------------------

from app.eh.title_parser import parse_detail_title, parse_title_authors


def test_title_parser_simple_author():
    clean, authors = parse_title_authors("[No1r] Yor Forger [AI Generated]")
    assert clean == "Yor Forger"
    assert authors == ["No1r"]


def test_title_parser_author_with_suffix_tags():
    clean, authors = parse_title_authors(
        "[610cc] Daiji na Musume o Okuridashita. | 소중한 딸을 내보냈다. [Korean] [Digital]"
    )
    assert clean == "Daiji na Musume o Okuridashita. | 소중한 딸을 내보냈다."
    assert authors == ["610cc"]


def test_title_parser_chinese_author():
    clean, authors = parse_title_authors("[種付け研究所] 風間いろは [AI Generated]")
    assert clean == "風間いろは"
    assert authors == ["種付け研究所"]


def test_title_parser_circle_and_artist():
    """Doujinshi convention: [Circle (Artist)] Title."""
    clean, authors = parse_title_authors(
        "[Digital Lover (Nakajima Yuka)] DLO-03", "Doujinshi"
    )
    assert clean == "DLO-03"
    assert authors == ["Digital Lover (Nakajima Yuka)"]


def test_title_parser_event_prefix():
    """(Event) prefix before the author bracket."""
    clean, authors = parse_title_authors(
        "(C98) [Circle (Artist)] My Doujin Title"
    )
    assert clean == "My Doujin Title"
    assert authors == ["Circle (Artist)"]


def test_title_parser_no_brackets():
    """Title with no brackets at all."""
    clean, authors = parse_title_authors("Plain Title Without Brackets")
    assert clean == "Plain Title Without Brackets"
    assert authors == []


def test_title_parser_only_suffix_brackets():
    """Brackets after title only (language/digital tags), no author bracket."""
    clean, authors = parse_title_authors("Some Title [Chinese] [Digital]")
    assert clean == "Some Title"
    assert authors == []


def test_title_parser_parens_in_title():
    """Parentheses inside the title text should be stripped from clean title."""
    clean, authors = parse_title_authors(
        "[PinchiVersus] Riley Andersen (Inside Out)"
    )
    assert clean == "Riley Andersen"
    assert authors == ["PinchiVersus"]


def test_title_parser_multiple_authors_comma():
    clean, authors = parse_title_authors("[Author1, Author2] Some Title")
    assert clean == "Some Title"
    assert authors == ["Author1, Author2"]


def test_title_parser_multiple_authors_jp_sep():
    clean, authors = parse_title_authors("[Author1、Author2] Some Title")
    assert clean == "Some Title"
    assert authors == ["Author1、Author2"]


def test_title_parser_whitespace_collapse():
    """Multiple spaces in clean title are collapsed."""
    clean, authors = parse_title_authors("[Author]   Too   Many   Spaces")
    assert clean == "Too Many Spaces"
    assert authors == ["Author"]


def test_title_parser_empty_author_bracket():
    """Empty brackets before title should not produce authors."""
    clean, authors = parse_title_authors("[] Empty Bracket Title")
    assert clean == "Empty Bracket Title"
    assert authors == []


def test_parse_detail_title_prefers_japanese():
    clean, authors = parse_detail_title(
        "[Ponkotsu Teikoku] Trans Story ~改造篇~ [Chinese MTL/中文机翻]",
        "[ポンコツ帝国] トランス・ストーリー ～改造編～[中国翻訳]",
        "Doujinshi",
    )
    assert clean == "トランス・ストーリー ～改造編～"
    assert authors == ["ポンコツ帝国"]


def test_parse_detail_title_falls_back_without_japanese():
    clean, authors = parse_detail_title(
        "[No1r] Yor Forger [AI Generated]", "", "Manga"
    )
    assert clean == "Yor Forger"
    assert authors == ["No1r"]


def test_parse_detail_title_falls_back_for_marker_only_japanese():
    """titleJpn is only bracket markers -> treated as missing, default wins."""
    clean, authors = parse_detail_title(
        "[No1r] Yor Forger [AI Generated]", "[中国翻訳]", "Doujinshi"
    )
    assert clean == "Yor Forger"
    assert authors == ["No1r"]


def test_parse_detail_title_author_bracket_kept_whole():
    """[Circle (Artist)] stays one author, even from the Japanese source."""
    clean, authors = parse_detail_title(
        "[Digital Lover (Nakajima Yuka)] DLO-03",
        "[Digital Lover (Nakajima Yuka)] テスト",
        "Doujinshi",
    )
    assert clean == "テスト"
    assert authors == ["Digital Lover (Nakajima Yuka)"]


# --------------------------------------------------------------------------
# tag status filter (global strategy)
# --------------------------------------------------------------------------

def _tag(ns: str, key: str, status: str, style: bool = False) -> "GalleryTag":
    from app.eh.models import TagStyle

    return GalleryTag(
        namespace=ns,
        key=key,
        status=status,
        style=TagStyle(color="#fff") if style else None,
    )


def test_apply_status_filter_balanced_default():
    """Default (balanced) keeps confidence + skepticism, drops incorrect."""
    tags = [
        _tag("female", "a", "confidence"),
        _tag("male", "b", "skepticism"),
        _tag("artist", "c", "incorrect"),
    ]
    out = apply_status_filter(tags)
    assert [str(t) for t in out] == ["female:a", "male:b"]


def test_apply_status_filter_strict():
    tags = [
        _tag("female", "a", "confidence"),
        _tag("male", "b", "skepticism"),
    ]
    out = apply_status_filter(tags, "strict")
    assert [str(t) for t in out] == ["female:a"]


def test_apply_status_filter_off_keeps_all():
    tags = [
        _tag("female", "a", "confidence"),
        _tag("male", "b", "incorrect"),
    ]
    out = apply_status_filter(tags, "off")
    assert len(out) == 2


def test_apply_status_filter_unknown_level_falls_back_to_balanced():
    tags = [
        _tag("female", "a", "confidence"),
        _tag("male", "b", "incorrect"),
    ]
    out = apply_status_filter(tags, "bogus")
    assert [str(t) for t in out] == ["female:a"]


def test_apply_status_filter_does_not_mutate_input():
    tags = [_tag("female", "a", "incorrect")]
    out = apply_status_filter(tags)
    assert out == []
    assert len(tags) == 1
