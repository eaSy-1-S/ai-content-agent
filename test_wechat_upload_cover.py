import json
import os
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
COVER_PATH = BASE_DIR / "cover.jpg"


def load_env_file():
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_wechat_access_token() -> str:
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

    if "access_token" not in data:
        raise RuntimeError(
            "获取 access_token 失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )

    return data["access_token"]


def upload_cover_image(access_token: str) -> dict:
    if not COVER_PATH.exists():
        raise RuntimeError(f"没有找到封面图：{COVER_PATH}")

    url = "https://api.weixin.qq.com/cgi-bin/material/add_material"

    params = {
        "access_token": access_token,
        "type": "image",
    }

    with COVER_PATH.open("rb") as file:
        files = {
            "media": ("cover.jpg", file, "image/jpeg")
        }

        resp = requests.post(
            url,
            params=params,
            files=files,
            timeout=60
        )

    return resp.json()


def main():
    load_env_file()

    access_token = get_wechat_access_token()
    result = upload_cover_image(access_token)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if "media_id" in result:
        print("\n封面图上传成功。")
        print("请把这个 media_id 填入 .env：")
        print(f"WECHAT_DEFAULT_THUMB_MEDIA_ID={result['media_id']}")
    else:
        print("\n封面图上传失败，请根据 errcode 和 errmsg 排查。")


if __name__ == "__main__":
    main()