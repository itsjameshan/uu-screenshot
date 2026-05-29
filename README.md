# UU 远程自动截图推送

这个小工具会定时截图当前电脑上可见的 UU 远程画面，并把截图通过邮件发出去。也可以额外发 Server 酱、Bark、通用 Webhook 或 Twilio 短信提醒。

最推荐的组合是：

```text
自动截图 -> QQ 邮箱发送附件 -> 手机 QQ 邮箱 App 提醒
```

直接自动给个人 QQ 号发消息通常不稳定，也容易被风控；如果你有自己的 QQ 官方机器人或群机器人 Webhook，可以用 `PUSH_WEBHOOK_URL` 接进去。

## 安全建议

如果脚本跑在公共 Windows 机器上，不建议把邮箱密码或 SMTP 授权码直接写进 `.env`。更稳的做法是：

```text
专用发件邮箱 + SMTP 授权码 + Windows 凭据管理器
```

这能防止别人直接打开脚本目录看到明文密码。它不能防住同一个 Windows 账号下的恶意程序，因为脚本能读取的密钥，其他同账号程序理论上也可能读到。所以最好再配合：

- 用一个专门发截图的邮箱，不用你的主邮箱。
- 只开启 SMTP，授权码用完可以随时撤销。
- 截图目录设置 `RETENTION_COUNT`，不要长期堆积。
- 公共电脑离开前停止脚本、撤销邮箱授权码。

## 快速开始

```bash
cd /Users/jameshan/Developments/uu_screenshot
cp .env.example .env
```

然后编辑 `.env`，至少填好这些：

```dotenv
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USERNAME=你的QQ邮箱@qq.com
MAIL_FROM=你的QQ邮箱@qq.com
MAIL_TO=接收邮箱@qq.com
SMTP_PASSWORD_KEYRING_SERVICE=uu_screenshot_smtp
SMTP_PASSWORD_KEYRING_USERNAME=你的QQ邮箱@qq.com
```

QQ 邮箱要填“SMTP 授权码”，不是 QQ 登录密码。你可以在 QQ 邮箱设置里开启 SMTP 服务后生成授权码。

在 Windows 上安装依赖：

```powershell
py -m pip install -r requirements.txt
```

然后把 SMTP 授权码存到 Windows 凭据管理器：

```powershell
py uu_screenshot_monitor.py --env .env --store-smtp-password
```

保存成功后，`.env` 里的 `SMTP_PASSWORD` 可以留空。

先试跑一次：

```bash
python3 uu_screenshot_monitor.py --once --verbose
```

如果只想确认截图能不能生成，先不发邮件：

```bash
python3 uu_screenshot_monitor.py --once --dry-run --verbose
```

持续运行：

```bash
python3 uu_screenshot_monitor.py --verbose
```

Windows 上可以持续运行：

```powershell
py uu_screenshot_monitor.py --env .env --verbose
```

也可以用任务计划程序每 5 分钟执行一次。关键设置是“只在用户登录时运行”，否则 Windows 后台会话可能截不到当前桌面。

```text
任务计划程序 -> 创建任务
常规 -> 只在用户登录时运行
触发器 -> 每天 -> 高级设置 -> 每隔 5 分钟重复任务
操作 -> 启动程序
程序: py
参数: uu_screenshot_monitor.py --once --env .env
起始于: 脚本所在目录
```

macOS/Linux 后台运行：

```bash
nohup python3 /Users/jameshan/Developments/uu_screenshot/uu_screenshot_monitor.py \
  --env /Users/jameshan/Developments/uu_screenshot/.env \
  > /Users/jameshan/Developments/uu_screenshot/monitor.out 2>&1 &
```

## macOS 权限

第一次截图时，macOS 可能会要求给 Terminal、iTerm、Python 或 Codex 开“屏幕录制”权限：

```text
系统设置 -> 隐私与安全性 -> 屏幕录制
```

给正在运行脚本的程序授权后，重新运行脚本。

## UU 远程窗口注意事项

普通截图只能截到当前电脑正在显示的内容，所以：

- UU 远程窗口最好保持可见。
- 电脑不要锁屏、睡眠。
- 如果你想让脚本截图前自动把 UU 远程拉到前台，可以在 `.env` 里设置：

```dotenv
MAC_ACTIVATE_APP_NAME=UU远程
```

应用名要和你 Mac 上实际显示的名字一致；如果不确定，先留空，默认截全屏。

## 推送通道

邮件是唯一默认会直接带截图附件的通道。

Server 酱、Bark、短信和通用 Webhook 默认只发文字提醒，因为本地截图文件没有公网 URL。你可以把它们当成手机弹窗提醒，截图本体继续走邮箱。

QQ 私聊推送有两种路：

- QQ 官方机器人：可以进入频道、群、消息列表单聊，但要注册机器人、配置使用范围/白名单，还会有 `AppSecret`/`Token` 这类密钥。对“每 5 分钟给自己发截图”来说偏重。
- 非官方 QQ 机器人：通常要登录 QQ 或保存登录态，公共机器上风险更高，不建议。

Server 酱：

```dotenv
SERVERCHAN_SENDKEY=你的SENDKEY
```

Bark：

```dotenv
BARK_KEY=你的BarkKey
```

通用 Webhook：

```dotenv
PUSH_WEBHOOK_URL=https://example.com/webhook
PUSH_WEBHOOK_METHOD=POST
PUSH_WEBHOOK_FORMAT=json
```

## Windows/Linux

macOS 不需要安装依赖。Windows 或 Linux 如果系统截图命令不可用，可以安装 Pillow：

```bash
python3 -m pip install -r requirements.txt
```

## 常见问题

如果邮件发不出去，优先检查：

- SMTP 授权码是否正确。
- `MAIL_FROM` 是否和 `SMTP_USERNAME` 一致。
- QQ 邮箱是否开启了 SMTP 服务。
- 网络是否能访问 `smtp.qq.com:465`。

如果截图是黑的或桌面不对，优先检查：

- macOS 屏幕录制权限。
- UU 远程窗口是否可见。
- 当前电脑是否锁屏或睡眠。
