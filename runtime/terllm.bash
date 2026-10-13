# terllm for bash >= 5.1: inline command suggestions from a local model (see runtime/terllmd.py).
# Usage: source /path/to/terllm.bash          Accept the suggestion: Right arrow / End / Ctrl-E at the end of the line. Never executes anything.
#
# Readline cannot draw text it does not own, so the suggestion is real text after the cursor that readline paints as the *active region*
# (grey). Every key that could act on it first removes it again (strip); Enter strips it, so only what you typed is ever executed.
[[ $- == *i* ]] || return 0
(( BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1) )) || return 0   # READLINE_MARK
_terllm_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_terllm_sock=${XDG_RUNTIME_DIR:-/tmp}/terllm.sock
_terllm_reg=off _terllm_seq=0 _terllm_sugg="" _terllm_line="" _terllm_tail="" _terllm_hn=$HISTCMD

_terllm_connect() {
  [[ -n ${TERLLM_PID:-} ]] && kill -0 "$TERLLM_PID" 2>/dev/null && return 0
  if [[ ! -S $_terllm_sock ]]; then
    (cd "$_terllm_dir/.." && HISTFILE=$HISTFILE nohup setsid python3 runtime/terllmd.py >/tmp/terllmd.log 2>&1 &)
    return 1                                       # daemon is warming up; try again on the next prompt
  fi
  coproc TERLLM { exec python3 -u "$_terllm_dir/terllm_relay.py" "$_terllm_sock" 2>/dev/null; }
}

_terllm_send() { [[ -n ${TERLLM_PID:-} ]] && printf '%s\n' "$1" >&"${TERLLM[1]}" 2>/dev/null; }

