FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml Makefile ./
COPY yds_graph ./yds_graph
COPY scripts ./scripts
COPY sample_data ./sample_data
COPY inbox ./inbox
COPY tests ./tests

ENV YDS_GRAPH_STUB=1
ENV PYTHONDONTWRITEBYTECODE=1

# Offline suite. A live model is not part of the image.
CMD ["python", "-m", "pytest"]
