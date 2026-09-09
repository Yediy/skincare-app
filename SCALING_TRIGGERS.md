# Scaling Triggers

**Commit this document describes:** see the commit this pass ends on.

Metrics that indicate when the architecture should advance to its next stage — not vague feelings, and not arbitrary round numbers picked without evidence. Every trigger below is a *reason to start the next piece of work*, not a reason to have already built it (`PRODUCTION_ARCHITECTURE.md` principle 7; the brief this pass implements calls this out explicitly: "No arbitrary numbers without evidence").

None of these have been measured against real production traffic yet — this repository has no production traffic. These are the metrics to start measuring (Phase 28, deferred this pass) and the thresholds to treat as a real signal once they exist, not claims that any of them have already fired.

## Add API replicas when

- Sustained CPU on the current replica count exceeds ~70% for a sustained window (not a momentary spike) — headroom matters because `pipeline.analyze()` runs synchronously and can spike CPU hard per-request.
- p95 request latency on `/analyze` exceeds an agreed SLO (not yet set — this needs a real number from `PLAN_SERVICE`/pipeline profiling, not a guess).
- Concurrent in-flight `/analyze` requests approach what one replica's CV throughput can sustain (needs real measurement once `analysis duration` is an actual metric — Phase 28).

This is the cheapest scaling lever in the whole architecture (`PRODUCTION_ARCHITECTURE.md` principle 4 exists specifically so this stays true) — reach for it first.

## Extract CV workers into their own pool when

- `/analyze` load materially affects the latency of *unrelated* endpoints on the same replica (e.g. `/login` p95 degrading because the event loop is busy with synchronous CV work) — this is the specific, concrete symptom of the coupling `FAILURE_DOMAINS.md`'s CV-worker section already flags as a live risk today, not a hypothetical.
- Or: CV compute needs different hardware (more CPU cores, or eventually GPU) than the HTTP-serving replicas need — provisioning API replicas identically to CV-heavy replicas becomes wasteful once this is true.

The domain boundary and queue abstraction this now needs already exist (`app/domain/analysis_service.py`, `app/queue/`) — extracting a CV worker at that point means writing the worker process and wiring `/analyze` to enqueue instead of calling the pipeline inline, not building the queue itself from scratch. Doing that wiring before either symptom above is real would still mean paying a real API-contract change (submit → poll, instead of a synchronous response) for zero measured benefit — exactly the overbuilding this pass's own brief warns against.

## Add a Postgres read replica or PgBouncer when

- **PgBouncer**: connection count from API replicas approaches Postgres's `max_connections` — this is a per-replica multiplier (`db_pool_max_size`, `app/config.py`) times replica count, so it becomes relevant as soon as API replica count grows past a handful, not at "billions of users." Deploy per the caveat in `PRODUCTION_ARCHITECTURE.md`'s PgBouncer section (`statement_cache_size=0` or session-mode pooling) — this is the more urgent of the two triggers because it's driven by replica *count*, which Phase 1 above already makes cheap to increase.
- **Read replica**: read-query load measurably contends with write latency on the primary, or the recovery-time requirement drops below what `POSTGRES_OPERATIONS.md`'s restore-from-backup can deliver (a warm streaming replica promotes in seconds; a restore takes as long as the last drill measured).

## Begin the first cell split when

- Regional latency to a real, geographically distant user base becomes measurably worse than an acceptable SLO — this requires the application to actually have users outside whatever region it launches in, not a launch-day speculation.
- Or: data-residency/jurisdiction requirements force it (a real legal constraint, not an architectural preference).
- Or: a single Postgres primary's size/throughput approaches what one instance can serve, even after read replicas and connection pooling.
- Or: blast-radius reduction becomes a real operational requirement — i.e., `FAILURE_DOMAINS.md`'s "Postgres primary" entry (currently: total outage, the largest single failure domain in this architecture) needs to shrink because the cost of that outage has grown past what a single-cell architecture can tolerate.

Each of these is independently sufficient justification — cell-splitting is not a single-threshold decision, and none of them should be treated as satisfied by user-count alone. "We might reach a billion users" is not, by itself, one of these triggers; a specific, measured symptom is.

## What NOT to scale for yet (per this pass's own brief, Phase 35)

Kubernetes, Kafka/Pulsar, a service mesh, dozens of microservices, a globally distributed SQL layer, multi-cloud active-active, GPU clusters, or a fleet of databases are all real answers to real problems this application does not have measured evidence of yet. Each has its own trigger, implied above (queue product choice is a Phase 15 decision once a queue is actually justified; a distributed SQL layer is a decision for if/when a single Postgres primary plus read replicas plus PgBouncer plus cell-splitting is provably insufficient, which is a very long way past where this application is today). Introducing any of them without a measured trigger like the ones above is exactly the complexity tax this document exists to keep out of the launch architecture.
