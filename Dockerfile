FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml /app/
COPY f916 /app/f916
COPY dashboard /app/dashboard
COPY defense /app/defense
COPY tuner /app/tuner
COPY invariants.py /app/invariants.py
RUN pip install '.[test]' && useradd --uid 10001 --create-home agent && mkdir -p /data && chown agent:agent /data
COPY contracts /app/contracts
COPY config /app/config
COPY prompts /app/prompts
COPY scripts /app/scripts
COPY tests /app/tests
USER 10001:10001
ENV DATA_DIR=/data
CMD ["python", "-m", "f916.loop"]
