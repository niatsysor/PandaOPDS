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


def test_opds_acq_mode_default_and_validation(monkeypatch):
    from app.config import load_settings

    # default: direct (compat-first)
    assert _settings().opds_acq_mode == "direct"
    assert Settings(ipb_member_id="1", ipb_pass_hash="abc").opds_acq_mode == "direct"

    # env parsing: direct / detail / unknown → direct
    monkeypatch.delenv("OPDS_ACQ_MODE", raising=False)
    assert load_settings().opds_acq_mode == "direct"
    monkeypatch.setenv("OPDS_ACQ_MODE", "detail")
    assert load_settings().opds_acq_mode == "detail"
    monkeypatch.setenv("OPDS_ACQ_MODE", "DETAIL")
    assert load_settings().opds_acq_mode == "detail"  # lower-cased
    monkeypatch.setenv("OPDS_ACQ_MODE", "bogus")
    assert load_settings().opds_acq_mode == "direct"  # unknown → direct
