"""Demo UI: same query, different users, different answers. Run: python3 demo_server.py [port]"""
import json
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from permission_rag import PermissionRAG

USERS = {
    "alice": {"id": "alice", "groups": ["eng"]},
    "bob": {"id": "bob", "groups": ["hr"]},
    "carol": {"id": "carol", "groups": ["eng", "exec"]},
    "guest": {"id": "guest", "groups": []},
}

rag = PermissionRAG()
rag.add_document("handbook", "Company handbook: vacation policy is twenty days per year. Remote work is allowed three days per week. Expense reports are due monthly.", {"*"})
rag.add_document("arch", "Engineering architecture doc: the payments service uses Postgres with a Redis cache. Deploys go through the staging cluster. On-call rotation is weekly.", {"group:eng"})
rag.add_document("salaries", "HR confidential: salary bands range from 90k for L1 to 250k for L6. Annual raises average four percent. Bonus pool is fifteen percent of profit.", {"group:hr"})
rag.add_document("merger", "Executive memo: the acquisition of Acme Corp closes next quarter at a valuation of 40 million. Do not discuss externally.", {"group:exec"})

PAGE = """<!doctype html><meta charset="utf-8"><title>Permission-Aware RAG</title>
<style>body{font-family:system-ui;max-width:720px;margin:2rem auto;padding:0 1rem}
.r{border:1px solid #ccc;border-radius:8px;padding:.6rem .8rem;margin:.5rem 0}
.meta{color:#666;font-size:.85em}.denied{color:#a00}</style>
<h1>Permission-Aware RAG</h1>
<p>Same corpus, same query &mdash; results depend on who is asking. Denied chunks are filtered <em>before</em> ranking.</p>
<form onsubmit="q(event)">
<select id="u"><option>alice (eng)</option><option>bob (hr)</option><option>carol (eng+exec)</option><option>guest</option></select>
<input id="query" size="40" placeholder="try: salary bands / acquisition / vacation" required>
<button>Search</button></form><div id="out"></div>
<script>
async function q(e){e.preventDefault();
const u=document.getElementById('u').value.split(' ')[0];
const query=document.getElementById('query').value;
const res=await fetch('/query?user='+u+'&q='+encodeURIComponent(query));
const data=await res.json();
document.getElementById('out').innerHTML =
 '<p class="meta">'+data.results.length+' result(s), <span class="denied">'+data.denied_chunks+' chunk(s) hidden by ACL</span></p>'
 + data.results.map(r=>'<div class="r"><b>'+r.doc_id+'</b> <span class="meta">score '+r.score+'</span><br>'+r.text+'</div>').join('')
 || '<p>No accessible results.</p>';}
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/query":
            qs = parse_qs(url.query)
            user = USERS.get(qs.get("user", [""])[0])
            query = qs.get("q", [""])[0]
            if not user or not query:
                self.send_response(400); self.end_headers(); return
            results = rag.retrieve(query, user, k=3)
            body = json.dumps({"results": results,
                               "denied_chunks": rag.audit[-1]["denied_chunks"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE.encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8420
    print(f"http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
