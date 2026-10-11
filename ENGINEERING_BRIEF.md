# Complete HTT Scanner Engineering Repair Brief

**Review target:** Fast, correct, recoverable offline textile scanning and composition scoring, with a fully accounted-for 2,645-brand queue. This history describes incidents, not confirmed fixes. Verify each against live source and saved evidence.

## Known incidents and bottlenecks

1. **Very low brand completion:** Days of running produced three verified new brand receipts: Aigle 1/5 (ID 207671, run 129eb49f-6a10-4f47-a6d1-2fdbc51f7540), BALR. 1/5 (ID 207835, run 06b250f6-1c77-4fed-ad2c-805531cc4bf4), Swim Good 4/5 (nine products, run b411b1f5-ba65-4613-921d-5390241f0665). Per-product successes alone are not brand completion.
2. **Budget exhaustion:** 15-minute first-pass and limited revisit visits leave dozens of runs DEFERRED/site_pass_budget. Existing revisit scheduler interleaves limited visits; test actual fairness, liveness and eventual completion.
3. **Massive catalogues:** 370, 1,156, and 9,535-product catalogues, thousands of pending items. Avoid recrawling confirmed products or starving smaller brands. Benchmark products/second and verified brands/hour.
4. **Browser-first bottleneck:** Prior queue forced capture_transport=browser for all brands, despite fast official HTTP/Shopify JSON sources. Direct-http-first with rendered fallback must preserve product identity, provenance and evidence safety. Avoid browser for confirmed APIs where justified.
5. **Renderer route error:** Offsite third-party subresource errors were previously treated like forbidden offsite navigation; isolated fix used request.resource_type == "document" and request.url == url to distinguish. Regression-test all redirect policies.
6. **Browser hangs and timeouts:** Playwright cancellation/route errors, 30-second timeouts, aborted visits. Ensure cleanup and bounded retries; no hangs.
7. **UnicodeEncodeError:** Long-running shard services W3/W4 failed even with PYTHONUTF8 and PYTHONIOENCODING hints. Locate underlying writer/encoding exception and test non-ASCII brand names.
8. **Extractor unresolved versus genuinely undisclosed:** Official pages sometimes contain only material names without percentages. Categorize VERIFIED_UNDISCLOSED, PARTIAL_COMPOSITION, TECHNICAL_FAILURE and EVIDENCE_CONFLICT accurately. No guessed fractions.
9. **Misleading page text extraction:** WAMA evidence had valid product composition snippets (e.g. 55% hemp / 45% cotton) but document-length parsing generated false composition totals/conflicts. Isolate product-owned evidence and normalize component percentages.
10. **Product source provenance:** Shopify JSON fallback needs verified product-specific source endpoint and response SHA; many existing runs lack these fields. Do not invent source hashes or trust titles as composition disclosures.
11. **HTTP/redirect security:** Offsite redirects, 429, HTTP 500, blocked/challenge pages, unknown domain and canonical product identity; never bypass source mismatch or SSRF protections. Example unlesscollective.com redirects to Under Armour.
12. **Mixed departments:** Clothing, accessories, non-textile items and uncertain categories must be scoped reliably; report explicit classified outcomes, not indefinite unresolved department.
13. **Catalogue discovery:** No confirmed products, stale product IDs, collection changes, duplicate candidates, sitemap cross-checks, catalogue-drop anomaly, JSON pagination and Shopify variant identity.
14. **Reporting destination:** report_destination_unresolved and audit export issues block otherwise useful runs. Offline scan receipt must be produced regardless of WordPress publishing.
15. **Checkpoint integrity:** Four SQLite shards W1–W4; one writer per shard; no duplicate jobs, no partial state loss, and no fingerprint mismatch. Never alter the original immutable source used by saved run IDs.
16. **State machine:** RUNNING, RETRY, DEFERRED, REPAIR_REQUIRED, SCANNER_COMPLETE must converge to VERIFIED_SCORE, VERIFIED_UNDISCLOSED or justified REVIEW_HOLD; never leave retriable errors stuck forever or silently call incomplete complete.
17. **Worker recovery:** Durable automatic restart and revisit timer, valid saved policy, state leases, lock ownership, recovery after signal/crash, and bounded job concurrency.
18. **Publication isolation:** WordPress is separate, existing scores/posts/SEO/images/affiliates preserved, seven STOP flags active; publish only by separate explicit authorization after receipt and parity checks. No automated production writes in this review.
19. **Infrastructure isolation:** htt-scanner-01 compute should not starve htt-web-01 public frontend, mapper or mobile app. Bounded CPU/memory/network pressure.
20. **Test environment:** Correct Playwright browser path /home/azureuser/.cache/ms-playwright. A test command timed out under Azure RunCommand; use managed async test jobs. 131 tests passed in one extensive suite, but do not substitute unit tests for full catalogue acceptance.

## Required acceptance tests

- Deterministic fixture test for each failure and success category; measured false-positive and false-negative score rates.
- Real controlled sample across Shopify, HTML, JS-rendered, blocked, redirect, large mixed catalogue and partial disclosure sites.
- Resource/latency benchmarks: pages/minute, products/minute, complete verified brands/hour, retries, browser usage, failure state rates, queue drain ETA.
- Repeated crash, pause, restart and checkpoint replay: same product identity and score evidence; no duplicate writes.
- Full queued brand reconciliation: each product/account has source receipt, terminal disposition or due checkpoint, with traceable next action and no unbounded loop.
- Verified numerical canonical_score plus SCANNER_COMPLETE, integrity PASS, audit_export_status PASS.
- Only authorized domains and official product identity; never score conflicting or insufficient evidence.
- No WordPress publication in tests or workers; seven STOP flags unchanged.
- Source release fingerprint reconciled with Azure before any rollout.

## CodeRabbit instructions

Report critical defects first with exact file/line evidence. Propose focused PRs with tests, rather than superficial “healthy workers” reports. Differentiate software bugs from manufacturer nondisclosure. Never mark all brands numerically scored by coercing missing evidence. Keep review output actionable, reproducible and safe.
