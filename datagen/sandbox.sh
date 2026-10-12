#!/usr/bin/env bash
# Only entry point for executing generated commands. Never run them on the host.
# usage: sandbox.sh [--scratch DIR] [--timeout SEC] -- CMD [ARGS...]
set -euo pipefail

IMAGE="${TERLLM_IMAGE:-terllm-sandbox:1}"
SCRATCH="" TIMEOUT=60
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scratch) SCRATCH="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ $# -gt 0 ]] || { echo "no command" >&2; exit 2; }

mounts=()
[[ -n "$SCRATCH" ]] && mounts=(-v "$(realpath "$SCRATCH"):/scratch")

exec timeout --kill-after=5 "$TIMEOUT" docker run --rm -i \
  --network none --user dev \
  --memory 1g --memory-swap 1g --cpus 1 --pids-limit 256 \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /home/dev:rw,exec,uid=1000,gid=1000,size=256m \
  --tmpfs /tmp:rw,exec,size=128m --tmpfs /srv:rw,exec,uid=1000,gid=1000,size=256m \
  "${mounts[@]}" "$IMAGE" "$@"
