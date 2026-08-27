FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY . .

ARG HEROKU_DB
ENV HEROKU_DB=$HEROKU_DB

RUN uv sync --frozen --no-cache \
    && apt-get update \
    && apt-get install --no-install-recommends ffmpeg -y \
    && apt-get install -y curl \
    && apt-get install -y unzip

ENV DENO_INSTALL="/usr/local"
RUN  curl -fsSL https://deno.land/install.sh | sh

ENV PATH="/app/.venv/bin:${DENO_INSTALL}/bin:${PATH}"

CMD ["python", "run.py"]
