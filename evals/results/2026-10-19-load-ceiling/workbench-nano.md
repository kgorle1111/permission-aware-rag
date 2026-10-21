# Demo workbench under Lightsail Nano limits (2026-10-19)

`deploy/aws/Dockerfile` image (main at feecc4f), run natively (arm64) with Nano's limits:
`docker run --memory 512m --cpus .25 -e SHOW_DENIED=1`. Client: `evals/load/workbench_load.py`, 20 s per cell,
random role and unique query per request. Laptop load average 5-8 during the run, so treat cells as noisy (about 2x).

| VUs | req/s | p50 ms | p95 ms | p99 ms | errors |
|---|---|---|---|---|---|
| 1 | 243.2 | 3.4 | 6.1 | 17.5 | 0 |
| 5 | 155.3 | 14.9 | 96.2 | 107.4 | 0 |
| 10 | 197.1 | 45.0 | 82.1 | 92.2 | 0 |
| 25 | 169.8 | 127.0 | 207.4 | 419.3 | 0 |
| 50 | 175.3 | 285.5 | 409.0 | 741.4 | 0 |

Container memory after the run: 42 MiB of 512 MiB.

Not established: Lightsail's real shared vCPU is not the same as a `--cpus .25` quota on this laptop, and an
amd64 run under emulation here was about 10x slower and is not reported. The live endpoint was not load-tested.
