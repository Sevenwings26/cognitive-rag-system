import urllib.request
import json
import traceback

url = 'http://127.0.0.1:4500/chat/query'
payload = {'query': 'Who is the customer associated with Bank Verification Number (BVN) 90000001008?'}
data = json.dumps(payload).encode('utf-8')
req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')

try:
    print('Sending POST /chat/query...')
    with urllib.request.urlopen(req, timeout=90) as resp:
        print('HTTP STATUS:', resp.status)
        result = json.loads(resp.read().decode('utf-8'))
        print('RESPONSE STATUS:', result.get('status'))
        print('ANSWER:\n', result.get('answer'))
        print('SOURCES COUNT:', len(result.get('sources', [])))
        print('IS GROUNDED:', result.get('is_grounded'))
except Exception as e:
    print('EXCEPTION OCCURRED:', e)
    traceback.print_exc()
