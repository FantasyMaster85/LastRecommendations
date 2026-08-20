FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/FantasyMaster85/LastRecommendations" \
      org.opencontainers.image.title="LastRecommendations" \
      org.opencontainers.image.description="Turns personalized Last.fm artist recommendations into a Lidarr Custom Import List and persistent recommendation history." \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

RUN mkdir -p /app/data

EXPOSE 9654

CMD ["python", "app.py"]
