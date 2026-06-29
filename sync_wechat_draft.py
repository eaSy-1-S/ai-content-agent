import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


# ============================================================
# 1. 基础配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

REQUEST_TIMEOUT_SHORT = 30
REQUEST_TIMEOUT_LONG = 120

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
WECHAT_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WECHAT_DRAFT_ADD_URL = "https://api.weixin.qq.com/cgi-bin/draft/add"

# 每次最多同步几篇草稿
SYNC_LIMIT = 1


# ============================================================
# 2. 飞书字段名
# ============================================================

FIELD_GENERATED_STATUS = "是否已生成"
FIELD_DRAFT_SYNC_STATUS = "是否已同步草稿"

FIELD_FINAL_TITLE = "最终标题"
FIELD_ARTICLE_SUMMARY = "文章摘要"
FIELD_PUBLIC_BODY = "公众号正文"

FIELD_DRAFT_MEDIA_ID = "草稿 media_id"
FIELD_DRAFT_SYNC_TIME = "草稿同步时间"
FIELD_DRAFT_SYNC_ERROR = "草稿同步失败原因"


# ============================================================
# 3. 读取 .env
# ============================================================

def load_env_file() -> None:
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'")
        )


load_env_file()

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "")
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "")

WECHAT_APP_ID = os.getenv("WECHAT_APP_ID", "")
WECHAT_APP_SECRET = os.getenv("WECHAT_APP_SECRET", "")
WECHAT_AUTHOR = os.getenv("WECHAT_AUTHOR", "")
WECHAT_DEFAULT_THUMB_MEDIA_ID = os.getenv("WECHAT_DEFAULT_THUMB_MEDIA_ID", "")


def validate_config() -> None:
    required = {
        "FEISHU_APP_ID": FEISHU_APP_ID,
        "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
        "FEISHU_APP_TOKEN": FEISHU_APP_TOKEN,
        "FEISHU_TABLE_ID": FEISHU_TABLE_ID,
        "WECHAT_APP_ID": WECHAT_APP_ID,
        "WECHAT_APP_SECRET": WECHAT_APP_SECRET,
        "WECHAT_AUTHOR": WECHAT_AUTHOR,
        "WECHAT_DEFAULT_THUMB_MEDIA_ID": WECHAT_DEFAULT_THUMB_MEDIA_ID,
    }

    missing = [key for key, value in required.items() if not value]

    if missing:
        raise RuntimeError("配置缺失：" + "、".join(missing))


# ============================================================
# 4. 通用工具
# ============================================================

def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    timeout: int = 30,
    max_retries: int = 3,
    **kwargs
):
    """
    带重试的请求函数。
    用于减少飞书/微信接口偶发 SSL 握手失败导致整个任务中断的问题。
    """
    session.trust_env = False
    last_error = None

    for attempt in range(max_retries):
        try:
            response = session.request(
                method=method,
                url=url,
                timeout=timeout,
                **kwargs
            )
            return response

        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as error:
            last_error = error
            print(f"网络请求失败，正在重试 {attempt + 1}/{max_retries}：{repr(error)}")
            time.sleep(2 * (attempt + 1))

    raise last_error

def feishu_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()

    if isinstance(value, dict):
        return str(
            value.get("text")
            or value.get("name")
            or value.get("value")
            or ""
        ).strip()

    return str(value).strip()


