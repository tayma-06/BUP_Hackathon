# GridWise LLM

An HTTP API for the BUP CSE Fest 2026 preliminary Smart Campus Energy
Optimization Challenge. It interprets 1-3 operator notes using a generative
language model, validates structured directives, and minimizes the 24-hour grid
electricity cost subject to every accepted constraint.

**Current status:** local solver and automated tests are supplied. Live model
accuracy, deployment reachability and the Docker image must be verified using
your own configured account/environment. See `START_HERE.md` for Windows steps
and `docs/VERIFICATION.md` for the recorded checks and their scope.

## Quickstart

Requires Python 3.12 and network access to your chosen model provider.
Start in the extracted project root, or clone your event-created repository:

```bash
git clone https://github.com/YOUR_ACCOUNT/YOUR_EVENT_REPOSITORY.git
cd YOUR_EVENT_REPOSITORY
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env privately: fill LLM_API_KEY and confirm provider/model.
python run.py
```

On Windows PowerShell, no environment activation is needed:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python.exe run.py
```

The server binds to `0.0.0.0`, uses `PORT` (default `8000`) and loads `.env`
without replacing already-defined environment variables. Keep one server
worker for this small workload and its in-memory cache.

## Endpoints and examples

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/optimize-energy \
  -H 'Content-Type: application/json' --data-binary @data/sample_request.json
```

On PowerShell, use `curl.exe` instead of `curl` and put the POST command on one
line. `data/sample_response.json` contains a locally calculated reference
response using the sample's supplied interpretation. It is not a recorded
live LLM response. Equivalent optimal battery schedules may differ.

| Endpoint/status | Behavior |
| --- | --- |
| `GET /health`, 200 | `{"status":"ok"}` after solver startup and LLM configuration |
| `GET /health`, 500 | No configured usable provider, or startup not ready |
| `POST /optimize-energy`, 200 | Exact required interpretation, plan, totals and summary fields |
| 400 | Malformed JSON or structural request error |
| 422 | Invalid physical values or infeasible interpreted hard constraints |
| 500 | Controlled model/solver/validation failure; no fabricated successful schedule |

`/health` does not make a paid model request. Check actual credentials and quota
with the live sample command. Response headers include `X-Interpreter-Sources`
(`llm:primary`, `llm:backup` or `cache:llm`), `X-Plan-Status` and `X-Self-Check`.
These headers are diagnostics; the JSON body follows the official contract.

## Configuration

