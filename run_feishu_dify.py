import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime
import requests


# ============================================================
# 1. 基础配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

DIFY_USER_ID = "feishu-auto-user"
REQUEST_TIMEOUT_SHORT = 30
REQUEST_TIMEOUT_LONG = 600

# 每次处理多少条未生成记录
# 当前建议先保持 1，避免一次消耗太多 Dify 额度
PROCESS_LIMIT = 3

# 是否显示 Dify streaming 详细事件
# 你要求不显示，所以默认 False
SHOW_DIFY_EVENTS = False


# ============================================================
# 2. 飞书字段名配置
# ============================================================

FIELD_BOOK_NAME = "书名"
FIELD_AUTHOR = "作者"
FIELD_CATEGORY = "分类"
FIELD_TARGET_READER = "目标读者"
FIELD_RECOMMEND_ANGLE = "推荐角度"
FIELD_KEYWORDS = "核心关键词"
FIELD_TITLE_DIRECTION = "文章标题方向"
FIELD_SUITABLE_PEOPLE = "适合人群"

FIELD_GENERATED_STATUS = "是否已生成"
FIELD_FULL_ARTICLE = "最终推文"
FIELD_REVIEW_RESULT = "审核结果"

FIELD_FINAL_TITLE = "最终标题"
FIELD_ARTICLE_SUMMARY = "文章摘要"
FIELD_COVER_TEXT = "封面文案"
FIELD_PUBLIC_BODY = "公众号正文"
FIELD_PRE_PUBLISH_CHECK = "发布前检查"
FIELD_GENERATED_TIME = "生成时间"


# ============================================================
# 3. 读取 .env 配置
# ============================================================

def load_env_file(env_path: Path) -> None:
    """
    读取同目录下的 .env 文件。

    格式示例：
    FEISHU_APP_ID=xxx
    FEISHU_APP_SECRET=xxx
    """
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ENV_PATH)

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "")
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "")

DIFY_API_KEY = os.getenv("DIFY_API_KEY", "")
DIFY_API_URL = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1/workflows/run")


def validate_config() -> None:
    required_configs = {
        "FEISHU_APP_ID": FEISHU_APP_ID,
        "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
        "FEISHU_APP_TOKEN": FEISHU_APP_TOKEN,
        "FEISHU_TABLE_ID": FEISHU_TABLE_ID,
        "DIFY_API_KEY": DIFY_API_KEY,
        "DIFY_API_URL": DIFY_API_URL,
    }

    missing = [key for key, value in required_configs.items() if not value]

    if missing:
        raise RuntimeError(
            "配置缺失，请检查 .env 文件："
            + "、".join(missing)
        )


# ============================================================
# 4. 通用工具函数
# ============================================================

def feishu_text(value: Any) -> str:
    """
    把飞书多维表格字段值转换为普通文本。

    兼容：
    - 普通文本
    - 富文本数组
    - 单选/多选
    - 空值
    """
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


def remove_think_tags(text: str) -> str:
    """
    删除模型返回中的 <think>...</think> 内容。
    """
    if not text:
        return ""

    return re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL
    ).strip()


def extract_between(text: str, start_marker: str, end_marker: Optional[str] = None) -> str:
    """
    从文本中提取两个标记之间的内容。
    """
    if not text:
        return ""

    start_index = text.find(start_marker)
    if start_index == -1:
        return ""

    start_index += len(start_marker)

    if end_marker:
        end_index = text.find(end_marker, start_index)
        if end_index == -1:
            return text[start_index:].strip()
        return text[start_index:end_index].strip()

    return text[start_index:].strip()


# ============================================================
# 5. 飞书 API
# ============================================================

def get_feishu_token(session: requests.Session) -> str:
    payload = {
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }

    response = session.post(
        FEISHU_TOKEN_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT_SHORT
    )

    data = response.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "获取飞书 tenant_access_token 失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )

    return data["tenant_access_token"]


def build_feishu_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def list_records(session: requests.Session, token: str) -> List[Dict[str, Any]]:
    """
    读取飞书多维表格记录。

    这里使用 records/search 接口。
    如果你的数据量未来很多，可以继续扩展筛选条件。
    """
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/search"
    )

    headers = build_feishu_headers(token)

    all_items: List[Dict[str, Any]] = []
    page_token = ""

    while True:
        payload: Dict[str, Any] = {
            "page_size": 100
        }

        if page_token:
            payload["page_token"] = page_token

        response = session.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SHORT
        )

        data = response.json()

        if data.get("code") != 0:
            raise RuntimeError(
                "读取飞书记录失败："
                + json.dumps(data, ensure_ascii=False, indent=2)
            )

        response_data = data.get("data", {})
        items = response_data.get("items", [])
        all_items.extend(items)

        has_more = response_data.get("has_more", False)
        page_token = response_data.get("page_token", "")

        if not has_more:
            break

    return all_items


