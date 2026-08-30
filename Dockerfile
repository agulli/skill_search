# Serving image only. The crawler runs on your machine; this container reads a
# database that was built offline and shipped to the volume, so it needs no
# GitHub credentials, no network egress, and no write access to the corpus.
#
# Memory settings are measured on the deployed corpus, broad query / peak RSS:
#
#   cache  64MB + mmap 2048MB ->  95ms / 517MB   does not fit a 512MB machine
#   cache  64MB + mmap    0   -> 128ms /  86MB
#   cache 192MB + mmap    0   -> 100ms / 189MB   <- chosen
#
# mmap maps the database into the address space and RSS counts those pages.
# Spending the same memory on SQLite's page cache instead is the same speed at
# a third of the footprint, so mmap is off.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1

# Serving posture: read-only database, rate limiter on, and trust the proxy's
# client-IP header because we sit behind Cloudflare.
ENV SKILL_ENGINE_PUBLIC=1
ENV SKILL_ENGINE_DB=/data/skills.db
ENV SKILL_ENGINE_CACHE_MB=192
ENV SKILL_ENGINE_MMAP_MB=0
ENV PORT=8080

WORKDIR /app

# Dependencies first so a code change does not invalidate the install layer.
COPY pyproject.toml README.md ./
COPY skill_engine ./skill_engine
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).status==200 else 1)"

CMD ["sh", "-c", "skill-engine --db \"$SKILL_ENGINE_DB\" serve --host 0.0.0.0 --port \"$PORT\""]
