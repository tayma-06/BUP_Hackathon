# Record your solution video (under 3 minutes)

Use this as an outline in your own words. Only demonstrate and claim the
checks you have actually run. Keep private keys, `.env` and account details
off screen. Target 2 minutes 40 seconds to leave a margin.

## 0:00-0:25: Problem and goal

Show the project README and describe a campus with demand, solar generation,
grid prices and a battery over 24 hours. Explain that operator notes change
constraints: for example, panel washing reduces solar and an emergency note
raises the battery reserve. Correct interpretation and valid schedules come
before minimizing cost.

## 0:25-1:00: Why the LLM is required

Show `app/interpreter.py`. Explain that the model reads each natural-language
note and chooses one of the five supported energy directives or `no_op`.
The model provides structured hours and numeric adjustments, which directly
become optimizer constraints. It is not merely writing a summary.

Use one example: an 80% solar reduction means 20% remains usable, so the factor
is 0.2. A 1 PM to 3 PM window means hours 13 and 14 because the end is excluded.

## 1:00-1:30: Guardrails and reliable interpretation

Show `app/guardrails.py`. Cover supported types, unique note indices, normalized
hours, finite numbers, reserve capacity limits and applies/no_op semantics.
Invalid model output is repaired or sent to a configured backup model within
a fixed time budget. If no model succeeds, the API returns an error instead of
inventing a harmless interpretation. Accepted model output is cached with full
note and battery context.

## 1:30-2:05: Optimization and validation

Show `app/optimizer.py` and `app/validator.py`. Explain the cost objective,
hourly energy balance, battery rates/capacity, grid caps and end-of-day return
to the initial battery energy. HiGHS solves the linear program. Hard directives
are never dropped, and a separate replay checks every returned hour and total.

## 2:05-2:40: Real demonstration

Show the deployed `/health` response and run:

```bash
python scripts/run_public_samples.py --base-url https://YOUR_PUBLIC_HOST
```

Show actual interpretation fields and the test result. Distinguish the live
LLM test from offline optimizer checks. Finish by showing the README's Docker
run command and the real registry reference, if you have built and tested it.

Do not claim a public deployment, live 10/10 result or pullable image until
those steps have been completed in your environment.
