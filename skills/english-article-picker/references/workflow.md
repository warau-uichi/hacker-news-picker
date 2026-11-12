# 操作とデータ契約

## 初回設定と作業フォルダ

作業フォルダには`.env`（ユーザー設定）と`.picker/`（履歴・関心分析・取得記録）を保存する。`collect`／`config`／`setup`の既定はカレントディレクトリ。別の保存先は`--workspace /absolute/path`をサブコマンド前に指定する。`body`等に`.picker/runs/<run>/bundle.json`を渡す場合は、そのパスから作業フォルダを推定する。明示した`--workspace`が最優先。

```sh
python3 "<skill>/scripts/picker.py" config
python3 "<skill>/scripts/picker.py" setup --hatena-user your_hatena_id
python3 "<skill>/scripts/picker.py" collect
```

`config`は通信せず設定の有無と利用するID・設定元をJSONで返す。未設定の初回のみ、スキルはユーザーからIDを聞いて`setup`で保存する。端末からの`setup`はID未指定・未設定の場合に入力を求め、非対話実行では`--hatena-user`を指定するよう案内する。設定済みなら再質問しない。`collect`も未設定なら通信・履歴作成前に停止する。

設定キーは`HATENA_USER`。空でない環境変数を優先し、なければ作業フォルダの`.env`を読む。`.env.example`をコピーして編集してもよい。IDには英数字・ハイフン・アンダースコアを使い、URLは入力しない。`.env`はシェルとして実行せず、このキーだけを読み取る。`setup`は他の設定を保持し、`.env`を所有者のみ読み書き可能な権限で保存する。環境変数と異なるIDを保存しても、実行時には環境変数が優先される。

`.env`と`.picker/`はGit管理対象外。共有するのは空欄の`.env.example`と共通の`profile.json`。`hatena_user`は実行時に設定から注入し、配布ファイルに個人のIDを埋め込まない。推薦方針・対象製品等の共通設定は`references/profile.json`で調整できる。

1つの作業フォルダは1つのはてなアカウントに対応する。RSS取得成功後に`.picker/account.json`へ記録し、別アカウントで既存の関心・配信履歴を使うことは拒否する。旧版の履歴は過去の取得成功回からアカウントを確認する。別アカウントには別の`--workspace`を使い、履歴を削除して切り替えない。

`<skill>` は読み込んだSKILL.mdのフォルダーに置き換える。以下の `python3` は、requirements.txtをインストールしたワークスペースの `.venv/bin/python` を使う。

```sh
python3 "<skill>/scripts/picker.py" collect
python3 "<skill>/scripts/picker.py" body /absolute/run/bundle.json ARTICLE_ID OTHER_ID
```

既定：HN上位30＋新着30、RSS先頭20件、直近7日更新のGitHub critical（100件×最大10ページ）、最近追加のKEV。人気の足切りなし。上限到達は不完全としてsource_statusへ記録。必要なら `--days 14 --hn-limit 60` を調整。ネットワークは通常の権限手続きに従う。

## ローカルからの情報収集

情報源への通信は、このPCで起動したPythonのHTTPクライアントに統一する。モデルは保存結果を読み、候補の選定や本文の解釈を行う。外部のWeb検索／閲覧ツールや外部取得プロキシへ切り替えない。OS側のネットワーク・プロキシ設定は引き継ぐため、特定の送信元IPやブラウザと同じCookie・接続条件を保証するものではない。

情報源を追加・変更するときも、`profile.json`の`feeds`／`discovery_pages`やPythonの取得・解析処理へ実装する。既存のHTTPクライアントと取得ログを使い、一時領域で実アクセス・解析結果を確認する。RSS内の原文リンクと取得範囲を保持し、失敗・未確認を掲載なしと扱わない。モデルの外部Webツール利用で不足を埋める設計にしない。

`collect`は`profile.json`のRSS（MetaFilter・Kottkeを含む）と`discovery_pages`の一覧ページ／公式APIを取得する。ページの本文・リンク一覧・失敗理由は各回の`page-fetches.json`、元レスポンスと抽出テキストは`pages/`に保存する。RSSは紹介投稿を候補に含むので、推薦する原文URLは紹介投稿のリンクから確認する。紹介投稿の時刻を原文の公開日へ流用しない。

MetaFilterは公開RSSとそのエントリ内のリンクを利用し、トップページは定常収集しない。個別投稿が読めなくてもRSSで確認できたリンクを利用し、コメント等の未確認範囲を明示する。

