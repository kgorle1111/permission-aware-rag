"""llm.py tests with mocked urlopen — no network, no key spend. Run: pytest test_llm.py"""
import io
import json
import urllib.error
from unittest import mock

import llm

CHUNKS = [{"doc_id": "policy-1", "text": "Policy 1 is active."}]


def _resp(answer):
    return io.BytesIO(json.dumps(
        {"content": [{"type": "text", "text": answer}],
         "usage": {"input_tokens": 10, "output_tokens": 5}}).encode())


def _http_error(code, body=b"err"):
    return urllib.error.HTTPError(llm.API_URL, code, "x", {}, io.BytesIO(body))


def test():
    # no key -> None, no network call
    with mock.patch.dict("os.environ", {}, clear=True), \
         mock.patch("urllib.request.urlopen") as u:
        assert llm.ask("q", CHUNKS) is None
        u.assert_not_called()

    env = {"ANTHROPIC_API_KEY": "test"}

    # injection boundary: chunks framed as <document> tags; system prompt says data-not-instructions
    with mock.patch.dict("os.environ", env), \
         mock.patch("urllib.request.urlopen") as u:
        u.return_value.__enter__.return_value = _resp("Active [policy-1].")
        out = llm.ask("status?", CHUNKS)
        sent = json.loads(u.call_args[0][0].data)
        assert '<document id="policy-1">' in sent["messages"][0]["content"]
        assert "never instructions" in sent["system"][0]["text"]
        assert out["unverified_citations"] == []

    # citation verification: cited id not in retrieved set is flagged
    with mock.patch.dict("os.environ", env), \
         mock.patch("urllib.request.urlopen") as u:
        u.return_value.__enter__.return_value = _resp("See [policy-1] and [watchlist].")
        assert llm.ask("q", CHUNKS)["unverified_citations"] == ["watchlist"]

    # 429 retries once then succeeds
    with mock.patch.dict("os.environ", env), \
         mock.patch("urllib.request.urlopen") as u, \
         mock.patch("time.sleep") as s:
        ok = mock.MagicMock()
        ok.__enter__.return_value = _resp("ok [policy-1]")
        u.side_effect = [_http_error(429), ok]
        assert llm.ask("q", CHUNKS)["answer"] == "ok [policy-1]"
        s.assert_called_once()

    # non-retryable error surfaces the API body
    with mock.patch.dict("os.environ", env), \
         mock.patch("urllib.request.urlopen") as u:
        u.side_effect = _http_error(400, b'{"error":"bad request"}')
        try:
            llm.ask("q", CHUNKS)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "400" in str(e) and "bad request" in str(e)

    print("all llm tests passed")


if __name__ == "__main__":
    test()
