# 工具脚本

大多是**一次性验证脚本**，留档是为了「怎么测的」有据可查。跑之前先看注释。

| 文件 | 用途 |
|---|---|
| `ob-hub-proxy.conf` | nginx 反代配置（放宝塔 vhost extension 目录） |
| `test_webconfig.sh` | 网页参数接口全链路自测（读/改相机、改码流并核对设备侧） |
| `acceptance.sh` | 端到端验收：延迟、带宽、故障演练（停服务器/重启/停 relay） |
| `deploy_v3.sh` | 一键部署：ob-hub + ob-relay + ob-pose |
| `final2.sh` | 部署后自检（屏/服务/接口/域名/开机自启） |
| `shot.sh` | 无头浏览器截图（踩过 4 个坑，见 `docs/09` §5） |
| `mkcompare2.py` | 拼「调优前/后」对比图 |

## 约定

* 需要 SSH 到树莓派的脚本用 `sshpass -f ~/.pi-pass`（**不把密码写进命令/仓库**）。
  用前先 `chmod 600 ~/.pi-pass && echo '<密码>' > ~/.pi-pass`。
* 部分脚本里的 IP / 域名是当时环境的（树莓派 `192.168.10.76`、聚合端 `192.168.5.20`），
  换环境要改。
* `shot.sh` 依赖本机装了 chromium：`apt install chromium`。