# syntax=docker/dockerfile:1.7
FROM --platform=linux/amd64 debian:bookworm-slim@sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e

ARG CRYPTOGRAPHY_WHEEL_URL=https://files.pythonhosted.org/packages/e6/8b/43011f7ebe515a8aa20d61f290a326cd890c2e738e16e59eaff8d9c3a412/cryptography-49.0.0-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ARG CRYPTOGRAPHY_WHEEL_SHA256=0e959b578856a3924bc0cbb710fc12c387b9412a951389f3ca61704a9e25f325
ARG CFFI_WHEEL_URL=https://files.pythonhosted.org/packages/fb/d2/4398416cd699b35167947c6e22aca52c47e69ad5695073c9f1f2c52e04aa/cffi-2.1.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ARG CFFI_WHEEL_SHA256=aa7a1b53a2a4452ada2d1b5dade9960b2522f1e61293a811a077439e39029565
ARG PYCPARSER_WHEEL_URL=https://files.pythonhosted.org/packages/0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3/pycparser-3.0-py3-none-any.whl
ARG PYCPARSER_WHEEL_SHA256=b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992
ARG XENON_CODE_DIGEST
ARG DEBIAN_FRONTEND=noninteractive

RUN test -n "${XENON_CODE_DIGEST}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl passwd python3 \
    && mkdir -p /usr/local/lib/python3.11/dist-packages /opt/algo/bin \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/cryptography.whl "${CRYPTOGRAPHY_WHEEL_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/cffi.whl "${CFFI_WHEEL_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/pycparser.whl "${PYCPARSER_WHEEL_URL}" \
    && printf '%s  %s\n' "${CRYPTOGRAPHY_WHEEL_SHA256}" /tmp/cryptography.whl | sha256sum --check --strict \
    && printf '%s  %s\n' "${CFFI_WHEEL_SHA256}" /tmp/cffi.whl | sha256sum --check --strict \
    && printf '%s  %s\n' "${PYCPARSER_WHEEL_SHA256}" /tmp/pycparser.whl | sha256sum --check --strict \
    && python3 -m zipfile --extract /tmp/cryptography.whl /usr/local/lib/python3.11/dist-packages \
    && python3 -m zipfile --extract /tmp/cffi.whl /usr/local/lib/python3.11/dist-packages \
    && python3 -m zipfile --extract /tmp/pycparser.whl /usr/local/lib/python3.11/dist-packages \
    && groupadd --gid 1001 xenon \
    && useradd --uid 1001 --gid 1001 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin xenon \
    && rm -rf /var/lib/apt/lists/* /tmp/*.whl

COPY algo_cli/__init__.py /usr/local/lib/python3.11/dist-packages/algo_cli/__init__.py
COPY algo_cli/xenon_browser_egress.py /usr/local/lib/python3.11/dist-packages/algo_cli/xenon_browser_egress.py
COPY algo_cli/xenon_browser_broker.py /usr/local/lib/python3.11/dist-packages/algo_cli/xenon_browser_broker.py
COPY algo_cli/xenon_browser_entry.py /usr/local/lib/python3.11/dist-packages/algo_cli/xenon_browser_entry.py
COPY algo_cli/resources/boron_browser/xenon_egress_broker.sh /opt/algo/bin/xenon-egress-broker

RUN chmod 0555 /opt/algo/bin/xenon-egress-broker \
    && /usr/bin/python3 -B -I -c 'import cryptography; import algo_cli.xenon_browser_entry; assert cryptography.__version__ == "49.0.0"'

LABEL org.opencontainers.image.title="Algo CLI Xenon egress broker" \
      org.opencontainers.image.version="0.18.0" \
      org.opencontainers.image.base.digest="sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e" \
      com.algo-cli.role="egress-broker" \
      com.algo-cli.protocol="1" \
      com.algo-cli.cryptography.version="49.0.0" \
      com.algo-cli.code.sha256="${XENON_CODE_DIGEST}"

USER 1001:1001
WORKDIR /tmp
CMD ["/opt/algo/bin/xenon-egress-broker"]
