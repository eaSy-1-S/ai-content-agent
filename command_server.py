import json
import os
import re
import threading
from run_feishu_dify import generate_articles_by_count
from pathlib import Path
from typing import Any, Dict, Optional
from sync_wechat_draft import sync_wechat_drafts_by_count
import requests
import time
from fastapi import FastAPI, Request
from sync_wechat_draft import (
    sync_wechat_drafts_by_count,
    sync_wechat_drafts_by_record_ids,
    get_content_status_summary,
)


# ============================================================
# 1. 基础配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_REPLY_URL_TEMPLATE = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"

REQUEST_TIMEOUT = 30

app = FastAPI()


# ============================================================
# 2. 读取 .env
# ============================================================

def load_env_file(env_path: Path) -> None:
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


def validate_config() -> None:
    missing = []

    if not FEISHU_APP_ID:
        missing.append("FEISHU_APP_ID")

    if not FEISHU_APP_SECRET:
        missing.append("FEISHU_APP_SECRET")

    if missing:
        raise RuntimeError("缺少配置：" + "、".join(missing))


# ============================================================
# 3. 飞书 API
# ============================================================

FEISHU_TOKEN_CACHE = {
    "token": "",
    "expires_at": 0,
}


def safe_request(method: str, url: str, timeout: int = 30, max_retries: int = 3, **kwargs):
    """
    command_server.py 专用的带重试请求。
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            with requests.Session() as session:
                session.trust_env = False

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
            print(f"飞书请求失败，正在重试 {attempt + 1}/{max_retries}：{repr(error)}")
            time.sleep(2 * (attempt + 1))

    raise last_error


def get_feishu_token() -> str:
    validate_config()

    now = time.time()

    if FEISHU_TOKEN_CACHE["token"] and FEISHU_TOKEN_CACHE["expires_at"] > now:
        return FEISHU_TOKEN_CACHE["token"]

    payload = {
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }

    response = safe_request(
        method="POST",
        url=FEISHU_TOKEN_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT
    )

    data = response.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "获取飞书 tenant_access_token 失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )

    token = data["tenant_access_token"]
    expire_seconds = data.get("expire", 7200)

    FEISHU_TOKEN_CACHE["token"] = token
    FEISHU_TOKEN_CACHE["expires_at"] = now + expire_seconds - 300

    return token


def reply_feishu_message(message_id: str, text: str) -> None:
    token = get_feishu_token()

    url = FEISHU_REPLY_URL_TEMPLATE.format(message_id=message_id)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    payload = {
        "msg_type": "text",
        "content": json.dumps(
            {
                "text": text
            },
            ensure_ascii=False
        )
    }

    response = safe_request(
        method="POST",
        url=url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT
    )

    data = response.json()

    if data.get("code") != 0:
        raise RuntimeError(
            "回复飞书消息失败："
            + json.dumps(data, ensure_ascii=False, indent=2)
        )


def safe_reply_feishu_message(message_id: str, text: str) -> None:
    """
    安全回复。
    如果飞书回复失败，只打印错误，不让后台线程再次崩溃。
    """
    try:
        reply_feishu_message(message_id, text)
    except Exception as error:
        print(f"飞书消息回复失败，但主任务不一定失败：{repr(error)}")


def run_generation_task(message_id: str, count: int) -> None:
    """
    后台执行完整任务：
    目标是尽量准备 count 篇公众号草稿。

    流程：
    1. 先尝试生成 count 篇未生成文章；
    2. 同步本次新生成成功的文章到公众号草稿箱；
    3. 如果数量不足，再同步飞书里已有的“已生成但未同步草稿”的文章；
    4. 最后回复执行结果。
    """
    try:
        generation_results = generate_articles_by_count(count)

        generation_lines = [
            f"文章生成阶段完成，共处理 {len(generation_results)} 条记录："
        ]

        success_record_ids = []

        for index, item in enumerate(generation_results, start=1):
            book_name = item.get("book_name") or "未命名书籍"
            status = item.get("status")
            message = item.get("message", "")

            if status == "success":
                record_id = item.get("record_id")
                if record_id:
                    success_record_ids.append(record_id)

                generation_lines.append(
                    f"{index}. 《{book_name}》：生成成功，已写回飞书。"
                )

            elif status == "empty":
                generation_lines.append(
                    f"{index}. {message}"
                )

            else:
                generation_lines.append(
                    f"{index}. 《{book_name}》：生成失败，原因：{message}"
                )

        draft_results = []

        # 1. 优先同步本次刚生成成功的记录，避免生成 A 却同步 B。
        if success_record_ids:
            draft_results.extend(
                sync_wechat_drafts_by_record_ids(success_record_ids)
            )

        # 2. 如果用户要 N 篇，但本次新生成并同步的不够，
        #    就继续从“已生成但未同步草稿”的旧文章里补足。
        current_success_or_skipped = sum(
            1
            for item in draft_results
            if item.get("status") in ["success", "skipped"]
        )

        remaining_count = count - current_success_or_skipped

        if remaining_count > 0:
            extra_draft_results = sync_wechat_drafts_by_count(remaining_count)
            draft_results.extend(extra_draft_results)

        draft_lines = [
            "",
            f"公众号草稿同步阶段完成，共处理 {len(draft_results)} 条记录："
        ]

        if not draft_results:
            draft_lines.append(
                "没有找到可以同步到公众号草稿箱的文章。请确认飞书表格中存在“是否已生成=是”且“是否已同步草稿=否”的记录。"
            )
        else:
            for index, item in enumerate(draft_results, start=1):
                title = item.get("title") or "未命名文章"
                status = item.get("status")
                message = item.get("message", "")

                if status == "success":
                    draft_lines.append(
                        f"{index}. 《{title}》：已同步到微信公众号草稿箱。"
                    )
                elif status == "skipped":
                    draft_lines.append(
                        f"{index}. 《{title}》：已同步过，跳过。"
                    )
                elif status == "empty":
                    draft_lines.append(
                        f"{index}. {message}"
                    )
                else:
                    draft_lines.append(
                        f"{index}. 《{title}》：同步失败，原因：{message}"
                    )

        final_message = "\n".join(generation_lines + draft_lines)

        safe_reply_feishu_message(message_id, final_message)

    except Exception as error:
        safe_reply_feishu_message(
            message_id,
            f"完整任务执行失败：{str(error)[:500]}"
        )


def run_sync_task(message_id: str, count: int) -> None:
    """
    只执行公众号草稿同步：
    从飞书表格里找“已生成但未同步草稿”的文章，同步到公众号草稿箱。
    """
    try:
        results = sync_wechat_drafts_by_count(count)

        lines = [
            f"公众号草稿同步阶段完成，共处理 {len(results)} 条记录："
        ]

        for index, item in enumerate(results, start=1):
            title = item.get("title") or "未命名文章"
            status = item.get("status")
            message = item.get("message", "")

            if status == "success":
                lines.append(f"{index}. 《{title}》：已同步到微信公众号草稿箱。")
            elif status == "empty":
                lines.append(f"{index}. {message}")
            else:
                lines.append(f"{index}. 《{title}》：同步失败，原因：{message}")

        safe_reply_feishu_message(message_id, "\n".join(lines))

    except Exception as error:
        safe_reply_feishu_message(
            message_id,
            f"草稿同步任务执行失败：{str(error)[:500]}"
        )


def run_status_task(message_id: str) -> None:
    """
    查看飞书选题库当前状态。
    """
    try:
        summary = get_content_status_summary()

        reply_text = (
            "当前选题库状态：\n"
            f"总记录：{summary['total']} 条\n"
            f"未生成 / 可生成：{summary['not_generated']} 条\n"
            f"已生成但未同步草稿：{summary['generated_not_synced']} 条\n"
            f"已同步到公众号草稿箱：{summary['synced']} 条\n"
            f"生成失败：{summary['generation_failed']} 条\n"
            f"草稿同步失败：{summary['draft_failed']} 条\n"
            f"已生成但缺少标题或正文：{summary['generated_but_missing_content']} 条\n\n"
            "可用命令：\n"
            "生成一篇 / 生成两篇 / 生成三篇\n"
            "同步一篇 / 同步两篇 / 同步三篇\n"
            "状态\n"
            "帮助"
        )

        safe_reply_feishu_message(message_id, reply_text)

    except Exception as error:
        print(f"状态查询失败：{error}")
        try:
            safe_reply_feishu_message(
                message_id,
                f"状态查询失败：{str(error)[:500]}"
            )
        except Exception as reply_error:
            print(f"回复状态查询失败消息也失败了：{reply_error}")

# ============================================================
# 4. 解析飞书事件
# ============================================================
def parse_message_text(message: Dict[str, Any]) -> str:
    """
    飞书 text 消息的 content 通常是 JSON 字符串，例如：
    {"text":"测试"}
    """
    content = message.get("content", "")

    if not content:
        return ""

    try:
        content_obj = json.loads(content)
    except json.JSONDecodeError:
        return str(content)

    return str(content_obj.get("text", "")).strip()

def parse_command(text: str) -> dict:
    """
    解析飞书机器人命令。

    当前只支持稳定命令：
    - 生成一篇 / 生成两篇 / 生成三篇
    - 同步一篇 / 同步两篇 / 同步三篇
    - 状态
    - 帮助
    """
    text = text.strip()

    chinese_number_map = {
        "一": 1,
        "二": 2,
        "两": 2,
        "俩": 2,
        "三": 3,
        "四": 4,
        "五": 5,
    }

    def extract_count(default: int = 1) -> int:
        digit_match = re.search(r"(\d+)\s*篇", text)
        if digit_match:
            return int(digit_match.group(1))

        for chinese_num, value in chinese_number_map.items():
            if f"{chinese_num}篇" in text or f"{chinese_num} 篇" in text:
                return value

        return default

    if text in ["帮助", "help", "使用说明", "命令"]:
        return {
            "action": "help",
            "count": 0,
            "raw_text": text,
        }

    if text in ["状态", "查看状态", "进度", "查看进度", "当前状态"]:
        return {
            "action": "status",
            "count": 0,
            "raw_text": text,
        }

    if text.startswith("生成"):
        return {
            "action": "prepare_draft",
            "count": extract_count(default=1),
            "raw_text": text,
        }

    if text.startswith("同步"):
        return {
            "action": "sync_draft",
            "count": extract_count(default=1),
            "raw_text": text,
        }

    return {
        "action": "unknown",
        "count": 0,
        "raw_text": text,
    }


def parse_generate_command(text: str) -> Dict[str, Any]:
    """
    解析用户发来的生成命令。

    支持示例：
    - 生成一篇
    - 生成1篇
    - 生成三篇不同书籍的公众号草稿
    - 帮我生成两篇公众号推文
    """
    text = text.strip()

    chinese_number_map = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
    }

    # 默认不是生成命令
    result = {
        "is_generate_command": False,
        "count": 0,
        "need_draft": "草稿" in text,
        "raw_text": text,
    }

    if "生成" not in text:
        return result

    result["is_generate_command"] = True

    # 优先识别阿拉伯数字：生成3篇
    digit_match = re.search(r"生成\s*(\d+)\s*篇", text)
    if digit_match:
        result["count"] = int(digit_match.group(1))
        return result

    # 再识别中文数字：生成三篇
    for chinese_num, value in chinese_number_map.items():
        if f"生成{chinese_num}篇" in text or f"生成 {chinese_num} 篇" in text:
            result["count"] = value
            return result

    # 如果说了“生成公众号推文”但没说数量，默认 1 篇
    result["count"] = 1
    return result

def extract_message_event(body: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    从飞书事件体中提取 message_id 和文本内容。
    """
    event = body.get("event", {})
    message = event.get("message", {})

    message_id = message.get("message_id", "")
    message_type = message.get("message_type", "")
    chat_type = message.get("chat_type", "")
    text = parse_message_text(message)

    if not message_id:
        return None

    return {
        "message_id": message_id,
        "message_type": message_type,
        "chat_type": chat_type,
        "text": text,
    }


