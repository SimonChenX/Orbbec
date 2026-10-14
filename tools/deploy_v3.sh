#!/bin/bash
# 部署：ob-hub（Go，本机） + ob-relay（树莓派） + ob-pose（侧车）
set -e
echo "########## 1. 构建并部署 ob-hub ##########"
cd /home/OrbbecPiApi
gofmt -w *.go
go build -o /usr/local/bin/ob-hub .
go test ./... >/dev/null && echo "  测试通过"
systemctl restart ob-hub
sleep 3
systemctl is-active ob-hub | sed 's/^/  ob-hub: /'
journalctl -u ob-hub -n 5 --no-pager | tail -4 | sed 's/^/  /'

echo
echo "########## 2. 部署采集端 relay 到树莓派 ##########"
tar czf - -C /home/Orbbec relay | sshpass -p "$PI_PASS" ssh -o StrictHostKeyChecking=no root@192.168.10.76 \
  'tar xzf - -C /opt/orbbec && python3 -m py_compile /opt/orbbec/relay/relay.py && systemctl restart ob-relay'
sleep 10
sshpass -p "$PI_PASS" ssh -o StrictHostKeyChecking=no root@192.168.10.76 \
  'systemctl is-active ob-relay | sed "s/^/  ob-relay: /"; journalctl -u ob-relay -n 6 --no-pager | tail -5 | sed "s/^/  /"'

echo
echo "########## 3. 安装 ob-pose 侧车 ##########"
install -m 644 /home/OrbbecPiApi/deploy/ob-pose.service /etc/systemd/system/ob-pose.service
[ -f /etc/default/ob-pose ] || install -m 644 /home/OrbbecPiApi/deploy/default-ob-pose /etc/default/ob-pose
systemctl daemon-reload
systemctl enable --now ob-pose >/dev/null 2>&1
systemctl restart ob-pose
sleep 12
systemctl is-active ob-pose | sed 's/^/  ob-pose: /'
journalctl -u ob-pose -n 8 --no-pager | tail -6 | sed 's/^/  /'

echo
echo "########## 4. 验证接口 ##########"
echo -n "  /api/status ok="; curl -s --max-time 8 http://127.0.0.1:9500/api/status | python3 -c "import sys,json;d=json.load(sys.stdin);print('%s  sensor_mode=%s  pose.online=%s' % (d['ok'], d.get('sensor_mode'), d.get('pose',{}).get('online')))"
echo "  /api/depth_at:"; curl -s --max-time 8 "http://127.0.0.1:9500/api/depth_at?x=160&y=120" | head -c 300; echo
echo "  /api/pose:"; curl -s --max-time 8 http://127.0.0.1:9500/api/pose | python3 -c "
import sys,json;j=json.load(sys.stdin)
print('    present=%s' % j.get('present'))
d=j.get('data') or {}
lms=d.get('landmarks') or []
print('    关键点 %d 个  推理 %.1fms  源图 %sx%s' % (len(lms), d.get('infer_ms') or 0, d.get('w'), d.get('h')))
if lms: print('    鼻(%s) x=%.3f y=%.3f vis=%.2f' % ('#0', lms[0][0], lms[0][1], lms[0][3]))
"