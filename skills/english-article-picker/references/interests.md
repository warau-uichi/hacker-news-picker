# ブックマーク差分と関心の継続更新

## 毎回の流れ

1. `collect`の`overview.json`で`interest_context`を読む。`delta`は新規・タグ/コメント/タイトル変更・前回の分析保留。URLを正規化して比較し、RSSと読み物ページの同一記事はまとめる。`new`は比較基準から新しく観測したURLで、保存時刻の厳密な区間判定ではない。
2. 差分記事は自動で`bundle.json`に`bookmark`枠として入る。そのIDを使って`body <bundle.json> ID...`で本文を取得。既存の関心仮説に先立って記事固有の問い・内容・展開を分析し、観察と好みの推測を分ける。必要な本文をすべて確認する。件数が多くても、読まずに分析済みにせず、未確認は保留する。
3. 下記のJSONを作り、`interests`でプレビューしてから同じ入力を`interests --commit`で保存。分析対象がゼロなら`{"bookmarks": [], "hypotheses": []}`。新しい関心がない場合も本文分析だけ保存できる。
4. 更新後の`bundle.json`の`profile.inferred_interests`と、本人の明示した好みから推薦する。最近の仮説は固定の分野から外れていても候補探しに使う。新しい仮説・変更があれば、根拠記事リンクとともに短く知らせる。

```sh
python3 "<skill>/scripts/picker.py" body /absolute/run/bundle.json BOOKMARK_ID
python3 "<skill>/scripts/picker.py" interests /absolute/run/bundle.json /absolute/interest-analysis-input.json
python3 "<skill>/scripts/picker.py" interests /absolute/run/bundle.json /absolute/interest-analysis-input.json --commit
```

ここでの`--commit`は関心分析の確定だけ。推薦配信や`finalize --commit`とは独立しており、紹介済みURLや速報の履歴は増やさない。推薦が見つからなくても関心分析は残す。開発試験では一時ワークスペースを使う。

## モデルが書く分析JSON

```json
{
  "bookmarks": [
    {
      "id": "差分の保存記事ID",
      "status": "reviewed",
      "body_reviewed": true,
      "scope": "partial",
      "observation": "本文で何を確認したか。部分確認ならその範囲も書く",
      "inference": "どのような関心の手がかりか。興味の仮説が立たない場合もそう記載する",
      "evidence": ["保存した本文に実在する12文字以上の抜粋"]
    },
    {
      "id": "本文未確認の差分記事ID",
      "status": "deferred",
      "reason": "本文取得不可、ページの一部しか読めず判断保留、今回まだ未読など"
    }
  ],
  "hypotheses": [
    {
      "id": "関心ごとの安定した短い識別名",
      "status": "active",
      "text": "最近は、観察から身近な現象の仕組みを調べる記事にも関心がありそう",
      "confidence": "tentative",
      "rationale": "今回の記事のどこから推測し、以前の仮説をどう追加・修正したか",
      "bookmark_ids": ["今回本文を読んだ差分記事ID"]
    }
  ]
}
```

上は形式の例。実際の`bookmarks`には今回の差分IDを過不足なく1回ずつ入れる。未確認記事を仮説の根拠には使わない。`scope`は実際に読んだ範囲を`full`/`partial`で明示し、本文の自動抽出が成功しただけで`full`にしない。

`confidence`は単発・弱い根拠なら`tentative`、独立した複数記事の本文が支持する場合に`supported`を使う。同じ記事のタグ変更や再試行を、独立した追加の支持と数えない。コードは少なくとも2つの異なる根拠URLを要求するが、記事間の意味上の独立性はモデルが判断する。

同じ関心は同じ`id`で更新し、無関係な関心に使い回さない。変更を書かなかった既存仮説は保持する。新しい本文から既存仮説の修正・撤回が必要なら、根拠と理由を添え、同じIDを更新する。撤回は`status: retired`。本人の訂正は`preferences.md`/`profile.json`へ本人由来として反映し、自動推測と混ぜない。

## 保存・再試行・範囲

- `.picker/state.sqlite3`の`interests`に、比較基準・分析済み/保留の状態・関心仮説・更新日時と根拠を保存。`collect`だけでは比較基準を進めない。分析入力・更新後の状態は各回の`interest-analysis.json`にも保存する。
- 旧実行履歴がある初回は、最新のはてなRSS取得成功回のブックマークを比較基準に移行する。そこに含まれる古い記事は「未分析の基準」として扱う。履歴がなければ、今取得できた記事を初期分析する。10年分の履歴を取得したとは扱わない。
- 保存済みURLは観測後にRSSから消えても保持し、再推薦の除外にも利用する。削除や関心の喪失を意味するとは判断しない。
- 本文未確認は`deferred`として残し、次回RSSから消えていても再分析対象へ戻す。タグ等が変われば新しい内容で再分析する。
- RSSか読み物ページの取得が失敗した回は、比較基準も仮説も更新しない。以前の関心を使って推薦は続けられるが、差分未確認を明示する。空のRSSは取得エラーとして扱う。
- RSS/タグページの取得範囲を超えて流れた保存は検出保証なし。長い実行間隔などでは、取得範囲が限定される旨を説明する。全履歴・削除差分・完全な期間差分とは呼ばない。
- 古い比較基準からの上書きは拒否。同じ実行・同じ分析の再確定は二重適用せず、異なる分析での再確定は拒否する。
- 推測を本人の明示発言に変換しない。最新の推測も実際の保存理由や恒久的な好みの確定ではなく、推薦に使う更新可能な仮説として扱う。
