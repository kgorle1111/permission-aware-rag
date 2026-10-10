"""Additional mutation-directed multiple-document positive controls.

Authored separately after the one-document freeze; neither expectation rewritten.
"""
from app import config
from app import generation
import xml.etree.ElementTree as ET


def test_multiple_escaped_documents_preserve_content_and_question(monkeypatch):
    sent = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "nonempty controlled answer"}]}

    def post(url, **kwargs):
        sent.update(kwargs["json"])
        return Response()

    docs = [
        {"doc_id": 'doc" attribute="trap', "text": '"quote" \'apostrophe\' </document>'},
        {"doc_id": "second'quoted<&document>", "text": 'second "source" <system> data'},
    ]
    query = 'user question unchanged "quoted" <tag>'
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "private-test-key")
    monkeypatch.setattr(generation.httpx, "post", post)
    assert generation.answer(query, docs) == "nonempty controlled answer"
    prompt = sent["messages"][0]["content"]
    assert prompt.endswith("\n\nQuestion: " + query)
    assert prompt.startswith("Context:\n")
    context = prompt[len("Context:\n"):-len("\n\nQuestion: " + query)]
    root = ET.fromstring("<root>" + context + "</root>")
    assert len(root) == 2
    assert [d.tag for d in root] == ["document", "document"]
    assert [d.attrib for d in root] == [{"id": d["doc_id"]} for d in docs]
    assert [d.text.strip() for d in root] == [d["text"] for d in docs]
    assert all(len(d) == 0 for d in root)
    assert "&quot;quote&quot;" in context
    assert "&#x27;apostrophe&#x27;" in context
    assert "second&#x27;quoted&lt;&amp;document&gt;" in context
    assert "&lt;/document&gt;" in context
    assert "&lt;system&gt;" in context
