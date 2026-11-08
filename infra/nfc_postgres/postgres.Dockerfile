FROM postgres:18.4-bookworm
RUN groupadd --gid 18084 nfc-secrets \
    && test -z "$(find /docker-entrypoint-initdb.d -mindepth 1 -maxdepth 1 -print -quit)"
COPY infra/nfc_postgres/bootstrap.sh /usr/local/bin/nfc-bootstrap.sh
COPY infra/nfc_postgres/clear-init-passwords.sh /docker-entrypoint-initdb.d/99-nfc-clear-init-passwords.sh
RUN sed -i 's/\r$//' /usr/local/bin/nfc-bootstrap.sh /docker-entrypoint-initdb.d/99-nfc-clear-init-passwords.sh \
    && chmod 0644 /docker-entrypoint-initdb.d/99-nfc-clear-init-passwords.sh
ENTRYPOINT ["/bin/sh", "/usr/local/bin/nfc-bootstrap.sh"]
