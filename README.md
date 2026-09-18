# GridWise LLM

BUP CSE Fest 2026 — Preliminary Smart Campus Energy Optimization Challenge.

An HTTP API that reads 1-3 short operator notes (e.g. "keep at least 120 kWh
in reserve from 6 PM to 9 PM"), interprets them with a generative language
model, validates the interpreted directives, and produces the minimum-cost
24-hour schedule for a campus battery, rooftop solar and grid import — while
respecting every accepted constraint and never inventing a `no_op`.

The entire application, its tests, and its Dockerfile live in [`gridwise/`](gridwise/).

## Repository layout

```text
README.md          this overview
gridwise/          runnable FastAPI application + tests
gridwise/app/      API, interpreter, guardrails, optimizer, validator
gridwise/data/     organizer fixture and reference responses
gridwise/docs/     setup, verification, deployment and submission guides
```

## Quick start

Requires **Python 3.12**. From the root of this repository, enter the
application folder before running any setup command:

```powershell
cd gridwise
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env privately: set LLM_API_KEY and confirm the provider/model.
.\.venv\Scripts\python.exe run.py
```

The server binds to `0.0.0.0` on `PORT` (default `8000`).

For Linux/macOS equivalents, a full configuration reference, the API
contract, and the optimization model, read [`gridwise/README.md`](gridwise/README.md).

## Verify the service

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/optimize-energy \
  -H 'Content-Type: application/json' --data-binary @data/sample_request.json
```

On PowerShell use `curl.exe` and keep the POST on one line.

## Guides

- [`gridwise/START_HERE.md`](gridwise/START_HERE.md) — step-by-step Windows setup
- [`gridwise/docs/VERIFICATION.md`](gridwise/docs/VERIFICATION.md) — recorded local, model and container checks
- [`gridwise/docs/DEPLOYMENT.md`](gridwise/docs/DEPLOYMENT.md) — hosting, Render and Docker
- [`gridwise/docs/SUBMISSION_CHECKLIST.md`](gridwise/docs/SUBMISSION_CHECKLIST.md) — final deliverables
- [`gridwise/docs/CHANGES.md`](gridwise/docs/CHANGES.md) — design and audit trail

## Security

- Keep credentials in `gridwise/.env`; it, editor-history backups (`.history/`),
  and other environment snapshots are Git-ignored.
- `gridwise/.env.example` documents every variable without secrets.
- The API never logs prompts, model replies or keys.

## Status

Public deployment and a pullable registry image still need verification.
See `gridwise/docs/VERIFICATION.md` for the recorded checks and their scope.