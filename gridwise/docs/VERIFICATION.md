# Verification record

Prepared and checked on 2026-09-18 using Python 3.12 on Linux.

| Check | Result | What it establishes |
| --- | --- | --- |
| Install all pinned runtime and development dependencies into a new venv | PASS | Reproducible dependency installation on this Python/Linux environment |
| `python -m pip check` in that clean venv | PASS | No dependency conflicts reported |
| `python -m pytest -q` in that clean venv | 85 passed, 2 deprecation warnings | API/guardrail/optimizer behavior, mocked provider wiring, error handling and independent cost checks |
| Official public samples, supplied ground-truth interpretations | 10/10 PASS | Valid schedules and reference-optimal costs for all supplied cases |
| Real `python run.py` startup and local HTTP requests | PASS | Launcher works; malformed JSON returns 400; absent provider produces controlled not-ready/model errors |
| Live LLM interpretation with a real account | NOT RUN | Needs your API key or reachable local model |
| Public service deployment and external access | NOT RUN | Needs a hosting account and configured service |
| Docker image build/pull/run | NOT RUN | Docker is not installed in this preparation environment |
| Recorded three-minute video | NOT CREATED | Outline supplied for the team's recording |

The two warnings are deprecations in the installed Starlette/AnyIO test-client
integration; the tests pass. They are not suppressed. The runtime dependencies
are pinned to the versions actually installed and tested.

## Public sample costs

These are optimizer checks with the organizers' interpretations, not live LLM
predictions. Every returned plan also passed the independent replay validator.

| Case | Supplied cost (BDT) | Calculated cost (BDT) |
| --- | ---: | ---: |
| SAMPLE-01 | 38,365 | 38,365 |
| SAMPLE-02 | 42,885 | 42,885 |
| SAMPLE-03 | 35,480 | 35,480 |
| SAMPLE-04 | 40,495 | 40,495 |
| SAMPLE-05 | 33,950 | 33,950 |
| SAMPLE-06 | 34,090 | 34,090 |
| SAMPLE-07 | 38,550 | 38,550 |
| SAMPLE-08 | 37,665 | 37,665 |
| SAMPLE-09 | 34,873 | 34,873 |
| SAMPLE-10 | 41,620 | 41,620 |

Machine-readable records: `offline_results.json` and `test_results.xml` in
this directory. The small offline solver timings are not live API latency.

Run after setting your LLM configuration and starting the service:

```bash
python scripts/run_public_samples.py --base-url http://127.0.0.1:8000 --report reports/live.json
```

Then repeat against the deployed public URL from another machine. Hidden-test
accuracy and live response time cannot be inferred from mocked replies or
offline sample passes.
