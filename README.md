# Hacker News Picker

はてなブックマークの保存記事を手がかりに、英語記事を選び、日本語で紹介するCodex用スキル。Hacker News、Lobsters、MetaFilter、Kottkeなどを入口に本文と議論を確認します。重大なセキュリティ情報は通常の推薦と別枠で扱います。

Pythonが収集・本文抽出・数値・履歴を処理し、Codexが本文を読んで関心の分析と推薦理由を作ります。Python CLIだけで推薦判断まで自動完結するものではありません。追加のモデルAPIキーやCloudflare Workerは不要です。

## セットアップ

Python 3.11以上とCodexのスキル実行環境が必要です。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r skills/english-article-picker/requirements.txt
.venv/bin/python skills/english-article-picker/scripts/picker.py setup
.venv/bin/python skills/english-article-picker/scripts/picker.py config
```

初回の`setup`で自分のはてなIDを入力します。作業フォルダの`.env`に`HATENA_USER`として保存され、2回目以降は入力不要です。`.env.example`を`.env`にコピーして編集するか、環境変数でも指定できます。空でない環境変数が`.env`より優先されます。

`skills/english-article-picker/`を利用中のCodexのスキル保存先に配置し、このプロジェクトを作業フォルダとして`$english-article-picker`を実行してください。[SKILL.md](skills/english-article-picker/SKILL.md)が実行手順です。収集だけを行う場合は次を使います。

```sh
.venv/bin/python skills/english-article-picker/scripts/picker.py collect
```

別の作業フォルダは`picker.py --workspace /path/to/workspace setup`のように指定します。アカウントごとに作業フォルダを分けてください。既存履歴と異なるアカウントでの実行は拒否されます。

## 設定と保存データ

- 好みと対象製品：`skills/english-article-picker/references/profile.json`の`explicit_preferences`と`products`。配布時は空です。未設定でも保存記事から関心仮説を更新できます。
- `.env`：利用者のはてなID。
- `.picker/`：取得本文、ブックマーク、関心仮説、通信記録、SQLite履歴。
- `research/digests/`：紹介結果のエクスポート。重複除外の参照元はSQLiteです。

利用者の設定・履歴はGit管理対象外です。取得本文や推薦結果にも個人の関心が含まれるため、公開前に内容を確認してください。この配布には個人のブックマーク、過去の推薦、チャットID、定期実行設定を含めていません。定期タスクは自動作成しません。

## 情報源と限界

情報源への通信はローカルPythonで行います。MetaFilterは公開RSSを入口とし、トップページを必須にしません。アクセス制限・robots.txt・有料壁を回避せず、取得失敗は未確認として扱います。

本文抽出にはTrafilaturaを使用します。抽出成功は全文保証ではありません。人気指標はサイトごとに示し、全世界の記事や脆弱性を網羅した監視とは扱いません。

詳細は[CLIとデータ契約](skills/english-article-picker/references/workflow.md)、[推薦方針](skills/english-article-picker/references/preferences.md)、[開発メモ](research/development.md)を参照してください。

## 検証

```sh
.venv/bin/python -m unittest discover -s tests -v
```

テストはネットワークをモックし、一時領域を使います。開発検証で利用者の配信履歴を更新しません。
