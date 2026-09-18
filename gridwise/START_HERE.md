# Start here

This is the completed local project for your uploaded GridWise preliminary task.
You are building an **HTTP API**, not a Kaggle notebook or CSV submission.

## 1. Run on your Windows laptop

Install Python **3.12** if it is not installed. Extract the ZIP and open the
`gridwise` folder in VS Code. Open a **PowerShell** terminal in that folder.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

In `.env`, put your own Groq API key after `LLM_API_KEY=`. Keep these two lines:

```dotenv
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-120b
```

Save and close Notepad. Start the API:

```powershell
.\.venv\Scripts\python.exe run.py
```

Keep that terminal open. Visit `http://127.0.0.1:8000/health`. It should show
`{"status":"ok"}` after a provider is configured. This checks startup and
configuration; the next test checks actual model access, quota and responses.

You can also double-click `start_windows.bat`; it performs the installation and
opens a new `.env` for editing. Python 3.12 must already be installed.

## 2. Test in a second terminal

```powershell
.\.venv\Scripts\python.exe scripts/run_public_samples.py --base-url http://127.0.0.1:8000 --report reports/live.json
```

Aim for **10/10 PASS**. If health fails, fill the LLM settings and restart the
server. If requests return `llm_unavailable`, check your model access, key,
quota and network. Do not paste your key into chat or commit `.env`.

You can verify the optimizer before obtaining a key:

```powershell
.\.venv\Scripts\python.exe scripts/run_public_samples.py --offline
```

The offline command supplies the organizers' interpretations. Passing it does
**not** prove that your chosen model interprets the notes correctly.

## 3. Finish submission

Follow `docs/DEPLOYMENT.md` to deploy this API and build/push a Docker image.
Record your own solution explanation using `docs/VIDEO_OUTLINE.md`.

The organizer requires a public API URL, a repository, documentation, a
pullable Docker image with an exact tag/digest, and a video of at most 3 minutes.
Keep the repository private during the event and make it public after the
deadline, as the supplied guide requires.

## What is ready, and what remains

- The optimizer matches all 10 supplied reference costs.
- The project includes input validation, LLM retries/backup, guardrails,
  independent replay, tests, sample requests, Docker files and instructions.
- Live model inference has **not** been tested with your account.
- No public deployment, registry image or recorded video has been created.
- Docker is unavailable in the preparation environment; run the documented
  container test before submitting a registry reference.

For architecture, configuration and limitations, read `README.md`.
