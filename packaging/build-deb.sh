#!/usr/bin/env bash
# Build dist/terllm_<version>_amd64.deb. The package is the repo layout rooted at /usr/lib/terllm (terllmd.py finds llama-server, the model and
# its helper modules relative to itself), plus a portable CPU llama-server (AVX2, $ORIGIN rpath) built from vendor/llama.cpp.
#   packaging/build-deb.sh 0.3.0 [models/gguf/terllm-v3-q4_k_m.gguf]       needs: cmake, g++, fakeroot, dpkg-deb, curl, git (llama.cpp and the model are fetched when missing)
set -euo pipefail
cd "$(dirname "$0")/.."
VER=${1:?usage: build-deb.sh <version> [model.gguf]}
MODEL=${2:-models/gguf/terllm-v3-q4_k_m.gguf}
if [ ! -d vendor/llama.cpp ]; then                       # CI / fresh clone: the pinned llama.cpp revision
  git clone -q https://github.com/ggml-org/llama.cpp vendor/llama.cpp && git -C vendor/llama.cpp checkout -q "$(cat packaging/llama.cpp.rev)"
fi
if [ ! -f "$MODEL" ]; then                               # the model lives on Hugging Face (models/ is not in git): terllm-v3-q4_k_m.gguf -> v3/
  b=$(basename "$MODEL"); v=${b#terllm-}; v=${v%%-*}; mkdir -p "$(dirname "$MODEL")"
  curl -fL --retry 3 -o "$MODEL" "https://huggingface.co/partum/terllm/resolve/main/$v/$b"
fi
LL=${LLAMA_BUILD:-packaging/_build}                      # reused when it already holds a llama-server
if [ ! -x "$LL/bin/llama-server" ]; then
  cmake -S vendor/llama.cpp -B "$LL" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_BMI2=ON \
    -DGGML_AVX512=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON '-DCMAKE_INSTALL_RPATH=$ORIGIN'
  cmake --build "$LL" --target llama-server -j"$(nproc)"
fi
S=$(mktemp -d); trap 'rm -rf "$S"' EXIT
R=$S/usr/lib/terllm
install -d "$R/runtime" "$R/datagen" "$R/vendor/llama.cpp/build/bin" "$R/models/gguf" "$S/DEBIAN" "$S/usr/share/doc/terllm"
install -m644 runtime/terllmd.py runtime/policy.py runtime/hist_baseline.py runtime/terllm.zsh runtime/terllm.bash runtime/terllm_relay.py "$R/runtime/"
install -m644 datagen/render.py datagen/quality.py datagen/bins.txt "$R/datagen/"
cp -a "$LL"/bin/llama-server "$LL"/bin/*.so* "$R/vendor/llama.cpp/build/bin/"
strip --strip-unneeded "$R"/vendor/llama.cpp/build/bin/* 2>/dev/null || true
install -m644 "$MODEL" "$R/models/gguf/$(basename "$MODEL")"
install -m755 packaging/postinst packaging/postrm "$S/DEBIAN/"
install -m644 packaging/copyright "$S/usr/share/doc/terllm/copyright"
sed -i "s#terllm-v3-q4_k_m.gguf#$(basename "$MODEL")#" "$R/runtime/terllmd.py"        # default model of the packaged daemon
cat > "$S/DEBIAN/control" <<CTL
Package: terllm
Version: $VER
Architecture: amd64
Maintainer: Nazar Mykhailyshchuk <partumyt@gmail.com>
Depends: python3 (>= 3.10), libc6, libstdc++6, libgcc-s1, libgomp1
Recommends: zsh
Section: shells
Priority: optional
Installed-Size: $(du -sk "$S/usr" | cut -f1)
Homepage: https://github.com/partum55/terllm
Description: local AI command autocomplete for zsh and bash (ghost text)
 A 135M-parameter model suggests the rest of the command you are typing, plus files, directories and
 commands (fish style), and never executes anything. Everything runs locally on the CPU.
 .
 Installing it enables it in the ~/.bashrc and ~/.zshrc of every normal user (a marked block, removed again by
 apt remove); new terminals start the suggestion daemon on demand and it exits when idle. Manual setup:
   zsh:  source /usr/lib/terllm/runtime/terllm.zsh      bash (5.1+):  source /usr/lib/terllm/runtime/terllm.bash
CTL
mkdir -p dist
fakeroot dpkg-deb --build -Zxz "$S" "dist/terllm_${VER}_amd64.deb" >/dev/null
ls -la "dist/terllm_${VER}_amd64.deb"
