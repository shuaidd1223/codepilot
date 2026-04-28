import * as Lark from '@larksuiteoapi/node-sdk';

const appId = process.env.CODEPILOT_FEISHU_APP_ID || '';
const appSecret = process.env.CODEPILOT_FEISHU_APP_SECRET || '';

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => {
      data += chunk;
    });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

if (!appId || !appSecret) {
  console.error('Missing CODEPILOT_FEISHU_APP_ID or CODEPILOT_FEISHU_APP_SECRET');
  process.exit(1);
}

const raw = await readStdin();
const payload = JSON.parse(raw || '{}');
const chatIds = Array.isArray(payload.chat_ids) ? payload.chat_ids.filter(Boolean) : [];
const card = payload.card;
const text = String(payload.text || '');

function buildPostContent(title, body) {
  const paragraphs = String(body || '')
    .split(/\n+/)
    .map(line => line.trim())
    .filter(Boolean)
    .slice(0, 20)
    .map(line => [{ tag: 'text', text: line }]);
  return {
    zh_cn: {
      title: String(title || 'CodePilot 通知').slice(0, 120),
      content: paragraphs.length ? paragraphs : [[{ tag: 'text', text: 'CodePilot 通知' }]],
    },
  };
}

if (!chatIds.length) {
  console.log(JSON.stringify({ ok: true, sent: 0 }));
  process.exit(0);
}

const client = new Lark.Client({
  appId,
  appSecret,
  appType: Lark.AppType.SelfBuild,
  domain: Lark.Domain.Feishu,
});

let sent = 0;
const failures = [];
for (const chatId of chatIds) {
  try {
    await client.im.message.create({
      params: { receive_id_type: 'chat_id' },
      data: {
        receive_id: chatId,
        msg_type: card ? 'interactive' : 'post',
        content: JSON.stringify(card || buildPostContent('CodePilot 通知', text)),
      },
    });
    sent += 1;
  } catch (error) {
    failures.push({
      chat_id: chatId,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

console.log(JSON.stringify({ ok: failures.length === 0, sent, failures }));
