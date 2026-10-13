# assist: the slow, big-model half on top of terllm's ghost text.
#   ai <what you want>   natural language -> command, pre-filled for review (alias: ask)
#   auto-fix             after a failed command, the fix appears as ghost text
#   Ctrl-O               ask the big model to complete this line (git commit gets the diff)
# Results are drawn through terllm (runtime/terllm.zsh) and pinned until you diverge.
# Backend: assist/backend.py, vendored from paiksca/zsh-ai-autocomplete (MIT, see LICENSE).
# Source AFTER runtime/terllm.zsh.
zmodload zsh/net/socket 2>/dev/null || return
(( $+functions[_terllm_paint] )) || { print -u2 "assist: source terllm.zsh first"; return }
autoload -Uz add-zsh-hook

typeset -g _assist_dir="${0:A:h}" _assist_fd="" _assist_for="" _assist_kind=""
: ${AIZSH_BACKEND:="$_assist_dir/backend.py"}
: ${AIZSH_PYTHON:=python3}
: ${AIZSH_SOCK:="${TMPDIR:-/tmp}/ai-zsh-${UID}.sock"}
: ${AIZSH_LOG:="${TMPDIR:-/tmp}/ai-zsh.log"}
: ${AIZSH_PROVIDER:=ollama}
: ${AIZSH_PROMPT_MODEL:=qwen2.5-coder:7b}
: ${AIZSH_GHOST_MODEL:=$AIZSH_PROMPT_MODEL}   # Ctrl-O + fixes: one big model resident
: ${AIZSH_OLLAMA_KEEPALIVE:=2m}              # then it unloads; terllm stays (~340 MB)
: ${AIZSH_FIM_TOKENS:=32}                    # Ctrl-O is on demand: allow longer lines
: ${AIZSH_RATE_MAX:=0}
: ${AIZSH_STATS:=0}
: ${AIZSH_HIST_N:=15}
: ${AIZSH_AUTOFIX:=1}
: ${AIZSH_AUTOFIX_SKIP:="grep egrep fgrep rg ag ack diff colordiff cmp test pgrep pkill which type whence man less more fzf ssh nvim vim vi nano ping"}
: ${ASSIST_FORCE_KEY:=^o}
export AIZSH_SOCK AIZSH_LOG AIZSH_PROVIDER AIZSH_PROMPT_MODEL AIZSH_GHOST_MODEL \
       AIZSH_OLLAMA_KEEPALIVE AIZSH_FIM_TOKENS AIZSH_RATE_MAX AIZSH_STATS

# --- backend transport (from paiksca's plugin) ------------------------------------
_assist_b64()  { print -rn -- "$1" | base64 | tr -d '\n'; }
_assist_b64d() { print -rn -- "$1" | base64 -d 2>/dev/null; }

_assist_json_get() {   # $1 = field; JSON object on stdin
    if (( $+commands[jq] )); then
        jq -r --arg k "$1" '.[$k] // empty' 2>/dev/null
    else
        "${=AIZSH_PYTHON}" -c 'import sys,json
try: print(json.load(sys.stdin).get(sys.argv[1],"") or "")
except Exception: pass' "$1" 2>/dev/null
    fi
}

_assist_via_socket() {   # $1 = frame; echoes the raw reply line
    local fd reply rc
    [[ -S "$AIZSH_SOCK" ]] || return 1
    zsocket "$AIZSH_SOCK" 2>/dev/null || return 1
    fd=$REPLY
    print -u $fd -r -- "$1"
    IFS= read -r -t "${AIZSH_READ_TIMEOUT:-130}" -u $fd reply; rc=$?
    exec {fd}>&- 2>/dev/null
    (( rc == 0 )) && [[ -n "$reply" ]] && print -r -- "$reply"
}

# $1 mode, $2 text, $3 history (computed by the caller: fc needs the parent shell)
_assist_request() {
    local frame resp
    frame="$1"$'\x1f'"$(_assist_b64 "$2")"$'\x1f'"$(_assist_b64 "$PWD")"$'\x1f'"$(_assist_b64 "$3")"
    resp="$(_assist_via_socket "$frame")"
    if [[ -z "$resp" ]]; then
        resp="$(print -r -- "$frame" | "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" oneshot 2>/dev/null)"
        _assist_start_daemon
    fi
    [[ -n "$resp" ]] && _assist_b64d "$resp"
}

_assist_history() { fc -ln -$AIZSH_HIST_N 2>/dev/null; }

