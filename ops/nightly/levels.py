import ssk_source as s, json, sys
out={}
for cid in ("muravai","fascial-labs","nuerosmile","puravita"):
    key=s.api_key(cid)
    if not key: out[cid]="no key"; continue
    rows,tr=s.get_all(key,"/inventory/levels")
    agg={}
    for r in rows:
        v=r.get("productVariant") or {}
        k=((v.get("sku") or "").strip(), v.get("title"))
        a=agg.setdefault(k,{"on":0,"av":0,"cm":0,"in":0,"loc":0})
        a["on"]+=r.get("onHandQuantity") or 0; a["av"]+=r.get("availableQuantity") or 0
        a["cm"]+=r.get("committedQuantity") or 0; a["in"]+=r.get("incomingQuantity") or 0; a["loc"]+=1
    out[cid]={"trunc":tr,"items":{f"{k[0]}|{k[1]}":v for k,v in agg.items() if any(v[x] for x in ("on","av","cm","in"))}}
json.dump(out,open("levels_1103am.json","w"),indent=1)
for c,v in out.items():
    print("==",c, v if isinstance(v,str) else v["trunc"])
    if not isinstance(v,str):
        for k,x in v["items"].items(): print("  ",k,x)
