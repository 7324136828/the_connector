import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { createServer as createHttpServer } from 'node:http';

let server;
let MessageItem;
let extractSpeechText;

before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: { server: createHttpServer() } }, appType: 'custom' });
  ({ MessageItem, extractSpeechText } = await server.ssrLoadModule('/src/components/MessageItem.jsx'));
});

after(async () => {
  await server?.close();
});

function render(content, message = {}) {
  return renderToStaticMarkup(React.createElement(MessageItem, {
    message: { role: 'assistant', content, ...message },
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

test('video JSON blocks render a player, thumbnail, and downloaded-video details', () => {
  const html = render('```video\n{"url":"https://cdn.example.test/movie.mp4","thumbnail":"https://cdn.example.test/thumb.jpg","title":"Demo clip","uploader":"Ava","description":"A short demo.","downloaded_date":"2026-09-24"}\n```');
  assert.match(html, /<video controls="" preload="metadata" poster="https:\/\/cdn\.example\.test\/thumb\.jpg">/);
  assert.match(html, /<source src="https:\/\/cdn\.example\.test\/movie\.mp4"/);
  assert.match(html, /<figcaption>Demo clip<\/figcaption>/);
  assert.match(html, /Uploaded by Ava/);
  assert.match(html, /Downloaded 2026-09-24/);
  assert.match(html, /A short demo\./);
});

test('tool video wrappers with malformed Markdown escapes render as players', () => {
  const payload = [{
    url: 'http://127.0.0.1:8010/api/media/demo/stream',
    thumbnail: 'http://127.0.0.1:8010/api/media/demo/thumbnail',
    title: 'イロドリ空 (Irodori Sora)',
    description: null,
    uploader: 'Rhit Keiichi',
    downloaded_date: '2026-09-24T05:16:12.187470+00:00',
  }];
  const content = JSON.stringify({
    video_block: `\`video\n${JSON.stringify(payload)}\n\``,
  }).replace('"video_block"', String.raw`"video\_block"`);
  const html = render(content);

  assert.match(html, /<video controls="" preload="metadata" poster="http:\/\/127\.0\.0\.1:8010\/api\/media\/demo\/thumbnail">/);
  assert.match(html, /<source src="http:\/\/127\.0\.0\.1:8010\/api\/media\/demo\/stream"/);
  assert.match(html, /<figcaption>イロドリ空 \(Irodori Sora\)<\/figcaption>/);
  assert.match(html, /Uploaded by Rhit Keiichi/);
  assert.doesNotMatch(html, /video_block|video\\_block/);
});

test('video blocks reject unsafe media URLs and remain inspectable as code', () => {
  const html = render('```video\n{"url":"javascript:alert(1)","title":"Unsafe"}\n```');
  assert.doesNotMatch(html, /<video|<source/);
  assert.match(html, /<span>video<\/span>/);
  assert.match(html, /javascript:alert\(1\)/);
});

test('agent reasoning trace is retained but collapsed by default', () => {
  const html = render('Final answer only.', {
    isAgent: true,
    agentSteps: [{ step: 1, thought: 'Internal reasoning summary', tool: null }],
  });
  assert.match(html, /<button type="button" class="agent-steps-header" aria-expanded="false">/);
  assert.match(html, /Reasoning &amp; Tool Trace \(1 step\)/);
  assert.match(html, /Expand/);
  assert.doesNotMatch(html, /Internal reasoning summary|step-card/);
  assert.match(html, /Final answer only\./);
});

test('speech is offered for every non-empty assistant response', () => {
  assert.equal(extractSpeechText('{"text":"  Read only this.  ","thought":"Do not read this."}'), 'Read only this.');
  assert.equal(extractSpeechText('  plain model response  '), 'plain model response');
  assert.equal(extractSpeechText('{"final_answer":"Not the text field"}'), null);
  assert.equal(extractSpeechText('{"text":42}'), null);
  assert.equal(extractSpeechText('```json\n{"text":"fenced"}\n```'), '```json\n{"text":"fenced"}\n```');

  const speakable = render('{"text":"Read only this.","thought":"Do not read this."}');
  assert.match(speakable, />Speak<\/button>/);
  assert.match(render('plain model response'), />Speak<\/button>/);
  assert.match(render('{"final_answer":"Unsupported JSON reports an error after click"}'), />Speak<\/button>/);
  assert.doesNotMatch(render(''), />Speak<\/button>/);
  assert.doesNotMatch(render('{"text":"User text"}', { role: 'user' }), />Speak<\/button>/);
});
