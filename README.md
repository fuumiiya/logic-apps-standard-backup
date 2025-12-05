# Logic Apps Backup - Azure Functions

Standard Logic AppsのワークフローのJSONコードを自動バックアップするAzure Functionsアプリケーションです。

## 背景

普段、Logic AppsでAIワークフローを組んでいます。非常に便利なサービスですが、バックアップが取りづらかったため、このツールを作成しました。

Logic Appsは構造上、誤操作で簡単に削除できてしまう恐れがあるため、定期的なバックアップが重要です。

## 対象プラン

**Standard SKUのみ対応**です。

- StandardプランはApp Serviceにホスティングされるプランで、VNet統合などの機能が利用できます
- Consumptionプランでは、バックアップコードの取得先が異なるため、このツールは使用できません

## 動作概要

指定したBlob Storageコンテナに、Logic AppsのワークフローJSONコードを自動バックアップします。

### 技術的な仕組み

Logic Apps StandardはApp Serviceにホスティングされるため、ワークフローの定義ファイルはApp ServiceのKudu（SCM）に保存されています。

このツールは、Kudu VFS API経由で以下の階層からワークフローJSONを取得します：

```
site/wwwroot/
├── {ワークフロー名}/
│   └── workflow.json
└── Workflows/  (または workflows/)
    └── {ワークフロー名}/
        └── workflow.json
```

取得手順：
1. ARM APIでPublishing Credentials（Basic認証用のユーザー名/パスワード）を取得
2. Kudu VFS API (`https://{site-name}.scm.azurewebsites.net/api/vfs/site/wwwroot/`) でワークフロー一覧を取得
3. 各ワークフローの`workflow.json`を取得
4. 機密情報（`api-key`など）をマスク
5. Blob Storageに保存

## 機能

- Timerトリガーによる自動バックアップ（3日ごと、8:00 JST）
- Managed Identity認証を使用したセキュアなアクセス
- ワークフローJSON内の`api-key`を自動的にマスク
- 最新バックアップのみをBlob Storageに保存

### 設計の経緯

元々は、イベントグリッドトリガーでワークフローの更新のたびにバックアップを取得する仕様にしたかったため、当初イベントグリッドトリガーを採用して実装しました。

しかし、StandardプランはApp Serviceにホスティングされるため、ワークフロー変更イベントで駆動させることができませんでした（App Serviceの更新系のイベント駆動になってしまうため）。

そのため、Timerトリガーによる定期バックアップ方式を採用しています。

## セットアップ

### 1. 依存関係のインストール

```bash
pip install -r requirements.txt
```

### 2. ローカル設定ファイルの作成

`local.settings.json.example`をコピーして`local.settings.json`を作成し、実際の値を設定してください：

```bash
cp local.settings.json.example local.settings.json
```

### 3. `local.settings.json`の設定値

以下の環境変数を設定してください：

- **FUNCTIONS_WORKER_RUNTIME**: `"python"` (固定値)
- **AzureWebJobsStorage**: ローカル開発時は `"UseDevelopmentStorage=true"`、本番環境ではストレージアカウントの接続文字列を設定
- **AzureFunctionsJobHost__python__fileName**: `"function_app.py"` (固定値)
- **BACKUP_STORAGE_ACCOUNT_URL**: バックアップ先のストレージアカウントURL（例: `https://mystorageaccount.blob.core.windows.net`）
- **BACKUP_CONTAINER_NAME**: バックアップ先のコンテナ名（デフォルト: `logicapps-backup`）
- **ARM_API_VERSION**: Azure Resource Manager APIバージョン（デフォルト: `2025-03-01`）
- **USE_PRIVATELINK_FOR_SCM**: Private Linkを使用する場合は `"true"`、しない場合は `"false"`
- **RETRY_MAX**: リトライ最大回数（デフォルト: `"3"`）
- **RETRY_BACKOFF_SEC**: リトライ時のバックオフ秒数（デフォルト: `"1.5"`）
- **SUBSCRIPTION_ID**: AzureサブスクリプションID
- **RESOURCE_GROUP_NAME**: Logic Appsが存在するリソースグループ名
- **LOGICAPP_SITE_NAME**: Logic AppsのApp Service名

## デプロイ

Azure Functionsへのデプロイは、Azure Functions Core ToolsまたはAzure Portalを使用してください。

## セキュリティ

⚠️ **重要**: `local.settings.json`には機密情報が含まれるため、Gitにコミットしないでください。このファイルは`.gitignore`に含まれています。

