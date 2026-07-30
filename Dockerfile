FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY welcome.mp4 .
COPY bot/ bot/

CMD ["python", "-m", "bot"]
