# Deploy and verify

## Prepare the repository

Use the new repository created for this event after the question reveal. Put
the **contents** of `gridwise` at its root, including the Dockerfile. Keep it
private during the event; follow the organizer's timing for making it public.
Never push `.env` or keys. No repository or deployment has been created for you
by this package.

## One hosting option: Render Docker web service

1. Open Render and choose **New > Web Service**.
2. Connect your Git provider and select the event repository.
3. Choose the Docker runtime; use the root Dockerfile and its default command.
4. Add the LLM variables from `.env.example` in the environment settings. Put
   the real key in the secret field. Set the health-check path to `/health`.
5. Choose a compute plan suitable for remaining reachable during judging, then
   create the service. Review the provider's current price/limitations first.
6. Test the assigned public URL. `run.py` already binds to `0.0.0.0` and reads
   `PORT`.

These steps follow [Render's Docker guide](https://render.com/docs/docker) and
[web service guide](https://render.com/docs/web-services). No hosting account
has been configured or charged here.

From a second machine/network, run:

```bash
curl https://YOUR_PUBLIC_HOST/health
python scripts/run_public_samples.py --base-url https://YOUR_PUBLIC_HOST --repeat 2 --report reports/deployed.json
```

Require HTTP 200, 10/10 sample passes per repetition, valid LLM provenance,
correct interpretations and replay, and acceptable latency. The guide's
per-request timeout is 30 seconds; its best latency band is p95 <= 5 seconds.
The script's small sample measurement is evidence, not a hidden-test guarantee.

## Required pullable Docker fallback

Docker is unavailable in the preparation environment, so the container is
**not yet build/run tested**. Test these commands on a machine with Docker:

```bash
docker build -t gridwise:1.0.0 .
docker run --rm --name gridwise --env-file .env -e PORT=8000 -p 8000:8000 gridwise:1.0.0
```

In another terminal, call `/health` and run the live sample test against
`http://127.0.0.1:8000`. Then stop the container and publish its tested image:

```bash
docker login
docker tag gridwise:1.0.0 YOUR_DOCKERHUB_USER/gridwise:1.0.0
docker push YOUR_DOCKERHUB_USER/gridwise:1.0.0
docker pull YOUR_DOCKERHUB_USER/gridwise:1.0.0
docker run --rm --env-file .env -e PORT=8000 -p 8000:8000 YOUR_DOCKERHUB_USER/gridwise:1.0.0
```

Run the health/sample checks again on the pulled image. Ensure the registry
reference remains pullable by organizers and submit its exact tag or digest.
Supply variable **names**, port 8000, and the actual verified run command.
Credentials are configured privately at runtime, never baked into the image.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `/health` reports `not_ready` | Set provider, valid model ID and required key; restart the server |
| Requests report `llm_unavailable` | Check provider access, key, model name, quota and networking; configure a real backup model if available |
| Requests report `infeasible_directives` | Check the note's interpretation and scenario values; never solve by removing a required constraint |
| `attempted relative import` | Run `python run.py` from the project root; do not run `app/main.py` directly |
| Cloud host cannot see the port | Preserve `0.0.0.0` binding and the platform's `PORT` value |
| Local model works outside Docker only | Set `LLM_BASE_URL` to an address reachable from inside the container |
| Test passes offline but fails live | Offline uses ground-truth interpretations; diagnose actual LLM extraction/access with the live command |
