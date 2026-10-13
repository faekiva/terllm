#!/usr/bin/env bash
# Runs postinst/postrm against fake users (a fake `getent` first on PATH). packaging/test_maint.sh
set -euo pipefail
cd "$(dirname "$0")"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/alice" "$T/bob" "$T/carol" "$T/svc"
cat > "$T/bin/getent" <<EOF
#!/bin/sh
cat <<P
root:x:0:0:root:/root:/bin/bash
svc:x:999:999::$T/svc:/bin/bash
alice:x:1000:1000::$T/alice:/bin/zsh
bob:x:1001:1001::$T/bob:/bin/bash
carol:x:1002:1002::$T/carol:/usr/sbin/nologin
P
EOF
chmod +x "$T/bin/getent"; export PATH="$T/bin:$PATH"
printf 'alias a=1\n' > "$T/alice/.bashrc"; printf 'alias z=1\n' > "$T/alice/.zshrc"       # alice: both shells
printf 'alias b=1\n' > "$T/bob/.bashrc"; printf 'x\n' > "$T/bob/real"; ln -s "$T/bob/real" "$T/bob/.zshrc"   # bob: bash + a symlinked .zshrc
printf 'alias c=1\n' > "$T/carol/.bashrc"; printf 'alias s=1\n' > "$T/svc/.bashrc"          # nologin / system uid: untouched
cp -r "$T" "$T.orig"
sh ./postinst configure | sed "s#$T#HOME#g"
sh ./postinst configure >/dev/null                                                             # second run: no duplicates
[ "$(grep -c '>>> terllm' "$T/alice/.bashrc")" = 1 ] && [ "$(grep -c '>>> terllm' "$T/alice/.zshrc")" = 1 ]
grep -q 'terllm.bash' "$T/alice/.bashrc" && grep -q 'terllm.zsh' "$T/alice/.zshrc" && grep -q 'terllm.bash' "$T/bob/.bashrc"
! grep -q terllm "$T/carol/.bashrc" && ! grep -q terllm "$T/svc/.bashrc" && ! grep -q terllm "$T/bob/real"
bash -n "$T/alice/.bashrc" && zsh -n "$T/alice/.zshrc"
sh ./postrm upgrade; grep -q terllm "$T/alice/.bashrc"                                         # upgrades keep it
sh ./postrm remove
for u in alice bob carol svc; do for f in .bashrc .zshrc; do [ -e "$T/$u/$f" ] && diff -q "$T/$u/$f" "$T.orig/$u/$f" >/dev/null || [ ! -e "$T/$u/$f" ]; done; done
echo "maintainer scripts ok"; rm -rf "$T.orig"
