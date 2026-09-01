const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const PORT = process.env.PORT || 10000;
const HOST = '0.0.0.0';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml'
};

function safePath(urlPath) {
  const clean = decodeURIComponent((urlPath || '/').split('?')[0]);
  const rel = clean === '/' ? 'index.html' : clean.replace(/^\/+/, '');
  const full = path.resolve(ROOT, rel);
  return full.startsWith(ROOT) ? full : null;
}

const server = http.createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, {'Content-Type': 'application/json; charset=utf-8'});
    res.end(JSON.stringify({ok:true}));
    return;
  }

  let file = safePath(req.url);
  if (!file) {
    res.writeHead(400);
    res.end('Bad request');
    return;
  }

  fs.stat(file, (err, stat) => {
    if (err || !stat.isFile()) file = path.join(ROOT, 'index.html');
    fs.readFile(file, (readErr, data) => {
      if (readErr) {
        res.writeHead(500, {'Content-Type': 'text/plain; charset=utf-8'});
        res.end('Server error');
        return;
      }
      const ext = path.extname(file).toLowerCase();
      res.writeHead(200, {
        'Content-Type': TYPES[ext] || 'application/octet-stream',
        'Cache-Control': ext === '.html' ? 'no-store' : 'public, max-age=3600'
      });
      res.end(data);
    });
  });
});

server.listen(PORT, HOST, () => {
  console.log(`ETH Radar listening on http://${HOST}:${PORT}`);
});
