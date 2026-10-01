# VRChat-OSC-Debug-Tool

[![Build & Release](https://github.com/OWNER/VRChat-OSC-Debug-Tool/actions/workflows/release.yml/badge.svg)](https://github.com/OWNER/VRChat-OSC-Debug-Tool/actions/workflows/release.yml)

一个 Windows 单文件 GUI 工具（免安装 Python），用于检测 VRChat 的 OSC 功能是否正常可用。

## 下载

前往 [Releases](../../releases) 页面下载最新的 `VRC-OSC-Tester.exe`，双击即可运行。

也可以自行构建（见下方「开发与构建」）。

## 功能

1. **一键环境检测**
   - 检测 VRChat 进程是否在运行（含 PID），并读取其启动命令行，
     自动解析 `--osc=输入端口:输出IP:输出端口` 启动参数
   - 解析最新日志（`output_log_*.txt`）：
     - `OSC enabled: True/False` —— OSC 启用状态
     - `Advertising Service ... of type OSC on <端口>` —— **实际生效的输入端口**
     - `of type OSCQuery on <端口>` —— OSCQuery 服务端口，并在线 HTTP 查询实时确认
   - 读取 osc.json（部分版本才生成，仅作参考）

   **端口判定优先级**：`--osc` 启动参数 > 日志实际端口 > OSCQuery 实时查询 > osc.json > 默认 9000/9001。
   检测完成后端口会**自动填入输入框**，直接点发送/监听即可，无需手动改。

2. **端口占用检测（TCP + UDP 全查）**
   - 基于 `netstat -ano` 扫描，同时覆盖 TCP 和 UDP（OSC 走 UDP，VRChat 只绑定 UDP 端口）
   - 输入端口被 VRChat 自己监听 → 绿色 ✓（输入方向正常）
   - 被其他程序占用 → 红色，**显示占用程序名和 PID**（如 `VRCFT.exe (PID 1234, UDP 0.0.0.0:9000)`）
   - 无任何监听 → 黄色提示（通常是 OSC 未在游戏内开启）
   - 输出端口同理，被占用时提示先关闭占用程序再监听

3. **详细调试日志**
   - 全程 `[调试]` 前缀输出：netstat 原始命中行、VRChat 完整命令行、日志文件路径/修改时间/大小、
     OSCQuery HTTP 原始响应、各步骤耗时等，方便排查
   - **所有日志同时写入文件**：exe 同目录下的 `logs/osc_tester_日期_时间.log`，
     每次启动生成新文件，带时间戳和级别标记；窗口关闭后日志仍可回查
   - 若 exe 目录不可写，自动降级写入 `%TEMP%\vrc_osc_tester.log`

4. **防火墙检测**
   - 查询域/专用/公用三个防火墙配置文件的开关状态
   - 搜索所有与 VRChat 相关的防火墙规则，判断是**被阻止规则拦截**（红色警告）还是**已放行**（绿色）
   - 说明：127.0.0.1 本机回环流量不受防火墙限制，本机 OSC 测试一般与防火墙无关；
     只有跨设备发 OSC（如 `--osc` 指定局域网 IP）时防火墙才可能成为影响因素

5. **被动监听**：监听输出端口，实时显示 VRChat 上报的所有 OSC 消息
   （如 Avatar 参数变化）。有消息进来说明 VRChat → 外部方向正常。

6. **发送聊天框测试**：向 `/chatbox/input` 发送测试消息。
   若消息出现在 VRChat 聊天框，说明 外部 → VRChat 方向正常。

## 判定标准

| 现象 | 结论 |
|---|---|
| 检测全绿 + 聊天框能看到测试消息 | OSC 输入方向正常 |
| 监听时能看到 OSC 消息滚动 | OSC 输出方向正常 |
| 日志/osc.json 显示 OSC 未启用 | 游戏内开启：主菜单 → Action Menu → Options → OSC → Enabled |
| 监听无任何消息 | 正常现象：VRChat 只在 Avatar 参数变化时才发数据，切换表情/动一下即可 |

## 注意

- VRChat 需运行且开启 OSC 后，聊天框测试才生效
- 若端口被占用，可在 osc.json 中修改端口
- 本工具只读取本机信息、只向 127.0.0.1 发送 OSC 测试包，不上传任何数据

## 开发与构建

```bash
# 依赖：Windows + Python 3.12
pip install python-osc psutil pyinstaller
pyinstaller --onefile --noconsole --name VRC-OSC-Tester --hidden-import psutil osc_tester.py
# 产物: dist/VRC-OSC-Tester.exe
```

## 自动构建发布

推送到 `main` 分支会自动构建并上传 Actions Artifact；
推送 `v*` 标签（如 `git tag v1.0.0 && git push --tags`）会自动构建 exe 并发布到 GitHub Releases。

## License

[MIT](LICENSE)
