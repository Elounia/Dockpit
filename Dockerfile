# Syntax: docker/dockerfile:1
FROM python:3.12-alpine AS runtime

# Install ca-certificates and curl for healthchecks
RUN apk add --no-cache ca-certificates curl

WORKDIR /app

# Create non-root user and group
RUN addgroup -g 1000 dockpit && \
    adduser -u 1000 -G dockpit -s /bin/sh -D dockpit

# Copy application files
COPY dockpit/ ./dockpit/
COPY bin/ ./bin/
COPY LICENSE README.md ./

# Set environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKPIT_HOST=0.0.0.0 \
    DOCKPIT_PORT=8989

# Switch to non-root user
USER dockpit:dockpit

EXPOSE 8989

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8989/ || exit 1

ENTRYPOINT ["python", "-m", "dockpit"]

