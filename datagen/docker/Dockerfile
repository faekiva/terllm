# build: docker build -t terllm-sandbox:1 -f datagen/docker/Dockerfile datagen
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash zsh git ca-certificates python3 python3-venv python3-pip nodejs npm \
      gcc g++ make cmake cargo tar gzip bzip2 xz-utils unzip zip curl wget \
      less file procps iproute2 openssh-client locales python-is-python3 \
    && rm -rf /var/lib/apt/lists/*
# ubuntu:24.04 ships uid 1000 as "ubuntu"; replace it with our non-root user
RUN locale-gen en_US.UTF-8
RUN userdel -r ubuntu 2>/dev/null; useradd -m -u 1000 -s /bin/zsh dev && mkdir -p /opt/terllm
COPY fixtures.py /opt/terllm/
COPY docker/bin/ /opt/terllm/bin/
COPY docker/gitconfig /opt/terllm/gitconfig
COPY docker/make_wheels.py /opt/terllm/
RUN python3 /opt/terllm/make_wheels.py
ENV PATH=/opt/terllm/bin:/usr/local/bin:/usr/bin:/bin \
    GIT_CONFIG_GLOBAL=/opt/terllm/gitconfig GIT_DISCOVERY_ACROSS_FILESYSTEM=1 \
    PIP_NO_INDEX=1 PIP_FIND_LINKS=/opt/wheels PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 HOME=/home/dev TERM=xterm-256color
USER dev
WORKDIR /home/dev
