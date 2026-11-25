#!/usr/bin/env bash
# ============================================================
# TURN-KEY LAUNCHER — standalone, for use from /home/flintx/soma-nov/turn-key
#   ./launcher.sh              start app + CORS proxy (detached)
#   ./launcher.sh status       what is running, on which port
#   ./launcher.sh stop         stop both
#   ./launcher.sh restart      stop then start
#   ./launcher.sh logs [vite|cors]
#   ./launcher.sh fg           CORS in background, vite in foreground
# Env: VITE_PORT (default 5173), CORS_PORT (default 8765)
# Logs: .launcher/logs/  State: .launcher/
# ============================================================
set -uo pipefail

TK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOLID="$TK/profile_builder/solid"
WEB="$TK/profile_builder/web"
STATE="$TK/.launcher"
LOGS="$STATE/logs"
PIDS="$STATE/pids"
VITE_PORT_FILE="$STATE/vite.port"
CORS_PORT_FILE="$STATE/cors.port"
mkdir -p "$LOGS" "$PIDS"

BOLD=$(tput bold 2>/dev/null || true)
NORMAL=$(tput sgr0 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)
CYAN=$(tput setaf 6 2>/dev/null || true)
DIM=$(tput dim 2>/dev/null || tput setaf 7 2>/dev/null || true)

# ── helpers ─────────────────────────────────────────────────
listen_pids() { lsof -ti ":$1" -sTCP:LISTEN 2>/dev/null | sort -u; }
port_has_listener() { [ -n "$(listen_pids "$1")" ]; }
my_pgid() { ps -o pgid= -p "$$" 2>/dev/null | tr -d ' '; }

proc_desc() {
  local port="$1" pid args
  pid=$(listen_pids "$port" | head -1)
  [ -z "$pid" ] && { printf 'unknown'; return; }
  args=$(ps -o args= -p "$pid" 2>/dev/null | cut -c1-70)
  printf 'pid %s %s' "$pid" "${args:-(?)}"
}

listener_matches() {
  local port="$1" pattern="$2" pid args
  for pid in $(listen_pids "$port"); do
    args=$(ps -o args= -p "$pid" 2>/dev/null)
    case "$args" in *"$pattern"*) return 0 ;; esac
  done
  return 1
}

# listener whose working directory is this project (survives relative argv)
listener_in_dir() {
  local port="$1" dir="$2" pid cwd args real
  real=$(readlink -f "$dir" 2>/dev/null || printf '%s' "$dir")
  for pid in $(listen_pids "$port"); do
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
    [ -n "$cwd" ] && [ "$cwd" = "$real" ] && return 0
    args=$(ps -o args= -p "$pid" 2>/dev/null)
    case "$args" in *"$dir"*|*"$real"*) return 0 ;; esac
  done
  return 1
}

all_listener_pids() { lsof -nP -iTCP -sTCP:LISTEN -ti 2>/dev/null | sort -u; }

owned_pids_dir() {
  local dir="$1" pid cwd real
  real=$(readlink -f "$dir" 2>/dev/null || printf '%s' "$dir")
  for pid in $(all_listener_pids); do
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
    [ -n "$cwd" ] && [ "$cwd" = "$real" ] && printf '%s\n' "$pid"
  done
}

stop_pid() {
  local pid="$1" pgid mine waited=0
  [ -z "$pid" ] && return 0
  kill -0 "$pid" 2>/dev/null || return 0
  mine=$(my_pgid)
  pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
  if [ -n "$pgid" ] && [ "$pgid" != "$mine" ]; then
    kill -TERM -"$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
  while (( waited < 25 )); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.1
    waited=$((waited + 1))
  done
  if [ -n "$pgid" ] && [ "$pgid" != "$mine" ]; then
    kill -KILL -"$pgid" 2>/dev/null || true
  fi
  kill -KILL "$pid" 2>/dev/null || true
}

