# syntax=docker/dockerfile:1.7
FROM --platform=linux/arm64 debian:bookworm-slim@sha256:9b67294679b30e5d6ab257b40594feeb4a4b81f7fcf4131f4decf0d6a212a9b0

ARG CHROMIUM_VERSION=150.0.7871.124-1~deb12u1
ARG CHROMIUM_DEB_URL=https://security.debian.org/debian-security/pool/updates/main/c/chromium/chromium_150.0.7871.124-1~deb12u1_arm64.deb
ARG CHROMIUM_DEB_SHA256=774473b94c99d695304892b4dc52d700191929b46ba67192e1b95cdddc5744b2
ARG CHROMIUM_COMMON_DEB_URL=https://security.debian.org/debian-security/pool/updates/main/c/chromium/chromium-common_150.0.7871.124-1~deb12u1_arm64.deb
ARG CHROMIUM_COMMON_DEB_SHA256=77c97940e17b90394ea4065a0dce0daffa00f28f1209d1fc14be4333e8daa1ae
ARG CHROMIUM_SANDBOX_DEB_URL=https://security.debian.org/debian-security/pool/updates/main/c/chromium/chromium-sandbox_150.0.7871.124-1~deb12u1_arm64.deb
ARG CHROMIUM_SANDBOX_DEB_SHA256=95bd9cfa5c74ff008bbaa31c4fce0c415d918d79261b3d7bb79eabac6d405352
ARG CRYPTOGRAPHY_WHEEL_URL=https://files.pythonhosted.org/packages/09/41/3797cfaf69cae04a13ee78ebd83f0678d9c02b4779d21ce24445326f1a69/cryptography-49.0.0-cp311-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl
ARG CRYPTOGRAPHY_WHEEL_SHA256=36d1709f992593689b45bda411498d62c6e365f2ca00b84657d4dadd24de16db
ARG CFFI_WHEEL_URL=https://files.pythonhosted.org/packages/22/d7/1a74539db16d8bfd839ff1515948948efbb162e574650fd3d846896eea95/cffi-2.1.0-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.whl
ARG CFFI_WHEEL_SHA256=88023dfe18799507b73f1dbb0d14326a17465de1bc9c9c7655c22845e9ddc3a2
ARG PYCPARSER_WHEEL_URL=https://files.pythonhosted.org/packages/0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3/pycparser-3.0-py3-none-any.whl
ARG PYCPARSER_WHEEL_SHA256=b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992
ARG BORON_CODE_DIGEST
ARG DEBIAN_FRONTEND=noninteractive

RUN test -n "${BORON_CODE_DIGEST}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libnss3-tools passwd python3 \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/chromium.deb "${CHROMIUM_DEB_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/chromium-common.deb "${CHROMIUM_COMMON_DEB_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/chromium-sandbox.deb "${CHROMIUM_SANDBOX_DEB_URL}" \
    && printf '%s  %s\n' "${CHROMIUM_DEB_SHA256}" /tmp/chromium.deb | sha256sum --check --strict \
    && printf '%s  %s\n' "${CHROMIUM_COMMON_DEB_SHA256}" /tmp/chromium-common.deb | sha256sum --check --strict \
    && printf '%s  %s\n' "${CHROMIUM_SANDBOX_DEB_SHA256}" /tmp/chromium-sandbox.deb | sha256sum --check --strict \
    && apt-get install -y --no-install-recommends /tmp/chromium-common.deb /tmp/chromium-sandbox.deb /tmp/chromium.deb \
    && mkdir -p /usr/local/lib/python3.11/dist-packages /opt/algo/bin /opt/algo/chrome /etc/chromium/policies/managed \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/cryptography.whl "${CRYPTOGRAPHY_WHEEL_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/cffi.whl "${CFFI_WHEEL_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/pycparser.whl "${PYCPARSER_WHEEL_URL}" \
    && printf '%s  %s\n' "${CRYPTOGRAPHY_WHEEL_SHA256}" /tmp/cryptography.whl | sha256sum --check --strict \
    && printf '%s  %s\n' "${CFFI_WHEEL_SHA256}" /tmp/cffi.whl | sha256sum --check --strict \
    && printf '%s  %s\n' "${PYCPARSER_WHEEL_SHA256}" /tmp/pycparser.whl | sha256sum --check --strict \
    && python3 -m zipfile --extract /tmp/cryptography.whl /usr/local/lib/python3.11/dist-packages \
    && python3 -m zipfile --extract /tmp/cffi.whl /usr/local/lib/python3.11/dist-packages \
    && python3 -m zipfile --extract /tmp/pycparser.whl /usr/local/lib/python3.11/dist-packages \
    && ln -s /usr/bin/chromium /opt/algo/chrome/chrome \
    && groupadd --gid 1000 algo \
    && useradd --uid 1000 --gid 1000 --home-dir /home/algo --no-create-home --shell /usr/sbin/nologin algo \
    && rm -rf /var/lib/apt/lists/* /tmp/*.whl /tmp/chromium*.deb

COPY algo_cli/__init__.py /usr/local/lib/python3.11/dist-packages/algo_cli/__init__.py
COPY algo_cli/boron_browser_wrapper.py /usr/local/lib/python3.11/dist-packages/algo_cli/boron_browser_wrapper.py
COPY algo_cli/boron_browser_entry.py /usr/local/lib/python3.11/dist-packages/algo_cli/boron_browser_entry.py
COPY algo_cli/resources/boron_browser/boron_browser_wrapper.sh /opt/algo/bin/boron-browser-wrapper
COPY algo_cli/resources/boron_browser/boron_managed_policy.json /etc/chromium/policies/managed/boron-managed-policy.json

RUN chmod 0555 /opt/algo/bin/boron-browser-wrapper \
    && chmod 0444 /etc/chromium/policies/managed/boron-managed-policy.json \
    && /usr/bin/python3 -B -I -c 'import cryptography; import algo_cli.boron_browser_entry; assert cryptography.__version__ == "49.0.0"' \
    && test "$(dpkg-query -W -f='${Version}' chromium)" = "${CHROMIUM_VERSION}" \
    && test "$(dpkg-query -W -f='${Version}' chromium-common)" = "${CHROMIUM_VERSION}" \
    && test "$(dpkg-query -W -f='${Version}' chromium-sandbox)" = "${CHROMIUM_VERSION}"

LABEL org.opencontainers.image.title="Algo CLI Carbon native managed browser" \
      org.opencontainers.image.version="0.18.0" \
      org.opencontainers.image.base.digest="sha256:9b67294679b30e5d6ab257b40594feeb4a4b81f7fcf4131f4decf0d6a212a9b0" \
      com.algo-cli.role="managed-browser" \
      com.algo-cli.protocol="1" \
      com.algo-cli.browser.family="chromium_stable" \
      com.algo-cli.browser.version="150.0.7871.124" \
      com.algo-cli.browser.release-at-ms="1784186325000" \
      com.algo-cli.browser.deb.sha256="sha256:774473b94c99d695304892b4dc52d700191929b46ba67192e1b95cdddc5744b2" \
      com.algo-cli.cryptography.version="49.0.0" \
      com.algo-cli.code.sha256="${BORON_CODE_DIGEST}"

USER 1000:1000
WORKDIR /home/algo
CMD ["/opt/algo/bin/boron-browser-wrapper"]
