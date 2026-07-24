# syntax=docker/dockerfile:1.7
FROM --platform=linux/amd64 debian:bookworm-slim@sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e

ARG CHROME_VERSION=150.0.7871.128-1
ARG CHROME_DEB_URL=https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_150.0.7871.128-1_amd64.deb
ARG CHROME_DEB_SHA256=83ed59c85878ebb8fa53915ebe7066cafc58d1c04c1c95449486e6f9d99a1efb
ARG CRYPTOGRAPHY_WHEEL_URL=https://files.pythonhosted.org/packages/e6/8b/43011f7ebe515a8aa20d61f290a326cd890c2e738e16e59eaff8d9c3a412/cryptography-49.0.0-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ARG CRYPTOGRAPHY_WHEEL_SHA256=0e959b578856a3924bc0cbb710fc12c387b9412a951389f3ca61704a9e25f325
ARG CFFI_WHEEL_URL=https://files.pythonhosted.org/packages/fb/d2/4398416cd699b35167947c6e22aca52c47e69ad5695073c9f1f2c52e04aa/cffi-2.1.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ARG CFFI_WHEEL_SHA256=aa7a1b53a2a4452ada2d1b5dade9960b2522f1e61293a811a077439e39029565
ARG PYCPARSER_WHEEL_URL=https://files.pythonhosted.org/packages/0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3/pycparser-3.0-py3-none-any.whl
ARG PYCPARSER_WHEEL_SHA256=b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992
ARG BORON_CODE_DIGEST
ARG DEBIAN_FRONTEND=noninteractive

RUN test -n "${BORON_CODE_DIGEST}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libnss3-tools passwd python3 \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/google-chrome.deb "${CHROME_DEB_URL}" \
    && printf '%s  %s\n' "${CHROME_DEB_SHA256}" /tmp/google-chrome.deb | sha256sum --check --strict \
    && apt-get install -y --no-install-recommends /tmp/google-chrome.deb \
    && mkdir -p /usr/local/lib/python3.11/dist-packages /opt/algo/bin /opt/algo/chrome \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/cryptography.whl "${CRYPTOGRAPHY_WHEEL_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/cffi.whl "${CFFI_WHEEL_URL}" \
    && curl --fail --location --proto '=https' --tlsv1.2 --output /tmp/pycparser.whl "${PYCPARSER_WHEEL_URL}" \
    && printf '%s  %s\n' "${CRYPTOGRAPHY_WHEEL_SHA256}" /tmp/cryptography.whl | sha256sum --check --strict \
    && printf '%s  %s\n' "${CFFI_WHEEL_SHA256}" /tmp/cffi.whl | sha256sum --check --strict \
    && printf '%s  %s\n' "${PYCPARSER_WHEEL_SHA256}" /tmp/pycparser.whl | sha256sum --check --strict \
    && python3 -m zipfile --extract /tmp/cryptography.whl /usr/local/lib/python3.11/dist-packages \
    && python3 -m zipfile --extract /tmp/cffi.whl /usr/local/lib/python3.11/dist-packages \
    && python3 -m zipfile --extract /tmp/pycparser.whl /usr/local/lib/python3.11/dist-packages \
    && ln -s /opt/google/chrome/chrome /opt/algo/chrome/chrome \
    && groupadd --gid 1000 algo \
    && useradd --uid 1000 --gid 1000 --home-dir /home/algo --no-create-home --shell /usr/sbin/nologin algo \
    && rm -rf /var/lib/apt/lists/* /tmp/*.whl /tmp/google-chrome.deb

COPY algo_cli/__init__.py /usr/local/lib/python3.11/dist-packages/algo_cli/__init__.py
COPY algo_cli/boron_browser_wrapper.py /usr/local/lib/python3.11/dist-packages/algo_cli/boron_browser_wrapper.py
COPY algo_cli/boron_browser_entry.py /usr/local/lib/python3.11/dist-packages/algo_cli/boron_browser_entry.py
COPY algo_cli/resources/boron_browser/boron_browser_wrapper.sh /opt/algo/bin/boron-browser-wrapper
COPY algo_cli/resources/boron_browser/boron_managed_policy.json /etc/opt/chrome/policies/managed/boron-managed-policy.json

RUN chmod 0555 /opt/algo/bin/boron-browser-wrapper \
    && chmod 0444 /etc/opt/chrome/policies/managed/boron-managed-policy.json \
    && /usr/bin/python3 -B -I -c 'import cryptography; import algo_cli.boron_browser_entry; assert cryptography.__version__ == "49.0.0"' \
    && test "$(dpkg-query -W -f='${Version}' google-chrome-stable)" = "${CHROME_VERSION}"

LABEL org.opencontainers.image.title="Algo CLI Boron managed browser" \
      org.opencontainers.image.version="0.18.0" \
      org.opencontainers.image.base.digest="sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e" \
      com.algo-cli.role="managed-browser" \
      com.algo-cli.protocol="1" \
      com.algo-cli.browser.version="150.0.7871.128" \
      com.algo-cli.browser.release-at-ms="1784235227785" \
      com.algo-cli.browser.deb.sha256="sha256:83ed59c85878ebb8fa53915ebe7066cafc58d1c04c1c95449486e6f9d99a1efb" \
      com.algo-cli.cryptography.version="49.0.0" \
      com.algo-cli.code.sha256="${BORON_CODE_DIGEST}"

USER 1000:1000
WORKDIR /home/algo
CMD ["/opt/algo/bin/boron-browser-wrapper"]
