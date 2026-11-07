"""Read-only public benchmark report; no cloud calls and no automatic grading review."""
import html
import json
import re
from .cloud import modern_trace_url
from .economics import read
from .benchmark_view import STATE_NOTES, check_explanation, data_agent_calls, timing_note

EXAMPLE = 'data/examples/lab3-instructor-record.json'


def esc(value):
    return html.escape(str(value))


def money(value, upper=None):
    if value is not None:return f'{value:.6f} USD'
    return '未算出' if upper is None else f'未算出（最大 {upper:.6f} USD）'


def frame(body):
    return """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>#3 Economics — 評価記録</title>
<style>:root{font-family:system-ui,sans-serif;color:#2B356B;background:#f4f8fb}body{margin:0}main{max-width:1200px;margin:auto;padding:2rem}a{color:#33578E}a:focus-visible,summary:focus-visible{outline:3px solid #1490C4;outline-offset:4px}nav{display:flex;gap:1.5rem;flex-wrap:wrap}h1{font-size:clamp(1.8rem,4vw,2.5rem)}section,article{background:white;border:1px solid #cbdce7;border-radius:12px;padding:1.4rem;margin:1.2rem 0}h2{border-left:5px solid #3FA4AC;padding-left:.8rem}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;border-bottom:1px solid #dce6eb;padding:.7rem;vertical-align:top}th{background:#eaf5f8}caption{text-align:left;font-weight:600;padding:.8rem 0}.scroll{overflow:auto}summary{cursor:pointer;padding:.7rem 0;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;color:#25314e;background:#f4f8fb;padding:1rem}p,li{line-height:1.7}.hint{color:#425778}.tag{display:inline-block;background:#eaf5f8;border-radius:8px;padding:.2rem .5rem}code{overflow-wrap:anywhere}.chip{display:inline-block;border-radius:999px;padding:.1rem .6rem;font-size:.85rem;font-weight:600;background:#eef3f7}.ok{background:#e6f4ec;color:#1f6b45}.hold{background:#fbf1dc;color:#6b4f10}.bad{background:#fbe4e4;color:#8a1f1f}.cell a{color:inherit;text-decoration:none;display:block}.cell a:hover{background:#f4f8fb}.cell small{display:block;color:#425778}.example{background:#f6f2fb;border-color:#d8cbe9}.notice{background:#fff7e0;border:1px solid #ecd58c;border-radius:10px;padding:.8rem 1rem}.bar{display:grid;grid-template-columns:9em 1fr 7em;gap:.6rem;align-items:center;margin:.3rem 0}.track{background:#eef3f7;border-radius:6px;height:14px;overflow:hidden}.fill{height:100%;background:#33578E}.fill.on{background:#3FA4AC}@media (max-width:640px){.bar{grid-template-columns:6em 1fr 6em}main{padding:1rem}}</style>
<main><nav aria-label="ハンズオン"><a href="/lab1">01 Semantics</a><a href="/">02 Observability</a><a href="/benchmarks" aria-current="page">03 Economics</a></nav>"""+body+'</main></html>'


def status_chip(t):
    if t['attempt_state']=='pending':return '<span class="chip">未実行</span>'
    if t['attempt_state']=='running':return '<span class="chip">実行中</span>'
    if t['attempt_state'] not in ('completed','recovered') or t['outcome']=='execution_failed':return '<span class="chip bad">失敗</span>'
    if not t['structured_success']:
        x=check_explanation(t)
        if x['system']:return '<span class="chip bad">記録と計画が不一致</span>'
        return '<span class="chip bad">不一致：'+esc('、'.join(x['short']) or '詳細を確認')+'</span>'
    return '<span class="chip '+('hold">保留・一致' if t['outcome']=='abstained' else 'ok">回答・一致')+'</span>'


def check_block(t):
    """回ごとの自動チェックの説明。照らした項目と、基準と違った項目・理由を、折りたたみの外に出す。"""
    if t['attempt_state'] in ('pending','running'):return ''
    x=check_explanation(t)
    matched='照らして基準と一致した項目：'+'、'.join(x['matched'])+'。' if x['matched'] else ''
    if t['structured_success']:
        return '<p class="hint">自動チェック：基準と一致。'+esc(matched)+'回答文の説明は、手順 5 で人が読んで確かめます。</p>'
    lines=''.join('<li>'+esc(text)+'</li>' for text in x['system']+x['problems'])
    tail=('<p class="hint">'+esc(matched)+'参加者の操作ミスではありません。回答文は、手順 5 と同じく人が読んで確かめます。</p>' if x['answer_issue']
          else ('<p class="hint">'+esc(matched)+'</p>' if matched else ''))
    return '<div class="notice"><p><strong>自動チェック：基準と違う項目があります</strong></p><ul>'+lines+'</ul>'+tail+'</div>'


