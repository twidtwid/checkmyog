FROM python:3.13-slim
# ffprobe (from ffmpeg) reads codec, pixel format, size and duration of og:video files.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --chmod=644 server.py ./
COPY --chmod=755 public/ ./public/
RUN useradd --system --no-create-home app
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "/app/server.py"]
