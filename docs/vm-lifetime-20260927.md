# Same-VM lifetime extension

At `2026-09-27T21:19:52Z`, the existing pool and claim were updated in place and independently fetched again using `get_pool` and `list_claims`.

| Setting | Read-back value |
| --- | --- |
| Pool/namespace | `ctrlloop-omarchy-20260927-1708` |
| Claim | `omarchy-trial` |
| Bound sandbox | `ctrlloop-omarchy-20260927-1708-e33048ae` |
| Pool original creation | `2026-09-27T17:08:51Z` |
| Pool creation-age TTL | `274261` seconds |
| Pool resulting expiry | `2026-09-30T21:19:52Z` |
| Claim lifecycle shutdown | `2026-09-30T21:19:52Z` |
| Claim shutdown policy / auto renew | `Delete` / `false` |
| Claim phase / pool replicas | `Bound` / `1` |

Both effective deadlines are **September 30, 2026, 2:19:52 PM PDT**, 72 hours after the extension. No VM was recreated, restarted, released, suspended, or replaced. The image, template, replica counts, disk, and desktop repairs were preserved.

The claim still displays its original `ttl_seconds_after_created=21600`. This field originally initialized the lifecycle; it is not a second earlier deadline after renewal. The installed pinned `cua-fleet==0.1.17` SDK's `renew_claim` documentation says lifecycle shutdown is the only liveness input honored by the claim reaper. The [public SDK implementation](https://github.com/trycua/cua/blob/main/libs/fleet/sdk/src/claims.rs#L178) documents the same contract and PATCHes only `spec.lifecycle.shutdownTime`. The [claim lifecycle schema](https://github.com/trycua/cua/blob/main/libs/fleet/sdk-schema/src/claim.rs) describes deletion after this absolute time. The [official expiry guide](https://cua.ai/docs/how-to-guides/sandbox/expire-pools-and-claims) explains that the TTL fills in a lifecycle deadline and an explicit lifecycle is retained. The SDK intentionally exposes no general claim-spec update, so the original initialization field was preserved.

This verification covers live API readback and the provider's published SDK/schema contract. The deployed controller binary is not available for independent inspection. The raw sanitized readback is retained locally in `.local/mvp/lifetime-verified.json`. Expiry is a provider deletion deadline, not a durable backup; data needed beyond it must be exported.
