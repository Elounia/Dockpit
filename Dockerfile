# syntax=docker/dockerfile:1
FROM python:3.12-alpine AS runtime

WORKDIR /app
RUN addgroup -g 1000 dockpit && adduser -u 1000 -G dockpit -s /bin/sh -D dockpit
COPY dockpit/ ./dockpit/
COPY LICENSE README.md ./
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKPIT_HOST=0.0.0.0 \
    DOCKPIT_PORT=8989
USER dockpit:dockpit
EXPOSE 8989
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('DOCKPIT_PORT', '8989') + '/readyz', timeout=3)"
ENTRYPOINT ["python", "-m", "dockpit"]
