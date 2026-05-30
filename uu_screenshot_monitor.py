#!/usr/bin/env python3
"""
定时截图当前可见的 UU 远程画面，并通过邮件、HTTP 推送或短信提醒。

macOS 默认使用系统自带的 screencapture，不需要安装额外依赖。
Windows/Linux 会尝试使用 Pillow 或系统截图命令。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import logging
import mimetypes
import os
import platform
import re
import shlex
import shutil
import smtplib
import ssl
import subprocess
import sys
import time
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable
from urllib import error, parse, request


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if key:
            values[key] = value

    return values


def build_config(env_path: Path) -> dict[str, str]:
    config = load_env_file(env_path)
    # Shell environment wins over .env so temporary overrides are easy.
    config.update(os.environ)
    return config


def get_bool(config: dict[str, str], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None or value == "":
        return default

    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"{key} must be a boolean value, got {value!r}")


def get_int(config: dict[str, str], key: str, default: int) -> int:
    value = config.get(key)
    if value is None or value.strip() == "":
        return default
    return int(value)


def get_float(config: dict[str, str], key: str, default: float) -> float:
    value = config.get(key)
    if value is None or value.strip() == "":
        return default
    return float(value)


def read_secret_from_keyring(service: str, username: str) -> str:
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "需要安装 keyring 才能从系统凭据管理器读取密钥："
            "python -m pip install keyring"
        ) from exc

    value = keyring.get_password(service, username)
    if not value:
        raise RuntimeError(f"系统凭据管理器里没有找到 {service!r}/{username!r}")
    return value


def write_secret_to_keyring(service: str, username: str, value: str) -> None:
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "需要安装 keyring 才能写入系统凭据管理器："
            "python -m pip install keyring"
        ) from exc

    keyring.set_password(service, username, value)


def get_secret(
    config: dict[str, str],
    key: str,
    *,
    default_keyring_service: str,
    default_keyring_username: str,
) -> str:
    direct_value = config.get(key, "").strip()
    if direct_value:
        return direct_value

    env_name = config.get(f"{key}_ENV", "").strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
        raise RuntimeError(f"环境变量 {env_name!r} 为空，无法读取 {key}")

    file_name = config.get(f"{key}_FILE", "").strip()
    if file_name:
        file_value = Path(file_name).expanduser().read_text(encoding="utf-8").strip()
        if file_value:
            return file_value
        raise RuntimeError(f"密钥文件 {file_name!r} 为空，无法读取 {key}")

    service = config.get(f"{key}_KEYRING_SERVICE", "").strip()
    username = config.get(f"{key}_KEYRING_USERNAME", "").strip()
    if service or username:
        return read_secret_from_keyring(
            service or default_keyring_service,
            username or default_keyring_username,
        )

    return ""


def smtp_keyring_identity(config: dict[str, str]) -> tuple[str, str]:
    username = (
        config.get("SMTP_USERNAME", "").strip()
        or config.get("SMTP_USER", "").strip()
        or config.get("MAIL_FROM", "").strip()
    )
    service = config.get("SMTP_PASSWORD_KEYRING_SERVICE", "").strip() or "uu_screenshot_smtp"
    secret_username = config.get("SMTP_PASSWORD_KEYRING_USERNAME", "").strip() or username
    return service, secret_username


def store_smtp_password(config: dict[str, str]) -> int:
    smtp_username = (
        config.get("SMTP_USERNAME", "").strip()
        or config.get("SMTP_USER", "").strip()
        or config.get("MAIL_FROM", "").strip()
    )
    if not smtp_username and not config.get("SMTP_PASSWORD_KEYRING_USERNAME", "").strip():
        raise RuntimeError("请先在 .env 里填写 SMTP_USERNAME，或填写 SMTP_PASSWORD_KEYRING_USERNAME")

    service, username = smtp_keyring_identity(config)
    password = getpass.getpass(f"输入要保存到系统凭据管理器的 SMTP 授权码 ({service}/{username})：")
    if not password:
        raise RuntimeError("授权码为空，没有保存。")

    write_secret_to_keyring(service, username, password)
    print(f"已保存到系统凭据管理器：{service}/{username}")
    print("现在可以把 .env 里的 SMTP_PASSWORD 留空。")
    return 0


def setup_logging(config: dict[str, str], verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = config.get("LOG_FILE", "uu_screenshot.log").strip()
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def output_dir(config: dict[str, str]) -> Path:
    directory = Path(config.get("OUTPUT_DIR", "screenshots")).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def screenshot_path(config: dict[str, str]) -> Path:
    prefix = config.get("SCREENSHOT_PREFIX", "uu_screenshot").strip() or "uu_screenshot"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir(config) / f"{prefix}_{timestamp}.png"


def activate_macos_app(config: dict[str, str]) -> None:
    app_name = config.get("MAC_ACTIVATE_APP_NAME", "").strip()
    if not app_name:
        return

    escaped_name = app_name.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "{escaped_name}" to activate'
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        logging.warning("激活 macOS 应用失败：%s", result.stderr.strip() or app_name)


def activate_windows_app(config: dict[str, str]) -> None:
    app_name = config.get("WIN_ACTIVATE_APP_NAME", "").strip()
    if not app_name:
        return

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    found_hwnds = []

    def callback(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if app_name in buf.value:
                    found_hwnds.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(callback), 0)

    if not found_hwnds:
        logging.warning("未找到包含 '%s' 的窗口", app_name)
        return

    logging.info("找到 %d 个窗口，逐个激活", len(found_hwnds))

    for hwnd in found_hwnds:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
            time.sleep(0.3)

        user32.ShowWindow(hwnd, 5)
        time.sleep(0.2)

        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)

        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.2)

    logging.info("已激活窗口：%s", app_name)


def maybe_prepare_screen(config: dict[str, str]) -> None:
    if platform.system() == "Darwin":
        activate_macos_app(config)
    elif platform.system() == "Windows":
        activate_windows_app(config)

    delay = get_float(config, "SCREENSHOT_DELAY_SECONDS", 0.5)
    if delay > 0:
        time.sleep(delay)


def run_custom_screenshot_command(config: dict[str, str], destination: Path) -> bool:
    command_template = config.get("SCREENSHOT_COMMAND", "").strip()
    if not command_template:
        return False

    command = command_template.format(output=str(destination))
    logging.debug("运行自定义截图命令：%s", command)
    subprocess.run(command, shell=True, check=True)
    return True


def capture_macos(config: dict[str, str], destination: Path) -> None:
    args = ["screencapture", "-x", "-t", "png"]

    if get_bool(config, "MAC_OMIT_WINDOW_SHADOW", True):
        args.append("-o")

    window_id = config.get("MAC_WINDOW_ID", "").strip()
    display_id = config.get("MAC_DISPLAY_ID", "").strip()
    if window_id:
        args.append(f"-l{window_id}")
    elif display_id:
        args.append(f"-D{display_id}")

    args.append(str(destination))
    logging.debug("运行截图命令：%s", shlex.join(args))
    subprocess.run(args, check=True)


def capture_with_pillow(destination: Path) -> None:
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RuntimeError("当前系统需要安装 Pillow 才能截图：python3 -m pip install Pillow") from exc

    image = ImageGrab.grab(all_screens=True)
    image.save(destination)


def capture_linux(destination: Path) -> None:
    commands: list[list[str]] = []

    if shutil.which("gnome-screenshot"):
        commands.append(["gnome-screenshot", "-f", str(destination)])
    if shutil.which("scrot"):
        commands.append(["scrot", str(destination)])
    if shutil.which("import"):
        commands.append(["import", "-window", "root", str(destination)])

    for command in commands:
        try:
            logging.debug("运行截图命令：%s", shlex.join(command))
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError:
            logging.warning("截图命令失败，尝试下一个：%s", shlex.join(command))

    capture_with_pillow(destination)


def capture_screenshot(config: dict[str, str]) -> Path:
    destination = screenshot_path(config)
    maybe_prepare_screen(config)

    if run_custom_screenshot_command(config, destination):
        pass
    else:
        system = platform.system()
        if system == "Darwin":
            capture_macos(config, destination)
        elif system == "Windows":
            capture_with_pillow(destination)
        elif system == "Linux":
            capture_linux(destination)
        else:
            capture_with_pillow(destination)

    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(f"截图失败，文件没有生成或为空：{destination}")

    logging.info("已保存截图：%s", destination)
    return destination


def parse_recipients(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]


def guess_attachment_type(path: Path) -> tuple[str, str]:
    content_type = mimetypes.guess_type(path.name)[0] or "image/png"
    maintype, subtype = content_type.split("/", 1)
    return maintype, subtype


def configured_email(config: dict[str, str]) -> bool:
    keys = [
        "SMTP_HOST",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_PASSWORD_ENV",
        "SMTP_PASSWORD_FILE",
        "SMTP_PASSWORD_KEYRING_SERVICE",
        "SMTP_PASSWORD_KEYRING_USERNAME",
        "MAIL_TO",
        "EMAIL_TO",
    ]
    return any(config.get(key, "").strip() for key in keys)


def send_email(config: dict[str, str], screenshot: Path) -> str | None:
    if not get_bool(config, "EMAIL_ENABLED", True):
        return None
    if not configured_email(config):
        return None

    host = config.get("SMTP_HOST", "").strip()
    port = get_int(config, "SMTP_PORT", 465)
    username = (
        config.get("SMTP_USERNAME", "").strip()
        or config.get("SMTP_USER", "").strip()
        or config.get("MAIL_FROM", "").strip()
    )
    password = get_secret(
        config,
        "SMTP_PASSWORD",
        default_keyring_service=smtp_keyring_identity(config)[0],
        default_keyring_username=smtp_keyring_identity(config)[1],
    )
    if not password:
        password = get_secret(
            config,
            "SMTP_AUTH_CODE",
            default_keyring_service=smtp_keyring_identity(config)[0],
            default_keyring_username=smtp_keyring_identity(config)[1],
        )
    from_addr = config.get("MAIL_FROM", "").strip() or username
    recipients = parse_recipients(config.get("MAIL_TO", "") or config.get("EMAIL_TO", ""))

    missing = [
        name
        for name, value in {
            "SMTP_HOST": host,
            "SMTP_USERNAME": username,
            "SMTP_PASSWORD": password,
            "MAIL_FROM": from_addr,
            "MAIL_TO": ",".join(recipients),
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("邮件配置不完整，缺少：" + ", ".join(missing))

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    base_subject = config.get("MAIL_SUBJECT", "UU远程自动截图").strip() or "UU远程自动截图"
    subject = base_subject
    if get_bool(config, "MAIL_SUBJECT_WITH_TIME", True):
        subject = f"{base_subject} {timestamp}"

    body = config.get("MAIL_BODY", "").strip()
    if not body:
        body = f"自动截图时间：{timestamp}"

    content_id = f"screenshot_{uuid.uuid4().hex[:8]}"

    html = f"""\
