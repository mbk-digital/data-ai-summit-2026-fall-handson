# Cloud Shell で教材を取得・更新・再開する

**コマンドはコードブロックごとにコピーして実行します。各操作の完了と結果を確認してから次へ進み、エラーが出たらそこで止めてください。**

コマンドは Google Cloud の Cloud Shell で実行します。結果を見る Web 画面も、同じ Cloud Shell から起動します。

## 初めて教材を取得する

Google Cloud コンソールで Cloud Shell を開き、次を実行してください。

```sh
git clone --branch main https://github.com/mbk-digital/data-ai-summit-2026-fall-handson.git summit-workshop
```

教材は Public リポジトリから取得するため、GitHub アカウントや GitHub へのログインは不要です。`Repository not found` や認証要求が出たら URL を確認し、解消しなければスタッフに相談してください。Google Cloud の操作には別途、実習用アカウントでの認証が必要です。

`fatal: destination path 'summit-workshop' already exists and is not an empty directory.` と表示された場合は、この Cloud Shell に以前取得した教材が残っています。以前と同じ Project で続ける場合は、削除せず「教材を更新する」へ進みます。新しい Project で最初からやり直す場合は、`mv ~/summit-workshop ~/summit-workshop-old` で名前を変えてから、もう一度 `git clone` を実行します。元のフォルダは、前の Project を片付けるときに必要なので消さずに残します。

教材フォルダへ移動します。

```sh
cd summit-workshop
```

取得できたら[README の「2．参加する回の手順を進める」](../README.md#2参加する回の手順を進める)へ進みます。共通の事前準備からこのページを開いた方は、[準備の手順 2](labs/preparation.md#手順-2project-を設定し教材データを用意する)へ戻ってください。

## 教材を更新する

主催者から更新の案内があったときに行います。分析の実行が終わってから、表示用サーバーを Ctrl+C で止めてください。

変更の有無を確認します。

```sh
cd ~/summit-workshop && git status --short
```

何も表示されなければ、次へ進みます。

ファイル名が表示された場合は、教材のファイルを自分で変更しています。変更は消さずに、次のコマンドで変更の内容をファイルに書き出してから、主催者に相談してください。

```sh
cd ~/summit-workshop && git diff HEAD > ~/summit-workshop-changes.diff
```

Cloud Shell のホームフォルダの `summit-workshop-changes.diff` に書き出されます。こちらを主催者にお見せください。このコマンドでは教材ファイルの中身は変わりません。

行の先頭が `??` のファイルは、教材にない、自分で作ったファイルです。このファイルは上の書き出しに含まれません。更新しても消えませんが、主催者に見せる場合や、気になる場合は、次のように教材フォルダの外へコピーします（`ファイル名` は一覧に出た名前に置き換えます）。

```sh
cp ~/summit-workshop/ファイル名 ~/
```

`participant/` フォルダ（実習で編集・保存するファイルの置き場所）と `config/workshop.local.json`（自分の Project の設定）は、教材の更新で上書きされません。この確認の一覧にも出ません。

```sh
cd ~/summit-workshop && git pull --ff-only
```

主催者から版の指定がある場合は、更新後の教材の commit ID を確認します。

```sh
cd ~/summit-workshop && git rev-parse HEAD
```

表示された 40 文字の英数字（例：`4289f696ab01e63e…`）が、教材の版を表す commit ID です。指定された文字列と先頭から一致するか確認します（指定は先頭の数文字だけの場合があります）。

クラウド上の教材データを確認します。

```sh
cd ~/summit-workshop && uv run --locked python3 -m workshop.cloud verify-bigquery
```

データに違いがある場合やエラーが出た場合は、追加の更新・構築をせず、出力を主催者へ伝えてください。

## Cloud Shell を開き直したら

教材のコマンドは、どれも `cd ~/summit-workshop && …` の形で教材フォルダへ移動してから実行するので、フォルダを移動し直す操作は要りません。別の場所に取得した場合は、コマンドの `~/summit-workshop` をそのフォルダに置き換えます。

教材のコマンドは `uv run --locked` から始まるため、環境を有効にし直す操作は要りません。`uv: command not found` が出た場合は、[事前準備の手順 1](labs/preparation.md#手順-1実習用の環境を用意する) の折りたたみを確認してください。

コマンドで `You do not currently have an active account selected` と表示された場合は、Cloud Shell の認証が切れています。Cloud Shell を閉じて開き直してから、同じコマンドをもう一度実行します。

ここから[実習 1](labs/lab1.md)・[実習 2](labs/lab2.md)の再開箇所へ戻り、必要な表示用サーバーを起動します。フォルダが見つからない場合は、同じ名前で作り直す前にバックアップの有無を確認してください。

<details>
<summary>以前 .venv-adk を作った方へ</summary>

以前の手順で作成した `.venv-adk` フォルダは残っていても実習に影響しません。`source .venv-adk/bin/activate` の実行は不要です。削除しても構いませんが、削除は必須ではありません。以後の手順は `uv run --locked` から直接実行します。

</details>

## 消さずに残すファイル

| 場所 | 保存されるもの |
|---|---|
| `config/workshop.local.json` | 自分の Project ID・環境設定 |
| `participant/` | 編集した業務定義・実習の設定・自分のメモ |
| `artifacts/` | 回答・実行記録・クラウド環境の片付けに必要な管理台帳 |

これらは GitHub から取得し直しても戻りません。教材フォルダを移す・作り直す場合は、先に[バックアップ手順](labs/backup.md)を確認してください。実習を終える場合は[クラウド環境の片付け](labs/cleanup.md)を済ませてからフォルダを整理します。