_assist_daemon_alive() {
    [[ "$(AIZSH_READ_TIMEOUT=0.5 _assist_via_socket ping)" == pong* ]]
}

_assist_start_daemon() {
    local lock="${AIZSH_SOCK}.lock"
    if mkdir "$lock" 2>/dev/null; then           # atomic: only one starter wins
        ( nohup "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" daemon >>"$AIZSH_LOG" 2>&1 & ) &!
        ( command sleep 3; rmdir "$lock" 2>/dev/null ) &!
    fi
}

# --- async fetch, drawn through terllm --------------------------------------------
_assist_cancel() {   # closing the pipe kills the fetch; the daemon then aborts generation
    [[ -n $_assist_fd ]] || return
    zle -F $_assist_fd 2>/dev/null
    exec {_assist_fd}<&- 2>/dev/null
    _assist_fd=""
}

# $1 mode, $2 text, $3 JSON field, $4 kind: fix (full command) | complete (suffix of $BUFFER)
_assist_async() {
    _assist_cancel
    local hist="$(_assist_history)"
    exec {_assist_fd}< <(_assist_request "$1" "$2" "$hist" | _assist_json_get "$3")
    _assist_kind=$4 _assist_for=$BUFFER
    zle -F $_assist_fd _assist_reply
}

_assist_reply() {   # zle -F handler
    local fd=$1 line out=""
    while IFS= read -r -u $fd line; do out+=$line; done
    zle -F $fd; exec {fd}<&-; _assist_fd=""
    zle -M ""
    [[ -n $out ]] && zle _assist_show -- "$out"
}

_assist_show() {
    local out=$1
    if [[ $_assist_kind == fix ]]; then
        [[ $out == "$BUFFER"* && $out != "$BUFFER" ]] || return   # still fits what's typed
        _terllm_last="" _terllm_sugg=$out
    else
        [[ $BUFFER == "$_assist_for" ]] || return                   # line changed meanwhile
        _terllm_last=$BUFFER _terllm_sugg=$out
    fi
    _terllm_pin=1
    _terllm_paint
    zle -R
}
zle -N _assist_show

# --- Ctrl-O: big-model completion of the current line -----------------------------
_assist_complete() {
    [[ -n $BUFFER ]] || return
    zle -M "assist: asking ${AIZSH_GHOST_MODEL}..."
    _assist_async ghost "$BUFFER" suggestion complete
}
zle -N _assist_complete
bindkey "$ASSIST_FORCE_KEY" _assist_complete

# --- auto-fix: a failed command's fix becomes ghost text --------------------------
_assist_preexec() { _assist_last=$1; _assist_ran=1; _assist_cancel; }

_assist_precmd() {
    local ec=$?
    [[ -n $_assist_ran ]] || return $ec
    _assist_ran=
    [[ $AIZSH_AUTOFIX == 1 ]] && (( ec != 0 && ec < 128 )) || return $ec   # >=128: signals
    local first=${${_assist_last%%[[:space:]]*}:t}
    [[ -n $first && " $AIZSH_AUTOFIX_SKIP " != *" $first "* ]] || return $ec
    [[ $first == (ai|ask|assist) ]] && return $ec
    _assist_async fix "${ec}"$'\x1e'"${_assist_last}" command fix
    return $ec   # keep $? intact for the hooks after us (terllm reports it too)
}

