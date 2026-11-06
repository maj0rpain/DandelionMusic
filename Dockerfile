FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

COPY pyproject.toml uv.lock ./

RUN uv sync --locked --no-cache --no-dev \
        --extra postgres --extra mysql

COPY . .

ENV PATH="/app/.venv/bin:${PATH}"

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app app \
    && mkdir -p /app/backup \
    && chown app:app /app /app/backup

USER app

CMD ["python", "run.py"]
