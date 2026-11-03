FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_HOME=/opt/torch \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

# The model ships inside the image, so the app works without downloads at startup.
ARG DEMUCS_MODEL=htdemucs
ENV DEMUCS_MODEL=${DEMUCS_MODEL}
RUN python -c "import os; from demucs.pretrained import get_model; get_model(os.environ['DEMUCS_MODEL'])"

RUN useradd --system --uid 1000 --no-create-home voiceoff \
    && mkdir /data \
    && chown voiceoff:voiceoff /data
VOLUME /data

COPY app ./app

USER voiceoff
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]


FROM runtime AS test

USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests ./tests
COPY pytest.ini .
USER voiceoff
ENV PYTHONPATH=/srv
HEALTHCHECK NONE
CMD ["pytest"]