def now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def markdown_to_wechat_html(markdown_text: str) -> str:
    """
    将 Markdown 风格正文转换为微信公众号草稿可用 HTML。

    优化点：
    - 普通段落首行缩进 2em
    - 增加行高和段落间距
    - 支持 **加粗**
    - 支持自然小标题
    - 支持列表
    - 过滤掉分隔线 ---
    """
    if not markdown_text:
        return ""

    import html

    def convert_bold(text: str) -> str:
        # 先转义，避免正文里的特殊字符影响 HTML
        text = html.escape(text)
        # 再转换 **加粗**
        text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
        return text

    lines = markdown_text.splitlines()
    html_parts = []

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            continue

        if line in ["---", "——", "———"]:
            continue

        # Markdown 二级标题
        if line.startswith("## "):
            title = convert_bold(line[3:].strip())
            html_parts.append(
                f'<h2 style="font-size:18px;font-weight:700;'
                f'line-height:1.8;margin:28px 0 14px;">{title}</h2>'
            )
            continue

        # Markdown 三级标题
        if line.startswith("### "):
            title = convert_bold(line[4:].strip())
            html_parts.append(
                f'<h3 style="font-size:17px;font-weight:700;'
                f'line-height:1.8;margin:24px 0 12px;">{title}</h3>'
            )
            continue

        # 项目符号
        if line.startswith("- ") or line.startswith("• "):
            item = line[2:].strip()
            item_html = convert_bold(item)
            html_parts.append(
                f'<p style="font-size:16px;line-height:1.9;'
                f'margin:0 0 12px;padding-left:1em;">• {item_html}</p>'
            )
            continue

        # 普通段落：首行缩进
        paragraph = convert_bold(line)
        html_parts.append(
            f'<p style="font-size:16px;line-height:1.9;'
            f'margin:0 0 16px;text-indent:2em;">{paragraph}</p>'
        )

    return "\n".join(html_parts)


# ============================================================
# 5. 飞书 API
# ============================================================

def get_feishu_token(session: requests.Session) -> str:
    payload = {
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }

    resp = request_with_retry(
        session=session,
        method="POST",
        url=FEISHU_TOKEN_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT_SHORT
    )

    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "获取飞书 token 失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )

    return data["tenant_access_token"]


def feishu_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def list_feishu_records(session: requests.Session, token: str) -> List[Dict[str, Any]]:
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/search"
    )

    headers = feishu_headers(token)
    all_items = []
    page_token = ""

    while True:
        payload: Dict[str, Any] = {
            "page_size": 100
        }

        if page_token:
            payload["page_token"] = page_token

        resp = session.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SHORT
        )

        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                "读取飞书记录失败："
                + json.dumps(data, ensure_ascii=False, indent=2)
            )

        data_obj = data.get("data", {})
        all_items.extend(data_obj.get("items", []))

        if not data_obj.get("has_more", False):
            break

        page_token = data_obj.get("page_token", "")

    return all_items


def find_records_to_sync(records: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """
    找出已生成但未同步草稿的记录。
    """
    selected = []

    for record in records:
        fields = record.get("fields", {})

        generated_status = feishu_text(fields.get(FIELD_GENERATED_STATUS))
        draft_status = feishu_text(fields.get(FIELD_DRAFT_SYNC_STATUS))

        title = feishu_text(fields.get(FIELD_FINAL_TITLE))
        body = feishu_text(fields.get(FIELD_PUBLIC_BODY))

        if generated_status == "是" and draft_status in ["", "否", "失败"] and title and body:
            selected.append(record)

        if len(selected) >= limit:
            break

    return selected


def update_draft_success(
    session: requests.Session,
    token: str,
    record_id: str,
    draft_media_id: str,
) -> None:
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
    )

    payload = {
        "fields": {
            FIELD_DRAFT_SYNC_STATUS: "是",
            FIELD_DRAFT_MEDIA_ID: draft_media_id,
            FIELD_DRAFT_SYNC_TIME: now_ms(),
            FIELD_DRAFT_SYNC_ERROR: "",
        }
    }

    resp = session.put(
        url,
        headers=feishu_headers(token),
        json=payload,
        timeout=REQUEST_TIMEOUT_SHORT
    )

    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "回写草稿同步成功状态失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )


def update_draft_failed(
    session: requests.Session,
    token: str,
    record_id: str,
    error_message: str,
) -> None:
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
    )

    payload = {
        "fields": {
            FIELD_DRAFT_SYNC_STATUS: "失败",
            FIELD_DRAFT_SYNC_ERROR: error_message[:1000],
        }
    }

    resp = session.put(
        url,
        headers=feishu_headers(token),
        json=payload,
        timeout=REQUEST_TIMEOUT_SHORT
    )

    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "回写草稿同步失败状态失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )


