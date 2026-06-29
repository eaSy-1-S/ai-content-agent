import json
import os
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def load_env_file():
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    load_env_file()

    app_id = os.getenv("WECHAT_APP_ID", "")
    app_secret = os.getenv("WECHAT_APP_SECRET", "")

    if not app_id or not app_secret:
        raise RuntimeError("请先在 .env 中配置 WECHAT_APP_ID 和 WECHAT_APP_SECRET")

    url = "https://api.weixin.qq.com/cgi-bin/token"

    params = {
        "grant_type": "client_credential",
        "appid": app_id,
        "secret": app_secret,
    }

    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()

    print(json.dumps(data, ensure_ascii=False, indent=2))

    if "access_token" in data:
        print("获取 access_token 成功。")
    else:
        print("获取 access_token 失败，请检查 AppID、AppSecret、IP 白名单。")


if __name__ == "__main__":
    main()