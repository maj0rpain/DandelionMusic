FROM python:3.11-slim

WORKDIR /app

COPY . .

ARG HEROKU_DB
ENV HEROKU_DB=$HEROKU_DB

RUN pip --no-cache-dir install -r requirements.txt \
    && apt-get update \
    && apt-get install --no-install-recommends ffmpeg -y

CMD ["python", "run.py"]