# ============================================================
# 5. FastAPI 路由
# ============================================================

@app.post("/feishu/events")
async def feishu_events(request: Request) -> Dict[str, Any]:
    body = await request.json()

    print("收到飞书事件：")
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 飞书事件订阅 URL 校验
    if "challenge" in body:
        return {
            "challenge": body["challenge"]
        }

    header = body.get("header", {})
    event_type = header.get("event_type", "")

    # 只处理接收消息事件
    if event_type != "im.message.receive_v1":
        return {
            "code": 0,
            "msg": "ignored"
        }

    message_event = extract_message_event(body)

    if not message_event:
        return {
            "code": 0,
            "msg": "no message"
        }

    message_id = message_event["message_id"]
    message_type = message_event["message_type"]
    user_text = message_event["text"]

    try:
        if message_type != "text":
            reply_feishu_message(
                message_id,
                "我目前只能处理文本消息。你可以发送：生成一篇公众号推文"
            )
        else:
            command = parse_command(user_text)
            action = command["action"]
            count = command["count"]

            if action in ["prepare_draft", "sync_draft"] and count > 5:
                reply_feishu_message(
                    message_id,
                    "一次最多处理 5 篇，请减少数量后再试。"
                )
                return {"code": 0}

            if action == "prepare_draft":
                reply_feishu_message(
                    message_id,
                    f"收到，准备生成并同步 {count} 篇公众号草稿。\n\n"
                    f"任务已开始执行，生成和同步过程可能需要几分钟。完成后我会再次回复你。"
                )

                thread = threading.Thread(
                    target=run_generation_task,
                    args=(message_id, count),
                    daemon=True
                )
                thread.start()

            elif action == "sync_draft":
                reply_feishu_message(
                    message_id,
                    f"收到，准备同步 {count} 篇已生成文章到微信公众号草稿箱。\n\n"
                    f"任务已开始执行，完成后我会再次回复你。"
                )

                thread = threading.Thread(
                    target=run_sync_task,
                    args=(message_id, count),
                    daemon=True
                )
                thread.start()

            elif action == "status":
                thread = threading.Thread(
                    target=run_status_task,
                    args=(message_id,),
                    daemon=True
                )
                thread.start()

            elif action == "help":
                reply_feishu_message(
                    message_id,
                    "可用命令：\n"
                    "1. 生成一篇 / 生成两篇 / 生成三篇：生成文章并同步到公众号草稿箱\n"
                    "2. 同步一篇 / 同步两篇 / 同步三篇：只同步已生成文章到公众号草稿箱\n"
                    "3. 状态：查看飞书选题库处理状态\n"
                    "4. 帮助：查看命令说明"
                )

            else:
                reply_feishu_message(
                    message_id,
                    "我还不能理解这条指令。\n\n"
                    "你可以发送：\n"
                    "生成一篇\n"
                    "同步一篇\n"
                    "状态\n"
                    "帮助"
                )

    except Exception as error:
        print(f"回复消息失败：{error}")

    return {
        "code": 0,
        "msg": "ok"
    }


@app.get("/")
async def health_check() -> Dict[str, str]:
    return {
        "status": "ok"
    }