#!/bin/bash
# 验证「网页可调参数」全链路：GET 读 → POST 改相机 → POST 改码流（重启采集端）
D=http://camera.internal.jiayouexp.com

echo "########## 1. GET /api/config（读当前参数）##########"
curl -s --max-time 10 $D/api/config | python3 -c "
import sys,json; j=json.load(sys.stdin)
print('  ok=%s  control online=%s' % (j['ok'], j['control']['online']))
r=j.get('relay') or {}
cam=r.get('camera',{}); print('  相机当前值:', json.dumps(cam, ensure_ascii=False))
print('  相机可调项:', ', '.join('%s[%s~%s]' % (s['key'], s.get('min'), s.get('max')) for s in r.get('camera_spec',[])))
print('  码流当前值:', json.dumps(r.get('stream',{}), ensure_ascii=False))
print('  码流可调项:', ', '.join('%s%s' % (s['key'], s['options']) for s in r.get('stream_spec',[])))"

echo
echo "########## 2. POST 改相机参数 gamma: 160 -> 190 ##########"
curl -s --max-time 15 -X POST -H 'Content-Type: application/json' \
  -d '{"action":"set_camera","controls":{"gamma":190}}' $D/api/config | python3 -c "
import sys,json; j=json.load(sys.stdin); print('  ok=%s msg=%s -> gamma=%s' % (j.get('ok'), j.get('msg'), (j.get('camera') or {}).get('gamma')))"
echo "  设备侧核对:"; sshpass -p "$PI_PASS" ssh -o StrictHostKeyChecking=no root@192.168.10.76 'v4l2-ctl -d /dev/video0 -l | grep gamma | sed "s/^/    /"'

echo
echo "########## 3. 改回 gamma=160 ##########"
curl -s --max-time 15 -X POST -H 'Content-Type: application/json' \
  -d '{"action":"set_camera","controls":{"gamma":160}}' $D/api/config | python3 -c "
import sys,json; j=json.load(sys.stdin); print('  ok=%s gamma=%s' % (j.get('ok'), (j.get('camera') or {}).get('gamma')))"

echo
echo "########## 4. POST 改码流：彩色帧率 30 -> 15（采集端会重启）##########"
curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
  -d '{"action":"set_stream","values":{"OB_COLOR_FPS":"15"}}' $D/api/config | python3 -c "
import sys,json; j=json.load(sys.stdin); print('  ok=%s msg=%s' % (j.get('ok'), j.get('msg')))"
echo "  等采集端重启…"; sleep 18
curl -s --max-time 8 $D/api/status | python3 -c "
import sys,json;d=json.load(sys.stdin);f=d['color'];print('  彩色现在: %s fps=%s' % (f['state'], f['fps']))"

echo
echo "########## 5. 恢复彩色帧率 30 ##########"
curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
  -d '{"action":"set_stream","values":{"OB_COLOR_FPS":"30"}}' $D/api/config | python3 -c "
import sys,json; j=json.load(sys.stdin); print('  ok=%s' % j.get('ok'))"
sleep 18
curl -s --max-time 8 $D/api/status | python3 -c "
import sys,json;d=json.load(sys.stdin)
print('  最终: ok=%s' % d['ok'])
for k in ('color','depth'): print('   %-6s %-5s fps=%-6s %s' % (k,d[k]['state'],d[k]['fps'],d[k]['meta'].get('size')))"

echo
echo "########## 6. 前端资源 ##########"
for p in "/" "/app.js" "/style.css"; do
  printf "  %-12s -> %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code} %{size_download}B' --max-time 5 $D$p)"
done