_terllm_precmd() {                                 # first in PROMPT_COMMAND: previous command finished
  local code=$? last=""
  _terllm_line="" _terllm_tail=""
  _terllm_connect || return $code
  if (( HISTCMD != _terllm_hn )); then
    last=$(HISTTIMEFORMAT= fc -ln -1 2>/dev/null); last=${last#"${last%%[![:space:]]*}"}
    last=${last//$'\t'/ }; last=${last//$'\n'/ }
  fi
  _terllm_hn=$HISTCMD
  _terllm_send "c"$'\t'"$code"$'\t'"$PWD"$'\t'"${VIRTUAL_ENV:-}"$'\t'"$last"
  return $code
}

_terllm_ask() {                                    # $1 = typed text -> _terllm_sugg (empty = abstain or timeout)
  _terllm_sugg=""
  [[ -n ${TERLLM_PID:-} ]] || return
  local seq=$(( ++_terllm_seq )) line f
  _terllm_send "q"$'\t'"$seq"$'\t'"$1" || return
  while IFS= read -r -t 0.15 -u "${TERLLM[0]}" line; do
    IFS=$'\t' read -r -a f <<<"$line"
    if [[ ${f[0]} == s && ${f[1]} == "$seq" ]]; then _terllm_sugg=${f[3]-}; return; fi   # answers to older queries are skipped
  done
}

_terllm_region() {                                 # the macro's N\C-a is exchange-point-and-mark (paints the region) only while there is a suggestion;
  local want=off; [[ -n $_terllm_tail ]] && want=on            # an empty region would still paint one character, so otherwise it is a no-op function
  [[ $want == "$_terllm_reg" ]] && return
  _terllm_reg=$want
  if [[ $want == on ]]; then bind '"\C-x\C-z\C-a": exchange-point-and-mark'; else bind '"\C-x\C-z\C-a": emacs-editing-mode'; fi
}

_terllm_strip() {                                  # remove our suggestion from the line if the line is still exactly typed+suggestion
  if [[ -n $_terllm_tail && $READLINE_LINE == "$_terllm_line" ]]; then
    READLINE_LINE=${READLINE_LINE:0:${#READLINE_LINE}-${#_terllm_tail}}
    (( READLINE_POINT > ${#READLINE_LINE} )) && READLINE_POINT=${#READLINE_LINE}
  fi
  _terllm_line="" _terllm_tail=""; READLINE_MARK=$READLINE_POINT
}

_terllm_show() {                                   # line is only typed text: ask, then append the suggestion after the cursor
  local typed=$READLINE_LINE
  READLINE_MARK=$READLINE_POINT
  [[ -n $typed && $typed != *$'\n'* ]] && (( READLINE_POINT == ${#typed} )) || return
  _terllm_ask "$typed"
  [[ -n $_terllm_sugg && $_terllm_sugg != *$'\n'* ]] || return
  _terllm_tail=$_terllm_sugg; READLINE_LINE=$typed$_terllm_tail; _terllm_line=$READLINE_LINE; READLINE_MARK=${#READLINE_LINE}
}

_terllm_show_region() { _terllm_show; _terllm_region; }

_terllm_char() {                                   # $1 = octal code of the typed character
  local c; printf -v c "\\$1"
  if [[ -n $_terllm_tail && $READLINE_LINE == "$_terllm_line" && $READLINE_POINT -eq $(( ${#READLINE_LINE} - ${#_terllm_tail} )) && $_terllm_tail == "$c"* ]]; then
    _terllm_tail=${_terllm_tail:1}; (( READLINE_POINT++ )); _terllm_line=$READLINE_LINE    # typing along the suggestion: no model call
    [[ -z $_terllm_tail ]] && _terllm_line=""
    _terllm_region; return
  fi
  _terllm_strip
  READLINE_LINE=${READLINE_LINE:0:READLINE_POINT}$c${READLINE_LINE:READLINE_POINT}; (( READLINE_POINT++ ))
  _terllm_show_region
}

_terllm_bs() {
  _terllm_strip
  if (( READLINE_POINT > 0 )); then
    READLINE_LINE=${READLINE_LINE:0:READLINE_POINT-1}${READLINE_LINE:READLINE_POINT}; (( READLINE_POINT-- ))
  fi
  _terllm_show_region
}

_terllm_accept() {                                 # $1 = right | end
  if [[ -n $_terllm_tail && $READLINE_LINE == "$_terllm_line" && $READLINE_POINT -eq $(( ${#READLINE_LINE} - ${#_terllm_tail} )) ]]; then
    READLINE_POINT=${#READLINE_LINE}; _terllm_line="" _terllm_tail=""; _terllm_send "a"
  elif [[ $1 == end ]]; then
    _terllm_strip; READLINE_POINT=${#READLINE_LINE}
  else
    _terllm_strip; (( READLINE_POINT < ${#READLINE_LINE} )) && (( READLINE_POINT++ ))
  fi
  READLINE_MARK=$READLINE_POINT; _terllm_region
}

_terllm_home() { _terllm_strip; READLINE_POINT=0; READLINE_MARK=0; _terllm_region; }
_terllm_left() { _terllm_strip; (( READLINE_POINT > 0 )) && (( READLINE_POINT-- )); READLINE_MARK=$READLINE_POINT; _terllm_region; }
_terllm_kill_end() { _terllm_strip; READLINE_LINE=${READLINE_LINE:0:READLINE_POINT}; READLINE_MARK=$READLINE_POINT; _terllm_region; }
_terllm_kill_start() { _terllm_strip; READLINE_LINE=${READLINE_LINE:READLINE_POINT}; READLINE_POINT=0; READLINE_MARK=0; _terllm_region; }
_terllm_kill_word() {
  _terllm_strip
  local head=${READLINE_LINE:0:READLINE_POINT}
  head=${head%"${head##*[![:space:]]}"}; head=${head%"${head##*[[:space:]]}"}
  READLINE_LINE=$head${READLINE_LINE:READLINE_POINT}; READLINE_POINT=${#head}; READLINE_MARK=$READLINE_POINT; _terllm_region
}

_terllm_bind() {
  local N='\C-x\C-z' i c k esc
  for i in {32..126}; do                           # N<char>: type that character; the macro then re-activates the region (grey suggestion)
    printf -v c "\\$(printf %03o "$i")"; esc=$c; [[ $c == '"' || $c == '\' ]] && esc="\\$c"
    bind -x "\"$N$esc\": _terllm_char $(printf %03o "$i")"
    bind "\"$esc\": \"$N$esc$N\\C-a$N\\C-a\""
  done
  bind "\"$N\\C-a\": emacs-editing-mode"
  bind -x "\"$N\\C-s\": _terllm_strip"
  bind "\"$N\\C-r\": accept-line"
  bind -x "\"$N\\C-?\": _terllm_bs";           for k in '\C-?' '\C-h'; do bind "\"$k\": \"$N\\C-?$N\\C-a$N\\C-a\""; done
  bind -x "\"$N\\C-f\": _terllm_accept right"; for k in '\e[C' '\eOC' '\C-f'; do bind "\"$k\": \"$N\\C-f$N\\C-a$N\\C-a\""; done
  bind -x "\"$N\\C-e\": _terllm_accept end";   for k in '\e[F' '\eOF' '\e[4~' '\C-e'; do bind "\"$k\": \"$N\\C-e$N\\C-a$N\\C-a\""; done
  bind -x "\"$N\\C-b\": _terllm_left";   for k in '\e[D' '\eOD' '\C-b'; do bind "\"$k\": \"$N\\C-b$N\\C-a$N\\C-a\""; done
  bind -x "\"$N\\C-t\": _terllm_home";   for k in '\e[H' '\eOH' '\e[1~' '\C-a'; do bind "\"$k\": \"$N\\C-t$N\\C-a$N\\C-a\""; done
  bind -x "\"$N\\C-k\": _terllm_kill_end";   bind "\"\\C-k\": \"$N\\C-k$N\\C-a$N\\C-a\""
  bind -x "\"$N\\C-u\": _terllm_kill_start"; bind "\"\\C-u\": \"$N\\C-u$N\\C-a$N\\C-a\""
  bind -x "\"$N\\C-w\": _terllm_kill_word";  bind "\"\\C-w\": \"$N\\C-w$N\\C-a$N\\C-a\""
  # keys that run a readline function: strip the suggestion first, then the function (bound once under N so the macro cannot recurse)
  local -A fn=(['\e[A']=previous-history ['\eOA']=previous-history ['\C-p']=previous-history ['\e[B']=next-history ['\eOB']=next-history ['\C-n']=next-history
               ['\C-i']=complete ['\C-r']=reverse-search-history ['\C-l']=clear-screen ['\C-m']=accept-line ['\C-j']=accept-line
               ['\eb']=backward-word ['\ef']=forward-word ['\e[1;5D']=backward-word ['\e[1;5C']=forward-word ['\C-d']=delete-char)
  for k in "${!fn[@]}"; do
    bind "\"$N\\C-]$k\": ${fn[$k]}"
    bind "\"$k\": \"$N\\C-s$N\\C-]$k\""
  done
}
_terllm_bind
bind 'set enable-active-region on'
bind 'set active-region-start-color "\e[90m"'
bind 'set active-region-end-color "\e[0m"'
if [[ ${PROMPT_COMMAND@a} == *a* ]]; then PROMPT_COMMAND=(_terllm_precmd "${PROMPT_COMMAND[@]}"); else PROMPT_COMMAND="_terllm_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}"; fi
