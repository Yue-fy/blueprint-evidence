#!/usr/bin/env bash
# Always-on watchdog for the demo (P1, captain's requests 14 Nov 12:44 / 12:52). User space only (no systemd).
#
# RESTART ONLY WHEN DEAD, NEVER A HEALTHY SERVICE. Dead =
#   (a) nothing listens on the port for 2 checks in a row  -> start it again, or
#   (b) it listens but its health check FAILS 3 times in a row (no reply within WATCHDOG_HEALTH_TIMEOUT = 20 s, or a
#       5xx / connection error): a hung process -> stop only the process that holds that port (TERM, then KILL after
#       10 s) and start it again. A slow but answering service (any 2xx-4xx reply) is never touched; one or two failures
#       are only logged (WARN). WATCHDOG_KILL_HUNG=0 turns (b) off (log only).
#
# Start it from the same shell (same environment: API keys, STOA_DATA, ...) the demo was started from: a revived
# service inherits the watchdog's environment. CONF lines: name|port|health_path|cwd|command (see the demo conf).
# Optional lines 'probe|label|url|json body|expected text' ask one cheap question end to end every
# WATCHDOG_PROBE_EVERY seconds (300) and log latency + OK/FAIL (they never restart anything).
# Env: WATCHDOG_INTERVAL (15 s), WATCHDOG_GRACE (120 s after a start), WATCHDOG_MAX_STARTS (5 per 10 min),
#      WATCHDOG_LOG, WATCHDOG_HEARTBEAT (300 s: an all-ok line at most this often).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CONF=${1:-$HERE/watchdog.demo.conf}
SHARED=${JUNCTION_SHARED:-.stoa}
LOG=${WATCHDOG_LOG:-$SHARED/logs/watchdog.log}
INTERVAL=${WATCHDOG_INTERVAL:-15}
GRACE=${WATCHDOG_GRACE:-120}
MAX_STARTS=${WATCHDOG_MAX_STARTS:-5}
HEARTBEAT=${WATCHDOG_HEARTBEAT:-300}
HEALTH_TIMEOUT=${WATCHDOG_HEALTH_TIMEOUT:-20}
KILL_HUNG=${WATCHDOG_KILL_HUNG:-1}
FAILS_TO_KILL=${WATCHDOG_FAILS:-3}
LOCK=${WATCHDOG_LOCK:-$SHARED/locks/watchdog.$(basename "$CONF").lock}
mkdir -p "$(dirname "$LOG")" "$(dirname "$LOCK")" "$SHARED/logs/watchdog"

exec 9>"$LOCK"
if ! flock -n 9; then echo "watchdog already running for $CONF (lock $LOCK)" >&2; exit 1; fi

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

