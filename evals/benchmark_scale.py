"""Stream synthetic chunks to an isolated real Qdrant server and measure scale.

This is a harness for engine storage/search and the actual SQL permission sync.
It is not a measurement of ingest_corpus(), which materializes the whole corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def percentile(samples, fraction):
    if not samples:
        raise ValueError("empty measurement")
    return sorted(samples)[math.ceil(fraction * len(samples)) - 1]


def policy_for(document, paragraph):
    return {
        "acl_doc": [f"group:bucket-{document % 100}"],
        "acl_section": ["user:analyst"] if document % 2 else ["*"],
        "acl_para": ["group:approved"] if paragraph % 2 else ["*"],
    }


def allowed(payload, principals):
    """Independent authored-policy oracle: wildcard or exact match at EVERY level."""
    return all(
        isinstance(payload.get(level), list)
        and bool(payload[level])
        and ("*" in payload[level] or bool(set(payload[level]) & set(principals)))
        for level in ("acl_doc", "acl_section", "acl_para")
    )


def text_for(document, paragraph):
    return (
        f"Policy topic{document % 1000} clause{paragraph} insurance coverage "
        "claims premium deductible exclusions renewal review disclosure."
    )


def memory_snapshot(container):
    if not container:
        return None
    result = subprocess.run(
        ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.current"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {"container_cgroup_bytes_including_page_cache": int(result.stdout)}


def run(args):
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    # Never use default databases or indexes. Modules are imported after setting env.
    with tempfile.TemporaryDirectory(prefix="permrag-scale-") as temp:
        os.environ["DATABASE_URL"] = f"sqlite:///{temp}/mirror.db"
        os.environ["PERMISSIONS_SOURCE"] = f"{temp}/permissions.jsonl"
        sys.path.insert(0, str(ROOT / "platform"))
        from app.embeddings import embed_one

        from app import config, store, sync, vectorstore

        config.COLLECTION = "scale-" + uuid.uuid4().hex
        config.QDRANT_URL = args.url
        remote = QdrantClient(url=args.url, timeout=180)
        vectorstore.client = lambda: remote
        store.init_db()
        report = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "runtime_source_sha256": {
                str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((ROOT / "platform" / "app").rglob("*.py"))
            },
            "documents": args.documents,
            "chunks_per_document": args.chunks,
            "points": args.documents * args.chunks,
            "method": {
                "seed": 7,
                "dimension": config.EMBED_DIM,
                "batch_size": args.batch,
                "transport": "HTTP",
                "server": remote.info().version,
                "client": importlib.metadata.version("qdrant-client"),
                "scope": "streamed engine ingest; actual sync_once; search only (no API/audit/generation)",
                "text": "synthetic repeated policy templates; cached actual hash embeddings",
                "sql_mirror": "temporary SQLite populated with all chunk ACL and policy rows",
                "latency": "sequential warm server queries; uncached client, includes HTTP; 5 warmups per bucket",
                "resource_limits": "caller-owned container; recorded externally",
                "targets": {"retrieval_p95_ms": 100, "folder_sync_seconds": 60},
            },
        }
        try:
            vectorstore.reset_collection()
            started = time.perf_counter()
            template_vectors = {}
            digest = hashlib.sha256()
            chunks, acl_rows, policy_rows = [], [], []

            def flush():
                vectorstore.upsert_chunks(chunks)
                with store.engine.begin() as conn:
                    conn.execute(store.ChunkACL.__table__.insert(), acl_rows)
                    conn.execute(store.ChunkPolicy.__table__.insert(), policy_rows)
                chunks.clear()
                acl_rows.clear()
                policy_rows.clear()

            for document in range(args.documents):
                for paragraph in range(args.chunks):
                    number = document * args.chunks + paragraph
                    payload = policy_for(document, paragraph)
                    text = text_for(document, paragraph)
                    if text not in template_vectors:
                        template_vectors[text] = embed_one(text)
                    chunks.append(
                        {
                            "id": number,
                            "doc_id": f"doc-{document}",
                            "text": text,
                            "acl": [],
                            **payload,
                            "vector": template_vectors[text],
                        }
                    )
                    acl_rows.append({"chunk_id": number, "doc_id": f"doc-{document}", "acl": []})
                    policy_rows.append(
                        {
                            "chunk_id": number,
                            "doc_acl": payload["acl_doc"],
                            "section_acl": payload["acl_section"],
                            "paragraph_acl": payload["acl_para"],
                        }
                    )
                    digest.update(json.dumps([number, text, payload], sort_keys=True).encode())
                    if len(chunks) >= args.batch:
                        flush()
                if document and document % 5000 == 0:
                    print(f"ingested {document}/{args.documents} documents", flush=True)
            if chunks:
                flush()
            report["ingest_seconds_including_sql_mirror"] = time.perf_counter() - started
            report["corpus_sha256"] = digest.hexdigest()
            with store.permission_transaction() as (_, state):
                state.pending = False
                state.rebuild_required = False
                state.revision += 1
            # Wait for background optimizers to settle; do not claim indexed results early.
            started = time.perf_counter()
            while True:
                info = remote.get_collection(config.COLLECTION)
                if str(info.status) == "green" and info.indexed_vectors_count >= report["points"] * 0.95:
                    break
                if time.perf_counter() - started > args.optimizer_timeout:
                    break
                print(
                    f"optimizer: {info.status}; indexed {info.indexed_vectors_count}/{report['points']}",
                    flush=True,
                )
                time.sleep(5)
            report["optimizer_wait_seconds"] = time.perf_counter() - started
            report["collection_before_measurement"] = {
                "status": str(info.status),
                "indexed_vectors": info.indexed_vectors_count,
                "points": info.points_count,
                "config": info.config.model_dump(mode="json"),
            }
            report["memory_before_queries"] = memory_snapshot(args.container)
            buckets = {}
            leaks = probes = positive = 0
            for percent in (1, 10, 60):
                principals = ["*", "user:analyst", "group:approved"] + [
                    f"group:bucket-{i}" for i in range(percent)
                ]
                predicate = Filter(
                    must=[
                        FieldCondition(key=level, match=MatchAny(any=principals))
                        for level in vectorstore.ACL_LEVELS
                    ]
                )
                visible = remote.count(config.COLLECTION, count_filter=predicate, exact=True).count
                samples = []
                for i in range(args.queries + 5):
                    # Queries span accessible and inaccessible topic IDs.
                    document = (i * 7919) % args.documents
                    paragraph = i % args.chunks
                    query = template_vectors[text_for(document, paragraph)]
                    started = time.perf_counter()
                    hits = vectorstore.search(query, principals, 4)
                    elapsed = (time.perf_counter() - started) * 1000
                    if i >= 5:
                        samples.append(elapsed)
                        probes += 1
                        positive += bool(hits)
                        for hit in hits:
                            doc = int(hit["doc_id"].removeprefix("doc-"))
                            para = hit["chunk_id"] % args.chunks
                            # Compare returned IDs against original generator, not returned ACLs.
                            leaks += not allowed(policy_for(doc, para), principals)
                buckets[str(percent)] = {
                    "visible_points": visible,
                    "visible_fraction": visible / report["points"],
                    "samples": len(samples),
                    "p50_ms": statistics.median(samples),
                    "p95_ms": percentile(samples, 0.95),
                    "target_met": percentile(samples, 0.95) < 100,
                    "raw_ms": samples,
                }
                print(f"selectivity {percent}%: p95 {buckets[str(percent)]['p95_ms']:.2f} ms", flush=True)
            report["retrieval"] = buckets
            report["security"] = {
                "query_probes": probes,
                "unauthorized_chunks": leaks,
                "nonempty_query_results": positive,
                "not_semantic_recall": True,
            }
            if leaks or positive != probes:
                raise AssertionError("leak or vacuous-denial control failed")
            # Principal with no document grant must receive zero hits.
            denied = vectorstore.search(next(iter(template_vectors.values())), ["*", "user:outsider"], 4)
            if denied:
                raise AssertionError("outsider control leaked")
            report["security"]["outsider_empty_control"] = True
            changes = {}
            # Real sync traverses and reconciles the full SQL mirror, not just the changed folder.
            for label, docs in (("one_document", [0]), ("folder", list(range(min(10000, args.documents))))):
                Path(config.PERMISSIONS_SOURCE).write_text(
                    "\n".join(json.dumps({"doc_id": f"doc-{d}", "acl": []}) for d in docs)
                )
                started = time.perf_counter()
                changed = sync.sync_once()
                elapsed = time.perf_counter() - started
                count = remote.count(
                    config.COLLECTION,
                    count_filter=Filter(
                        must=[
                            FieldCondition(key="doc_id", match=MatchAny(any=[f"doc-{d}" for d in docs])),
                            FieldCondition(
                                key="acl_doc", match=MatchAny(any=[f"group:bucket-{i}" for i in range(100)])
                            ),
                        ]
                    ),
                    exact=True,
                ).count
                if count:
                    raise AssertionError("revoked folder still grants access")
                changes[label] = {
                    "requested_documents": len(docs),
                    "changed_documents": len(changed),
                    "sync_seconds": elapsed,
                    "remaining_granted_points": count,
                }
                print(f"{label} reconciliation: {elapsed:.2f} s", flush=True)
            changes["folder"]["target_met"] = changes["folder"]["sync_seconds"] < 60
            report["revocation"] = changes
            report["memory_after_reconciliation"] = memory_snapshot(args.container)
            report["harness_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
                1 if sys.platform == "darwin" else 1024
            )
            report["limitations"] = [
                "Not production ingest_corpus or API end-to-end performance",
                "Repeated synthetic texts and lexical hash embeddings; no semantic recall claim",
                "Container cgroup memory includes file cache; not isolated quantized-vector RAM",
                "Single host/run; no concurrency or cold-cache guarantee",
                "SQL sync scans all mirror rows",
            ]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            return report
        finally:
            remote.delete_collection(config.COLLECTION)
            remote.close()
            store.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--documents", type=int, default=100000)
    parser.add_argument("--chunks", type=int, default=15)
    parser.add_argument("--batch", type=int, default=512)
    parser.add_argument("--queries", type=int, default=75)
    parser.add_argument("--optimizer-timeout", type=int, default=600)
    parser.add_argument("--container", help="Owned local Docker container for memory readings")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.documents, args.chunks, args.batch, args.queries) < 1:
        parser.error("counts must be positive")
    run(args)


if __name__ == "__main__":
    main()