def find_unfinished_records(records: List[Dict[str, Any]], limit: int = 1) -> List[Dict[str, Any]]:
    """
    找出未生成记录。

    状态为空、否、失败，都视为待处理。
    """
    unfinished = []

    for record in records:
        fields = record.get("fields", {})
        status = feishu_text(fields.get(FIELD_GENERATED_STATUS))

        if status in ["", "否", "失败"]:
            unfinished.append(record)

        if len(unfinished) >= limit:
            break

    return unfinished


def update_record(
    session: requests.Session,
    token: str,
    record_id: str,
    final_article: str,
    review_result: str,
) -> None:
    """
    回写最终推文、审核结果，以及拆分后的标题、摘要、封面文案、正文等字段。
    """
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
    )

    headers = build_feishu_headers(token)
    parsed_article = parse_final_article(final_article)

    payload = {
        "fields": {
            FIELD_GENERATED_STATUS: "是",
            FIELD_GENERATED_TIME: int(datetime.now().timestamp() * 1000),
            FIELD_FULL_ARTICLE: final_article,
            FIELD_REVIEW_RESULT: review_result,
            FIELD_FINAL_TITLE: parsed_article[FIELD_FINAL_TITLE],
            FIELD_ARTICLE_SUMMARY: parsed_article[FIELD_ARTICLE_SUMMARY],
            FIELD_COVER_TEXT: parsed_article[FIELD_COVER_TEXT],
            FIELD_PUBLIC_BODY: parsed_article[FIELD_PUBLIC_BODY],
            FIELD_PRE_PUBLISH_CHECK: parsed_article[FIELD_PRE_PUBLISH_CHECK],
        }
    }

    response = session.put(
        url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT_SHORT
    )

    data = response.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "回写飞书记录失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )


def mark_record_failed(
    session: requests.Session,
    token: str,
    record_id: str,
    error_message: str,
) -> None:
    """
    某条记录处理失败时，标记为失败，并把错误写入审核结果字段。
    """
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
        f"{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
    )

    headers = build_feishu_headers(token)

    payload = {
        "fields": {
            FIELD_GENERATED_STATUS: "失败",
            FIELD_REVIEW_RESULT: f"自动生成失败：{error_message[:1000]}",
        }
    }

    response = session.put(
        url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT_SHORT
    )

    data = response.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "标记失败状态时，飞书回写失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )


# ============================================================
# 6. Dify API
# ============================================================

def build_book_info(fields: Dict[str, Any]) -> str:
    """
    拼接 Dify 工作流需要的 book_info。
    """
    return f"""书名：{feishu_text(fields.get(FIELD_BOOK_NAME))}
作者：{feishu_text(fields.get(FIELD_AUTHOR))}
分类：{feishu_text(fields.get(FIELD_CATEGORY))}
目标读者：{feishu_text(fields.get(FIELD_TARGET_READER))}
推荐角度：{feishu_text(fields.get(FIELD_RECOMMEND_ANGLE))}
核心关键词：{feishu_text(fields.get(FIELD_KEYWORDS))}
标题方向：{feishu_text(fields.get(FIELD_TITLE_DIRECTION))}
适合人群：{feishu_text(fields.get(FIELD_SUITABLE_PEOPLE))}
"""


def call_dify(session: requests.Session, book_info: str) -> Dict[str, Any]:
    """
    调用 Dify Workflow API。

    使用 streaming 模式，避免长工作流 blocking 超时。
    不打印 text_chunk / node_started / node_finished 等详细过程。
    """
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "inputs": {
            "book_info": book_info,
        },
        "response_mode": "streaming",
        "user": DIFY_USER_ID,
    }

    response = session.post(
        DIFY_API_URL,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT_LONG,
        stream=True
    )

    if response.status_code != 200:
        preview = response.text[:500]
        raise RuntimeError(
            f"Dify 调用失败。HTTP 状态码：{response.status_code}，返回前 500 字：{preview}"
        )

    final_outputs: Dict[str, Any] = {}

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue

        line = raw_line.strip()

        if not line.startswith("data:"):
            continue

        data_text = line[len("data:"):].strip()

        if data_text == "[DONE]":
            break

        try:
            event_data = json.loads(data_text)
        except json.JSONDecodeError:
            continue

        event_name = event_data.get("event")

        if SHOW_DIFY_EVENTS and event_name:
            print(f"   Dify事件：{event_name}")

        if event_name == "workflow_finished":
            data_obj = event_data.get("data", {})
            final_outputs = data_obj.get("outputs", {})
            break

    if not final_outputs:
        raise RuntimeError("Dify streaming 已结束，但没有拿到 workflow_finished.outputs。")

    return final_outputs


