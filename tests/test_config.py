"""Config unit tests: endpoint derivation per site."""

from app.config import Settings


def _settings(**kw) -> Settings:
    base = dict(ipb_member_id="1", ipb_pass_hash="abc")
    base.update(kw)
    return Settings(**base)


def test_api_url_per_site():
    assert _settings(eh_site="e-hentai").api_url == "https://api.e-hentai.org/api.php"
    # exhentai has no api. subdomain
    assert _settings(eh_site="exhentai").api_url == "https://exhentai.org/api.php"


def test_http_origin_and_cookies():
    s = _settings(eh_site="e-hentai")
    assert s.http_origin == "https://e-hentai.org"
    assert s.cookies["nw"] == "1"
    assert s.cookies["datatags"] == "1"
    assert s.cookies["ipb_member_id"] == "1"
    assert "igneous" not in s.cookies

    # igneous only seeded when provided and not "mystery"
    assert "igneous" not in _settings(igneous="mystery").cookies
    assert _settings(igneous="abc123").cookies["igneous"] == "abc123"


def test_pse_page_base():
    assert _settings().pse_page_base == 1
    assert _settings(pse_page_base=0).pse_page_base == 0


def test_opds_acq_detail_default_and_validation(monkeypatch):
    from app.config import load_settings

    # default: false (direct mode, compat-first)
    assert _settings().opds_acq_detail is False
    assert Settings(ipb_member_id="1", ipb_pass_hash="abc").opds_acq_detail is False

    # boolean OPDS_ACQ_DETAIL: true/1/yes/on -> detail; anything else -> false
    monkeypatch.delenv("OPDS_ACQ_DETAIL", raising=False)
    monkeypatch.delenv("OPDS_ACQ_MODE", raising=False)
    assert load_settings().opds_acq_detail is False
    monkeypatch.setenv("OPDS_ACQ_DETAIL", "true")
    assert load_settings().opds_acq_detail is True
    monkeypatch.setenv("OPDS_ACQ_DETAIL", "1")
    assert load_settings().opds_acq_detail is True
    monkeypatch.setenv("OPDS_ACQ_DETAIL", "YES")
    assert load_settings().opds_acq_detail is True  # case-insensitive
    monkeypatch.setenv("OPDS_ACQ_DETAIL", "0")
    assert load_settings().opds_acq_detail is False
    monkeypatch.setenv("OPDS_ACQ_DETAIL", "bogus")
    assert load_settings().opds_acq_detail is False

    # legacy OPDS_ACQ_MODE=detail|direct (string) honored when OPDS_ACQ_DETAIL unset
    monkeypatch.delenv("OPDS_ACQ_DETAIL", raising=False)
    monkeypatch.setenv("OPDS_ACQ_MODE", "detail")
    assert load_settings().opds_acq_detail is True
    monkeypatch.setenv("OPDS_ACQ_MODE", "DETAIL")
    assert load_settings().opds_acq_detail is True  # lower-cased
    monkeypatch.setenv("OPDS_ACQ_MODE", "bogus")
    assert load_settings().opds_acq_detail is False

    # OPDS_ACQ_DETAIL takes precedence over the legacy string
    monkeypatch.setenv("OPDS_ACQ_DETAIL", "false")
    monkeypatch.setenv("OPDS_ACQ_MODE", "detail")
    assert load_settings().opds_acq_detail is False
