/** npm preview server; proxies API calls to the Python backend. */
import http from 'node:http';
import { readFile } from 'node:fs/promises';

const backend = new URL(process.env.COUNT_MESSAGE_BACKEND_URL || 'http://127.0.0.1:8403');
const assets = { '/': ['index.html', 'text/html'], '/index.html': ['index.html', 'text/html'], '/src/index.js': ['src/index.js', 'text/javascript'] };
const server = http.createServer(async (req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname;
  if (path === '/api/count_message' || path === '/health') {
    const upstream = http.request(new URL(req.url, backend), { method: req.method, headers: { 'content-type': req.headers['content-type'] || 'application/json' } }, (response) => {
      res.writeHead(response.statusCode, { 'content-type': response.headers['content-type'] || 'application/json', 'cache-control': 'no-store' });
      response.pipe(res);
    });
    upstream.setTimeout(10000, () => upstream.destroy(new Error('Backend timeout')));
    upstream.on('error', () => { if (!res.headersSent) res.writeHead(503, { 'content-type': 'application/json' }); res.end(JSON.stringify({ detail: 'count_message backend unavailable' })); });
    req.pipe(upstream);
    return;
  }
  if (req.method !== 'GET' || !assets[path]) { res.writeHead(404); res.end('Not found'); return; }
  try {
    const [file, mime] = assets[path];
    const content = await readFile(new URL(file, import.meta.url));
    res.writeHead(200, { 'content-type': `${mime}; charset=utf-8` });
    res.end(content);
  } catch { res.writeHead(404); res.end('Not found'); }
});
server.listen(Number(process.env.COUNT_MESSAGE_UI_PORT || 5133), '127.0.0.1', () => {
  console.log(`count_message UI: http://127.0.0.1:${server.address().port}`);
});
