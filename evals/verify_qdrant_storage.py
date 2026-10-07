"""Check hierarchy and storage configuration on a real Qdrant server.

Run from platform/: python ../evals/verify_qdrant_storage.py --url http://localhost:6333
Creates and deletes only a unique synthetic collection. Not a performance benchmark.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform"))
from app.embeddings import embed_one
from qdrant_client import QdrantClient

from app import config, vectorstore

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", required=True)
config.QDRANT_URL = parser.parse_args().url
config.COLLECTION = "hierarchy-check-" + uuid.uuid4().hex
remote = QdrantClient(url=config.QDRANT_URL)
vectorstore.client = lambda: remote
try:
    vectorstore.reset_collection()
    info = remote.get_collection(config.COLLECTION)
    assert info.config.params.on_disk_payload is True
    assert info.config.params.vectors.on_disk is True
    assert info.config.quantization_config.scalar.type.value == "int8"
    assert info.config.quantization_config.scalar.always_ram is True
    assert set(info.payload_schema) == {"acl_doc", "acl_section", "acl_para", "doc_id"}
    rows = []
    for i, para in enumerate([["*"], ["group:executive"]]):
        rows.append(
            {
                "id": i,
                "doc_id": "nested",
                "text": "salary policy " + ("ordinary" if i == 0 else "secret"),
                "acl": [],
                "acl_doc": ["group:hr"],
                "acl_section": ["user:bob"],
                "acl_para": para,
                "vector": embed_one("salary policy"),
            }
        )
    vectorstore.upsert_chunks(rows)
    query = embed_one("salary policy")
    assert [r["chunk_id"] for r in vectorstore.search(query, ["*", "user:bob", "group:hr"], 20)] == [0]
    assert vectorstore.search(query, ["*", "user:bob"], 20) == []
    assert vectorstore.search(query, ["*", "user:alice", "group:hr"], 20) == []
    assert {
        r["chunk_id"] for r in vectorstore.search(query, ["*", "user:bob", "group:hr", "group:executive"], 20)
    } == {0, 1}
    vectorstore.update_doc_acls({"nested": []})
    assert vectorstore.search(query, ["*", "user:bob", "group:hr", "group:executive"], 20) == []
    points = remote.retrieve(config.COLLECTION, ids=[0, 1], with_payload=True)
    assert all(p.payload["acl_section"] == ["user:bob"] for p in points)
    assert points[1].payload["acl_para"] == ["group:executive"]
    print(
        json.dumps(
            {
                "server": remote.info().version,
                "hierarchy": "passed",
                "revocation": "passed",
                "indexes": sorted(info.payload_schema),
                "on_disk_payload": True,
                "on_disk_original_vectors": True,
                "quantization": "int8/config verified; performance not measured",
            }
        )
    )
finally:
    remote.delete_collection(config.COLLECTION)
    remote.close()
