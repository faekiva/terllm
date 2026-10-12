# terllm: inline command suggestions from a local model (see runtime/terllmd.py).
# Usage: source /path/to/terllm.zsh   (disable zsh-autosuggestions first: both draw ghost text in POSTDISPLAY)
# Accept the suggestion: Right arrow / End at the end of the line.  Never executes anything.
zmodload zsh/net/socket zsh/system 2>/dev/null || return
autoload -Uz add-zle-hook-widget add-zsh-hook

typeset -g _terllm_dir="${0:A:h}" _terllm_sock="${XDG_RUNTIME_DIR:-/tmp}/terllm.sock"
typeset -g _terllm_fd _terllm_seq=0 _terllm_sugg="" _terllm_last="" _terllm_lastcmd=""

_terllm_connect() {
  [[ -n $_terllm_fd ]] && return 0
  if [[ ! -S $_terllm_sock ]]; then
    (cd $_terllm_dir/.. && nohup uv run runtime/terllmd.py >/tmp/terllmd.log 2>&1 &)
    return 1                                   # daemon is warming up; try again on the next prompt
  fi
  zsocket $_terllm_sock 2>/dev/null || return 1
  _terllm_fd=$REPLY
  zle -F $_terllm_fd _terllm_reply
}

_terllm_send() { [[ -n $_terllm_fd ]] && print -r -u $_terllm_fd -- "$1" 2>/dev/null; }

_terllm_preexec() { _terllm_lastcmd=${1//$'\t'/ }; _terllm_lastcmd=${_terllm_lastcmd//$'\n'/ }; }

_terllm_precmd() {
  local code=$?
  _terllm_connect || return
  _terllm_send "c"$'\t'"$code"$'\t'"$PWD"$'\t'"${VIRTUAL_ENV:-}"$'\t'"$_terllm_lastcmd"
  _terllm_lastcmd=""
}

_terllm_show() {                                # draw `$1` as grey ghost text after the cursor
  _terllm_sugg=$1
  POSTDISPLAY=$1
  region_highlight=("${#BUFFER} $(( ${#BUFFER} + ${#POSTDISPLAY} )) fg=8")
}

_terllm_reply() {                               # zle -F handler: a line arrived from the daemon
  local line
  if ! read -r -u $1 line 2>/dev/null; then
    zle -F $1; exec {1}>&-; _terllm_fd=""; return
  fi
  zle _terllm_apply -- "$line"                  # BUFFER is only visible inside a widget
}

_terllm_apply() {
  local -a f
  f=("${(@ps:\t:)1}")
  [[ $f[1] == s && $f[2] == $_terllm_seq && $f[3] == $BUFFER && -n $f[4] ]] || return
  _terllm_last=$BUFFER
  _terllm_show $f[4]
  zle -R
}

_terllm_redraw() {                              # runs on every buffer change
  [[ $BUFFER == *$'\n'* ]] && { POSTDISPLAY=""; _terllm_sugg=""; return }
  local want=$_terllm_last$_terllm_sugg
  if [[ -n $_terllm_sugg && $want == ${BUFFER}* ]]; then   # still typing along the suggestion: no model call
    _terllm_show ${want#$BUFFER}
    return
  fi
  POSTDISPLAY=""; _terllm_sugg=""
  [[ -n $_terllm_fd ]] || return
  (( _terllm_seq++ ))
  _terllm_send "q"$'\t'"$_terllm_seq"$'\t'"$BUFFER"
}

_terllm_accept() {
  if [[ -n $_terllm_sugg && $CURSOR -eq ${#BUFFER} ]]; then
    BUFFER+=$_terllm_sugg; CURSOR=${#BUFFER}; _terllm_send "a"
    POSTDISPLAY=""; _terllm_sugg=""; _terllm_last=""
  else
    zle ${1:-forward-char}
  fi
}
_terllm_accept_right() { _terllm_accept forward-char }
_terllm_accept_end() { _terllm_accept end-of-line }

zle -N _terllm_apply; zle -N _terllm_accept_right; zle -N _terllm_accept_end
bindkey '^[[C' _terllm_accept_right '^[OC' _terllm_accept_right '^[[F' _terllm_accept_end '^[OF' _terllm_accept_end
add-zle-hook-widget line-pre-redraw _terllm_redraw
add-zsh-hook preexec _terllm_preexec
add-zsh-hook precmd _terllm_precmd