設定済みRSSの取得結果は`feed-fetches.json`、元XMLと取り込んだエントリは`feeds/`へ保存する。各結果に要求URL・転送先URL・取得日時・元XMLのハッシュ・パス・フィード内件数・取り込み件数・上限・上限による限定の有無を残す。`source_status`はRSSとページで別記録にする。RSS失敗と0件、RSS成功とサイト全体の確認完了を混同しない。

RSS/Atomのdescription・summary・content・content:encoded内のリンクはURLと表示文言を保持する。相対URLはxml:base等の基準URLで解決し、候補の`feed_entries`に取得元RSS・紹介投稿URL・取得時刻・元XMLのパスとともに保存する。同じ候補を他のソースが更新してもこの記録は残す。`overview.json`の`feed_fetches_index`から取得結果を確認し、対象候補の`feed_entries[].links`または`entries_path`から原文をたどる。紹介投稿が403でも、RSS内に原文リンクがあればその原文を`import`→`body`で確認できる。複数リンクの先頭を自動で原文と決めず、別記事・参考資料・過去投稿を区別する。

追加調査も同じ経路で行う：

```sh
python3 "<skill>/scripts/picker.py" fetch /absolute/run/bundle.json https://aws.amazon.com/security/security-bulletins/
```

`fetch`は取得先ごとの結果を追記し、`page-fetches.json`の場所を返す。既存の候補や配信履歴を変えない。HTMLはTrafilaturaの本文と通常のリンク、JSON/XML/テキストはソースを保存する。一覧ページで本文抽出できなくてもリンクと元HTMLを調べられるが、それを記事本文の確認済み根拠にはしない。AWS・Python・NGINX・Martinの一覧から個別告知を取得し、CA事故も公表資料のURLを同じコマンドでたどる。無制限にリンクを巡回しない。

CLIの`collect`／`body`／`discussion`／`fetch`が送ったGETは、各回の`requests.jsonl`に時刻・URL・User-Agent・HTTPステータス・リダイレクト・失敗理由を記録する。`robots.txt`と対象ページのリクエストは別行。`fetch`のHTTP失敗には`failed_url`も残す。DNSや接続の失敗にはHTTPステータスがない。ローカル取得でも403になり得る。取得禁止・認証・有料壁は回避せず、公開RSS等の別資料は別URLの取得結果として扱う。

開発中の実アクセス確認は、一時ディレクトリの`bundle.json`（`{}`でよい）に対して`fetch`を使う。実際の推薦実行や`finalize --commit`で動作検証しない。

ワークスペースの `.picker/state.sqlite3` を紹介済み・速報履歴の唯一の参照元とし、`.picker/runs/<run>/bundle.json` に候補・ブックマーク・数値・時刻・失敗を保存する。本文は別ファイルとハッシュ。`research/digests/` は結果の保存用で、履歴として読み込まない。SQLiteを失うと紹介済み判定も失われるため、履歴を保持する場合はSQLiteをバックアップする。
ブックマークの比較基準・分析・関心仮説もSQLiteに保存する。推薦の前に[interests.md](interests.md)の差分本文分析を`interests --commit`で確定し、更新後の`bundle.json`を読む。取得成功回の`finalize`は関心分析が未確定なら拒否する。この確定は推薦の配信履歴とは独立。
候補が多いときはoverview.jsonを先に読む。速報は同じCVEをまとめても初回には多くなる場合がある。重要なものを先に説明し、全必須項目を含む生成済みdigestへのリンクも示す。通常の5件の枠で必須速報を省かない。初回収集の過去7日分を「今発生した新規事件」と扱わない。

HTML本文はTrafilaturaの標準モードで抽出する。記事内コメントを除く設定で、表・整形を保持し、重複削除や言語による除外は行わない。RSS要約・HNコメントの変換とは分ける。取得とrobots.txt確認は既存処理を使い、Trafilatura自身のダウンローダーは使わない。

抽出失敗・100文字未満は本文取得エラーとして記録し、従来のページ全体抽出へ自動で戻さない。再取得失敗時は以前の本文スナップショットを保持するが、`body_error`がある記事を今回の取得成功として扱わない。必要なら原文を確認して `body --text-file` を使う。依存がない場合もインストール方法を含むエラーを記録する。

本文には `extractor`（名前・版・設定。text-file取り込みは名前のみ）と `truncated`（20万文字の上限で切り詰めたか）を記録する。旧キャッシュにこれらがなくても利用できるが、Trafilaturaで抽出したとは扱わない。既存スナップショットは自動で移行しない。

本文確認範囲は引き続き `unverified-extraction`。モデルが実際に読み、ナビ等の混入と本文の欠落を確認する。抽出器を変更しても全文確認にはならず、ページ分割・画像・JavaScriptで描画される内容の取得は保証しない。全ページ未確認なら全文確認としない。

