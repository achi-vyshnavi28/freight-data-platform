FROM python:3.12-slim
WORKDIR /opt/freight-data-platform
COPY pyproject.toml ./
COPY fdp ./fdp
RUN pip install --no-cache-dir .
COPY . .
ENV FDP_PG_DSN=postgresql://fdp:fdp@db:5432/fdp FDP_PG_HOST=db
CMD ["python", "-m", "fdp.pipeline", "run"]
