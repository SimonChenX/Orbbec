#!/bin/bash
# 验收测试：端到端延迟 + 三项故障演练
PI=192.168.10.76
SSH="sshpass -p "$PI_PASS" ssh -o StrictHostKeyChecking=no root@$PI"

st() { curl -s --max-time 5 http://127.0.0.1:9500/api/status; }

echo "########## 1. 端到端延迟（采集时间戳 → 服务器收到）##########"
st | python3 -c "
import sys,json; d=json.load(sys.stdin)
for k in ('color','depth'):
    f=d[k]; print('  %-6s lag_ms=%s  age_ms=%s  fps=%s' % (k, f.get('lag_ms'), f.get('age_ms'), f['fps']))
print('  screen lag_ms=%s age_ms=%s' % (d['screen'].get('lag_ms'), d['screen'].get('age_ms')))"

echo
echo "########## 2. 带宽 ##########"
st | python3 -c "
import sys,json; d=json.load(sys.stdin); r=d['relay']
print('  入站 %.1f Mbps (%.2f MB/s)，累计 %.1f GB' % (r['ingress_mbps'], r['ingress_bps']/1e6, r['ingress_total_bytes']/1e9))
print('  设备上报: 丢帧 彩%s 深%s | 重连 %s | 温度 %s°C' % (r.get('color_dropped'), r.get('depth_dropped'), r.get('link_reconnects'), r.get('cpu_temp_c')))"

echo
echo "########## 3. 树莓派侧资源 ##########"
$SSH 'top -bn1 | grep "%Cpu"; ps -eo pcpu,comm --sort=-pcpu --no-headers | head -4 | tr "\n" " "; echo; uptime'

echo
echo "########## 4. 故障演练 A：停掉服务器后端 ##########"
systemctl stop ob-hub; sleep 8
echo "  --- 树莓派 relay 日志（应看到连接失败 + 退避，且采集不中断）---"
$SSH 'journalctl -u ob-relay -n 4 --no-pager | tail -3; echo "  relay 状态: $(systemctl is-active ob-relay)"'
echo "  --- 服务器已停，页面应显示错误提示 ---"

echo
echo "########## 5. 恢复：重启后端 ##########"
systemctl start ob-hub; sleep 12
st | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('  ok=%s' % d['ok'])
for k in ('color','depth','screen'): print('    %-6s %-6s fps=%-5s seq=%s' % (k, d[k]['state'], d[k]['fps'], d[k]['seq']))"

echo
echo "########## 6. 故障演练 B：停掉树莓派 relay（服务器应标 stale）##########"
$SSH 'systemctl stop ob-relay'; sleep 6
st | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('  ok=%s' % d['ok'])
for k in ('color','depth'): print('    %-6s state=%-6s error=%s' % (k, d[k]['state'], d[k].get('error')))"

echo
echo "########## 7. 恢复 relay ##########"
$SSH 'systemctl start ob-relay'; sleep 10
st | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('  ok=%s' % d['ok'])
for k in ('color','depth','screen'): print('    %-6s %-6s fps=%-5s seq=%s' % (k, d[k]['state'], d[k]['fps'], d[k]['seq']))"