## ローカル探索からの追加

extra.jsonは配列。publishedを入れるときは記事の公開日を確認し、URLの年月やHN投稿時刻を流用しない。速報のcvssは原典から転記しsourceを付ける。

```json
[
  {"url":"https://example.org/story","title":"Original title","source":"local-discovery","lane":"article"},
  {"url":"https://example.org/advisory","title":"Advisory","source":"vendor","lane":"security","cvss":[{"score":9.1,"version":"CVSS:3.1","vector":null,"source":"https://example.org/advisory"}]}
]
```

```sh
python3 "<skill>/scripts/picker.py" import /absolute/run/bundle.json /absolute/extra.json
python3 "<skill>/scripts/picker.py" body /absolute/run/bundle.json ARTICLE_ID --text-file /absolute/body.txt --evidence-url https://example.org/story --scope partial
```

ローカル取得結果で確認した本文だけをbody.txtに保存する。タイトル・見出し・要約だけを本文に登録しない。確認した全文ならscope full、一部ならpartial。

## モデルのselection.json

```json
{
  "recommendations": [
    {
      "id": "収集済みの記事ID",
      "category": "reading",
      "body_reviewed": true,
      "summary": "原文に基づく短い日本語紹介",
      "reason": "本人の好みや保存記事との具体的なつながり",
      "how_to_read": "グラフ・操作型作品の場合、何を表しどこを操作すると何が分かるか。原文で確認した内容のみ",
      "discussion": [{"summary":"確認したHNコメントでの論点・補足・異論。記事の事実と区別", "comment_ids":[123456]}],
      "evidence": ["本文ファイルに実在する12文字以上の抜粋"]
    }
  ],
  "security": [{"id":"収集済み速報ID","note":"一次情報に基づく更新点・影響条件・次の確認事項。出典リンク付き"}],
  "limitations": "今回確認できなかった範囲。なければ空文字"
}
```

categoryはreading / experiment / technology / other。根拠抜粋はローカル照合用でユーザー出力には載せない。コードは実在・ハッシュ・ID・重複を検証するが、解釈の正しさを証明するものではない。

```sh
python3 "<skill>/scripts/picker.py" finalize /absolute/run/bundle.json /absolute/selection.json
# digest.mdの意味・リンク・非技術枠・速報・制約を確認してから：
python3 "<skill>/scripts/picker.py" finalize /absolute/run/bundle.json /absolute/selection.json --commit
```

commitは実際に届ける結果にのみ使う。試運転は付けない。新規・変更の機械条件付き速報はモデルが省いても出力される。件数枠で速報を抑制しない。
同じCVEの機械収集記録は表示上まとめる。commit時は `research/digests/` にも読み返し・エクスポート用の結果を保存する。この保存先は重複除外や速報の差分判定には使わない。定期実行自体の設定は変えない。

生成済みdigestを基にチャットへ返す。速報の重要な差分はモデルも確認し、表記修正だけを重大な更新と呼ばない。数値・取得日時を書き換えず、公開日は不明なら不明とする。

## 時間を含む注目度

`attention-policy.json` は個人用の暫定目安で、公式の人気基準ではない。recentは上から最初の一致を採用。取得時刻から同じ投稿のsubmitted_atを引き、記事公開日やレンダリング時刻は使わない。HNのUnix秒、Lobstersのタイムゾーン付きISO日時に対応する。

表示例：`HN 120pt／35コメント・投稿後3.0時間・今かなり話題（取得時点：…）`。24時間超の投稿は累積の反応として表示し「今」のラベルを付けない。日時やスコアが欠ける場合、対応サイト以外、未来やタイムゾーン不明の日時は判定未確認。未取得を低評価と扱わない。

単一観測から「急上昇」「直近1時間に増加」とは表現しない。増加量には同じ投稿の比較可能な複数観測が必要。複数サイトや別投稿を合算しない。生成済みの数値・経過時間・ラベルをチャットでもそのまま使い、古い取得値を現在の反応と呼ばない。

## 複数サイトでの掲載

候補の発見先と、同じ記事を紹介しているサイトを分ける。対象はHatena（はてなブックマーク全体。本人の保存に限定しない）、HN、Lobsters、MetaFilter、Kottkeの5サイト。収集済み候補・RSS・`fetch`した一覧／紹介投稿のリンクから原文URLを照合する。サイト内の公開検索もURLを確認して`fetch`で取得する。MetaFilter/KottkeのRSSや一覧は探索の入口で、原文へのリンクを確認するまで掲載数へ加えない。403等は突破しない。

