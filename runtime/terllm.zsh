# terllm: inline command suggestions from a local model (see runtime/terllmd.py).
# Usage: source /path/to/terllm.zsh   (disable zsh-autosuggestions first: both draw ghost text in POSTDISPLAY)
# Accept the suggestion: Right arrow / End at the end of the line.  Never executes anything.
zmodload zsh/net/socket zsh/system 2>/dev/null || return
autoload -Uz add-zle-hook-widget add-zsh-hook

typeset -g _terllm_dir="${0:A:h}" _terllm_sock="${XDG_RUNTIME_DIR:-/tmp}/terllm.sock"
typeset -g _terllm_fd _terllm_seq=0 _terllm_sugg="" _terllm_last="" _terllm_lastcmd="" _terllm_qbuf=""
typeset -g _terllm_pin=""   # set by other plugins (assist/) to keep their suggestion until the user diverges

_terllm_connect() {
  [[ -n $_terllm_fd ]] && return 0
  if [[ ! -S $_terllm_sock ]]; then
    (cd $_terllm_dir/.. && nohup python3 runtime/terllmd.py >/tmp/terllmd.log 2>&1 &)
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

_terllm_paint() {                               # the only place that touches POSTDISPLAY / highlight: ghost text = rest of last+sugg after BUFFER
  local want=$_terllm_last$_terllm_sugg tail=""
  region_highlight=("${(@)region_highlight:#*memo=terllm}")
  if [[ -n $_terllm_sugg && $CURSOR -eq ${#BUFFER} && $want == "$BUFFER"* && $want != "$BUFFER" ]]; then
    tail=${want#"$BUFFER"}
    region_highlight+=("${#BUFFER} $(( ${#BUFFER} + ${#tail} )) fg=8,memo=terllm")
  else
    _terllm_sugg=""; _terllm_pin=""
  fi
  POSTDISPLAY=$tail
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
  [[ $f[1] == s && $f[2] == $_terllm_seq && $f[3] == $BUFFER && -n $f[4] ]] || return   # stale answers are dropped
  [[ -n $_terllm_pin && -n $_terllm_sugg ]] && return    # a pinned suggestion wins over the model
  _terllm_last=$BUFFER; _terllm_sugg=$f[4]
  _terllm_paint
  zle -R
}

_terllm_redraw() {                              # line-pre-redraw: runs on every redraw
  [[ $BUFFER == *$'\n'* ]] && { _terllm_sugg=""; _terllm_paint; return }
  _terllm_paint
  [[ -n $_terllm_sugg ]] && return              # still typing along the suggestion: no model call
  [[ -n $_terllm_fd && $CURSOR -eq ${#BUFFER} && $BUFFER != $_terllm_qbuf ]] || return
  _terllm_qbuf=$BUFFER
  (( _terllm_seq++ ))
  _terllm_send "q"$'\t'"$_terllm_seq"$'\t'"$BUFFER"$'\x1f'"${(pj:\x1e:)${(z)BUFFER}}"   # + zsh's own word split
}

_terllm_init() { _terllm_qbuf=$'\x01'; _terllm_last=""; _terllm_sugg=""; _terllm_pin="" }   # new prompt: ask again, also for the empty line
_terllm_finish() { _terllm_last=""; _terllm_sugg=""; _terllm_pin=""; _terllm_paint }       # accepted line: leave no ghost text in the scrollback

_terllm_accept() {
  local want=$_terllm_last$_terllm_sugg            # the suggestion is relative to what was typed when it arrived: typing along (cl -> cle) must not append it twice
  if [[ -n $_terllm_sugg && $CURSOR -eq ${#BUFFER} && $want == "$BUFFER"* ]]; then
    BUFFER=$want; CURSOR=${#BUFFER}; _terllm_send "a"
    _terllm_last=""; _terllm_sugg=""; _terllm_paint
  else
    zle ${1:-forward-char}
  fi
}
_terllm_accept_right() { _terllm_accept forward-char }
_terllm_accept_end() { _terllm_accept end-of-line }
_terllm_accept_or_run() { _terllm_accept accept-line }   # no suggestion: behave like Enter

zle -N _terllm_apply; zle -N _terllm_accept_right; zle -N _terllm_accept_end; zle -N _terllm_accept_or_run
: ${TERLLM_ACCEPT_KEY:=^J}   # iTerm2 (Claude Code setup) sends ^J for Shift+Enter; '' = don't bind
[[ -n $TERLLM_ACCEPT_KEY ]] && bindkey "$TERLLM_ACCEPT_KEY" _terllm_accept_or_run
bindkey '^[[C' _terllm_accept_right '^[OC' _terllm_accept_right '^[[F' _terllm_accept_end '^[OF' _terllm_accept_end
add-zle-hook-widget line-pre-redraw _terllm_redraw
add-zle-hook-widget line-init _terllm_init
add-zle-hook-widget line-finish _terllm_finish
(( $+functions[_zsh_autosuggest_start] )) && typeset -g _ZSH_AUTOSUGGEST_DISABLED=1   # zsh-autosuggestions also draws in POSTDISPLAY: terllm takes over
add-zsh-hook preexec _terllm_preexec
add-zsh-hook precmd _terllm_precmd