stop_port_listener() {
  local port="$1" pid pgid mine waited=0
  mine=$(my_pgid)
  for pid in $(listen_pids "$port"); do
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -n "$pgid" ] && [ "$pgid" != "$mine" ]; then
      kill -TERM -"$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  while (( waited < 30 )); do
    [ -z "$(listen_pids "$port")" ] && return 0
    sleep 0.1
    waited=$((waited + 1))
  done
  for pid in $(listen_pids "$port"); do
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -n "$pgid" ] && [ "$pgid" != "$mine" ]; then
      kill -KILL -"$pgid" 2>/dev/null || true
    fi
    kill -KILL "$pid" 2>/dev/null || true
  done
  sleep 0.2
  [ -z "$(listen_pids "$port")" ]
}

next_free_port() {
  local p="$1" tries=0
  while (( tries < 3000 )); do
    (( p > 65535 )) && p=5173
    port_has_listener "$p" || { printf '%s' "$p"; return 0; }
    p=$((p + 1))
    tries=$((tries + 1))
  done
  return 1
}

wait_listen() {
  local port="$1" limit="${2:-25}" waited=0
  while (( waited < limit * 5 )); do
    port_has_listener "$port" && return 0
    sleep 0.2
    waited=$((waited + 1))
  done
  return 1
}

vite_present() {
  [ -x "$SOLID/node_modules/.bin/vite" ] || [ -f "$SOLID/node_modules/vite/bin/vite.js" ]
}

ensure_deps() {
  vite_present && return 0
  local log="$LOGS/install.log" rc=0
  printf '  %svite missing — npm install (log: %s)%s\n' "$YELLOW" "$log" "$NORMAL"
  ( cd "$SOLID" && npm install --no-audit --no-fund ) > "$log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '  %sretrying with --legacy-peer-deps%s\n' "$YELLOW" "$NORMAL"
    ( cd "$SOLID" && npm install --no-audit --no-fund --legacy-peer-deps ) >> "$log" 2>&1 || true
  fi
  if vite_present; then
    printf '  %sdependencies installed%s\n' "$GREEN" "$NORMAL"
    return 0
  fi
  printf '  %sdependency install failed%s\n' "$RED" "$NORMAL"
  tail -6 "$log" | sed 's/^/    | /'
  return 1
}

# ── port selection ──────────────────────────────────────────
vite_port() {
  if [ -n "${VITE_PORT:-}" ]; then printf '%s' "$VITE_PORT"
  elif [ -f "$VITE_PORT_FILE" ]; then tr -d '[:space:]' < "$VITE_PORT_FILE"
  else printf '5173'; fi
}
cors_port() {
  if [ -f "$CORS_PORT_FILE" ]; then tr -d '[:space:]' < "$CORS_PORT_FILE"
  else printf '%s' "${CORS_PORT:-8765}"; fi
}

# ── start ───────────────────────────────────────────────────
start_cors() {
  local port pid log
  port=$(cors_port)
  log="$LOGS/cors.log"

  if port_has_listener "$port"; then
    if listener_matches "$port" "cors_proxy.py"; then
      pid=$(listen_pids "$port" | head -1)
      printf '  %scors already up%s on %s (pid %s)\n' "$DIM" "$NORMAL" "$port" "$pid"
      return 0
    fi
    local np
    np=$(next_free_port $((port + 1))) || { printf '  %sno free port for cors%s\n' "$YELLOW" "$NORMAL"; return 1; }
    printf '  %sport %s held by %s — cors moving to %s%s\n' "$YELLOW" "$port" "$(proc_desc "$port")" "$np" "$NORMAL"
    port="$np"
  fi

  [ -f "$WEB/cors_proxy.py" ] || { printf '  %scors_proxy.py missing%s\n' "$YELLOW" "$NORMAL"; return 1; }

  : > "$log"
  (
    cd "$WEB" || exit 1
    setsid nohup python3 cors_proxy.py "$port" </dev/null >> "$log" 2>&1 &
    echo $! > "$PIDS/cors.pid"
  )
  if wait_listen "$port" 10; then
    printf '%s\n' "$port" > "$CORS_PORT_FILE"
    printf '  %scors up%s on %s\n' "$GREEN" "$NORMAL" "$port"
    return 0
  fi
  printf '  %scors did not open port %s%s\n' "$YELLOW" "$port" "$NORMAL"
  tail -4 "$log" | sed 's/^/    | /'
  return 1
}

