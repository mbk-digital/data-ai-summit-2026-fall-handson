"""Read-only public benchmark report; no cloud calls and no automatic grading review."""
import html
import json
import re
from .cloud import modern_trace_url
from .economics import read


def esc(value):
    return html.escape(str(value))


def money(value, upper=None):
    if value is not None:return f'{value:.6f} USD'
    return '未算出' if upper is None else f'未算出（最大 {upper:.6f} USD）'


def frame(body):
    return """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>#3 Economics — 評価記録</title>
<style>:root{font-family:system-ui,sans-serif;color:#2B356B;background:#f4f8fb}body{margin:0}main{max-width:1200px;margin:auto;padding:2rem}a{color:#33578E}a:focus-visible,summary:focus-visible{outline:3px solid #1490C4;outline-offset:4px}nav{display:flex;gap:1.5rem;flex-wrap:wrap}h1{font-size:clamp(1.8rem,4vw,2.5rem)}section,article{background:white;border:1px solid #cbdce7;border-radius:12px;padding:1.4rem;margin:1.2rem 0}h2{border-left:5px solid #3FA4AC;padding-left:.8rem}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;border-bottom:1px solid #dce6eb;padding:.7rem;vertical-align:top}th{background:#eaf5f8}caption{text-align:left;font-weight:600;padding:.8rem 0}.scroll{overflow:auto}summary{cursor:pointer;padding:.7rem 0;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#25314e;background:#f4f8fb;padding:1rem}p,li{line-height:1.7}.hint{color:#425778}.tag{display:inline-block;background:#eaf5f8;border-radius:8px;padding:.2rem .5rem}code{overflow-wrap:anywhere}</style>
<main><nav aria-label="ハンズオン"><a href="/lab1">01 Semantics</a><a href="/">02 Observability</a><a href="/benchmarks" aria-current="page">03 Economics</a></nav>"""+body+'</main></html>'