同じ話題の別記事、転載、翻訳は同一記事の掲載数に含めない。URLの違いは実際のリダイレクト先等を確認し、`url`または`resolved_url`と照合する。複数サイトが互いを参照することもあるので「独立した支持」や英語圏全体の人気とは呼ばない。同一サイトの別投稿はリンクを残すがサイト数は1と数える。好みとの一致を優先し、掲載数で足切りしない。

ローカル取得結果で確認した記録は`import`用JSONの対象記事に`coverage`配列を付ける。`source`は発見・補足経路で、サイト数の根拠にはしない。`not_found`の注記には取得したRSS・一覧・検索URLとその範囲を記し、掲載なしと断定しない。`failed`には実際に失敗したURLと取得方法を記す。RSSが成功してトップページが403なら、それぞれ別の結果として扱う。再インポート時はその記事の`coverage`全体を置き換えるため、残したい観測も含める。

RSSのリンクから同一記事の掲載を確認した場合も、`post_url`はそのRSSエントリの紹介投稿URL、`article_url`は実際に含まれる原文URLとする。取得日時はRSSの観測時刻を使い、注記にRSSのURL・スナップショットと「個別投稿やコメントは未確認」を記す。トップページの403だけを理由に、RSSで確認できた掲載まで`failed`にしない。

```json
[
  {
    "url": "https://example.org/story",
    "source": "web-coverage-check",
    "coverage": [
      {"site":"Kottke","status":"found","post_url":"https://kottke.org/26/10/example","article_url":"https://example.org/story","retrieved_at":"2026-10-30T12:00:00+00:00"},
      {"site":"MetaFilter","status":"failed","retrieved_at":"2026-10-30T12:00:00+00:00","note":"公開投稿ページの取得が403"},
      {"site":"Hatena","status":"not_found","retrieved_at":"2026-10-30T12:00:00+00:00","note":"原文URLのエントリ検索で紹介を確認できず。サイト全体の不掲載は保証しない"}
    ]
  }
]
```

`site`は上記5つの識別名、`status`は`found` / `not_found` / `failed` / `unchecked`。各観測にタイムゾーン付き取得日時を残す。`found`には紹介投稿URLと実際にリンクされている記事URL、その他には検索範囲・失敗理由等の`note`が必要。検索結果の不在だけで不掲載と断定しない。HN/Lobstersは収集済み`metrics`の投稿リンクからも掲載を数える。PuddingのRSSや曖昧な`sources`だけからは数えない。

`finalize`はサイトを重複排除し、掲載確認数・根拠リンク・確認時点と未確認サイトを表示する。表示例：`対象5サイト中、掲載を確認できたのは3サイト（HN・Lobsters・Kottke）。Hatena：確認範囲で未発見／MetaFilter：取得失敗`。反応数は合算せず、MetaFilter/KottkeにHN等の話題ラベル閾値を流用しない。形式・URL照合はコードが検証するが、紹介投稿が本当にその記事をリンクしているかはモデルが確認する。

## HNの議論

```sh
python3 "<skill>/scripts/picker.py" discussion /absolute/run/bundle.json ARTICLE_ID --limit 60
```

選定したHN投稿について公式APIのコメントを幅優先で取得し、親ID・本文・個別リンク・取得時刻をスナップショットに保存する。既定は最大60ノード、--limitは1〜200。上限・個別失敗はpartial、投稿取得失敗はfailedとして記録する。削除・deadの本文は使わず返信は辿る。複数HN投稿は別々に取得する。APIの表示順は得票数ではなく、コメントの得票数は推定しない。

保存ファイルを実際に読み、上位の独立したスレッドと返信を検討する。論点、評価された点、補足・実体験、批判や反論を内容に応じて2〜3点に整理し、selectionのdiscussionに根拠comment_idsを入れる。反対意見がない場合は捏造しない。親コメントを読まずに返信の意味を決めず、皮肉・訂正・記事と無関係な脱線に注意する。部分取得から「HNの総意」「多数派」と断定しない。コメント内の主張を記事本文の事実として混ぜない。コードはIDと保存ハッシュを検証するが解釈はモデルが担う。

グラフ・地図・操作型作品ではhow_to_readに形式、図の意味、実際に確認した操作と見どころを添える。ブラウザ操作未確認の場合はその範囲を明示し、コメントだけから作品の動作を断定しない。議論が取得できなければ状態を表示して推薦自体は継続し、本文やタイトルからコメントを想像しない。新しい有用な議論を確認できた場合にのみ既紹介記事の補足として扱い、通常の新規推薦と混同しない。
