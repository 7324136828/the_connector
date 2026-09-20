import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';

let server;
let MessageItem;

before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ MessageItem } = await server.ssrLoadModule('/src/components/MessageItem.jsx'));
});

after(async () => {
  await server?.close();
});

function render(content) {
  return renderToStaticMarkup(React.createElement(MessageItem, {
    message: { role: 'assistant', content },
  }));
}

test('renders headings, paragraphs, emphasis, nested lists and quotes as Markdown', () => {
  const html = render('# Heading\n\nA **bold** paragraph with *emphasis* and `inline code`.\n\n- Parent\n  - Child\n\n1. First\n2. Second\n\n> A quotation');
  assert.match(html, /<h1>Heading<\/h1>/);
  assert.match(html, /<p>A <strong>bold<\/strong> paragraph with <em>emphasis<\/em> and <code>inline code<\/code>\.<\/p>/);
  assert.match(html, /<li>Parent\n<ul>\n<li>Child<\/li>/);
  assert.match(html, /<ol>\n<li>First<\/li>\n<li>Second<\/li>/);
  assert.match(html, /<blockquote>\n<p>A quotation<\/p>/);
});

test('renders GFM tables, task lists and strikethrough', () => {
  const html = render('| Name | Value |\n| --- | --- |\n| count | 3 |\n\n- [x] Done\n- [ ] Pending\n\n~~removed~~');
  assert.match(html, /class="markdown-table-wrapper"><table>/);
  assert.match(html, /<th>Name<\/th>/);
  assert.match(html, /<td>3<\/td>/);
  assert.match(html, /type="checkbox" disabled="" checked=""/);
  assert.match(html, /<del>removed<\/del>/);
});

test('unlabelled code keeps its first line, indentation, blank lines and trailing spaces', () => {
  const html = render('```\nfirst line\n  indented  \n\nlast line\n```');
  assert.match(html, /<pre><code>first line\n  indented  \n\nlast line\n<\/code><\/pre>/);
  assert.match(html, />Copy code<\/button>/);
  assert.match(html, /<span>code<\/span>/);
});

test('language-labelled and unfinished streaming code fences render safely', () => {
  const html = render('```js\nconst tag = "<div>";\n');
  assert.match(html, /<span>js<\/span>/);
  assert.match(html, /<code class="language-js">const tag = &quot;&lt;div&gt;&quot;;\n<\/code>/);
});

test('safe links open with isolation and dangerous URLs and HTML are not executed', () => {
  const html = render('[Docs](https://example.com/docs)\n\n[Unsafe](javascript:alert%281%29)\n\n<img src="x" onerror="alert(1)">\n\n<script>alert(1)</script>');
  assert.match(html, /href="https:\/\/example.com\/docs" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(html, /href="javascript:|<script|onerror=|<img/);
});
