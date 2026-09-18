"""Run from the project root: python run.py"""
import os
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
                workers=1, log_level="info")
