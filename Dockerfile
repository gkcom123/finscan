# FinScan — one image for both containers:
#   run (Container Apps Job):   finscan company <name>      (default: finscan companies)
#   API (Container App):        finscan-api                 (port 8000, $PORT to change)
#
# The project root inside the image is /app. Reviewed mappings are baked in (a new
# mapping means a new image); inbox/ and _work/ are mount points for file shares.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code changes do not reinstall them.
COPY requirements.txt pyproject.toml ./
RUN pip install -r requirements.txt

COPY src ./src
RUN pip install --no-deps .

COPY mappings ./mappings
RUN mkdir -p inbox _work models

EXPOSE 8000
CMD ["finscan", "companies"]
