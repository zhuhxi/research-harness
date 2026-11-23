#!/usr/bin/env bash
# 守护夜间巡检：巡检进程无论因为什么退出，只要没到截止时刻，就记录并在 30 秒后重启。
# 用法：setsid nohup bash 守护巡检.sh <课题目录> 09:00 > 巡检/守护.out 2>&1 &
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$1"; UNTIL="${2:-09:00}"
LOG="$HERE/../巡检/巡检.log"
# 截止时刻的时间戳：今天的 UNTIL，若已过则取明天
END=$(date -d "today $UNTIL" +%s); [ "$END" -le "$(date +%s)" ] && END=$(date -d "tomorrow $UNTIL" +%s)
n=0
while [ "$(date +%s)" -lt "$END" ]; do
  n=$((n+1))
  echo "[$(date '+%m-%d %H:%M:%S')] 守护：第 $n 次启动巡检" >> "$LOG"
  python3 "$HERE/夜间巡检.py" "$PROJ" --until "$UNTIL" --iters 4
  rc=$?
  [ "$(date +%s)" -ge "$END" ] && break
  echo "[$(date '+%m-%d %H:%M:%S')] 守护：巡检意外退出（rc=$rc），30 秒后重启" >> "$LOG"
  echo -e "\n## 巡检进程意外退出（$(date '+%m-%d %H:%M')，rc=$rc），守护已重启\n" >> "$HERE/../巡检/问题日志_$(date +%Y%m%d).md"
  sleep 30
done
echo "[$(date '+%m-%d %H:%M:%S')] 守护：到达截止时刻 $UNTIL，结束" >> "$LOG"
