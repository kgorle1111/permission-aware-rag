# Qdrant nested-prefetch filter propagation (2026-10-13)

Question: when a hybrid query nests an **unfiltered** dense candidate stage under a filtered sparse
rescore, does the engine still keep hidden rows out of the candidate stage?

Setup: qdrant-client 1.19.1; server `qdrant/qdrant:v1.19.1` (docker, localhost); 2 visible points
(`pub`), 6 hidden points (`secret`) that are dense-closer and sparse-stronger than every visible one.

```
local  inner_filter=True  with_hidden=[(1, 1.0)] no_hidden=[(1, 1.0)] invariant=True
local  inner_filter=False with_hidden=[(1, 1.0)] no_hidden=[(1, 1.0)] invariant=True
local  unfiltered dense top-2 = [100, 101]; nested result for 'pub' = [1] => PROPAGATED into the candidate stage
server inner_filter=True  with_hidden=[(1, 1.0)] no_hidden=[(1, 1.0)] invariant=True
server inner_filter=False with_hidden=[(1, 1.0)] no_hidden=[(1, 1.0)] invariant=True
server unfiltered dense top-2 = [100, 101]; nested result for 'pub' = [1] => PROPAGATED into the candidate stage
```

**Result.** Both engines propagate the outer filter into the nested candidate stage. The control
shows the unfiltered dense top-2 are hidden rows (100, 101), yet the nested query for `pub` returns
only visible chunk 1. Hybrid results are identical with and without hidden rows, with or without the
inner filter.

**What this does not show.** Other Qdrant versions or distributed deployments. The platform does not
rely on this behaviour: `vectorstore.search` sends the ACL filter at every prefetch level, and a
request-shape test asserts it.
