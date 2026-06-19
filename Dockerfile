FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e ".[server]"
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8787"]