<html>
<body>
<p>{body}</p>
<p><img src="cid:{content_id}" style="max-width:100%" alt="UU Screenshot"></p>
</body>
</html>"""

    message = MIMEMultipart("related")
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(html, "html"))

    if get_bool(config, "EMAIL_ATTACH_SCREENSHOT", True):
        maintype, subtype = guess_attachment_type(screenshot)
        with screenshot.open("rb") as file:
            img = MIMEImage(file.read(), _subtype=subtype)
            img.add_header("Content-ID", f"<{content_id}>")
            img.add_header("Content-Disposition", "inline", filename=screenshot.name)
            message.attach(img)

    security = config.get("SMTP_SECURITY", "").strip().lower()
    if not security:
        security = "ssl" if port == 465 else "starttls" if port == 587 else "plain"

    timeout = get_int(config, "SMTP_TIMEOUT_SECONDS", 30)
    logging.info("发送邮件到：%s", ", ".join(recipients))

    if security == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            if security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(message)

    return f"邮件已发送到 {', '.join(recipients)}"


def http_request(
    url: str,
    *,
    method: str = "POST",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> str:
    req = request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read(600).decode("utf-8", errors="replace")
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {body}")
            return body
    except error.HTTPError as exc:
        body = exc.read(600).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def notification_title(config: dict[str, str]) -> str:
    return config.get("PUSH_TITLE", "UU远程截图已生成").strip() or "UU远程截图已生成"


def notification_body(screenshot: Path) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{timestamp} 已生成截图：{screenshot.name}。如果配置了邮件，截图已作为附件发送。"


def send_serverchan(config: dict[str, str], screenshot: Path) -> str | None:
    sendkey = config.get("SERVERCHAN_SENDKEY", "").strip()
    url = config.get("SERVERCHAN_URL", "").strip()
    if not sendkey and not url:
        return None
    if not url:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"

    data = parse.urlencode(
        {
            "title": notification_title(config),
            "desp": notification_body(screenshot),
        }
    ).encode("utf-8")
    http_request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=get_int(config, "PUSH_TIMEOUT_SECONDS", 20),
    )
    return "Server酱推送已发送"


def send_bark(config: dict[str, str], screenshot: Path) -> str | None:
    key = config.get("BARK_KEY", "").strip()
    url = config.get("BARK_URL", "").strip()
    if not key and not url:
        return None
    if not url:
        server = config.get("BARK_SERVER", "https://api.day.app").strip().rstrip("/")
        url = f"{server}/{parse.quote(key, safe='')}"

    payload = {
        "title": notification_title(config),
        "body": notification_body(screenshot),
    }
    open_url = config.get("BARK_OPEN_URL", "").strip()
    if open_url:
        payload["url"] = open_url

    data = parse.urlencode(payload).encode("utf-8")
    http_request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=get_int(config, "PUSH_TIMEOUT_SECONDS", 20),
    )
    return "Bark 推送已发送"


def send_generic_webhook(config: dict[str, str], screenshot: Path) -> str | None:
    url = config.get("PUSH_WEBHOOK_URL", "").strip()
    if not url:
        return None

    method = config.get("PUSH_WEBHOOK_METHOD", "POST").strip().upper()
    body_format = config.get("PUSH_WEBHOOK_FORMAT", "json").strip().lower()
    payload = {
        "title": notification_title(config),
        "body": notification_body(screenshot),
        "screenshot_path": str(screenshot),
        "screenshot_name": screenshot.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    if method == "GET":
        separator = "&" if "?" in url else "?"
        query = parse.urlencode(payload)
        http_request(
            f"{url}{separator}{query}",
            method="GET",
            timeout=get_int(config, "PUSH_TIMEOUT_SECONDS", 20),
        )
    elif body_format == "form":
        data = parse.urlencode(payload).encode("utf-8")
        http_request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=get_int(config, "PUSH_TIMEOUT_SECONDS", 20),
        )
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            timeout=get_int(config, "PUSH_TIMEOUT_SECONDS", 20),
        )

    return "通用 Webhook 推送已发送"


def configured_twilio(config: dict[str, str]) -> bool:
    keys = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO"]
    return any(config.get(key, "").strip() for key in keys)


def send_twilio_sms(config: dict[str, str], screenshot: Path) -> str | None:
    if not configured_twilio(config):
        return None

    sid = config.get("TWILIO_ACCOUNT_SID", "").strip()
    token = config.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = config.get("TWILIO_FROM", "").strip()
    to_number = config.get("TWILIO_TO", "").strip()
    missing = [
        name
        for name, value in {
            "TWILIO_ACCOUNT_SID": sid,
            "TWILIO_AUTH_TOKEN": token,
            "TWILIO_FROM": from_number,
            "TWILIO_TO": to_number,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Twilio 短信配置不完整，缺少：" + ", ".join(missing))

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    message_body = config.get("SMS_BODY", "").strip() or notification_body(screenshot)
    data = parse.urlencode(
        {
            "From": from_number,
            "To": to_number,
            "Body": message_body,
        }
    ).encode("utf-8")
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")

    http_request(
        url,
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=get_int(config, "PUSH_TIMEOUT_SECONDS", 20),
    )
    return f"短信已发送到 {to_number}"


def send_notifications(config: dict[str, str], screenshot: Path) -> tuple[int, int]:
    senders: list[tuple[str, Callable[[dict[str, str], Path], str | None]]] = [
        ("email", send_email),
        ("serverchan", send_serverchan),
        ("bark", send_bark),
        ("webhook", send_generic_webhook),
        ("twilio", send_twilio_sms),
    ]

    sent = 0
    failed = 0
    for name, sender in senders:
        try:
            result = sender(config, screenshot)
        except Exception:
            failed += 1
            logging.exception("%s 通知失败", name)
            continue

        if result:
            sent += 1
            logging.info(result)

    if sent == 0 and failed == 0:
        logging.warning("没有配置通知渠道；截图只保存在本地。")

    return sent, failed


def cleanup_old_screenshots(config: dict[str, str]) -> None:
    keep = get_int(config, "RETENTION_COUNT", 100)
    if keep <= 0:
        return

    directory = output_dir(config)
    prefix = config.get("SCREENSHOT_PREFIX", "uu_screenshot").strip() or "uu_screenshot"
    files = sorted(
        directory.glob(f"{prefix}_*.png"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for old_file in files[keep:]:
        try:
            old_file.unlink()
            logging.debug("删除旧截图：%s", old_file)
        except OSError:
            logging.warning("删除旧截图失败：%s", old_file)


def run_cycle(config: dict[str, str], dry_run: bool) -> bool:
    try:
        screenshot = capture_screenshot(config)
    except Exception:
        logging.exception("截图失败")
        return False

    if dry_run:
        logging.info("dry-run：跳过发送通知。")
        cleanup_old_screenshots(config)
        return True

    sent, failed = send_notifications(config, screenshot)
    cleanup_old_screenshots(config)
    return failed == 0 or sent > 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="定时截图 UU 远程界面，并通过邮件、Webhook 或短信推送提醒。",
    )
    parser.add_argument("--env", default=".env", help="配置文件路径，默认 .env")
    parser.add_argument("--once", action="store_true", help="只截图并发送一次，然后退出")
    parser.add_argument("--interval", type=int, help="覆盖 INTERVAL_SECONDS，单位秒")
    parser.add_argument("--dry-run", action="store_true", help="只截图，不发送邮件/推送")
    parser.add_argument("--store-smtp-password", action="store_true", help="把 SMTP 授权码安全保存到系统凭据管理器")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印更详细日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = build_config(Path(args.env).expanduser())
    if args.interval:
        config["INTERVAL_SECONDS"] = str(args.interval)

    if args.store_smtp_password:
        return store_smtp_password(config)

    setup_logging(config, args.verbose)

    interval = get_int(config, "INTERVAL_SECONDS", 300)
    if interval <= 0:
        raise ValueError("INTERVAL_SECONDS 必须大于 0")

    logging.info("启动 UU 截图监控，间隔 %s 秒。", interval)
    while True:
        ok = run_cycle(config, args.dry_run)
        if args.once:
            return 0 if ok else 1

        logging.info("等待 %s 秒后继续。", interval)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
