FROM python:3.12-slim

WORKDIR /srv/talk-to-db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python scripts/create_demo_db.py

# Run as non-root
RUN useradd -m ttdb && chown -R ttdb /srv/talk-to-db
USER ttdb

ENV TTDB_HOST=0.0.0.0
EXPOSE 7860
CMD ["python", "run.py"]
