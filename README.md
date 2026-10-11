# HTT Scanner — CodeRabbit Engineering Review

**PRIVATE / SOURCE-ONLY / DO NOT DEPLOY DIRECTLY**

This repository is a sanitized engineering review workspace for the Hold The Throne textile scanner. Its source snapshot originates from a local scanner copy and **must be reconciled against the currently running immutable Azure release before any deployment**. It does not contain production databases, credentials, evidence captures, WordPress configuration, or customer data.

## Mission

Fix the scanner as a complete state machine: rapid and reliable product discovery, verified fibre composition extraction, safe scoring, checkpoint-safe bulk operation, bounded retries, reports, and crash recovery. An acceptable final result means zero unexplained failures, zero stuck runs, zero duplicate writers, no lost checkpoints, every catalogue item accounted for, and no fabricated numerical scores. Legitimately absent or contradictory external evidence must result in auditable terminal non-score outcomes, not invented scores.

## Immediate CodeRabbit review

See [ENGINEERING_BRIEF.md](ENGINEERING_BRIEF.md) for incidents and acceptance tests. Review scanner/*.py for correctness, security, source identity, data integrity, throughput and resume logic. Produce concrete PRs with deterministic regression tests and benchmarks. Treat immutable scanner fingerprint compatibility as a hard constraint.

**Publication is out of scope.** Never remove the seven bulk STOP flags, publish to WordPress, change existing production scores, or alter images, SEO, affiliates or posts. Work in review branches.

## Repository caveat

Do not confuse this snapshot with the running Azure code. Verify source fingerprints and changelogs before merging any code into a live release.
