"""Bounded Azure independent source review; AI never supplies a score."""
import hashlib,json,os,time,urllib.request
from datetime import datetime,timezone
from evidence_ai_rules import (Review,SYSTEM,PROMPT_VERSION,VALIDATOR_VERSION,
                               source_packet,validate_review,adjudicate_review)

ENDPOINT='https://thronescanner.cognitiveservices.azure.com'
_token=None

def request_json(url,body=None,headers=None):
    request=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
    with urllib.request.urlopen(request,timeout=75) as response:
        return json.load(response)

def azure_review(packet):
    global _token
    if _token is None or float(_token['expires_on']) < time.time()+120:
        _token=request_json('http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fcognitiveservices.azure.com%2F',headers={'Metadata':'true'})
    result=request_json(ENDPOINT+'/openai/deployments/gpt-5-mini/chat/completions?api-version=2025-04-01-preview',
        {'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(packet,ensure_ascii=False)}],
         'max_completion_tokens':7000,'reasoning_effort':'low',
         'response_format':{'type':'json_schema','json_schema':{'name':'textile_review','strict':True,'schema':strict_schema()}}},
        {'Authorization':'Bearer '+_token['access_token'],'Content-Type':'application/json'})
    choice=result['choices'][0]
    if choice.get('finish_reason')!='stop' or choice['message'].get('refusal'):
        raise ValueError('incomplete_independent_review')
    review=Review.model_validate_json(choice['message']['content'])
    return review,{'response_id':result.get('id'),'model':result.get('model'),'usage':result.get('usage')}

def strict_schema():
    schema=Review.model_json_schema()
    def walk(node):
        if isinstance(node,dict):
            node.pop('default',None)
            if node.get('type')=='object':
                node['additionalProperties']=False;node['required']=list(node.get('properties',{}))
            for value in node.values():walk(value)
        elif isinstance(node,list):
            for value in node:walk(value)
    walk(schema);return schema

def _verify_packet_once(packet,row,reviewer):
    """Blind review first; compare independently grounded fractions afterward."""
    raw,metadata=reviewer(packet)
    corrected,decisions=adjudicate_review(packet,raw)
    validation=validate_review(packet,corrected)
    from throne_read import _seg_pairs
    from throne_standard import MATERIAL_TERMS
    signature=sorted((MATERIAL_TERMS.get(name.lower(),(name.lower(),None))[0],pct)
                     for pct,name in _seg_pairs(row.get('composition') or '') if pct>0)
    grounded=[sorted((name,pct) for name,pct in s if pct>0) for s in validation['grounded_whole_product_signatures']]
    shared=all(d.applies_to_variants=='product_shared' and (not d.variant_ids or set(d.variant_ids)==set(packet['variant_ids']))
               for d in corrected.declarations if d.scope=='whole_product' and d.fractions)
    parity=corrected.verdict=='complete' and signature in grounded and row.get('scoreable') is True and shared
    if not grounded and corrected.verdict=='partial' and row.get('scoreable') is True:
        parity=component_parity(packet, row, corrected)
    passed=validation['pass'] and parity
    return {'rule_version':VALIDATOR_VERSION,'prompt_version':PROMPT_VERSION,
        'prompt_sha256':hashlib.sha256(SYSTEM.encode()).hexdigest(),
        'packet_sha256':hashlib.sha256(json.dumps(packet,sort_keys=True).encode()).hexdigest(),
        'checked_at':datetime.now(timezone.utc).isoformat(),'raw_review':raw.model_dump(),
        'review':corrected.model_dump(),'scope_decisions':decisions,'validation':validation,
        'python_parity':parity,'pass':passed,**metadata}

def verify_item(item,row,html=""):
    if row.get('scoreable') is not True:
        return row
    packet=source_packet(item,html=html,captured_at=item.get('fetchedAt'))
    # Never silently clip sources or feed the score to AI.
    if len(json.dumps(packet))>220000:raise ValueError('independent_review_input_limit')
    receipt=verify_packet(packet,row)
    out=dict(row);out['independent_review']=receipt
    if not receipt['pass']:
        out.update(score=None,passes=None,scoreable=False,assessment_status='unresolved',
                   evidence_state='manual_review_required',terminal_state='manual_review_required',
                   how='independent-evidence-review',score_reason='Independent source review or reader parity requires review')
    return out


def component_parity(packet,row,review):
    """Match named complete components under the existing Standard; no weights."""
    from throne_read import component_declarations,_seg_pairs
    from evidence_ai_rules import canonical_material,TOLERANCE
    import re
    def label(value):
        value=re.sub(r'[^a-z0-9]+',' ',(value or '').lower()).strip()
        return {'body':'main','self':'main','main fabric':'main','fabric':'main','linings':'lining','trims':'trim'}.get(value,value)
    expected=set()
    for name,text in component_declarations(row.get('composition') or ''):
        pairs=_seg_pairs(text)
        if not pairs or abs(sum(p for p,n in pairs)-100)>TOLERANCE:return False
        signature=tuple(sorted((canonical_material(n),p) for p,n in pairs if p>0))
        if any(n is None for n,p in signature):return False
        expected.add((label(name),signature))
    found=set()
    for d in review.declarations:
        if not d.fractions:continue
        if d.scope!='component' or d.applies_to_variants!='product_shared' or (d.variant_ids and set(d.variant_ids)!=set(packet['variant_ids'])):return False
        if abs(sum(f.percent for f in d.fractions)-100)>TOLERANCE:return False
        found.add((label(d.component),tuple(sorted((canonical_material(f.material),f.percent) for f in d.fractions if f.percent>0))))
    return bool(expected) and found==expected


def verify_packet(packet,row,reviewer=azure_review,max_attempts=2):
    """At most two reviews; preserve every failed attempt and disclose no score."""
    attempts=[];request=packet
    for number in range(max_attempts):
        receipt=_verify_packet_once(request,row,reviewer)
        attempts.append(receipt)
        if receipt['pass']:break
        # A second independent review gets validation defects, never the Python
        # fractions/score or the desired benchmark. Original text stays intact.
        request={**packet,'audit_feedback':{
            'instruction':'Re-audit original sources. Correct invalid citations or scope; keep real conflicts. Do not infer missing numbers or fibres.',
            'validation_errors':receipt['validation']['errors'],
            'instruction_detail':'Ignore previous guessed declarations. Produce a fresh review using shortest literal quotations. Non-fibre claims and fibre-absence statements belong in observations, not quantified declarations.'}}
    result=dict(attempts[-1]);result['attempts']=attempts
    result['source_packet_sha256']=hashlib.sha256(json.dumps(packet,sort_keys=True).encode()).hexdigest()
    return result