The shipped example selects Groq's `openai/gpt-oss-120b`, listed in its
[supported models](https://console.groq.com/docs/models). Obtain a valid key
and confirm that model access/quota are available for your account. Provider
costs and quotas are your responsibility; this project makes no free-tier
availability guarantee.

| Variable | Meaning/default |
| --- | --- |
| `LLM_PROVIDER` | `groq`, `anthropic`, `gemini`, `openai`, `openrouter`, `ollama`, or `custom` |
| `LLM_MODEL` | Model ID from your provider; explicit for every preset except Groq |
| `LLM_API_KEY` | Required for hosted presets; optional only for Ollama/custom local servers |
| `LLM_BASE_URL` | Override preset URL; required for `custom`; include `/v1` when your server requires it |
| `LLM_EXTRA_BODY` | Optional JSON object; example uses `{"reasoning_effort":"low"}` for GPT-OSS |
| `BACKUP_LLM_PROVIDER`, `BACKUP_LLM_MODEL`, `BACKUP_LLM_API_KEY`, `BACKUP_LLM_BASE_URL`, `BACKUP_LLM_EXTRA_BODY` | Optional independent real-model fallback |
| `LLM_TIMEOUT_S` | Per-provider-attempt wall-clock limit, default 8 seconds, maximum 12 |
| `LLM_MAX_TRIES` | Attempts per provider, default 2, maximum 4 |
| `INTERPRET_BUDGET_S` | Shared interpretation deadline, default 20 seconds, maximum 22 |
| `NOTE_CACHE_SIZE` | LRU entries, default 2048; 0 disables caching |
| `PORT` | HTTP port, default 8000 |
| `LOG_LEVEL` | Logging level, default INFO |

When switching providers, clear `LLM_EXTRA_BODY` unless the new model supports
those parameters. The Anthropic preset uses `/v1/messages`; other presets use
OpenAI-compatible `/chat/completions`. Protocol handling is tested with mocked
HTTP responses, not with credentials for every vendor. Model availability must
be checked with the provider and the live tests.

For an already-running Ollama model, set `LLM_PROVIDER=ollama`,
`LLM_MODEL=<your-installed-model>`, leave the key blank and clear the extra body.
Within a container, `localhost` means that container; configure `LLM_BASE_URL`
to the actual reachable model host. A local model must meet the 30-second
judging timeout. A rule-based parser is not a compliant substitute.

## Architecture and LLM role

1. `schemas.py` validates exactly 24 unique hourly records, 1-3 non-empty
   notes, finite non-negative numbers, battery bounds and starting energy.
2. `interpreter.py` sends quoted notes and battery context to the LLM in one
   request. Its system prompt defines all six types, end-exclusive time
   windows, solar remaining fractions, percentages, distractors and units.
3. `guardrails.py` checks note-derived fields, supported types, applies
   semantics, time windows and numeric bounds. It expands windows, sorts and
   deduplicates hours, and converts model-extracted capacity percentages to
   kWh. Conflicting fields trigger a repair attempt.
4. Invalid entries are retried using their original indices and full note
   context. A configured backup model gets a turn before a second primary
   attempt. Every attempt shares the same bounded time budget.
5. `optimizer.py` converts accepted directives into LP bounds and solves the
   energy schedule with SciPy/HiGHS. It never drops a hard directive.
6. `validator.py` independently reconstructs the directive limits and replays
   all 24 rows. A failed replay blocks HTTP 200. Summary and totals are computed
   deterministically from the validated schedule.

Only accepted LLM answers are cached. Keys include the complete note list and
all battery context, avoiding stale percentage conversions or a note being
reused with different surrounding text. The API never loads test fixtures.
No regular-expression fallback is used. If all models fail, the API returns a
controlled error instead of silently making an important note `no_op`.

## Optimization model

For each hour, variables are grid import `g`, used solar `s`, battery charge
`c`, discharge `d`, and battery energy after the hour `e`. All are non-negative.

- Minimize `sum(tariff[h] * g[h])`.
- `g[h] + s[h] + d[h] = demand[h] + c[h]`.
- `e[h] = e[h-1] + c[h] - d[h]`, with `e[-1] = initial`.
- `0 <= s[h] <= effective_solar[h]`.
- Charge/discharge obey hourly rate limits and outage windows.
- Grid caps are upper bounds; reserve directives raise the battery lower bound.
- `e[23] = initial`, enforcing end-of-day neutrality.

The objective has no additive cycling penalty, so very small tariff differences
are preserved. A positive scale normalizes the cost vector. In the lossless
battery model, any simultaneous LP charge/discharge is netted into a single
action without changing energy balance, stored energy or grid cost. Curtailment
is allowed and export is forbidden. The solver uses a 3-second internal limit;
the post-parse request pipeline has a 27-second wall-clock budget. These are
limits, not guarantees about latency on an overloaded host.

## Testing

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/run_public_samples.py --offline --report reports/offline.json
python scripts/run_public_samples.py --base-url http://127.0.0.1:8000 --report reports/live.json
# Run again from a DIFFERENT machine using the deployed URL:
python scripts/run_public_samples.py --base-url https://YOUR_PUBLIC_HOST --repeat 2 --report reports/deployed.json
```

The offline sample command uses organizer interpretations, checks physical
validity and compares cost against all 10 reference optima. Automated tests
also exercise the API with mocked model replies, malformed requests, provider
outages, guardrails, caching and an independent discrete-state cost oracle.
Mocked model tests do not measure language understanding. The live command
checks extracted fields and independently replays the plan against the
**organizer's** directives, then checks cost. It exits nonzero on a failure and
reports sample latency. Explanation wording and the exact schedule are not
compared byte-for-byte.

## Docker and deployment

```bash
docker build -t gridwise:1.0.0 .
docker run --rm --name gridwise --env-file .env -e PORT=8000 -p 8000:8000 gridwise:1.0.0
```

From a second terminal, call health and run the live samples above. Then push
the tested image to your registry. For example, replacing `YOUR_DOCKERHUB_USER`:

```bash
docker login
docker tag gridwise:1.0.0 YOUR_DOCKERHUB_USER/gridwise:1.0.0
docker push YOUR_DOCKERHUB_USER/gridwise:1.0.0
docker pull YOUR_DOCKERHUB_USER/gridwise:1.0.0
docker run --rm --env-file .env -e PORT=8000 -p 8000:8000 YOUR_DOCKERHUB_USER/gridwise:1.0.0
```

Submit the actual registry reference, ideally including its digest. The exact
published image and run command need your verification: Docker is not installed
in the environment where this project was prepared. `compose.yaml` is an
optional local alternative using `docker compose up --build`.

See `docs/DEPLOYMENT.md` for hosting and `docs/SUBMISSION_CHECKLIST.md` for the
remaining deliverables. Never commit `.env`; the image copies an explicit file
list and excludes secrets through `.dockerignore`. Provider response bodies,
raw prompts and model replies are not logged by the application.

## Limitations and documented assumptions

- Hidden semantic accuracy depends on the selected model. No live credentials
  were available during preparation; run the actual LLM tests before submission.
- The supplied statement does not define precedence for overlapping solar
  reductions. This implementation multiplies their factors in overlapping
  hours. Reserves combine by maximum, caps by minimum, outages by union. Ask the
  organizers if they publish an additional solar-overlap rule.
- Equal start/end times are rejected as ambiguous; an all-day window is
  explicitly `[0,24]`. Fractional-hour notes are outside the whole-hour contract.
- Structurally valid but semantically wrong LLM output can pass guardrails.
  Guardrails verify allowed structure and bounds, not the meaning of language.
- Infeasible interpreted directives return 422 and invalidate the cached
  interpretation. No constraint is discarded to manufacture a successful plan.
- Caching is in memory, process-local and lost on restart. Provider availability,
  real-account rate limits and host load determine live latency/failure rate.

## Sources and credits

Challenge behavior and scoring come from the two supplied BUP PDFs; samples in
`data/public_samples.json` are the supplied organizer fixture. No hidden tests
are assumed. The starting modules were supplied by the team from its Claude
session and were completed/reviewed with OpenAI Codex. The team must understand
and explain its own architecture and disclose AI assistance under event rules.

Dependencies: FastAPI/Starlette (API), Uvicorn (ASGI server), HTTPX (LLM calls),
NumPy and SciPy/HiGHS (optimization), python-dotenv (local settings), pytest
(testing), plus pinned transitive packages in the requirements files.
See [SciPy HiGHS documentation](https://docs.scipy.org/doc/scipy/reference/optimize.linprog-highs.html),
[Groq models](https://console.groq.com/docs/models) and
[Render Docker deployment documentation](https://render.com/docs/docker).