def parse_dify_outputs(outputs: Dict[str, Any]) -> Dict[str, str]:
    """
    从 Dify outputs 中解析最终推文和审核结果。

    兼容旧字段名：
    - final_acticle 是之前误拼写的字段
    - article 是之前用过的字段
    """
    final_article = (
        outputs.get("final_article")
        or outputs.get("final_acticle")
        or outputs.get("final")
        or outputs.get("article")
        or ""
    )

    review_result = (
        outputs.get("review_result")
        or outputs.get("review")
        or ""
    )

    final_article = remove_think_tags(str(final_article))
    review_result = remove_think_tags(str(review_result))

    if not final_article:
        raise RuntimeError(
            "Dify 没有返回最终推文字段，实际 outputs 为："
            + json.dumps(outputs, ensure_ascii=False, indent=2)
        )

    return {
        "final_article": final_article,
        "review_result": review_result,
    }


def parse_final_article(final_article: str) -> Dict[str, str]:
    """
    把 Dify 返回的最终推文拆成：
    - 最终标题
    - 文章摘要
    - 封面文案
    - 公众号正文
    - 发布前检查
    """
    title = extract_between(final_article, "【最终标题】", "【文章摘要】")
    summary = extract_between(final_article, "【文章摘要】", "【封面文案】")
    cover_text = extract_between(final_article, "【封面文案】", "【公众号正文】")
    body = extract_between(final_article, "【公众号正文】", "【发布前检查】")
    checklist = extract_between(final_article, "【发布前检查】")

    return {
        FIELD_FINAL_TITLE: title,
        FIELD_ARTICLE_SUMMARY: summary,
        FIELD_COVER_TEXT: cover_text,
        FIELD_PUBLIC_BODY: body,
        FIELD_PRE_PUBLISH_CHECK: checklist,
    }


# ============================================================
# 7. 主业务流程
# ============================================================

def process_record(
    session: requests.Session,
    token: str,
    record: Dict[str, Any],
) -> None:
    record_id = record["record_id"]
    fields = record.get("fields", {})

    book_name = feishu_text(fields.get(FIELD_BOOK_NAME)) or "未命名书籍"

    print(f"正在处理：《{book_name}》")

    book_info = build_book_info(fields)
    outputs = call_dify(session, book_info)

    parsed_outputs = parse_dify_outputs(outputs)
    final_article = parsed_outputs["final_article"]
    review_result = parsed_outputs["review_result"]

    update_record(
        session=session,
        token=token,
        record_id=record_id,
        final_article=final_article,
        review_result=review_result,
    )

    print(f"完成：《{book_name}》已写回飞书。")


def generate_articles_by_count(count: int) -> list:
    """
    给飞书机器人 / Agent 控制器调用的生成入口。

    参数：
    count: 要生成几篇

    返回：
    [
        {
            "book_name": "深度工作",
            "status": "success",
            "message": "已写回飞书"
        },
        ...
    ]
    """
    validate_config()

    results = []

    with requests.Session() as session:
        token = get_feishu_token(session)
        records = list_records(session, token)
        unfinished_records = find_unfinished_records(records, limit=count)

        if not unfinished_records:
            return [
                {
                    "book_name": "",
                    "status": "empty",
                    "message": "没有找到需要生成的记录。请确认“是否已生成”字段为空、否或失败。"
                }
            ]

        for record in unfinished_records:
            record_id = record["record_id"]
            fields = record.get("fields", {})
            book_name = feishu_text(fields.get(FIELD_BOOK_NAME)) or "未命名书籍"

            try:
                process_record(session, token, record)

                results.append(
                    {
                        "record_id": record_id,
                        "book_name": book_name,
                        "status": "success",
                        "message": "已生成并写回飞书"
                    }
                )

            except Exception as error:
                error_message = str(error)

                try:
                    mark_record_failed(session, token, record_id, error_message)
                except Exception:
                    pass

                results.append(
                    {
                        "record_id": record_id,
                        "book_name": book_name,
                        "status": "failed",
                        "message": error_message[:300]
                    }
                )

    return results


def main() -> None:
    validate_config()

    with requests.Session() as session:
        print("1. 正在获取飞书 token...")
        token = get_feishu_token(session)

        print("2. 正在读取飞书多维表格记录...")
        records = list_records(session, token)

        print(f"读取到 {len(records)} 条记录。")

        unfinished_records = find_unfinished_records(records, limit=PROCESS_LIMIT)

        if not unfinished_records:
            print("没有找到需要生成的记录。请确认“是否已生成”字段为空、否或失败。")
            return

        print(f"找到 {len(unfinished_records)} 条待生成记录。")

        for record in unfinished_records:
            record_id = record["record_id"]

            try:
                process_record(session, token, record)
            except Exception as error:
                error_message = str(error)
                print(f"处理失败：{error_message}")

                try:
                    mark_record_failed(session, token, record_id, error_message)
                    print("已将该记录标记为失败。")
                except Exception as mark_error:
                    print(f"标记失败状态也失败：{mark_error}")

        print("本次任务执行结束。")




if __name__ == "__main__":
    main()