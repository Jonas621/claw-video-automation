FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir edge-tts Pillow

WORKDIR /app

COPY bin/mac_api.py bin/
COPY comfy/ comfy/

EXPOSE 8787

CMD ["python", "bin/mac_api.py"]
