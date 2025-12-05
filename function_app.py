import os
import json
import re
import time
import logging
import requests
from typing import Tuple, List, Optional

import azure.functions as func
from azure.functions import TimerRequest  # ← Timer 用
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

# ---- v2モデルのエントリ ----
app = func.FunctionApp()

# ---- App Settings ----
BACKUP_STORAGE_ACCOUNT_URL = os.environ.get("BACKUP_STORAGE_ACCOUNT_URL")  # 例: https://<account>.blob.core.windows.net
BACKUP_CONTAINER_NAME      = os.environ.get("BACKUP_CONTAINER_NAME", "logicapps-backup")
ARM_API_VERSION            = os.environ.get("ARM_API_VERSION", "2025-03-01")  # Web/sites publishingcredentials API
USE_PRIVATELINK_FOR_SCM    = os.environ.get("USE_PRIVATELINK_FOR_SCM", "true").strip().lower() == "true"

# ★ Timer 用：対象サイト情報を環境変数から取得 ★
SUBSCRIPTION_ID      = os.environ.get("SUBSCRIPTION_ID")
RESOURCE_GROUP_NAME  = os.environ.get("RESOURCE_GROUP_NAME")
LOGICAPP_SITE_NAME   = os.environ.get("LOGICAPP_SITE_NAME")

# 任意の堅牢化設定
RETRY_MAX         = int(os.environ.get("RETRY_MAX", "3"))
RETRY_BACKOFF_SEC = float(os.environ.get("RETRY_BACKOFF_SEC", "1.5"))

# ---- MI Credential ----
cred = ManagedIdentityCredential()

# ---- HTTPリトライ（指数バックオフ） ----
def _retry_request(method, url, **kwargs):
    for attempt in range(1, RETRY_MAX + 1):
        try:
            resp = requests.request(method, url, timeout=30, **kwargs)
            return resp
        except Exception:
            if attempt == RETRY_MAX:
                raise
            time.sleep(RETRY_BACKOFF_SEC * attempt)

# ---- ARM: Publishing Credentials（Basic認証用 ユーザー／パス）取得 ----
def get_publishing_credentials(subscription_id: str, resource_group: str, site_name: str) -> Tuple[str, str]:
    token = cred.get_token("https://management.azure.com/.default").token
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}/providers/Microsoft.Web/sites/{site_name}"
        f"/config/publishingcredentials/list?api-version={ARM_API_VERSION}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    resp = _retry_request("POST", url, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    props = payload.get("properties", payload)
    user = props.get("publishingUserName") or payload.get("publishingUserName")
    pwd  = props.get("publishingPassword") or payload.get("publishingPassword")
    if not (user and pwd):
        raise RuntimeError("Publishing credentials not found")
    return user, pwd

# ---- Kudu SCMのホスト名（privatelink切替） ----
def scm_host(site_name: str, use_privatelink: bool) -> str:
    if use_privatelink:
        return f"{site_name}.scm.privatelink.azurewebsites.net"
    else:
        return f"{site_name}.scm.azurewebsites.net"

# ---- wwwroot直下を列挙し、workflow.json があるフォルダを抽出（新規追加も自動検出） ----
def list_workflows(site_name: str, pub_user: str, pub_pass: str, use_privatelink: bool = True) -> List[str]:
    host = scm_host(site_name, use_privatelink)
    root = f"https://{host}/api/vfs/site/wwwroot/"
    resp = _retry_request("GET", root, auth=(pub_user, pub_pass))
    resp.raise_for_status()
    entries = resp.json()

    found: List[str] = []
    for e in entries:
        if e.get("mime") == "inode/directory":
            wf_name = e.get("name")
            probe = f"https://{host}/api/vfs/site/wwwroot/{wf_name}/workflow.json"
            r = _retry_request("GET", probe, auth=(pub_user, pub_pass))
            if r.status_code == 200:
                found.append(wf_name)
            elif r.status_code not in (404, 403):
                r.raise_for_status()

    # 互換のため Workflows/（大文字小文字差）も一応見る（環境差対策）
    for workflows_dir in ("Workflows", "workflows"):
        base = f"https://{host}/api/vfs/site/wwwroot/{workflows_dir}/"
        r = _retry_request("GET", base, auth=(pub_user, pub_pass))
        if r.status_code == 200:
            for e in r.json():
                if e.get("mime") == "inode/directory":
                    wf_name = e.get("name")
                    probe = f"https://{host}/api/vfs/site/wwwroot/{workflows_dir}/{wf_name}/workflow.json"
                    rr = _retry_request("GET", probe, auth=(pub_user, pub_pass))
                    if rr.status_code == 200 and wf_name not in found:
                        found.append(wf_name)
        elif r.status_code not in (404, 403):
            r.raise_for_status()

    return sorted(found)

