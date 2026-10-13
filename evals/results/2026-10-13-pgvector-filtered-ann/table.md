# pgvector filtered-ANN recall under RLS, 2026-10-13

100,000 chunks, dim 256, 200 clusters; 200 queries per cell, k=10; pgvector 0.8.7, PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2); HNSW m=16 ef_construction=64. Recall is against exact search through the same `rag_search`. Leaks are checked against an independent ACL oracle. Embeddings are synthetic (clustered Gaussians), visibility is independent of the embedding, one connection, no concurrency.

| visibility (measured) | config | recall@k | min recall | mean results (exact) | empty answers | p50 ms | p95 ms | leaks |
|---|---|---|---|---|---|---|---|---|
| 1% (0.97%) | shipped (no ANN index) | 1.000 | 1.00 | 10.00 (10.00) | 0 | 55.1 | 62.4 | 0 |
| 1% (0.97%) | exact (index present, indexscan off) | 1.000 | 1.00 | 10.00 (10.00) | 0 | 58.5 | 66.4 | 0 |
| 1% (0.97%) | HNSW ef_search=40 (default), planner's choice | 1.000 | 1.00 | 10.00 (10.00) | 0 | 54.8 | 59.4 | 0 |
| 1% (0.97%) | HNSW ef_search=40, forced | 0.043 | 0.00 | 0.44 (10.00) | 124 | 54.6 | 60.2 | 0 |
| 1% (0.97%) | HNSW ef_search=200, forced | 0.192 | 0.00 | 1.93 (10.00) | 29 | 55.0 | 59.8 | 0 |
| 1% (0.97%) | HNSW ef_search=1000, forced | 0.826 | 0.10 | 8.65 (10.00) | 0 | 63.9 | 71.6 | 0 |
| 1% (0.97%) | HNSW iterative strict_order, forced | 0.771 | 0.00 | 10.00 (10.00) | 0 | 64.8 | 81.8 | 0 |
| 1% (0.97%) | HNSW iterative relaxed_order, forced | 0.893 | 0.50 | 10.00 (10.00) | 0 | 63.4 | 73.0 | 0 |
| 10% (10.11%) | shipped (no ANN index) | 1.000 | 1.00 | 10.00 (10.00) | 0 | 56.9 | 61.9 | 0 |
| 10% (10.11%) | exact (index present, indexscan off) | 1.000 | 1.00 | 10.00 (10.00) | 0 | 59.0 | 65.3 | 0 |
| 10% (10.11%) | HNSW ef_search=40 (default), planner's choice | 1.000 | 1.00 | 10.00 (10.00) | 0 | 60.3 | 66.2 | 0 |
| 10% (10.11%) | HNSW ef_search=40, forced | 0.381 | 0.00 | 3.89 (10.00) | 5 | 56.2 | 61.8 | 0 |
| 10% (10.11%) | HNSW ef_search=200, forced | 0.990 | 0.00 | 9.99 (10.00) | 0 | 57.0 | 64.1 | 0 |
| 10% (10.11%) | HNSW ef_search=1000, forced | 0.995 | 0.90 | 10.00 (10.00) | 0 | 63.7 | 71.9 | 0 |
| 10% (10.11%) | HNSW iterative strict_order, forced | 0.956 | 0.00 | 10.00 (10.00) | 0 | 57.3 | 65.5 | 0 |
| 10% (10.11%) | HNSW iterative relaxed_order, forced | 0.973 | 0.00 | 10.00 (10.00) | 0 | 57.5 | 62.6 | 0 |
| 60% (60.06%) | shipped (no ANN index) | 1.000 | 1.00 | 10.00 (10.00) | 0 | 73.2 | 76.4 | 0 |
| 60% (60.06%) | exact (index present, indexscan off) | 1.000 | 1.00 | 10.00 (10.00) | 0 | 71.2 | 77.5 | 0 |
| 60% (60.06%) | HNSW ef_search=40 (default), planner's choice | 1.000 | 1.00 | 10.00 (10.00) | 0 | 71.7 | 77.9 | 0 |
| 60% (60.06%) | HNSW ef_search=40, forced | 0.961 | 0.00 | 10.00 (10.00) | 0 | 61.9 | 68.5 | 0 |
| 60% (60.06%) | HNSW ef_search=200, forced | 0.990 | 0.10 | 10.00 (10.00) | 0 | 63.1 | 69.0 | 0 |
| 60% (60.06%) | HNSW ef_search=1000, forced | 0.996 | 0.80 | 10.00 (10.00) | 0 | 68.6 | 73.7 | 0 |
| 60% (60.06%) | HNSW iterative strict_order, forced | 0.961 | 0.00 | 10.00 (10.00) | 0 | 62.6 | 70.0 | 0 |
| 60% (60.06%) | HNSW iterative relaxed_order, forced | 0.961 | 0.00 | 10.00 (10.00) | 0 | 62.7 | 70.2 | 0 |

Shipped path cost split (median ms, no index, as `rag_definer` under each user's RLS):

- 1%: `count(*)` for denied_chunks 26.38 ms; ORDER BY ... LIMIT k 27.09 ms
- 10%: `count(*)` for denied_chunks 28.33 ms; ORDER BY ... LIMIT k 29.42 ms
- 60%: `count(*)` for denied_chunks 34.57 ms; ORDER BY ... LIMIT k 35.45 ms

Planner choice for the inner top-k query (as `rag_definer`, user u1, first query):

- shipped (no ANN index): `->  Seq Scan on chunks c  (cost=0.00..18470.00 rows=1 width=14)`
- exact (index present, indexscan off): `->  Seq Scan on chunks c  (cost=0.00..18470.00 rows=1 width=14)`
- HNSW ef_search=40 (default), planner's choice: `->  Seq Scan on chunks c  (cost=0.00..18470.00 rows=1 width=14)`
- HNSW ef_search=40, forced: `->  Index Scan using chunks_embedding_hnsw on chunks c  (cost=463.63..136072.00 rows=1 width=14)`
- HNSW ef_search=200, forced: `->  Index Scan using chunks_embedding_hnsw on chunks c  (cost=1608.29..136072.00 rows=1 width=14)`
- HNSW ef_search=1000, forced: `->  Index Scan using chunks_embedding_hnsw on chunks c  (cost=6276.06..136072.00 rows=1 width=14)`
- HNSW iterative strict_order, forced: `->  Index Scan using chunks_embedding_hnsw on chunks c  (cost=463.63..136072.00 rows=1 width=14)`
- HNSW iterative relaxed_order, forced: `->  Index Scan using chunks_embedding_hnsw on chunks c  (cost=463.63..136072.00 rows=1 width=14)`
