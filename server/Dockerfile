FROM rust:1.88-slim AS iso2god-next-builder

ARG ISO2GOD_NEXT_VERSION=v1.3.0

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL -o iso2god-next.tar.gz \
       "https://github.com/georgewoodall82/iso2god-next/archive/refs/tags/${ISO2GOD_NEXT_VERSION}.tar.gz" \
    && tar -xzf iso2god-next.tar.gz \
    && mv "iso2god-next-${ISO2GOD_NEXT_VERSION#v}" iso2god-next \
    && rm iso2god-next.tar.gz

WORKDIR /src/iso2god-next
RUN cargo build --release --locked

FROM python:3.12-slim

ARG ISO2GOD_VERSION=v1.8.1
ARG ISO2GOD_SHA256=4cee1da8dff34659246a4e26bfafd56d023b1ff6b07b3f652b9302b1d2b089a1
ARG EXTRACT_XISO_VERSION=build-202609111233
ARG EXTRACT_XISO_SHA256=c4b9fbf86b57c74d07fb643522740ae82034d8280ccde24528752cd1cb0b798d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    START_BACKGROUND=true \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl unzip \
    && curl -fsSL -o /usr/local/bin/iso2god \
       "https://github.com/iliazeus/iso2god-rs/releases/download/${ISO2GOD_VERSION}/iso2god-x86_64-linux" \
    && echo "${ISO2GOD_SHA256}  /usr/local/bin/iso2god" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/iso2god \
    && curl -fsSL -o /tmp/extract-xiso.zip \
       "https://github.com/XboxDev/extract-xiso/releases/download/${EXTRACT_XISO_VERSION}/extract-xiso_Linux.zip" \
    && echo "${EXTRACT_XISO_SHA256}  /tmp/extract-xiso.zip" | sha256sum -c - \
    && unzip -p /tmp/extract-xiso.zip extract-xiso > /usr/local/bin/extract-xiso \
    && chmod 0755 /usr/local/bin/extract-xiso \
    && rm /tmp/extract-xiso.zip \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=iso2god-next-builder /src/iso2god-next/target/release/iso2god-next /usr/local/bin/iso2god-next
RUN chmod 0755 /usr/local/bin/iso2god-next

COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin xbox360 \
    && mkdir -p /data/games-GoD /data/downloaded-worked-on \
    && chown -R xbox360:xbox360 /data
WORKDIR /app
COPY --chown=xbox360:xbox360 app /app

USER xbox360
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "0", "app:app"]
