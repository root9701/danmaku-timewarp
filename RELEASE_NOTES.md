# 发布页固定说明

`<!-- BODY -->` 之后原样输出，只替换 `{{VERSION}}` / `{{SIZE_MB}}` / `{{ZIP}}`。
版本更新见 `CHANGELOG.md`。

<!-- BODY -->

# 弹幕时光机 {{VERSION}}

B 站官方回放的弹幕播放器。数据与程序分离，本包不含比赛数据。

## 下载

`{{ZIP}}` · {{SIZE_MB}} MB

解压即用，无需安装 Python。

## 使用

1. 双击 `Start.bat`，浏览器打开 `http://127.0.0.1:8789`
2. 点击底栏「拉取弹幕」下载比赛数据

手机或平板改用 `Start-LAN.bat`，需与电脑同一 WiFi。停止服务用 `Stop.bat`。

## 更新

发现新版本时右上角版本号会亮红点。点开可看到本版更新内容，点「立即更新」后程序自行下载、替换并重启，页面自动恢复。`data/` 里的比赛数据与你自己调过的对齐参数不会被覆盖。

从源码（`git clone`）运行的不走这条路，请用 `git pull`。

## 校准

播放端不含校准入口。校准结果由采集端算好并随数据下发，直接观看；微调几秒用「手动微调」。
确需重算 offset 时，在仓库里运行 `python tools/calibrate.py --date <日期> --bv <BV>`。

## 常见问题

| 现象 | 处理 |
|---|---|
| 连不上下载通道 | 几个通道全不通。换个网络（如手机热点）再试 |
| 数据校验失败 | 镜像缓存旧文件，程序已自动换通道；反复出现请报 [issue](https://github.com/root9701/danmaku-timewarp/issues) |
| 提示 404 | 该天数据尚未发布，等待即可 |
| 端口被占用 | 双击 `Stop.bat` |
| 手机打不开 | 防火墙未放行 8789，见下方命令 |
| 更新后还是旧版本 | 关掉旧的控制台窗口，重新双击 `Start.bat` |
| 更新提示失败 | 页面会写明原因，程序仍是更新前的版本，可继续使用 |

下载走镜像（gh-proxy / ghfast / jsDelivr），国内直连可用，**不需要代理**。官方源排最后，仅在其它通道都不通时尝试。

以管理员身份运行 PowerShell 执行一次（放行防火墙 8789）：

```
New-NetFirewallRule -DisplayName "弹幕时光机" -Direction Inbound -Protocol TCP -LocalPort 8789 -Action Allow -Profile Private
```

数据版权归原发布者及哔哩哔哩所有。

## 更新内容