start_vite() {
  local port pid log attempt
  port=$(vite_port)
  log="$LOGS/vite.log"

  if port_has_listener "$port"; then
    if listener_in_dir "$port" "$SOLID"; then
      pid=$(listen_pids "$port" | head -1)
      printf '%s\n' "$port" > "$VITE_PORT_FILE"
      printf '  %svite already up%s on %s (pid %s)\n' "$DIM" "$NORMAL" "$port" "$pid"
      return 0
    fi
    local np
    np=$(next_free_port $((port + 1))) || { printf '  %sno free port for vite%s\n' "$RED" "$NORMAL"; return 1; }
    printf '  %sport %s held by %s — vite moving to %s%s\n' "$YELLOW" "$port" "$(proc_desc "$port")" "$np" "$NORMAL"
    port="$np"
  fi

  ensure_deps || return 1

  for attempt in 1 2; do
    : > "$log"
    (
      cd "$SOLID" || exit 1
      setsid nohup ./node_modules/.bin/vite --port "$port" --host 0.0.0.0 </dev/null >> "$log" 2>&1 &
      echo $! > "$PIDS/vite.pid"
    )
    sleep 0.2
    if wait_listen "$port" 25; then
      printf '%s\n' "$port" > "$VITE_PORT_FILE"
      pid=$(listen_pids "$port" | head -1)
      [ -n "$pid" ] && printf '%s\n' "$pid" > "$PIDS/vite.pid"
      printf '  %svite up%s on %s\n' "$GREEN" "$NORMAL" "$port"
      return 0
    fi
    printf '  %svite did not open port %s (attempt %s)%s\n' "$YELLOW" "$port" "$attempt" "$NORMAL"
    if grep -q 'EADDRINUSE' "$log" 2>/dev/null; then
      local np
      np=$(next_free_port $((port + 1))) || break
      port="$np"
      printf '  retrying on port %s\n' "$port"
      continue
    fi
    if grep -qE 'Cannot find module|ERR_MODULE_NOT_FOUND|MODULE_NOT_FOUND|Failed to resolve import' "$log" 2>/dev/null; then
      printf '  missing module detected — reinstalling\n'
      ensure_deps >/dev/null 2>&1 || true
      continue
    fi
    break
  done
  printf '  %svite failed to start%s\n' "$RED" "$NORMAL"
  tail -10 "$log" | sed 's/^/    | /'
  return 1
}

cmd_start() {
  printf '\n  %sTurn-Key%s\n' "$BOLD" "$NORMAL"
  local vp cp rc=0
  start_cors || rc=$?
  start_vite || rc=$?
  vp=$(vite_port)
  cp=$(cors_port)
  printf '\n'
  if port_has_listener "$vp"; then
    printf '  %sapp%s  http://127.0.0.1:%s\n' "$BOLD" "$NORMAL" "$vp"
  else
    printf '  %sapp is not running%s\n' "$RED" "$NORMAL"
  fi
  if port_has_listener "$cp"; then
    printf '  %scors%s http://127.0.0.1:%s\n' "$BOLD" "$NORMAL" "$cp"
  else
    printf '  %scors not running%s\n' "$DIM" "$NORMAL"
  fi
  printf '  %slogs%s %s\n\n' "$DIM" "$NORMAL" "$LOGS"
  return "$rc"
}