# ---- 指定workflowの workflow.json を取得（直下優先 → Workflows/ 互換） ----
def get_workflow_json(site_name: str, wf_name: str, pub_user: str, pub_pass: str, use_privatelink: bool = True) -> Optional[str]:
    host = scm_host(site_name, use_privatelink)
    candidates = [
        f"https://{host}/api/vfs/site/wwwroot/{wf_name}/workflow.json",
        f"https://{host}/api/vfs/site/wwwroot/Workflows/{wf_name}/workflow.json",
        f"https://{host}/api/vfs/site/wwwroot/workflows/{wf_name}/workflow.json",
    ]
    for url in candidates:
        resp = _retry_request("GET", url, auth=(pub_user, pub_pass))
        if resp.status_code == 200:
            return resp.text
        if resp.status_code not in (404, 403):
            resp.raise_for_status()
    return None

# ---- 機密値の簡易マスク（api-key をテキスト置換で一括マスク） ----
REDACTED = "***REDACTED***"

def redact_workflow_json(raw_text: str) -> str:
    """
    workflow.json 内の "api-key": "<値>" をすべて ***REDACTED*** に置換。
    - ネストや場所は問わない
    - 値が空文字でも対応（[^"]*）
    - 前後のスペースや改行の揺れも許容
    """
    pattern = r'("api-key"\s*:\s*")[^"]*(")'
    return re.sub(pattern, r'\1' + REDACTED + r'\2', raw_text)

# ---- Blobへアップロード（最新のみ上書き保存 / MI認証） ----
def upload_latest_to_blob(account_url: str, container: str, logicapp_name: str, wf_name: str, content: str) -> str:
    bsc = BlobServiceClient(account_url=account_url, credential=cred)
    cc = bsc.get_container_client(container)
    try:
        cc.create_container()
    except Exception:
        pass  # 既存ならOK
    blob_path = f"{logicapp_name}/{wf_name}/latest.json"  # ← 固定名
    cc.get_blob_client(blob_path).upload_blob(content.encode("utf-8"), overwrite=True)
    return blob_path

# ---- Timer トリガー（3日おき / 8:00 JST = 前日23:00 UTC） ----
@app.function_name(name="TimerBackup")
@app.schedule(schedule="0 0 23 */3 * *", arg_name="timer", run_on_startup=True, use_monitor=True)
def TimerBackup(timer: TimerRequest):
    logging.info("TimerBackup triggered")

    # 0) 必須設定チェック
    if not (SUBSCRIPTION_ID and RESOURCE_GROUP_NAME and LOGICAPP_SITE_NAME):
        logging.error("Missing env: SUBSCRIPTION_ID / RESOURCE_GROUP_NAME / LOGICAPP_SITE_NAME")
        return None

    # 1) MIで Publishing Credentials を ARM から取得
    pub_user, pub_pass = get_publishing_credentials(SUBSCRIPTION_ID, RESOURCE_GROUP_NAME, LOGICAPP_SITE_NAME)

    # 2) Kudu VFSで wwwroot直下を列挙 → workflow.json のあるフォルダを抽出
    workflows = list_workflows(LOGICAPP_SITE_NAME, pub_user, pub_pass, use_privatelink=USE_PRIVATELINK_FOR_SCM)

    # 3) 各 workflow.json を取得＆機密マスク＆Blobに「最新のみ」上書き保存
    uploaded: List[str] = []
    for wf in workflows:
        content = get_workflow_json(LOGICAPP_SITE_NAME, wf, pub_user, pub_pass, use_privatelink=USE_PRIVATELINK_FOR_SCM)
        if content:
            safe = redact_workflow_json(content)

            # 監査用：未マスクの可能性があれば警告
            if '"api-key"' in safe and REDACTED not in safe:
                logging.warning("[redact-check] api-key が未マスクの可能性あり")

            path = upload_latest_to_blob(BACKUP_STORAGE_ACCOUNT_URL, BACKUP_CONTAINER_NAME, LOGICAPP_SITE_NAME, wf, safe)
            uploaded.append(path)

    # 4) 結果ログ
    result = {"site": LOGICAPP_SITE_NAME, "workflows_found": workflows, "uploaded": uploaded}
    logging.info(json.dumps(result, ensure_ascii=False))

    return None