# 開発メモ

## 構成

- ソースは`skills/english-article-picker/`。Pythonは収集・抽出・正規化・数値・保存・表示、モデルは本文分析・推薦理由・HN議論の解釈を担当する。
- 操作と保存形式は[workflow.md](../skills/english-article-picker/references/workflow.md)、関心更新は[interests.md](../skills/english-article-picker/references/interests.md)を参照する。
- `.picker/state.sqlite3`が紹介履歴の唯一の参照元。`research/digests/`はエクスポートであり、履歴として取り込まない。

## 実行と検証

- `.venv/bin/python -m unittest discover -s tests -v`を実行する。ネットワークはモックし、状態は一時領域に隔離する。
- 実アクセス確認は一時ワークスペースの`fetch`で行う。開発検証で`finalize --commit`を実行しない。
- 本文のハッシュ・抜粋と議論のコメントIDを照合するが、解釈の正しさや全文取得を保証するものではない。

## 配布と個人設定

- 配布には`.env`、`.picker/`、推薦結果、ブックマークの実データ、個人のチャットID・運用設定を含めない。
- 個人ID・絶対パス・個人用エンドポイントを既定値にしない。好みと対象製品は利用者が設定する。
- スキルを別フォルダへインストールしている場合、ソース変更だけでは反映されない。利用者が指定したインストール先へ必要なファイルを同期する。
- 定期タスクの作成・変更は、その依頼がある場合だけ行う。
