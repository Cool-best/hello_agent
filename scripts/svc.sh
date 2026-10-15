#!/usr/bin/env bash
# 一键启停三个常驻服务:飞书操作台(8000) / 主服务(8001) / kb_service(8002)
# (工具 MCP 服务器为外部独立项目,不在本仓库,如需请单独部署启动,见 SPEC「工具 MCP 服务器(外部依赖)」)
# 用法: scripts/svc.sh start|stop|restart|status
# 日志: .dev_ws/logs/<name>.log;pidfile: .dev_ws/pids/<name>.pid(存 Windows PID)
set -u
cd "$(dirname "$0")/.."
PY="myenv310/Scripts/python.exe"
LOG_DIR=".dev_ws/logs"
PID_DIR=".dev_ws/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# name port module...(顺序即启动顺序:main 最后,它的 lifespan 探测依赖下游就绪)
SERVICES=(
  "feishu_console 8000 -m feishu_console.app"
  "kb_service 8002 -m kb_service.main"
  "main       8001 main.py"
)

port_pid() { # 监听该端口的 Windows PID
  netstat -ano | grep -E ":$1\s" | grep LISTENING | awk '{print $NF}' | head -1
}

winpid_alive() { # Windows PID 是否存活(Git Bash 的 kill 只认 MSYS pid,用 tasklist 判活)
  [ -n "$1" ] && tasklist //FI "PID eq $1" 2>/dev/null | grep -qE "\b$1\b"
}

proc_start_time() { # Windows PID -> 启动时间(yyyy-MM-dd HH:mm:ss)
  [ -n "$1" ] && powershell -NoProfile -Command \
    "(Get-Process -Id $1 -ErrorAction SilentlyContinue).StartTime.ToString('yyyy-MM-dd HH:mm:ss')" 2>/dev/null
}

svc_pid() { # 服务的当前 Windows PID:pidfile 优先,端口兜底
  local name="$1" port="$2" pid=""
  [ -f "$PID_DIR/$name.pid" ] && pid="$(cat "$PID_DIR/$name.pid")"
  winpid_alive "$pid" || pid="$(port_pid "$port")"
  echo "$pid"
}

is_running() { # pidfile 有效且端口在听
  local name="$1" port="$2"
  local pidfile="$PID_DIR/$name.pid"
  [ -f "$pidfile" ] && winpid_alive "$(cat "$pidfile")" && [ -n "$(port_pid "$port")" ]
}

start_one() {
  local name="$1" port="$2"; shift 2
  if is_running "$name" "$port"; then
    echo "[skip] $name 已在运行(pid $(cat "$PID_DIR/$name.pid"), :$port)"
    return 0
  fi
  local stale; stale="$(port_pid "$port")"
  if [ -n "$stale" ]; then
    echo "[warn] :$port 被其他进程($stale)占用,先停止它再启动 $name"
    return 1
  fi
  nohup "$PY" -X utf8 "$@" > "$LOG_DIR/$name.log" 2>&1 &
  # $! 是 MSYS 中间进程 pid,与真正监听的 python 进程 Windows PID 不一致;
  # 直接轮询端口,把实际监听 PID 落 pidfile,保证跨会话可停
  local winpid=""
  for _ in $(seq 1 20); do
    winpid="$(port_pid "$port")"
    [ -n "$winpid" ] && break
    sleep 1
  done
  if [ -n "$winpid" ]; then
    echo "$winpid" > "$PID_DIR/$name.pid"
    echo "[ok]   $name 启动中(pid $winpid, :$port, 启动于 $(proc_start_time "$winpid"), 日志 $LOG_DIR/$name.log)"
  else
    echo "[warn] $name 20s 内未监听 :$port,查看日志 $LOG_DIR/$name.log"
    return 1
  fi
}

stop_one() {
  local name="$1" port="$2"
  local pid=""
  [ -f "$PID_DIR/$name.pid" ] && pid="$(cat "$PID_DIR/$name.pid")"
  if ! winpid_alive "$pid"; then
    pid="$(port_pid "$port")"  # 兜底:按端口找(手动起的进程也能停)
  fi
  if winpid_alive "$pid"; then
    taskkill //PID "$pid" //F > /dev/null 2>&1
    echo "[ok]   $name 已停止(pid $pid)"
  else
    echo "[skip] $name 未在运行"
  fi
  rm -f "$PID_DIR/$name.pid"
}

status_one() {
  local name="$1" port="$2"
  local code; code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$port/health" 2>/dev/null)"
  if [ "$code" = "200" ]; then
    echo "[up]   $name :$port (health 200)"
  else
    echo "[down] $name :$port (health=${code:-无响应})"
  fi
}

cmd="${1:-}"
case "$cmd" in
  start|restart)
    if [ "$cmd" = "restart" ]; then
      for line in "${SERVICES[@]}"; do
        read -r name port _ <<< "$line"
        pid="$(svc_pid "$name" "$port")"
        if winpid_alive "$pid"; then
          echo "[old]  $name 当前进程 pid $pid,启动于 $(proc_start_time "$pid")"
        else
          echo "[old]  $name 当前未在运行"
        fi
      done
      "$0" stop
    fi
    rc=0
    for line in "${SERVICES[@]}"; do
      # shellcheck disable=SC2086
      start_one $line || rc=1
    done
    sleep 4
    for line in "${SERVICES[@]}"; do status_one $line; done
    exit $rc
    ;;
  stop)
    for line in "${SERVICES[@]}"; do stop_one $line; done
    ;;
  status)
    for line in "${SERVICES[@]}"; do status_one $line; done
    ;;
  *)
    echo "用法: scripts/svc.sh start|stop|restart|status"
    exit 1
    ;;
esac
