FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 gridwise
COPY app ./app
COPY scripts ./scripts
COPY data ./data
COPY run.py ./run.py
USER gridwise
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health',timeout=3)"
CMD ["python", "run.py"]
