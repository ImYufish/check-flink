import json, sys
d = json.load(sys.stdin)
ls = d.get('link_status', [])
print('link_status 数量:', len(ls))
print('timestamp:', d.get('timestamp'))
print('total_count:', d.get('total_count'))
print('首条友链:', ls[0].get('name') if ls else '<空>')
if ls:
    print('首条 siteshot:', (ls[0].get('siteshot') or '')[:80])
s = sum(1 for x in ls if x.get('siteshot'))
print('有 siteshot 的友链:', s, '/', len(ls))
