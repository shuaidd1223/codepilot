import * as Lark from '@larksuiteoapi/node-sdk';
import { spawnSync } from 'node:child_process';

const appId = process.env.CODEPILOT_FEISHU_APP_ID || '';
const appSecret = process.env.CODEPILOT_FEISHU_APP_SECRET || '';
const pythonCmd = process.env.CODEPILOT_FEISHU_PYTHON || 'python';

if (!appId || !appSecret) {
  console.error('Missing CODEPILOT_FEISHU_APP_ID or CODEPILOT_FEISHU_APP_SECRET');
  process.exit(1);
}

const client = new Lark.Client({
  appId,
  appSecret,
  appType: Lark.AppType.SelfBuild,
  domain: Lark.Domain.Feishu,
});

function log(message, extra) {
  if (typeof extra === 'undefined') {
    console.log(`[feishu-worker] ${message}`);
    return;
  }
  console.log(`[feishu-worker] ${message}`, extra);
}

function extractText(rawContent) {
  if (!rawContent) return '';
  try {
    const parsed = JSON.parse(rawContent);
    if (parsed && typeof parsed.text === 'string') {
      return parsed.text;
    }
  } catch {}
  return String(rawContent);
}

function invokePython(payload) {
  log('dispatch python handler', { chat_id: payload.chat_id, text: payload.text });
  const result = spawnSync(
    pythonCmd,
    ['-m', 'codepilot', 'feishu', 'handle-event'],
    {
      input: JSON.stringify(payload),
      encoding: 'utf8',
      maxBuffer: 1024 * 1024,
      env: process.env,
    },
  );
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || '').trim() || `python exit=${result.status}`);
  }
  return JSON.parse(result.stdout || '{}');
}

async function sendReply(chatId, reply) {
  if (!reply || reply.type === 'ignore') {
    log('skip reply', { chatId, reason: 'ignore' });
    return;
  }
  if (reply.type === 'interactive' && reply.card) {
    log('send interactive reply', { chatId });
    await client.im.message.create({
      params: { receive_id_type: 'chat_id' },
      data: {
        receive_id: chatId,
        msg_type: 'interactive',
        content: JSON.stringify(reply.card),
      },
    });
    return;
  }
  const text = String(reply.text || 'CodePilot 已收到，但没有可发送的结果。');
  log('send text reply', { chatId, preview: text.slice(0, 80) });
  await client.im.message.create({
    params: { receive_id_type: 'chat_id' },
    data: {
      receive_id: chatId,
      msg_type: 'text',
      content: JSON.stringify({ text }),
    },
  });
}

const dispatcher = new Lark.EventDispatcher({}).register({
  'im.message.receive_v1': async (data) => {
    const message = data?.message || {};
    if (String(message.message_type || '').toLowerCase() !== 'text') {
      log('ignore non-text message', { message_type: message.message_type || '' });
      return;
    }
    const chatId = message.chat_id;
    const text = extractText(message.content);
    if (!chatId || !text.trim()) {
      log('ignore empty message', { chatId, text });
      return;
    }
    log('received text message', { chatId, message_id: message.message_id || '', text });
    try {
      const reply = invokePython({
        text,
        chat_id: chatId,
        message_id: message.message_id || '',
      });
      await sendReply(chatId, reply);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      log('handler failed', { chatId, detail });
      await sendReply(chatId, { type: 'text', text: `CodePilot 飞书处理失败：${detail}` });
    }
  },
});

const wsClient = new Lark.WSClient({
  appId,
  appSecret,
  loggerLevel: Lark.LoggerLevel.info,
});

process.on('uncaughtException', (error) => {
  console.error('[feishu-worker] uncaughtException', error);
});

process.on('unhandledRejection', (error) => {
  console.error('[feishu-worker] unhandledRejection', error);
});

log('starting long connection');
wsClient.start({ eventDispatcher: dispatcher });