add-zsh-hook preexec _assist_preexec
add-zsh-hook precmd _assist_precmd
precmd_functions=(_assist_precmd ${precmd_functions:#_assist_precmd})   # first: sees the real $?

# --- ai: natural language -> command (paiksca's `prompt`, renamed) ----------------
# Spinner on stderr; Esc or Ctrl-C dismisses. Echoes the decoded JSON.
_assist_fetch_spin() {
    emulate -L zsh
    setopt localoptions no_notify
    local mode="$1" text="$2" label="${3:-thinking}" tmp out hist="$(_assist_history)"
    local DIM=$'\e[2m' RST=$'\e[0m' key skipped= i=1
    local chars=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    tmp="$(mktemp -t assist.XXXXXX)" || return 1
    { _assist_request "$mode" "$text" "$hist" >| "$tmp" 2>/dev/null } &!
    local pid=$!
    trap 'skipped=1' INT
    while kill -0 $pid 2>/dev/null; do
        [[ -n $skipped ]] && { kill $pid 2>/dev/null; break; }
        printf '\r%s%s %s… (Esc to dismiss)%s' "$DIM" "${chars[i]}" "$label" "$RST" >&2
        i=$(( i % 10 + 1 ))
        if read -t 0.1 -k 1 -s key 2>/dev/null && [[ "$key" == $'\e' ]]; then
            if read -t 0.02 -k 1 -s 2>/dev/null; then
                while read -t 0.005 -k 1 -s 2>/dev/null; do :; done   # arrow/fn key
            else
                kill $pid 2>/dev/null; skipped=1; break               # bare Esc
            fi
        fi
    done
    trap - INT
    printf '\r\e[K' >&2
    [[ -n $skipped ]] && { rm -f "$tmp"; return 1; }
    out="$(<"$tmp")"; rm -f "$tmp"
    [[ -n $out ]] && print -r -- "$out"
}

function ai {   # `function`: immune to the noglob alias below on re-source
    emulate -L zsh
    local query="$*" json cmd expl danger chosen
    [[ -n "$query" ]] || { print -u2 "usage: ai <what you want>"; return 1; }
    json="$(_assist_fetch_spin prompt "$query" thinking)" || return 1
    cmd="$(print -r -- "$json"    | _assist_json_get command)"
    expl="$(print -r -- "$json"   | _assist_json_get explanation)"
    danger="$(print -r -- "$json" | _assist_json_get danger)"
    [[ -n "$cmd" ]] || { print -u2 "assist: ${expl:-no command returned}"; return 1; }

    local -a alts
    alts=("${(@f)$(print -r -- "$json" | { (( $+commands[jq] )) && jq -r '.alternatives[]? // empty' 2>/dev/null; })}")
    alts=(${(M)alts:#?*})
    chosen="$cmd"
    if (( ${#alts} )) && (( $+commands[fzf] )); then
        local TAB=$'\t' picked a
        picked="$({
            print -r -- "${cmd}${TAB}${expl}"
            for a in "${alts[@]}"; do print -r -- "${a}${TAB}(alternative)"; done
        } | fzf --height='~45%' --reverse --no-sort --prompt='run> ' \
                --delimiter=$'\t' --with-nth='1,2' \
                --header='↑↓ choose · Enter pre-fill · Esc cancel')"
        [[ -z "$picked" ]] && { print -u2 "cancelled"; return 130; }
        chosen="${picked%%$TAB*}"
    fi

    local DIM=$'\e[2m' RST=$'\e[0m' YEL=$'\e[33m' RED=$'\e[1;31m'
    case "$danger" in
        dangerous)
            print -r -- "${RED}⛔ DANGEROUS${RST} ${DIM}${expl}${RST}"
            print -r -- "   ${chosen}"
            read -q "REPLY?   pre-fill this command for review anyway? [y/N] " \
                || { print; print -u2 "aborted"; return 1; }
            print ;;
        caution) print -r -- "${YEL}⚠ caution${RST}  ${DIM}${expl}${RST}" ;;
        *)       [[ -n "$expl" ]] && print -r -- "${DIM}↳ ${expl}${RST}" ;;
    esac
    print -z -- "$chosen"     # next prompt, editable; nothing runs until Enter
}
alias ask='noglob ai'
alias ai='noglob ai'

# --- management -------------------------------------------------------------------
assist() {
    case "${1:-status}" in
        status)
            if _assist_daemon_alive; then print "● backend running   $AIZSH_SOCK"
            else print "○ backend not running"; fi
            print "  model   : $AIZSH_PROMPT_MODEL (keep_alive $AIZSH_OLLAMA_KEEPALIVE)"
            print "  autofix : $AIZSH_AUTOFIX    Ctrl-O key: $ASSIST_FORCE_KEY"
            print "  log     : $AIZSH_LOG" ;;
        doctor)  "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" doctor ;;
        restart) "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" stop >/dev/null 2>&1
                 rmdir "${AIZSH_SOCK}.lock" 2>/dev/null
                 _assist_start_daemon; print "restarted" ;;
        stop)    "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" stop ;;
        log)     ${PAGER:-cat} "$AIZSH_LOG" ;;
        autofix) case "$2" in
                     on)  AIZSH_AUTOFIX=1; print "auto-fix: ON" ;;
                     off) AIZSH_AUTOFIX=0; print "auto-fix: OFF" ;;
                     *)   print "auto-fix is $AIZSH_AUTOFIX — use: assist autofix on|off" ;;
                 esac ;;
        *) print "usage: assist {status|doctor|restart|stop|log|autofix on|off}" ;;
    esac
}

_assist_daemon_alive || _assist_start_daemon
