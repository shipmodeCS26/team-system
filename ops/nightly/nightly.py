import ssk_source as s, json, collections, sys
from datetime import datetime, timezone, timedelta, date
import os
S=os.environ.get("NIGHTLY_DIR", os.path.dirname(os.path.abspath(__file__)))+"/"
from zoneinfo import ZoneInfo
ET=ZoneInfo("America/New_York"); RD=date.fromisoformat(sys.argv[1]); START=RD-timedelta(days=28)
MAP={"muravai":{"3 filters":{"MUR001":1},"showerhead":{"MUR002":1},"hose and connector set":{"MUR003":1},"showerhead + hose and connector set":{"MUR002":1,"MUR003":1},"shower hose":{"MUR004":1},"shower connector":{"MUR005":1}},
"fascial_labs":{"FASCSUPP-1":{"TF":1},"FASCSUPPx2":{"TF":2},"FAC3XBDL":{"TF":3},"TrueForm Bundle-3":{"TF":2,"TS":1},"TrueSleep-gummies-shipmode":{"TS":1},"TrueSleep-gummies-shipmodex3":{"TS":3},"FASCIAL D3+K2 SM":{"D3K2":1},"FASCIAL D3+K2 SM x3":{"D3K2":3}},
"nuerosmile":{"NEURO-120":{"NEURO-120":1},"MAG-SPRAY-360":{"MAG-SPRAY-360":1},"PILL-CARRIER-360":{"PILL-CARRIER-360":1}},
"puravita":{"CAP-MAGNESIUM-360":{"CAP":1},"CAP-MAGNESIUM-360x3":{"CAP":3},"CAP-MAGNESIUM-360x5":{"CAP":5}},
"claritymd":{"Astaxanthin - Gels - 360":{"ASTA":1},"Astaxanthin - Gels - 360x2":{"ASTA":2},"Astaxanthin - Gels - 360x3":{"ASTA":3},"Astaxanthin - Gels - 360x5":{"ASTA":5}}}
res={}
for c,m in MAP.items():
    k=s.api_key(c); units=collections.defaultdict(collections.Counter); orders=collections.Counter(); unm=collections.Counter(); page=1; stop=False
    while page<=300 and not stop:
        b=s._get(k,"/shipments",{"sortOrder":"desc","limit":100,"page":page})
        for r in b["data"]:
            d=datetime.fromisoformat(r["createdAt"].replace("Z","+00:00")).astimezone(ET).date()
            if d<START: stop=True; continue
            if r.get("voidStatus"): continue
            orders[str(d)]+=1
            for p in r.get("packages") or []:
                for li in p.get("lineItems") or []:
                    sku=((li.get("productVariant") or {}).get("sku") or "").strip(); q=li.get("quantity") or 0
                    if sku in m:
                        for u,x in m[sku].items(): units[str(d)][u]+=x*q
                    else: unm[(str(d),sku)]+=q
        if not b["data"] or not b.get("hasMore",len(b["data"])==100): break
        page+=1
    rows,tr=s.get_all(k,"/inventory/levels")
    lv=[]
    for r in rows:
        v=r.get("productVariant") or r.get("variant") or {}
        lv.append({kk:(r.get(kk+"Quantity") if r.get(kk+"Quantity") is not None else r.get(kk)) for kk in ("onHand","available","committed","incoming","damaged","reserved")}|{"sku":(v.get("sku") or r.get("sku")),"title":v.get("title") or r.get("title")})
    if not lv and orders: raise SystemExit(f"{c}: no inventory levels returned; stop and re-pull before the audit")
    days=sorted(orders); last7=[d for d in days if d>str(RD-timedelta(days=7))]
    tot=collections.Counter(); t7=collections.Counter()
    for d in days:
        for u,v in units[d].items():
            tot[u]+=v
            if d in last7: t7[u]+=v
    res[c]={"rd_orders":orders.get(str(RD),0),"rd_units":dict(units.get(str(RD),{})),"orders_by_day":dict(orders),
      "units_by_day":{d:dict(units[d]) for d in days},"avg28_shipday":{u:round(v/max(len(days),1),1) for u,v in tot.items()},
      "avg28_cal":{u:round(v/28,1) for u,v in tot.items()},"avg7_cal":{u:round(v/7,1) for u,v in t7.items()},"shipdays28":len(days),
      "unmapped":{f"{a}|{b}":q for (a,b),q in unm.items()},"levels":[x for x in lv if any((x[f] or 0) for f in ("onHand","available","committed","incoming","damaged","reserved"))],"lv_trunc":tr}
    r=res[c]; print("==",c,"RD",r["rd_orders"],r["rd_units"],"| 28d/shipday",r["avg28_shipday"],"| 7d/cal",r["avg7_cal"],"| shipdays",r["shipdays28"],"| unmapped",{k:v for k,v in r["unmapped"].items() if k.startswith(str(RD))})
    print("   last days",[(d[5:],orders[d]) for d in days[-8:]])
    for x in r["levels"]: print("   LV",x)
json.dump(res,open(S+f"nightly_{RD}.json","w"))