# ============================================================
# 6. 微信公众号 API
# ============================================================

def get_wechat_access_token(session: requests.Session) -> str:
    params = {
        "grant_type": "client_credential",
        "appid": WECHAT_APP_ID,
        "secret": WECHAT_APP_SECRET,
    }

    resp = session.get(
        WECHAT_TOKEN_URL,
        params=params,
        timeout=REQUEST_TIMEOUT_SHORT
    )

    data = resp.json()

    if "access_token" not in data:
        raise RuntimeError(
            "获取微信公众号 access_token 失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )

    return data["access_token"]


def add_wechat_draft(
    session: requests.Session,
    access_token: str,
    title: str,
    digest: str,
    content_html: str,
) -> str:
    url = WECHAT_DRAFT_ADD_URL

    params = {
        "access_token": access_token
    }

    article = {
        "title": title[:64],
        "author": WECHAT_AUTHOR,
        "digest": digest[:120],
        "content": content_html,
        "thumb_media_id": WECHAT_DEFAULT_THUMB_MEDIA_ID,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }

    payload = {
        "articles": [
            article
        ]
    }

    # 微信接口处理中文时，显式使用 UTF-8 更稳
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    resp = session.post(
        url,
        params=params,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8"
        },
        timeout=REQUEST_TIMEOUT_LONG
    )

    data = resp.json()

    if "media_id" not in data:
        raise RuntimeError(
            "新增微信公众号草稿失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )

    return data["media_id"]


# ============================================================
# 7. 主流程
# ============================================================

def sync_one_record(
    session: requests.Session,
    feishu_token: str,
    wechat_token: str,
    record: Dict[str, Any],
) -> Dict[str, str]:
    record_id = record["record_id"]
    fields = record.get("fields", {})

    title = feishu_text(fields.get(FIELD_FINAL_TITLE))
    digest = feishu_text(fields.get(FIELD_ARTICLE_SUMMARY))
    body = feishu_text(fields.get(FIELD_PUBLIC_BODY))

    content_html = markdown_to_wechat_html(body)

    try:
        draft_media_id = add_wechat_draft(
            session=session,
            access_token=wechat_token,
            title=title,
            digest=digest,
            content_html=content_html,
        )

        update_draft_success(
            session=session,
            token=feishu_token,
            record_id=record_id,
            draft_media_id=draft_media_id,
        )

        return {
            "title": title,
            "status": "success",
            "message": "已同步到微信公众号草稿箱",
        }

    except Exception as error:
        error_message = str(error)

        update_draft_failed(
            session=session,
            token=feishu_token,
            record_id=record_id,
            error_message=error_message,
        )

        return {
            "title": title,
            "status": "failed",
            "message": error_message[:300],
        }

def sync_wechat_drafts_by_record_ids(record_ids: List[str]) -> List[Dict[str, str]]:
    """
    按指定飞书 record_id 同步公众号草稿。
    用于确保“刚生成哪几篇，就同步哪几篇”。
    """
    validate_config()

    results = []

    with requests.Session() as session:
        session.trust_env = False

        feishu_token = get_feishu_token(session)
        wechat_token = get_wechat_access_token(session)

        records = list_feishu_records(session, feishu_token)
        record_map = {
            record["record_id"]: record
            for record in records
        }

        for record_id in record_ids:
            record = record_map.get(record_id)

            if not record:
                results.append(
                    {
                        "title": record_id,
                        "status": "failed",
                        "message": "没有找到对应的飞书记录"
                    }
                )
                continue

            fields = record.get("fields", {})
            title = feishu_text(fields.get(FIELD_FINAL_TITLE)) or "未命名文章"
            draft_status = feishu_text(fields.get(FIELD_DRAFT_SYNC_STATUS))

            if draft_status == "是":
                results.append(
                    {
                        "title": title,
                        "status": "skipped",
                        "message": "这篇文章已经同步过草稿箱，已跳过"
                    }
                )
                continue

            result = sync_one_record(
                session=session,
                feishu_token=feishu_token,
                wechat_token=wechat_token,
                record=record,
            )

            results.append(result)

    return results


def get_content_status_summary() -> Dict[str, int]:
    """
    统计飞书选题库当前处理状态。
    给飞书机器人“状态”命令使用。
    """
    validate_config()

    with requests.Session() as session:
        session.trust_env = False

        feishu_token = get_feishu_token(session)
        records = list_feishu_records(session, feishu_token)

    total = len(records)

    not_generated = 0
    generated_not_synced = 0
    synced = 0
    generation_failed = 0
    draft_failed = 0
    generated_but_missing_content = 0

    for record in records:
        fields = record.get("fields", {})

        generated_status = feishu_text(fields.get(FIELD_GENERATED_STATUS))
        draft_status = feishu_text(fields.get(FIELD_DRAFT_SYNC_STATUS))

        title = feishu_text(fields.get(FIELD_FINAL_TITLE))
        body = feishu_text(fields.get(FIELD_PUBLIC_BODY))

        if generated_status in ["", "否", "失败"]:
            not_generated += 1

        if generated_status == "失败":
            generation_failed += 1

        if generated_status == "是" and draft_status in ["", "否", "失败"]:
            generated_not_synced += 1

        if draft_status == "是":
            synced += 1

        if draft_status == "失败":
            draft_failed += 1

        if generated_status == "是" and (not title or not body):
            generated_but_missing_content += 1

    return {
        "total": total,
        "not_generated": not_generated,
        "generated_not_synced": generated_not_synced,
        "synced": synced,
        "generation_failed": generation_failed,
        "draft_failed": draft_failed,
        "generated_but_missing_content": generated_but_missing_content,
    }



def sync_wechat_drafts_by_count(count: int = 1) -> List[Dict[str, str]]:
    """
    给飞书机器人调用的草稿同步入口。
    从飞书表格中找出“已生成但未同步草稿”的文章，同步到微信公众号草稿箱。
    """
    validate_config()

    results = []

    with requests.Session() as session:
        session.trust_env = False

        feishu_token = get_feishu_token(session)
        wechat_token = get_wechat_access_token(session)

        records = list_feishu_records(session, feishu_token)
        records_to_sync = find_records_to_sync(records, limit=count)

        if not records_to_sync:
            return [
                {
                    "title": "",
                    "status": "empty",
                    "message": "没有找到需要同步到公众号草稿箱的文章。"
                }
            ]

        for record in records_to_sync:
            result = sync_one_record(
                session=session,
                feishu_token=feishu_token,
                wechat_token=wechat_token,
                record=record,
            )
            results.append(result)

    return results


def main() -> None:
    validate_config()

    with requests.Session() as session:
        print("1. 正在获取飞书 token...")
        feishu_token = get_feishu_token(session)

        print("2. 正在获取微信公众号 access_token...")
        wechat_token = get_wechat_access_token(session)

        print("3. 正在读取飞书记录...")
        records = list_feishu_records(session, feishu_token)

        records_to_sync = find_records_to_sync(records, limit=SYNC_LIMIT)

        if not records_to_sync:
            print("没有找到需要同步到公众号草稿箱的文章。")
            return

        print(f"找到 {len(records_to_sync)} 篇待同步文章。")

        for record in records_to_sync:
            result = sync_one_record(
                session=session,
                feishu_token=feishu_token,
                wechat_token=wechat_token,
                record=record,
            )

            if result["status"] == "success":
                print(f"同步成功：{result['title']}")
            else:
                print(f"同步失败：{result['title']}，原因：{result['message']}")

        print("本次草稿同步任务结束。")


if __name__ == "__main__":
    main()