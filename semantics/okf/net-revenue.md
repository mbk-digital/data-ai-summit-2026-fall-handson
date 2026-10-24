---
type: Metric
title: 純売上
description: 架空のEC企業における値引後販売額と返金の期間別集計ルール
---

# Definition

この教材の純売上は、対象期間の値引後販売額から、対象期間に計上する返金額を差し引いた金額です。税・送料を除く整数の日本円を使います。この定義は架空企業の教材上の取り決めです。

- `orders.status = 'COMPLETED'`の注文だけを対象とします。キャンセル注文は販売・返金とも除外します。
- 販売額は`order_items.quantity * order_items.unit_price_jpy - order_items.discount_jpy`です。`discount_jpy`は明細全体の値引額で、個数を掛けません。
- 販売は`orders.ordered_date`（注文日）に計上します。
- 返金は`refunds.refunded_date`（返金日）に`refund_amount_jpy`を控除します。対象期間より前の注文への返金も、返金日が期間内なら含めます。
- 日付は日本時間の業務日付です。SQLでは開始日以上・終了日未満で期間を指定します。回答の期間表現は開始日と最終日で述べます。
- 1つの明細に返金が複数行ある場合でも、販売額を重複計上しません。販売と返金を別のイベントとして集計するか、それぞれを適切な粒度で集計します。

# Data mapping

`products`、`orders`、`order_items`、`refunds`が関連する生テーブルです。`v_revenue_events`は、この教材の販売・返金のイベントを表す集計用Viewです。Project IDとDataset IDは参加者設定から解決します。

期間全体の純売上を求める際、Graphで返金のある明細だけを抽出した結果を、全明細の売上として代用しません。
