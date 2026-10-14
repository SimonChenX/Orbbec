#!/bin/bash
echo "########## 1. 树莓派屏（应为日历）##########"
sshpass -p "$PI_PASS" ssh -o StrictHostKeyChecking=no root@192.168.10.76 \
 'echo "  kiosk 指向: $(cat /etc/pi-kiosk-url)"; echo "  ob-relay: $(systemctl is-active ob-relay) | ob-viewer: $(systemctl is-active ob-viewer) | pi-dashboard: $(systemctl is-active pi-dashboard)"; echo -n "  服务进程: "; ps -eo pcpu,comm --sort=-pcpu --no-headers | head -3 | tr "\n" " "; echo; top -bn1 | grep "%Cpu" | sed "s/^/  /"'

echo
echo "########## 2. 相机服务（最优参数）##########"
curl -s --max-time 6 http://127.0.0.1:9500/api/status | python3 -c "
import sys,json;d=json.load(sys.stdin);r=d['relay']
print('  ok=%s  control在线=%s' % (d['ok'], d['control']['online']))
for k in ('color','depth'):
    f=d[k];m=f['meta']
    print('  %-6s %-5s fps=%-6s %-9s %s' % (k,f['state'],f['fps'],m.get('size'), ('valid=%s%%'%m.get('valid_pct')) if k=='depth' else ''))
dm=d['depth']['meta']
print('  压缩比 %.1fx | 入站 %.1f Mbps | 设备温度 %s°C | 丢帧 彩%s 深%s' % (dm.get('zlib_ratio') or 0, r['ingress_mbps'], r.get('cpu_temp_c'), r.get('color_dropped'), r.get('depth_dropped')))"

echo
echo "########## 3. 网页参数接口 ##########"
curl -s --max-time 10 http://camera.internal.jiayouexp.com/api/config | python3 -c "
import sys,json;j=json.load(sys.stdin)
print('  GET ok=%s' % j['ok'])
rel=j.get('relay') or {}
print('  相机当前:', json.dumps(rel.get('camera',{}),ensure_ascii=False))
print('  码流当前:', json.dumps(rel.get('stream',{}),ensure_ascii=False))"

echo
echo "########## 4. 域名可达性 ##########"
for p in "/" "/api/status" "/api/config" "/snapshot/color.jpg" "/snapshot/depth.jpg"; do
  printf "  %-22s -> %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code} %{size_download}B' --max-time 8 http://camera.internal.jiayouexp.com$p)"
done
printf "  %-22s -> %s\n" "/screen.mjpg (已停用)" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://camera.internal.jiayouexp.com/screen.mjpg)"

echo
echo "########## 5. 服务开机自启 ##########"
echo "  ob-hub  : $(systemctl is-active ob-hub) / $(systemctl is-enabled ob-hub)"
sshpass -p "$PI_PASS" ssh -o StrictHostKeyChecking=no root@192.168.10.76 'echo "  ob-relay: $(systemctl is-active ob-relay) / $(systemctl is-enabled ob-relay)"'