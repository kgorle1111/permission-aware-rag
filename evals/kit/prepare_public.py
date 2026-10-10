"""Extract Constitution paragraphs from a separately downloaded official HTML file."""

import argparse
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

SOURCE = "https://www.archives.gov/founding-docs/constitution-transcript"


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tag, self.buffer, self.items = None, [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "h2", "h3"):
            self.tag, self.buffer = tag, []

    def handle_data(self, data):
        if self.tag:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == self.tag:
            self.items.append((tag, " ".join("".join(self.buffer).split())))
            self.tag = None


PRINCIPALS = [
    {"id": "bob", "groups": ["hr", "finance", "exec"]},
    {"id": "bob", "groups": ["hr"]},
    {"id": "bob", "groups": []},
    {"id": "alice", "groups": ["hr"]},
    {"id": "charlie", "groups": ["hr-admin"]},
    {"id": "dana", "groups": []},
    {"id": "banker", "groups": ["banking*"]},
    {"id": "guest", "groups": []},
]
OVERLAYS = [
    (["*"], ["*"], ["*"]),
    (["group:hr"], ["*"], ["*"]),
    (["group:hr"], ["user:bob"], ["*"]),
    (["*"], ["*"], ["group:exec"]),
    (["group:hr-admin"], ["*"], ["*"]),
    (["group:banking*"], ["*"], ["*"]),
    (["user:dana"], ["*"], ["*"]),
    (["group:hr"], ["user:bob"], ["group:finance"]),
]


def extract(html: str):
    parser = Paragraphs()
    parser.feed(html)
    start = next(i for i, (_, text) in enumerate(parser.items) if text.startswith("We the People"))
    end = next(
        i for i, (_, text) in enumerate(parser.items[start:], start) if text.startswith("done in Convention")
    )
    texts = [text for tag, text in parser.items[start:end] if tag == "p" and text]
    if not texts or "Attest William Jackson Secretary" != texts[-1]:
        raise ValueError("source boundaries changed; review extraction before updating corpus")
    return texts


def make_fixture(texts):
    chunks = []
    for i, text in enumerate(texts):
        doc, section, para = OVERLAYS[i % len(OVERLAYS)]
        chunks.append(
            {
                "id": f"constitution-p{i:03d}",
                "text": text,
                "acl_doc": doc,
                "acl_section": section,
                "acl_para": para,
            }
        )
    return {
        "name": "national-archives-constitution-fictional-acls-v1",
        "principals": PRINCIPALS,
        "chunks": chunks,
        "ks": [1, 3, 20],
        "revocations": [
            {
                "id": chunks[1]["id"],
                "principal": {"id": "bob", "groups": ["hr"]},
                "levels": {"acl_doc": ["user:revoked-owner"]},
            }
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("public_constitution.json"))
    args = parser.parse_args()
    raw = args.html.read_bytes()
    fixture = make_fixture(extract(raw.decode("utf-8")))
    args.output.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
    manifest = {
        "source_url": SOURCE,
        "retrieved_date": "2026-10-08",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "fixture_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "paragraphs": len(fixture["chunks"]),
        "permission_overlay": "fictional cyclic eight-pattern v1",
        "text_rights": "Public-domain U.S. Constitution transcription; NARA reuse policy applies to NARA works",
        "rights_source": "https://www.archives.gov/global-pages/privacy.html#copyright",
        "extraction": "HTML p text from We the People through Attest William Jackson Secretary; whitespace normalized; inline links retain their text",
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
