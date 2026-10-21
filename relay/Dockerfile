FROM python:3.12-slim
RUN useradd --system --uid 10001 --home /var/lib/vigil-relay relay \
 && mkdir -p /var/lib/vigil-relay/queue && chown -R relay /var/lib/vigil-relay
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vigil_relay ./vigil_relay
USER relay
EXPOSE 8443
VOLUME /var/lib/vigil-relay
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,ssl,os;ctx=ssl._create_unverified_context();s='https' if os.environ.get('RELAY_TLS_CERT') else 'http';urllib.request.urlopen(f'{s}://127.0.0.1:'+os.environ.get('RELAY_PORT','8443')+'/api/v1/health/',context=ctx,timeout=4)"
CMD ["python", "-m", "vigil_relay"]