def render(value):
    parts=['<h1>品質・対応範囲・費用を比べる</h1><p>Production-ready AI Agents on Google Cloud<br>本番運用に向けて、業務定義・観測・費用を学ぶハンズオン</p>',
           '<p class="hint">評価計画 <code>'+esc(value['batch_id'])+'</code> ／価格表 '+esc(value['pricing_version'])+'</p>',
           '<p>費用は公表単価からの参考換算で、請求額ではありません。途中の小計による順位付けは行いません。回答の検査合格と参加者が内容を確認した成功を分けています。参加者確認は学習用で、主催者による公式評価ではありません。旧記録の確認者区分が不明な判定は引き継ぎません。</p>']
    legacy=value.get('review_policy')!='participant-v1'
    if legacy:parts.append('<p class="hint">旧版の確認記録は確認者区分が不明なため、参加者確認の成功件数・費用を表示しません。再集計し、確認を記録してください。</p>')
    timing=value.get('timing_comparison')
    if timing and timing['status']!='single_variant':
        pairs=', '.join(p['case_id']+'（'+str(p['repetition'])+' 回目）' for p in timing['pairs']) or '対象なし／全試行終了後に算出'
        parts.append('<p class="hint">共通回答試行の時間は、全構成が回答した同じ質問・同じ回だけを比較します。誤答の回答も含め、適切な判断保留も対象です。対応外や失敗を除いた事後の部分集計なので、全体の速さや品質を表しません。対象 '+esc(pairs)+'</p>')
        parts.append('<details><summary>時間比較の対象外と理由</summary><pre>'+esc(json.dumps(timing['excluded'],ensure_ascii=False,indent=2))+'</pre></details>')
    for g in value['groups']:
        parts+=['<section><h2>'+esc(g.get('label') or g['variant'])+'</h2>', '<p><span class="tag">'+('全試行終了' if g['finished'] else '途中経過')+'</span> 予定 '+str(g['planned'])+' 件 ／実行済み '+str(g['attempted'])+' 件</p>',
          '<div class="scroll" tabindex="0" role="region" aria-label="評価と費用"><table><caption>同じ評価計画での結果</caption><thead><tr><th scope="col">指標</th><th scope="col">結果</th><th scope="col">読み方</th></tr></thead><tbody>']
        metrics=[('処理時間の中央値',('未計測' if g['median_wall_seconds'] is None else str(round(g['median_wall_seconds'],1))+' 秒'),'Root 起動から終了まで。失敗・中断も計測できた試行を含む'),
          ('構造化回答の検査合格',str(g['structured_successes'])+' / '+str(g['planned']),'公開問題の数値・期間・単位・判断保留の契約で検査'),
          ('参加者確認を含む成功',str(g['verified_successes'])+' 件 ／確認待ち '+str(g['human_review_pending'])+' 件','構造化回答、取得元、回答文と SQL の人による確認'),
          ('参考費用の合計',money(g['reference_total_usd'],g.get('reference_total_upper_usd')),'失敗を含む全予定試行の費用が揃った場合のみ。照合できない BigQuery 分があるときは、候補のジョブを全部数えた上限を「最大」で示す'),
          ('換算済み分の小計',money(g['known_subtotal_usd']),'未算出 '+str(g['incomplete_cost_trials'])+' 試行・'+str(g['missing_cost_components'])+' 項目。比較順位には使わない'),
          ('検査合格 1 件あたり参考費用',money(g['reference_usd_per_structured_success'],g.get('reference_usd_per_structured_success_upper')),'全試行の参考費用 ÷ 構造化回答の検査合格数'),
          ('参加者確認を含む成功 1 件あたり参考費用',money(g['reference_usd_per_verified_success']),'確認待ちがある場合は未算出'),
          ('実際の取得試行',json.dumps(g.get('physical_route_calls'),ensure_ascii=False) if g.get('physical_route_calls') is not None else '未集計','実行した経路ごとの呼び出し試行数。選択だけして実行しなかった経路は含めない'),
          ('期待経路との一致',str(g['route_matches'])+' / '+str(g['planned']),'振り分け方針の分析軸。回答の合否とは独立')]
        if g.get('common_timing') is not None:
            common=g['common_timing'];seconds=common['median_seconds']
            metrics.insert(1,('共通回答試行の中央値','未算出' if seconds is None else str(round(seconds,1))+' 秒',str(common['trials'])+' 試行。全試行の中央値と対応範囲を合わせて読む'))
        if legacy:metrics=[m for m in metrics if not m[0].startswith('参加者確認')]
        if not g.get('show_route_match',False):metrics=[m for m in metrics if m[0]!='期待経路との一致']
        for title,result,note in metrics:parts.append('<tr><th scope="row">'+esc(title)+'</th><td>'+esc(result)+'</td><td>'+esc(note)+'</td></tr>')
        parts.append('</tbody></table></div><details><summary>質問分類ごとの対応範囲</summary><pre>'+esc(json.dumps(g['coverage_by_class'],ensure_ascii=False,indent=2))+'</pre></details></section>')
    parts.append('<h2>質問ごとの根拠を確認する</h2>')
    for t in value['trials']:
        label='検査合格' if t['structured_success'] else '未実行' if t['attempt_state']=='pending' else '実行中' if t['attempt_state']=='running' else '対応範囲外' if t['outcome']=='unsupported' else '検査不合格'
        parts.append('<article><h3>'+esc(t['case_id'])+' · '+esc(t.get('variant_label') or t['variant'])+' · '+str(t['repetition'])+' 回目</h3><p>'+esc(t['question'])+'</p><p>'+label+' ／状態 '+esc(t['attempt_state'])+' ／取得元 '+esc(t['provenance']['status'])+' ／参加者確認 '+esc('旧版・区分不明' if legacy else t['human_review'])+'</p>')
        for label,obj in [('回答と検査結果',{'answer':t['answer'],'errors':t['errors']}),('取得表・SQL・取得元',{'evidence':t['evidence'],'provenance':t['provenance']}),('選択経路と実際の処理',t['routing']),('費用の内訳と確かさ',{'reference_total_usd':t['reference_total_usd'],'known_subtotal_usd':t['known_subtotal_usd'],'components':t.get('cost_components')})]:
            parts.append('<details><summary>'+label+'</summary><pre>'+esc(json.dumps(obj,ensure_ascii=False,indent=2))+'</pre></details>')
        trace=modern_trace_url(t.get('trace_console_url'))
        link=('<a href="'+esc(trace)+'" target="_blank" rel="noopener">Cloud Trace で処理を開く ↗</a>'
              if isinstance(trace,str) and trace.startswith('https://console.cloud.google.com/') else
              'Cloud Trace のリンクなし。古いレポートなら <code>benchmark report</code> で再集計してください。'
              '再集計しても出ない場合は実行記録に URL がないため、Trace 未確認として残します。'
              'URL があっても Trace の到達・表示は保証しません')
        parts.append('<p class="hint">実行 ID <code>'+esc(t['run_id'])+'</code> ／ '+link
                     +'<br>保存済みの記録を開くだけで、新しい推論は行いません。</p></article>')
    parts.append('<section><h2>この結果の範囲</h2><ul>'+''.join('<li>'+esc(s)+'</li>' for s in value['limitations'])+'</ul></section>')
    return frame(''.join(parts))


def page(root, cfg, ident=None):
    base=root/'artifacts/benchmarks'
    if ident is not None:
        if not re.fullmatch('[a-f0-9]{16}',ident):return None
        folder=base/ident
        if not (folder/'plan.json').is_file() or not (folder/'report.json').is_file():return None
        plan=read(folder/'plan.json');context=plan.get('context',{}).get('config',{})
        if any(context.get(k)!=cfg.get(k) for k in ('project_id','dataset_id')):return None
        return render(read(folder/'report.json'))
    items=[]
    for folder in sorted(base.glob('*'),reverse=True):
        if not re.fullmatch('[a-f0-9]{16}',folder.name) or not (folder/'plan.json').is_file():continue
        v=read(folder/'plan.json');c=v.get('context',{}).get('config',{})
        if any(c.get(k)!=cfg.get(k) for k in ('project_id','dataset_id')):continue
        label=esc(folder.name)+' · '+esc(v['created_at'])+' · '+str(len(v['trials']))+' 試行'
        items.append('<li>'+('<a href="/benchmarks/'+folder.name+'">'+label+'</a>' if (folder/'report.json').exists() else label+'（集計前）')+'</li>')
    return frame('<h1>#3 Economics — 評価記録</h1><p>Cloud Shell で評価計画を作成・実行し、集計した結果をここで確認します。</p><p>この画面は保存記録を表示します。ページを開いても推論や課金対象の操作は始まりません。</p><ul>'+(''.join(items) or '<li>評価計画はまだありません。GitHub の #3 手順書に沿って進めてください。</li>')+'</ul>')
