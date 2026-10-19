FROM python:3.13-slim
ARG WITH_FORECA=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 EWT_HOST=0.0.0.0 EWT_PORT=8080
WORKDIR /app
COPY requirements-foreca.txt ./
# tzdata: the zone database, in case the base image's is missing or stale
RUN pip install --no-cache-dir tzdata \
 && if [ "$WITH_FORECA" = "1" ]; then pip install --no-cache-dir -r requirements-foreca.txt; fi \
 && useradd --system --uid 10001 --no-create-home app
COPY eventweather ./eventweather
COPY www ./www
USER app
EXPOSE 8080
CMD ["python", "-m", "eventweather"]
