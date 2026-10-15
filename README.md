# Predict.fun Market Making Bot

![Predict MM Desk](docs/images/predict-mm-desk.jpg)

[简体中文](README.md) | [English](README.en.md)

面向 [Predict.fun](https://predict.fun) 的被动做市机器人。它根据实时订单簿为选定市场报价，替换过期挂单，并在价格或深度条件不再安全时撤单。可通过 Windows 桌面程序或 Ubuntu/Debian 服务器上的英文网页控制台操作。

放在订单簿上的订单都可能成交。紧急卖出不保证一定成交，也不保证成交价格。请先使用模拟运行，再从小数量开始。

## 目录

- [机器人做什么](#机器人做什么)
- [准备条件](#准备条件)
- [报价方式](#报价方式)
- [风险控制](#风险控制)
- [控制台](#控制台)
- [账户配置](#账户配置)
- [Windows 桌面版](#windows-桌面版)
- [服务器安装](#服务器安装)
- [命令行](#命令行)
- [配置说明](#配置说明)
- [安全](#安全)

## 机器人做什么

机器人为你选择的结果提交限价买单。它不会用市价单追价。对每个启用的市场，它读取订单簿，检查价差和风险限制，再通过 Predict.fun 官方接口提交 post-only 限价单。

运行期间它会：

- 在订单簿变化时，或挂单达到设定存活时间时，刷新报价。
- 当市场价格逼近挂单时撤单。
- 当前方买盘过薄或快速下降时撤单，深度恢复后再重新报价。
- 通过钱包事件和订单状态识别买单成交。
- 在买单成交后，可暂停该市场并提交限价卖单。
- 在启动和停止时撤销它正在管理的订单。

签名后的限价单在创建 300 秒后过期。撤单会把订单移出公开订单簿。较短的有效期用于限制进程或网络中断、撤单尚未确认时，签名仍可能被使用的时间。

## 准备条件

- Predict.fun 账户、从 [Predict.fun Developers](https://developers.predict.fun/) 申请的 API Key，以及用于签单的钱包私钥。
- 如需设置链上交易授权，签名钱包中要有少量 BNB。使用 Predict Account 时，请把 BNB 转到 Privy 签名钱包，不要转到 Predict Account 存款地址。
- Windows 10/11 64 位，或安装了 Python 3.11 及以上版本的 Ubuntu/Debian 服务器。服务器推荐 Ubuntu 24.04。
- Windows 需要 Microsoft Edge WebView2 Runtime。程序提示缺少时，可从[微软官网](https://developer.microsoft.com/microsoft-edge/webview2/)安装。

请为机器人单独使用一个钱包，不要使用还存放其他资金的钱包。

## 报价方式

默认策略是被动做市：报价位于当前买卖价差内部，而不是直接排在买一和卖一。默认偏移为 2 个 tick。Tick 来自市场本身。如果盘口没有足够空间放下这个偏移，机器人会把报价保留在最小 tick，而不是放弃该市场。

可以选择只挂 Yes、只挂 No，或两边都挂。No 的买价由 Yes 卖价的补价计算，因为 Predict.fun 的订单簿以 Yes 价格表示。

任一侧盘口缺失，或价差超出设定范围时，机器人会跳过该市场。默认允许的价差范围是 0.006 到 0.20。对于 tick 为 0.001 的市场，一个 tick 的价差仍然可以报价。

每个市场单独设置挂单数量。仓位上限是共用的：默认单市场 10 份，全部市场合计 50 份。超出上限的报价不会提交。

## 风险控制

| 控制项 | 默认值 | 作用 |
| --- | --- | --- |
| 挂单存活时间 | 60 秒 | 到期后撤单并重新报价。若买一、卖一和目标价格都没有变化，订单可以再保留一个同样长度的周期。价格逼近时仍会立即撤单。 |
| 价格接近保护 | 开启 | 市场价格接近挂单时撤单。实时盘口连接断开后，改为定期检查。 |
| 买盘深度保护 | 开启 | 价格严格高于你的买价的外部买单总量低于 `max(200 份, 2 × 挂单数量)` 时撤单；或该深度在 2 秒内下降 50%，且剩余深度低于恢复阈值时撤单。Yes 与 No 分别计算，不含自己的挂单。 |
| 深度恢复 | 400 份或 4 × 数量 | 需经过 10 秒冷却和 3 秒稳定深度后才恢复，提交前会再次检查。 |
| 运行有效期 | 关闭 | 从每次点击启动开始倒计时。到期后撤销全部受管理订单并停止。最长 72 小时。 |
| 紧急卖出 | 开启 | 买单全部或部分成交后，撤销该市场上机器人的其他订单，并提交非 post-only 限价卖单。市场 tick 为 0.001 时限价为 0.001，否则为 0.01。可能卖出账户中原有的同选项持仓，并可能造成亏损。 |
| 模拟运行 | 关闭 | 只计算报价并写日志，不提交真实订单。连接官方 API 时，模拟运行使用示例订单簿，不使用实时订单簿。 |

深度保护会减少挂单次数，不能保证订单不会成交。

实盘还需要交易授权。**Check approvals** 只读取状态。**Set approvals** 会通过官方 Predict SDK 发送 BNB Chain 交易，并由签名钱包支付 Gas。这一步不会创建预测市场订单。缺少必要授权时，机器人不会开始实盘报价。

## 控制台

界面为英文。市场名称保留所粘贴网址返回的语言。运行日志保留原文。

| 页面 | 用途 |
| --- | --- |
| Dashboard | 机器人状态、USDT 余额、当前挂单、剩余运行时间和最近日志。 |
| Account settings | API Key、签名私钥，以及可选的 Predict Account 地址。保存时在本地生成 JWT。 |
| Quote settings | 市场、数量、存活时间、运行有效期、紧急卖出、模拟运行和深度阈值。 |
| Activity logs | 报价、撤单和成交保护记录。选中文字时暂停自动刷新。 |

显示的余额是交易账户的链上 USDT 余额，不包含持仓的市值。

打开控制台不会开始报价。保存账户和市场后，点击 **Start bot**。**Stop & cancel orders** 会请求停止并撤单。**Cancel orders in all markets** 只撤销受管理订单，不会另外启动机器人。

## 账户配置

1. 在 [Predict.fun Developers](https://developers.predict.fun/) 申请 API Key。
2. 在 Predict.fun 网站打开个人资料，复制用户名下方的钱包地址。这是 Predict Account 地址。如果使用外部拥有账户（EOA），此项留空。
3. 导出签名私钥。Predict Account 使用个人资料或设置中的 Privy 钱包私钥。EOA 使用该钱包自己的私钥。
4. 在 **Account settings** 填入 API Key、私钥，以及 Predict Account 地址（如使用）。点击 **Save account settings**。私钥只写入本机 `.env`。控制台之后只显示私钥是否已保存，不显示私钥本身。
5. 在 **Quote settings** 至少保存一个市场。
6. 打开 **Account settings**，点击 **Check approvals**。如有缺失项，先按页面显示的 Gas 钱包地址转入少量 BNB，再点击 **Set approvals**。
7. 全新钱包请先在官网完成一笔交易，再交给机器人。当前授权流程依赖这一步建立的账户状态。
8. 回到 **Dashboard**，点击 **Start bot**。

## Windows 桌面版

1. 解压 `PredictMMBot-Windows-x64.zip`。
2. 将 `PredictMMBot.exe` 与 `_internal` 文件夹放在同一目录。
3. 双击 `PredictMMBot.exe`。
4. 保存 **Account settings** 和 **Quote settings**，然后点击 **Start bot**。

不需要安装 Python。Windows 版本为预览版。同一时间只能打开一个窗口，因为两个窗口会使用同一份本地订单记录。

正常关闭窗口时，程序会请求停止并等待撤单完成。无法确认撤单时，窗口保持打开并显示错误。睡眠、关机或强制结束会中断本地进程，可能留下机器人尚未撤销的订单。

配置、日志和订单记录保存在 `%LOCALAPPDATA%\PredictMMBot`，与程序目录分开。不要把这个文件夹或其中的 `.env` 发送给任何人。更新时先停止机器人并关闭窗口，再把新版本解压到新文件夹。原有配置仍留在数据目录中。

需要无人值守运行时，请使用下面的服务器安装。电脑睡眠后，桌面版会停止。

## 服务器安装

把本项目复制到服务器，通过 SSH 登录，在项目目录执行：

```bash
sudo bash install.sh
```

如果当前用户已经是 `root`，可以去掉 `sudo`。安装脚本支持 Ubuntu 和 Debian，并要求 Python 3.11 或更高版本。它会安装虚拟环境和 Python 依赖，把程序放到 `/opt/predict-mm-bot`，并创建名为 `predict-mm-bot` 的 systemd 服务。服务监听 `127.0.0.1:8080`，服务器重启后会自动恢复。服务本身不会自动开始实盘报价。

如果 8080 端口被无法识别的进程占用，或机器人仍在运行，安装会停止。已有的 `.env`、`config.toml` 和 `logs` 会被保留。从旧的手动安装第一次切换时，如果新目录中还没有这些文件，脚本可以从 `/root/predict-mm-bot` 复制它们。

更新时先点击 **Stop & cancel orders**，替换程序文件，再执行一次 `sudo bash install.sh`。

控制台只绑定本机回环地址。在自己的电脑上建立 SSH 隧道，并保持该终端打开：

```bash
ssh -L 8080:127.0.0.1:8080 用户名@服务器IP
```

然后在浏览器打开 `http://127.0.0.1:8080`。关闭隧道只会断开浏览器访问。服务器上的服务，以及已经启动的机器人，会继续运行。电脑睡眠或重启后，需要重新建立隧道。

服务未运行时，可以先启动服务，等它响应后再用已保存的配置启动机器人：

```bash
sudo systemctl start predict-mm-bot
timeout 30 bash -c 'until curl -fsS http://127.0.0.1:8080/api/status >/dev/null; do sleep 1; done'
curl -fsS -X POST http://127.0.0.1:8080/api/start
```

查看服务日志：

```bash
sudo journalctl -u predict-mm-bot -f
```

重启网页服务：

```bash
sudo systemctl restart predict-mm-bot
```

重启或更新前先点击 **Stop & cancel orders**。服务重启只会恢复控制台，不会自动恢复报价，需要再次启动机器人。

## 命令行

从源码目录可以不打开网页直接配置并运行。需要 Python 3.11 或更高版本。先安装项目：

```bash
python -m pip install -e .
python -m predict_mm.main --setup
python -m predict_mm.main --config config.toml
```

`--setup` 会在当前目录写入 `.env` 和 `config.toml`，然后退出。之后用 `--config` 读取这些文件。加上 `--no-setup` 时，如果任一文件不存在，程序会直接失败，而不会打开配置向导。

也可以直接启动网页控制台：

```bash
python -m predict_mm.web --host 127.0.0.1 --port 8080
```

请保持监听地址为 `127.0.0.1`。控制台本身没有登录验证。

## 配置说明

网页控制台写入的两个文件，与命令行使用的是同一套配置。

`.env` 保存账户凭据：

```bash
PREDICT_API_BASE_URL=https://api.predict.fun
PREDICT_API_KEY=
PREDICT_JWT_TOKEN=
PREDICT_PRIVATE_KEY=
PREDICT_ACCOUNT_ADDRESS=
LOG_LEVEL=INFO
```

`PREDICT_API_BASE_URL` 默认指向官方 API。REST 请求使用这个地址。订单簿和钱包事件的 WebSocket 固定为 `wss://ws.predict.fun/ws`。JWT 可以留空；在控制台保存账户时会生成，Predict.fun 拒绝过期令牌时机器人会自动刷新。

`config.toml` 保存市场和风险设置。`config.example.toml` 是完整示例。最常修改的字段：

| 字段 | 含义 |
| --- | --- |
| `dry_run` | `true` 为模拟报价，`false` 为真实下单。 |
| `cancel_after_seconds` | 刷新前的挂单存活时间。 |
| `run_duration_seconds` | `0` 表示不自动停止。 |
| `emergency_exit_on_buy_fill` | 买单成交后是否提交保护性卖单。 |
| `markets` | 每个市场一项，包含 `id`、`outcome` 和 `quote_size`。 |
| `risk.max_position_per_market` | 单个市场的最大仓位。 |
| `risk.max_total_position` | 全部市场的最大仓位。 |
| `depth_protection` | 撤单和恢复阈值。恢复阈值必须高于撤单阈值。 |

`outcome` 为 `YES`、`NO`，或两边都挂。`quote_size` 的单位是份额。

## 安全

- 私钥只在本机用于签署 Predict.fun 登录消息和订单。保存后，控制台不再显示私钥。
- `.env` 和 `config.toml` 已被 Git 忽略。不要提交、不要粘贴到聊天中，也不要出现在截图里。
- 服务器安装会把这两个文件设为 `0600`，并以无特权用户 `predictmm` 运行服务，只监听本机地址。
- 不要把 8080 端口公开到互联网。任何能打开控制台的人都可以用已保存的密钥启动机器人。
- 交易授权允许 Predict.fun 合约为交易移动已授权资产。只对准备交给这个机器人的钱包进行授权。
- 每次实盘结束后，把机器人显示的挂单与 Predict.fun 页面上的挂单核对。启动和停止时的撤单只覆盖当前进程正在管理的订单。
