FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates chromium fonts-dejavu-core ffmpeg nginx-light util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 studio \
    && useradd --uid 10001 --gid 10001 --no-create-home studio
WORKDIR /opt/studio
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt yt-dlp==2026.10.26
COPY app ./app
COPY deploy/md2pdf.py deploy/md2pdf.sha256 ./deploy/
RUN cd deploy && echo "$(cat md2pdf.sha256)  md2pdf.py" | sha256sum --check --status
COPY deploy/docker/entrypoint.sh ./deploy/docker/entrypoint.sh
COPY agent/studio-agent.zip agent/studio-agent-setup.exe ./agent/
ARG STUDIO_REVISION=development
LABEL org.opencontainers.image.title="Studio" \
      org.opencontainers.image.revision=$STUDIO_REVISION
USER 10001:10001
ENTRYPOINT ["/bin/sh", "/opt/studio/deploy/docker/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "/opt/studio/app", "--uds", "/run/studio/web.sock", "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "10", "--ws-max-size", "16778240", "--ws-ping-interval", "20", "--ws-ping-timeout", "20"]
