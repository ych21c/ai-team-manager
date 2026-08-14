FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx python-dotenv
COPY notifier.py .
CMD ["python", "notifier.py"]
