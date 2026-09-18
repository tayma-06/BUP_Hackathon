# Changes from the uploaded modules

| Original issue | Change |
| --- | --- |
| Flat files used package-relative imports | Added an `app` package, root launcher and complete project layout |
| Missing dependencies/configuration/start instructions | Added pinned requirements, `.env.example`, Windows launcher and quickstart |
| LLM outage became a regex interpretation or invented no_op | Only validated real-model interpretations enter the scheduling path; failures return controlled errors |
| Optimizer dropped directives to find an easier plan | Infeasibility is explicit; every accepted hard constraint must hold |
| Failed self-check was logged but still returned HTTP 200 | Failed replay now blocks successful responses |
| Replay only tested the subset of retained constraints | Independent replay checks all reported constraints; sample tests use organizer ground truth |
| 0.5% reserve could be treated as 50% | Percent units are interpreted literally; no implicit fractional-percent guess |
| Model note indices were guessed/coerced/shifted | Exact integer mapping is required; duplicates and missing indices are rejected |
| Cache depended only on lowercased note text | Full exact note context and all battery parameters form the cache key |
| Tiny cycling penalty could distort the true cost optimum | Pure cost objective with positive scaling; simultaneous flows are netted afterward |
| Provider retries could reuse full timeout repeatedly | Per-attempt wall-clock limits and a shared request budget |
| Rate-limit errors were retried immediately despite Retry-After | Provider cooldowns, bounded backoff and ready-backup selection within the interpretation deadline |
| Editor history could put credential copies into Git | Root and application ignore rules, Docker history exclusions, and removal of tracked snapshots from the index; committed credentials still require rotation |
| Clone instructions assumed application files were at repository root | Root README, quickstart and deployment instructions use the gridwise subfolder |
| Missing tests, sample runner and submission guidance | Added automated checks, independent cost oracle, live/offline sample runner, Docker files and submission/video guides |

`llm_client.py` and `llm_client (1).py` in the upload were identical. The project
uses one canonical `app/llm_client.py`. The original `rule_fallback.py` is not
included in the running application because rule-only interpretation does not
satisfy the mandatory LLM requirement.
