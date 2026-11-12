# 開発ガイド

## Package Manager
- Python 3.11以上。本文抽出にはTrafilaturaを使う。
- 初期設定：`python3 -m venv .venv`、`.venv/bin/python -m pip install -r skills/english-article-picker/requirements.txt`
- 実行・検証には `.venv/bin/python` を使う。検証：`.venv/bin/python -m unittest discover -s tests -v`

## Key Conventions
- 収集・履歴・数値・表示はPython、本文とHNコメントの解釈・推薦理由はモデルが担当。
- 情報源の追加・変更はローカルPCから通信するPython収集処理で対応し、外部Web検索・閲覧ツールへ依存させない。RSS内の原文リンク・取得範囲・失敗を保持する（[収集契約](skills/english-article-picker/references/workflow.md#ローカルからの情報収集)）。
- はてなIDは`HATENA_USER`環境変数または作業フォルダの`.env`から読む。配布ファイルに個人ID・絶対保存先を固定せず、アカウントごとの履歴を混ぜない（[初回設定](skills/english-article-picker/references/workflow.md#初回設定と作業フォルダ)）。
- 実装・保存先・反映方法は [開発メモ](research/development.md) を必要時に参照。
- 推薦実行や `finalize --commit` は開発検証と分ける。試験で配信履歴を更新しない。
- 定期実行の設定は、その変更を依頼された場合だけ更新する。

## Local Skills / 必要時の参照先
- 推薦実行：[english-article-picker](skills/english-article-picker/SKILL.md)
- CLI・データ契約・HN議論：[workflow.md](skills/english-article-picker/references/workflow.md)
- 好み・収集先：[preferences.md](skills/english-article-picker/references/preferences.md)（読み味の分析は [reading-preferences.md](research/reading-preferences.md)）
- 速報条件：[security-requirements.md](research/security-requirements.md)
- 注目度：[attention-signals.md](research/attention-signals.md)、判定値は [attention-policy.json](skills/english-article-picker/references/attention-policy.json)
- 履歴や大きなJSON・本文キャッシュは、対象の回・記事が必要なときだけ読む。

## Commit Attribution
- AIがコミットする場合は、自身のモデル名・帰属情報の `Co-Authored-By` トレーラーを付ける。