declare -A DOWN STARTED STARTS LAST FAILS
names=(); probes=()
while IFS='|' read -r name port path cwd cmd; do
  [[ -z "${name// }" || "$name" == \#* ]] && continue
  if [[ "$name" == probe ]]; then  # probe|<label>|<url>|<json body>|<text the reply must contain>  (log only)
    probes+=("$port|$path|$cwd|$cmd"); continue
  fi
  names+=("$name"); declare -g "P_$name=$port" "H_$name=$path" "D_$name=$cwd" "C_$name=$cmd"
  DOWN[$name]=0; STARTED[$name]=0; STARTS[$name]=""; LAST[$name]=""; FAILS[$name]=0
done < "$CONF"
PROBE_EVERY=${WATCHDOG_PROBE_EVERY:-300}
[ ${#names[@]} -gt 0 ] || { echo "no services in $CONF" >&2; exit 2; }
log "watchdog start pid $$ conf $CONF services: ${names[*]} (interval ${INTERVAL}s; dead = not listening x2 or health failed x$FAILS_TO_KILL; kill hung: $KILL_HUNG)"
trap 'log "watchdog stop pid $$"; exit 0' INT TERM

listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }  # something accepts connections on the port

probe_all() {  # end to end: one cheap question per probe line; logs latency and OK/FAIL, never acts
  local pr label url body expect out code ms
  for pr in "${probes[@]}"; do
    IFS='|' read -r label url body expect <<< "$pr"
    out=$(curl -s -m 120 -w '\n%{http_code} %{time_total}' -H 'content-type: application/json' \
          ${body:+--data-binary "$body"} "$url" 2>/dev/null)
    code=$(tail -1 <<< "$out"); ms=$(awk -v s="${code##* }" 'BEGIN{printf "%d", s*1000}'); code=${code%% *}
    if [[ "$code" =~ ^2 && "$out" == *"$expect"* ]]; then
      log "probe $label OK ${ms}ms"
    else
      log "probe $label FAIL http=$code ${ms}ms reply: $(head -c 160 <<< "$out" | tr '\n' ' ')"
    fi
  done
}

start() {
  local n=$1 now; now=$(date +%s)
  local recent=() t
  for t in ${STARTS[$n]}; do (( now - t < 600 )) && recent+=("$t"); done
  if (( ${#recent[@]} >= MAX_STARTS )); then
    log "$n GIVE-UP: ${#recent[@]} starts in 10 min; next try after the window"; STARTS[$n]="${recent[*]}"; return
  fi
  local cwd="D_$n" cmd="C_$n" out="$SHARED/logs/watchdog/$n.log"
  ( cd "${!cwd}" && echo "=== $(date '+%F %T') watchdog start: ${!cmd}" >> "$out" \
      && exec setsid nohup bash -c "${!cmd}" >> "$out" 2>&1 < /dev/null ) &
  STARTS[$n]="${recent[*]} $now"; STARTED[$n]=$now
  log "$n START pid $! on :$(eval echo \$P_$n) cwd ${!cwd}; output $out"
}

last_beat=0; last_probe=0
while true; do
  now=$(date +%s); line=""
  if (( ${#probes[@]} > 0 && now - last_probe >= PROBE_EVERY )); then probe_all; last_probe=$now; fi
  for n in "${names[@]}"; do
    p="P_$n"; h="H_$n"; port=${!p}; path=${!h}
    if (( now - STARTED[$n] < GRACE )) && ! listening "$port"; then
      line+=" $n=starting"; continue
    fi
    if listening "$port"; then
      DOWN[$n]=0
      code=$(curl -s -o /dev/null -m "$HEALTH_TIMEOUT" -w '%{http_code} %{time_total}' "http://127.0.0.1:$port$path" 2>/dev/null)
      c=${code%% *}; ms=$(awk -v s="${code##* }" 'BEGIN{printf "%d", s*1000}')
      if [[ "$c" =~ ^[23] ]]; then st="ok"; FAILS[$n]=0
      elif [[ "$c" =~ ^4 ]]; then st="WARN($c)"; FAILS[$n]=0   # it answers: alive (wrong health path?), left alone
      else st="FAIL($c)"; FAILS[$n]=$(( FAILS[$n] + 1 )); fi
      line+=" $n=$st:${ms}ms"
      if [[ "$st" != ok && "${LAST[$n]}" == ok ]]; then log "$n $st health $path (listening)"; fi
      if [[ "$st" == ok && -n "${LAST[$n]}" && "${LAST[$n]}" != ok ]]; then log "$n OK again"; fi
      LAST[$n]=$st
      if (( FAILS[$n] >= FAILS_TO_KILL && now - STARTED[$n] >= GRACE )); then
        pids=$(lsof -t -i ":$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')
        if [[ "$KILL_HUNG" == 1 && -n "${pids// }" ]]; then
          log "$n DEAD: health failed ${FAILS[$n]}x (no reply in ${HEALTH_TIMEOUT}s / 5xx) -> stopping pid $pids on :$port, then start"
          kill $pids 2>/dev/null; for _ in 1 2 3 4 5 6 7 8 9 10; do listening "$port" || break; sleep 1; done
          listening "$port" && { log "$n still listening after TERM -> KILL $pids"; kill -9 $pids 2>/dev/null; sleep 1; }
          FAILS[$n]=0; start "$n"
        else
          log "$n DEAD? health failed ${FAILS[$n]}x but WATCHDOG_KILL_HUNG=0 (or no pid found): left alone"; FAILS[$n]=0
        fi
      fi
    else
      DOWN[$n]=$(( DOWN[$n] + 1 ))
      line+=" $n=DOWN(${DOWN[$n]})"
      [[ "${LAST[$n]}" != DOWN ]] && log "$n DOWN: nothing listening on :$port"
      LAST[$n]=DOWN
      if (( DOWN[$n] >= 2 )); then start "$n"; DOWN[$n]=0; fi
    fi
  done
  if [[ "$line" == *DOWN* || "$line" == *WARN* || "$line" == *FAIL* || "$line" == *starting* ]] || (( now - last_beat >= HEARTBEAT )); then
    log "check$line"; last_beat=$now
  fi
  sleep "$INTERVAL"
done