def trial_cost(t):
    state=t.get('cost_state')
    if t['reference_total_usd'] is not None:return f"{t['reference_total_usd']:.3f} USD"
    if t.get('reference_total_upper_usd') is not None:return f"未算出（最大 {t['reference_total_upper_usd']:.3f} USD）"
    return {'failed':'未算出（失敗）','not_collected':'未計算','pending':'—'}.get(state,'未算出')


def seconds(value):
    return '—' if value is None else f'{value:.1f} 秒'


def anchor(t):
    return 'trial-'+re.sub('[^A-Za-z0-9-]','-',t['case_id']+'-'+t['variant'])


def jst_full(stamp):
    import datetime as dt
    try:return dt.datetime.fromisoformat(stamp).astimezone(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M')
    except (TypeError,ValueError):return '日時不明'


def totals_table(rows, caption):
    """rows: (label, successes, planned, total, median, timing_trials, calls)。差は 2 つとも揃ったときだけ。"""
    head='<table><caption>'+esc(caption)+'</caption><thead><tr><th scope="col"></th>'+''.join('<th scope="col">'+esc(r[0])+'</th>' for r in rows)+('<th scope="col">差</th>' if len(rows)==2 else '')+'</tr></thead><tbody>'
    def diff(a,b):
        return '—' if a is None or b is None or not a else f'{(b-a)/a*100:+.0f}%'
    lines=[('自動チェックで基準と一致',[str(r[1])+' / '+str(r[2]) for r in rows],''),
           ('参考費用（合計）',[('未算出' if r[3] is None else f'{r[3]:.3f} USD') for r in rows],diff(rows[0][3],rows[1][3]) if len(rows)==2 else ''),
           ('処理時間の中央値',[seconds(r[4])+('' if r[5] is None else '（'+str(r[5])+' 問）') for r in rows],diff(rows[0][4],rows[1][4]) if len(rows)==2 else ''),
           ('Data Agent に頼んだ回数',[str(r[6])+' 回' for r in rows],'')]
    body=''.join('<tr><th scope="row">'+esc(name)+'</th>'+''.join('<td>'+esc(c)+'</td>' for c in cells)+('<td>'+esc(d)+'</td>' if len(rows)==2 else '')+'</tr>' for name,cells,d in lines)
    return '<div class="scroll" tabindex="0" role="region" aria-label="'+esc(caption)+'">'+head+body+'</tbody></table></div>'


def bars(rows):
    out=[]
    for title,index,fmt in (('参考費用（合計）',3,lambda v:f'{v:.3f} USD'),('処理時間の中央値',4,lambda v:f'{v:.1f} 秒')):
        values=[r[index] for r in rows]
        if any(v is None for v in values) or not max(values):continue
        top=max(values)
        out.append('<h3>'+title+'</h3>'+''.join('<div class="bar"><span>'+esc(r[0].replace('Data Agent・',''))+'</span><div class="track"><div class="fill'+(' on' if n else '')+'" style="width:'+str(round(r[index]/top*100))+'%"></div></div><span>'+fmt(r[index])+'</span></div>' for n,r in enumerate(rows)))
    return ''.join(out)


def render_summit(value, example=None):
    """Summit の #3（--preset summit）の比べる画面。判定と数字は report のまま、読み方だけを変える。"""
    trials=value['trials'];groups=value['groups']
    done=sum(t['attempt_state'] not in ('pending','running') for t in trials)
    parts=['<h1>#3 の結果</h1>',
           '<p>3 つの質問を、'+str(len(groups))+' つのやり方で 1 回ずつ答えさせました（合計 '+str(len(trials))+' 回のうち '+str(done)+' 回が終了）。評価計画 <code>'+esc(value['batch_id'])+'</code>、日本時間 '+esc(jst_full(value.get('created_at')))+' に作成。</p>',
           '<p class="notice">この画面は、AI の出力を調べた結果です。<strong>参加者の採点ではありません。</strong></p>',
           '<p class="notice">費用は<strong>参考の見積もり</strong>です（使った量 × 公表価格。請求額ではありません）。Data Agent は 2026-12-31 まで無料試用中のため、今は Data Agent の分の料金はかかりません。「未算出」は 0 円という意味ではありません。</p>']
    if done<len(trials):parts.append('<p class="notice">まだ実行中です。終わってから、このページを再読み込みしてください。</p>')
    cases=list(dict.fromkeys(t['case_id'] for t in trials))
    by={(t['case_id'],t['variant']):t for t in trials}
    grid=['<section aria-labelledby="grid"><h2 id="grid">質問 × やり方</h2><p class="hint">マスを押すと、その回の記録へ移動します。「一致」「不一致」は、AI が回答と一緒に返したデータ（数字・期間・単位など）を、教材の基準と照らした結果です。「保留」は「データからは言い切れない」と判断を保留したことです。不一致のマスを押すと、どの項目が違ったかが分かります。回答文の説明は、手順 5 で人が確かめます。</p><div class="scroll" tabindex="0" role="region" aria-label="質問 × やり方"><table><thead><tr><th scope="col">質問</th>'+''.join('<th scope="col">'+esc(g['label'])+'</th>' for g in groups)+'</tr></thead><tbody>']
    for case in cases:
        question=next(t['question'] for t in trials if t['case_id']==case)
        grid.append('<tr><th scope="row">'+esc(case)+'<small class="hint" style="display:block;font-weight:400">'+esc(question)+'</small></th>')
        for g in groups:
            t=by.get((case,g['variant']))
            grid.append('<td class="cell">'+('—' if not t else '<a href="#'+anchor(t)+'">'+status_chip(t)+'<small>参考費用 '+esc(trial_cost(t))+'</small><small>処理時間 '+esc(seconds(t['elapsed_seconds']))+'</small><small>Data Agent に頼んだ回数 '+str(data_agent_calls(t))+' 回</small></a>')+'</td>')
        grid.append('</tr>')
    grid.append('</tbody></table></div></section>')
    parts+=grid
    rows=[(g['label'],g['structured_successes'],g['planned'],g['reference_total_usd'],(g.get('common_timing') or {}).get('median_seconds'),(g.get('common_timing') or {}).get('trials'),sum(data_agent_calls(t) for t in trials if t['variant']==g['variant'])) for g in groups]
    # 中央値だけを見ると「時間も半分になった」と読まれやすい。短縮するかどうかは断定しない。
    time_note='中央値の差だけで判断せず、質問ごとの処理時間も確認してください。'
    notes=[n for n in [timing_note(value)] if n]
    if len(rows)==2 and all(r[4] is not None for r in rows):notes.insert(0,time_note)
    states=[t.get('cost_state') for t in trials]
    notes+=[STATE_NOTES[s] for s in ('failed','permission','unmatched','waiting','unknown','not_collected') if s in states]
    parts.append('<section aria-labelledby="totals"><h2 id="totals">やり方ごとの合計</h2><p class="hint">同じエージェントで、Data Agent に頼む回数だけを変えています。費用の差は、両方とも全回の費用を計算できたときだけ出します。上限（「最大」）は差に使いません。</p>'
                 +totals_table(rows,'あなたの結果')+bars(rows)+''.join('<p class="hint">'+esc(n)+'</p>' for n in notes)+'</section>')
    if example:
        erows=[(g['label'],g['structured_successes'],g['planned'],g['reference_total_usd'],g.get('median_seconds'),g.get('timing_trials'),g['data_agent_calls']) for g in example['groups']]
        parts.append('<section class="example"><details><summary>講師の記録例と比べる（あなたの結果ではありません）</summary><p>評価計画 <code>'+esc(example['batch_id'])+'</code>、日本時間 '+esc(jst_full(example.get('created_at')))+' に作成。同じ 3 問・同じやり方を、講師の環境で実行した結果です。</p>'
                     +totals_table(erows,'講師の記録例')+'<p class="hint">'+esc(time_note)+'講師の記録例の質問ごとの処理時間は、講師の記録例のページの「質問ごとの結果」にあります。</p></details></section>')
    else:
        parts.append('<section class="example"><p class="hint">講師の記録例は準備中です。</p></section>')
    parts.append('<h2>質問ごとの記録</h2>')
    for t in trials:
        components=t.get('cost_components') or {}
        def part(key):
            if key not in components:return '—'
            amount=components[key].get('reference_usd')
            return '未算出' if amount is None else f'{amount:.6f} USD'
        bq=part('bigquery_query')+('（BigQuery が保存済みのクエリ結果を再利用したため）' if t.get('bigquery_cache_reused') else '')
        answer=(t.get('answer') or {}).get('customer_message_ja')
        trace=modern_trace_url(t.get('trace_console_url'))
        link='<a href="'+esc(trace)+'" target="_blank" rel="noopener">Cloud Trace で処理を開く ↗</a>' if isinstance(trace,str) and trace.startswith('https://console.cloud.google.com/') else 'Cloud Trace のリンクはありません'
        state=t.get('cost_state')
        parts.append('<article id="'+anchor(t)+'"><h3>'+esc(t['case_id'])+' · '+esc(t['variant_label'])+'</h3><p>'+esc(t['question'])+'</p><p>'+status_chip(t)+' 処理時間 '+esc(seconds(t['elapsed_seconds']))+' ／ Data Agent に頼んだ回数 '+str(data_agent_calls(t))+' 回 ／ 人の確認 '+esc({'pass':'渡せる','fail':'直す必要あり'}.get(t.get('human_review'),'未確認'))+'</p>'
                     +check_block(t)
                     +('<h4>回答文</h4><p>'+esc(answer)+'</p>' if answer else '<p class="hint">回答文はありません。</p>')
                     +'<table><caption>参考費用の内訳</caption><tbody><tr><th scope="row">Root Agent の AI</th><td>'+esc(part('root_model'))+'</td></tr><tr><th scope="row">Data Agent</th><td>'+esc(part('data_agent'))+'</td></tr><tr><th scope="row">BigQuery</th><td>'+esc(bq)+'</td></tr><tr><th scope="row">合計</th><td>'+esc(trial_cost(t))+'</td></tr></tbody></table>'
                     +('<p class="hint">'+esc(STATE_NOTES[state])+'</p>' if state in STATE_NOTES else '')
                     +'<p class="hint">'+link+' ／ 実行 ID <code>'+esc(t['run_id'])+'</code>（手順 5 で人の確認を記録するときに使います）</p>'
                     +'<details><summary>詳しい結果・自動チェックの理由</summary><pre>'+esc(json.dumps({'answer':t.get('answer'),'errors':t['errors'],'evidence':t.get('evidence'),'provenance':t.get('provenance'),'routing':t.get('routing'),'cost_components':components},ensure_ascii=False,indent=2))+'</pre></details></article>')
    parts.append('<section><details><summary>講師用の集計とこの結果の範囲</summary><ul>'+''.join('<li>'+esc(x)+'</li>' for x in value['limitations'])+'</ul><pre>'+esc(json.dumps({'groups':groups,'timing_comparison':value.get('timing_comparison')},ensure_ascii=False,indent=2))+'</pre></details></section>')
    return frame(''.join(parts))


def render(value, example=None):
    if value.get('preset')=='summit':return render_summit(value,example)
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
        example=root/EXAMPLE
        value=read(folder/'report.json')
        # 教材を更新する前に保存した集計には、自動チェックの基準（expected）がない。
        # 集計に使った計画と同じと確かめられたときだけ、評価計画から補う（違えば基準不明として表示）。
        from .benchmark import sha
        cases=(plan.get('cases') or {}) if value.get('plan_sha256') and value.get('plan_sha256')==sha(plan) else {}
        for trial in value.get('trials',[]):
            if 'expected' not in trial and isinstance((cases.get(trial.get('case_id')) or {}).get('expected'),dict):
                trial['expected']=cases[trial['case_id']]['expected']
        return render(value,read(example) if example.is_file() else None)
    items=[]
    for folder in sorted(base.glob('*'),reverse=True):
        if not re.fullmatch('[a-f0-9]{16}',folder.name) or not (folder/'plan.json').is_file():continue
        v=read(folder/'plan.json');c=v.get('context',{}).get('config',{})
        if any(c.get(k)!=cfg.get(k) for k in ('project_id','dataset_id')):continue
        label=esc(folder.name)+' · '+esc(v['created_at'])+' · '+str(len(v['trials']))+' 試行'
        items.append('<li>'+('<a href="/benchmarks/'+folder.name+'">'+label+'</a>' if (folder/'report.json').exists() else label+'（集計前）')+'</li>')
    return frame('<h1>#3 Economics — 評価記録</h1><p>Cloud Shell で評価計画を作成・実行し、集計した結果をここで確認します。</p><p>この画面は保存記録を表示します。ページを開いても推論や課金対象の操作は始まりません。</p><ul>'+(''.join(items) or '<li>評価計画はまだありません。GitHub の #3 手順書に沿って進めてください。</li>')+'</ul>')