cmd_stop() {
  local vp cp pid p name
  vp=$(vite_port)
  cp=$(cors_port)
  for name in vite cors; do
    pid=$( [ -f "$PIDS/$name.pid" ] && tr -d '[:space:]' < "$PIDS/$name.pid" )
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      printf '  stopping %s (pid %s)\n' "$name" "$pid"
      stop_pid "$pid"
    fi
    rm -f "$PIDS/$name.pid"
  done
  if port_has_listener "$vp" && listener_in_dir "$vp" "$SOLID"; then
    stop_port_listener "$vp" || true
  fi
  if port_has_listener "$cp" && listener_matches "$cp" "cors_proxy.py"; then
    stop_port_listener "$cp" || true
  fi
  for p in $(owned_pids_dir "$SOLID"); do
    printf '  stopping leftover vite (pid %s)\n' "$p"
    stop_pid "$p"
  done
  printf '  %sturn-key stopped%s\n' "$GREEN" "$NORMAL"
}

cmd_status() {
  local vp cp vpid cpid
  vp=$(vite_port)
  cp=$(cors_port)
  vpid=$( [ -f "$PIDS/vite.pid" ] && tr -d '[:space:]' < "$PIDS/vite.pid" )
  cpid=$( [ -f "$PIDS/cors.pid" ] && tr -d '[:space:]' < "$PIDS/cors.pid" )
  printf '\n  %sTurn-Key status%s\n\n' "$BOLD" "$NORMAL"
  if port_has_listener "$vp"; then
    printf '  app   %sUP%s    http://127.0.0.1:%s  pid %s\n' "$GREEN" "$NORMAL" "$vp" "${vpid:--}"
  else
    printf '  app   %sdown%s  port %s\n' "$RED" "$NORMAL" "$vp"
  fi
  if port_has_listener "$cp"; then
    printf '  cors  %sUP%s    http://127.0.0.1:%s  pid %s\n' "$GREEN" "$NORMAL" "$cp" "${cpid:--}"
  else
    printf '  cors  %sdown%s  port %s\n' "$DIM" "$NORMAL" "$cp"
  fi
  printf '\n  logs  %s\n  dir   %s\n\n' "$LOGS" "$SOLID"
}

cmd_logs() {
  local which="${1:-vite}" f
  case "$which" in
    cors) f="$LOGS/cors.log" ;;
    vite|app) f="$LOGS/vite.log" ;;
    *) printf 'usage: %s logs [vite|cors]\n' "$0"; exit 2 ;;
  esac
  if [ ! -f "$f" ]; then
    printf '  no log at %s yet.\n  (an instance started before this launcher has no log; restart with ./launcher.sh to capture one)\n' "$f"
    return 0
  fi
  tail -60 "$f"
}

cmd_fg() {
  ensure_deps || exit 1
  start_cors || true
  local port
  port=$(vite_port)
  if port_has_listener "$port" && listener_in_dir "$port" "$SOLID"; then
    printf '  stopping background vite on %s first\n' "$port"
    stop_port_listener "$port" || true
  fi
  printf '  %sforeground vite on http://127.0.0.1:%s%s  (ctrl+c to stop)\n\n' "$GREEN" "$port" "$NORMAL"
  printf '%s\n' "$port" > "$VITE_PORT_FILE"
  cd "$SOLID" || exit 1
  exec ./node_modules/.bin/vite --port "$port" --host 0.0.0.0
}

usage() {
  cat <<EOF
TURN-KEY LAUNCHER

  ./launcher.sh              start app + CORS (detached), print URLs
  ./launcher.sh status       show what is running
  ./launcher.sh stop         stop both
  ./launcher.sh restart      stop then start
  ./launcher.sh logs [vite|cors]
  ./launcher.sh fg           vite in the foreground (cors still detached)

  VITE_PORT=5173 CORS_PORT=8765 ./launcher.sh
EOF
}

case "${1:-start}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) cmd_logs "${2:-vite}" ;;
  fg|foreground) cmd_fg ;;
  -h|--help|help) usage ;;
  *) printf 'unknown command: %s\n\n' "$1"; usage; exit 2 ;;
esac
