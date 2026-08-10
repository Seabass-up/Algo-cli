# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
FROM --platform=linux/amd64 docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32 AS dockerfile_frontend_pin
# Keep the pinned frontend in the reachable LLB graph so BuildKit records it
# as a provenance material without copying it into the runtime image.
FROM --platform=linux/amd64 debian:bookworm-slim@sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e

ARG DEBIAN_SNAPSHOT=20260712T202631Z
ARG DEBIAN_SECURITY_SNAPSHOT=20260712T194830Z
ARG DEBIAN_BOOKWORM_INRELEASE_SHA256=77737fa4b34f2693e982cc9ee35736816c35a7778fc2d326cc1bbf5b301fe1aa
ARG DEBIAN_UPDATES_INRELEASE_SHA256=1027134746585f4f75c7170a957ebcb83582ea0a1dcb1b36ceeb3da0009e1b04
ARG DEBIAN_SECURITY_INRELEASE_SHA256=d25ac813817a3b28e2a35defaf0eeb29a747017b5ab013ff597e0b5dc5b667c0
ARG CA_CERTIFICATES_VERSION=20230311+deb12u1
ARG CURL_VERSION=7.88.1-10+deb12u15
ARG PASSWD_VERSION=1:4.13+dfsg1-1+deb12u2
ARG PYTHON3_VERSION=3.11.2-1+b1
ARG DPKG_LOCK_SHA256=945e9057beb01efbdcf89ca6ba002f260eb6bda40f5d535337e7ca7dc6eed640
ARG DPKG_LOCK_ENTRIES=122
ARG CRYPTOGRAPHY_WHEEL_URL=https://files.pythonhosted.org/packages/d9/41/029086c34d91052fc3b88bcc8056f709a7c915c7a23b235a54eb800b1c97/cryptography-50.0.0-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ARG CRYPTOGRAPHY_WHEEL_SHA256=06a32a980526a6ab9a4b9bf8f7385800791e2bb960903cb6b530e4817509a3b7
ARG CFFI_WHEEL_URL=https://files.pythonhosted.org/packages/fb/d2/4398416cd699b35167947c6e22aca52c47e69ad5695073c9f1f2c52e04aa/cffi-2.1.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ARG CFFI_WHEEL_SHA256=aa7a1b53a2a4452ada2d1b5dade9960b2522f1e61293a811a077439e39029565
ARG PYCPARSER_WHEEL_URL=https://files.pythonhosted.org/packages/0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3/pycparser-3.0-py3-none-any.whl
ARG PYCPARSER_WHEEL_SHA256=b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992
ARG XENON_CODE_DIGEST
ARG DEBIAN_FRONTEND=noninteractive

RUN --mount=type=bind,from=dockerfile_frontend_pin,source=/bin/dockerfile-frontend,target=/tmp/dockerfile-frontend,readonly \
    test -x /tmp/dockerfile-frontend \
    && test -n "${XENON_CODE_DIGEST}" \
    && printf '%s\n' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
        'Suites: bookworm bookworm-updates' \
        'Components: main' \
        'Check-Valid-Until: no' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SECURITY_SNAPSHOT}/" \
        'Suites: bookworm-security' \
        'Components: main' \
        'Check-Valid-Until: no' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Languages=none -o Acquire::Retries=5 update \
    && printf '%s  %s\n' \
        "${DEBIAN_BOOKWORM_INRELEASE_SHA256}" "/var/lib/apt/lists/snapshot.debian.org_archive_debian_${DEBIAN_SNAPSHOT}_dists_bookworm_InRelease" \
        "${DEBIAN_UPDATES_INRELEASE_SHA256}" "/var/lib/apt/lists/snapshot.debian.org_archive_debian_${DEBIAN_SNAPSHOT}_dists_bookworm-updates_InRelease" \
        "${DEBIAN_SECURITY_INRELEASE_SHA256}" "/var/lib/apt/lists/snapshot.debian.org_archive_debian-security_${DEBIAN_SECURITY_SNAPSHOT}_dists_bookworm-security_InRelease" \
        | sha256sum --check --strict \
    && apt-get install -y --no-install-recommends \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "curl=${CURL_VERSION}" \
        "passwd=${PASSWD_VERSION}" \
        "python3=${PYTHON3_VERSION}" \
    && test -z "$(dpkg --audit)" \
    && dpkg-query -W -f='${binary:Package}=${Version}\n' | LC_ALL=C sort > /tmp/dpkg.lock \
    && test "$(awk 'END { print NR }' /tmp/dpkg.lock)" = "${DPKG_LOCK_ENTRIES}" \
    && printf '%s  %s\n' "${DPKG_LOCK_SHA256}" /tmp/dpkg.lock | sha256sum --check --strict \
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
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /etc/apt/sources.list.d/* \
        /tmp/*.whl /tmp/dpkg.lock

COPY algo_cli/__init__.py /usr/local/lib/python3.11/dist-packages/algo_cli/__init__.py
COPY algo_cli/xenon_browser_egress.py /usr/local/lib/python3.11/dist-packages/algo_cli/xenon_browser_egress.py
COPY algo_cli/xenon_browser_broker.py /usr/local/lib/python3.11/dist-packages/algo_cli/xenon_browser_broker.py
COPY algo_cli/xenon_browser_entry.py /usr/local/lib/python3.11/dist-packages/algo_cli/xenon_browser_entry.py
COPY algo_cli/resources/boron_browser/xenon_egress_broker.sh /opt/algo/bin/xenon-egress-broker

RUN chmod 0555 /opt/algo/bin/xenon-egress-broker \
    && /usr/bin/python3 -B -I -c 'import cryptography; import algo_cli.xenon_browser_entry; assert cryptography.__version__ == "50.0.0"'

LABEL org.opencontainers.image.title="Algo CLI Xenon egress broker" \
      org.opencontainers.image.version="0.18.0" \
      org.opencontainers.image.base.digest="sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e" \
      com.algo-cli.role="egress-broker" \
      com.algo-cli.protocol="1" \
      com.algo-cli.cryptography.version="50.0.0" \
      com.algo-cli.debian.snapshot="20260712T202631Z" \
      com.algo-cli.debian.security-snapshot="20260712T194830Z" \
      com.algo-cli.dpkg.lock.sha256="sha256:945e9057beb01efbdcf89ca6ba002f260eb6bda40f5d535337e7ca7dc6eed640" \
      com.algo-cli.dpkg.lock.entries="122" \
      com.algo-cli.build.hermetic="false" \
      com.algo-cli.build.reproducible="false" \
      com.algo-cli.code.sha256="${XENON_CODE_DIGEST}"

USER 1001:1001
WORKDIR /tmp
CMD ["/opt/algo/bin/xenon-egress-broker"]
