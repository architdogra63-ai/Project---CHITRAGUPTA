// Usage: node export-vault.js "C:/path/to/YourVault"  ->  writes graph.json (same schema as the backend API)
// Each .md note = a node. [[wikilinks]] = edges.
// Optional frontmatter in a note:  type: person|account|device|phone|location   conflict: [[Other Note]]
const fs = require('fs'), path = require('path');
const root = process.argv[2]; if (!root) { console.log('Give vault path'); process.exit(1); }
const files = [];
(function walk(d){ for (const f of fs.readdirSync(d)) {
  if (f.startsWith('.')) continue;
  const p = path.join(d,f);
  fs.statSync(p).isDirectory() ? walk(p) : f.endsWith('.md') && files.push(p);
}})(root);

const names = files.map(f => path.basename(f, '.md'));
const idx = Object.fromEntries(names.map((n,i)=>[n.toLowerCase(),i]));
const edges = [], seen = new Set(), degree = names.map(()=>0);
const nodes = files.map((f,i) => {
  const txt = fs.readFileSync(f,'utf8');
  const type = (txt.match(/^type:\s*(person|account|device|phone|location)\s*$/mi)||[])[1] || 'person';
  const body = txt.replace(/^---[\s\S]*?---/,'').trim();
  for (const m of txt.matchAll(/\[\[([^\]|#]+)/g)) {
    const j = idx[m[1].trim().toLowerCase()];
    if (j === undefined || j === i) continue;
    const key = [Math.min(i,j),Math.max(i,j)].join('-');
    if (seen.has(key)) continue; seen.add(key);
    const conflict = new RegExp('conflict:.*\\[\\['+m[1].trim()+'\\]\\]','i').test(txt);
    edges.push({ source: names[i], target: names[j], label: conflict ? 'conflict' : 'link', conflict }); degree[i]++; degree[j]++;
  }
  return { id: names[i], label: names[i], type, description: body.split('\n')[0].slice(0,160) };
});
nodes.forEach((n,i)=> n.size = 10 + Math.min(degree[i]*3, 14));
fs.writeFileSync('graph.json', JSON.stringify({nodes,edges},null,1));
console.log(nodes.length+' nodes, '+edges.length+' edges -> graph.json');
