import copy

import pytest

from evo_tiktok import gdoc, publish
from evo_tiktok.config import Settings

MD = """# Filming pack: week of 5 October 2026

- **This week:** 3 × Value comparison
- **Giveaway prize:** shoes 🔥

## 1. Value comparison (about 25s)

**Hook:** Which one would you pick?
1. Wide shot
2. Close-up
> Best golf polo under £40?
| Posts | Views |
|---|---|
| 6 | 23700 |
"""


def test_markdown_becomes_doc_paragraphs():
    paras = gdoc.markdown_to_paragraphs(MD)
    texts = [p.text for p in paras]
    assert texts[0] == "Filming pack: week of 5 October 2026" and paras[0].style == "HEADING_1"
    assert paras[1].bullet == "BULLET" and paras[1].text == "This week: 3 × Value comparison"
    assert paras[1].bold == [(0, len("This week:"))]
    assert paras[3].style == "HEADING_2"
    assert [p.bullet for p in paras[5:7]] == ["NUMBERED", "NUMBERED"] and paras[5].text == "Wide shot"
    assert paras[7].italic and paras[7].text == "Best golf polo under £40?"
    assert "Posts · Views" in texts and "6 · 23700" in texts and not any("---" in t for t in texts)
    assert texts[-1] == gdoc.DIVIDER


def test_requests_use_utf16_indexes():
    paras = gdoc.markdown_to_paragraphs("- **A:** 🔥\n- next")
    reqs = gdoc.build_requests(paras)
    inserted = reqs[0]["insertText"]["text"]
    assert reqs[0]["insertText"]["location"]["index"] == 1
    # The emoji is 2 UTF-16 units, so the second paragraph starts at 1 + len("A: 🔥\n") in UTF-16.
    styles = [r["updateParagraphStyle"]["range"] for r in reqs if "updateParagraphStyle" in r]
    assert styles[1]["startIndex"] == 1 + gdoc.u16("A: 🔥\n")
    assert styles[-1]["endIndex"] == 1 + gdoc.u16(inserted)
    [bullets] = [r["createParagraphBullets"] for r in reqs if "createParagraphBullets" in r]
    assert bullets["range"]["startIndex"] == 1 and bullets["bulletPreset"].startswith("BULLET")
    bold = [r["updateTextStyle"] for r in reqs if r.get("updateTextStyle", {}).get("textStyle") == {"bold": True}]
    assert bold[0]["range"] == {"startIndex": 1, "endIndex": 1 + gdoc.u16("A:")}


class FakeSession:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def post(self, url, json):
        self.calls.append((url, json))
        return type("R", (), {"status_code": self.status, "text": "err"})()


def test_prepend_posts_one_batch_update():
    session = FakeSession()
    n = gdoc.GoogleDoc(None, session=session).prepend("DOC123", "# Hi\nthere")
    [(url, body)] = session.calls
    assert url.endswith("/DOC123:batchUpdate") and n == 3
    assert body["requests"][0]["insertText"]["text"] == f"Hi\nthere\n{gdoc.DIVIDER}\n"
    with pytest.raises(gdoc.DocsError):
        gdoc.GoogleDoc(None, session=FakeSession(403)).prepend("DOC123", "x")


def test_publish_routes_by_destination(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(gdoc.GoogleDoc, "__init__", lambda self, s, session=None: None)
    monkeypatch.setattr(gdoc.GoogleDoc, "prepend", lambda self, doc_id, md: sent.append((doc_id, md)) or 2)
    data = copy.deepcopy(settings.data)
    s = Settings(data, settings.source, env={"GOOGLE_DOC_ID": "ENVDOC"})
    assert publish.publish(s, "# hello")["doc_id"] == "ENVDOC" and sent == [("ENVDOC", "# hello")]
    assert publish.publish(Settings(data, settings.source, env={}), "# hi")["published"] is False
    data["outputs"]["destination"] = "none"
    assert publish.publish(Settings(data, settings.source, env={}), "# hi")["published"] is False


def test_google_sign_in_prefers_oauth_user(settings):
    from evo_tiktok.gsheets import authorized_session

    env = {"GOOGLE_OAUTH_CLIENT_ID": "id.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "secret",
           "GOOGLE_OAUTH_REFRESH_TOKEN": "1//refresh"}
    session = authorized_session(Settings(settings.data, settings.source, env=env), gdoc.SCOPES)
    creds = session.credentials
    assert creds.refresh_token == "1//refresh" and creds.client_id == env["GOOGLE_OAUTH_CLIENT_ID"]
    assert creds.token_uri == "https://oauth2.googleapis.com/token"


def test_google_sign_in_errors_are_clear(settings):
    from evo_tiktok.config import ConfigError
    from evo_tiktok.gsheets import authorized_session

    with pytest.raises(ConfigError, match="missing GOOGLE_OAUTH_REFRESH_TOKEN"):
        authorized_session(Settings(settings.data, settings.source,
                                    env={"GOOGLE_OAUTH_CLIENT_ID": "a", "GOOGLE_OAUTH_CLIENT_SECRET": "b"}), [])
    with pytest.raises(ConfigError, match="No Google sign-in"):
        authorized_session(Settings(settings.data, settings.source, env={}), [])
