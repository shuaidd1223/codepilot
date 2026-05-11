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
      cwd: process.cwd(),
      env: process.env,
    },
  );
  if (result.error) {
    throw result.error;
  }
  const parsed = parsePythonReply(result.stdout || '');
  if (parsed) {
    return parsed;
  }
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || '').trim() || `python exit=${result.status}`);
  }
  throw new Error('python handler did not return valid JSON');
}

function parsePythonReply(stdout) {
  const raw = String(stdout || '').trim();
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {}
  const lines = raw
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      return JSON.parse(lines[index]);
    } catch {}
  }
  return null;
}

function buildErrorCard(text) {
  return {
    type: 'interactive',
    card: {
      config: { wide_screen_mode: true },
      header: {
        title: { tag: 'plain_text', content: 'CodePilot 飞书处理失败' },
        template: 'red',
      },
      elements: [
        {
          tag: 'div',
          text: { tag: 'lark_md', content: String(text || 'CodePilot 飞书处理失败。') },
        },
      ],
    },
  };
}

function buildProcessingCard(text) {
  const raw = String(text || '').trim();
  const displayText = raw
    ? `已收到您的消息「${raw.slice(0, 200)}」，正在处理中，请稍候...`
    : '已收到您的消息，正在处理中，请稍候...';
  return {
    type: 'interactive',
    card: {
      config: { wide_screen_mode: true },
      header: {
        title: { tag: 'plain_text', content: 'CodePilot 处理中' },
        template: 'blue',
      },
      elements: [
        {
          tag: 'div',
          text: { tag: 'lark_md', content: displayText },
        },
        {
          tag: 'note',
          elements: [
            { tag: 'plain_text', content: '智能体正在处理中，处理完成后会自动更新此卡片。' },
          ],
        },
      ],
    },
  };
}

async function processIncomingMessage(payload) {
  const chatId = payload.chat_id;
  let processingMsgId = null;
  try {
    // 先发送处理中反馈，让用户立即感知消息已被接收，并记录卡片 message_id
    processingMsgId = await sendReply(chatId, buildProcessingCard(payload.text));
    const reply = invokePython(payload);

    // 处理完成后，优先更新已有的处理中卡片（一条消息从"处理中"变成最终结果）
    if (reply && reply.type !== 'ignore') {
      if (processingMsgId && reply.type === 'interactive' && reply.card) {
        // 卡片回复 → 直接更新处理中卡片为最终结果
        await updateReply(chatId, processingMsgId, reply.card);
      } else if (processingMsgId) {
        // 非卡片回复 → 更新处理中卡片标记完成，再发送实际结果
        const doneCard = buildProcessingCard('处理完成，请查看下方回复。');
        doneCard.card.header.template = 'green';
        await updateReply(chatId, processingMsgId, doneCard.card);
        await sendReply(chatId, reply);
      } else {
        // 没有处理中卡片 message_id（极少见），直接发送结果
        await sendReply(chatId, reply);
      }
    }
    // reply.type === 'ignore'：什么都不做，处理中卡片保留在聊天中
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    log('handler failed', { chatId, detail });
    try {
      if (processingMsgId) {
        // 更新处理中卡片为错误状态
        await updateReply(chatId, processingMsgId, buildErrorCard(`CodePilot 飞书处理失败：${detail}`).card);
      } else {
        await sendReply(chatId, buildErrorCard(`CodePilot 飞书处理失败：${detail}`));
      }
    } catch (replyError) {
      log('failed to send error reply', {
        chatId,
        detail: replyError instanceof Error ? replyError.message : String(replyError),
      });
    }
  }
}

function cardActionEvent(data) {
  const event = data?.event && typeof data.event === 'object' ? data.event : data;
  return event && typeof event === 'object' ? event : {};
}

function cardActionChatId(event) {
  return String(
    event?.chat_id
    || event?.open_chat_id
    || event?.context?.open_chat_id
    || event?.context?.chat_id
    || '',
  );
}

function cardActionMessageId(event) {
  return String(
    event?.message_id
    || event?.open_message_id
    || event?.context?.open_message_id
    || '',
  );
}

function cardActionCommand(event) {
  const value = event?.action?.value;
  if (!value || typeof value !== 'object') return '';
  return String(value.command || value.cmd || value.text || '').trim();
}

async function processCardAction(data) {
  const event = cardActionEvent(data);
  const chatId = cardActionChatId(event);
  const command = cardActionCommand(event);
  if (!chatId || !command) {
    log('ignore card action without command', { chatId, command });
    return { toast: { type: 'warning', content: '这个按钮没有可执行命令' } };
  }
  let processingMsgId = null;
  try {
    processingMsgId = await sendReply(chatId, buildProcessingCard(command));
    const reply = invokePython({
      event_type: 'card.action.trigger',
      text: command,
      chat_id: chatId,
      message_id: cardActionMessageId(event),
      event_id: data?.header?.event_id || data?.event_id || '',
      action: event.action || {},
      context: event.context || {},
      operator: event.operator || {},
    });
    if (reply && reply.type !== 'ignore') {
      if (processingMsgId && reply.type === 'interactive' && reply.card) {
        await updateReply(chatId, processingMsgId, reply.card);
      } else if (processingMsgId) {
        const doneCard = buildProcessingCard('操作已完成。');
        doneCard.card.header.template = 'green';
        await updateReply(chatId, processingMsgId, doneCard.card);
        await sendReply(chatId, reply);
      } else {
        await sendReply(chatId, reply);
      }
    }
    return { toast: { type: 'success', content: '已执行' } };
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    log('card action failed', { chatId, detail });
    try {
      if (processingMsgId) {
        await updateReply(chatId, processingMsgId, buildErrorCard(`CodePilot 飞书处理失败：${detail}`).card);
      } else {
        await sendReply(chatId, buildErrorCard(`CodePilot 飞书处理失败：${detail}`));
      }
    } catch (replyError) {
      log('failed to send card action error reply', {
        chatId,
        detail: replyError instanceof Error ? replyError.message : String(replyError),
      });
    }
    return { toast: { type: 'error', content: '执行失败' } };
  }
}

function buildPostContent(title, text, content = null) {
  if (Array.isArray(content)) {
    return {
      zh_cn: {
        title: String(title || 'CodePilot 回复').slice(0, 120),
        content,
      },
    };
  }
  const paragraphs = String(text || '')
    .split(/\n+/)
    .map(line => line.trim())
    .filter(Boolean)
    .slice(0, 20)
    .map(line => [{ tag: 'text', text: line }]);
  return {
    zh_cn: {
      title: String(title || 'CodePilot 回复').slice(0, 120),
      content: paragraphs.length ? paragraphs : [[{ tag: 'text', text: 'CodePilot 已收到。' }]],
    },
  };
}

async function sendReply(chatId, reply) {
  if (!reply || reply.type === 'ignore') {
    log('skip reply', { chatId, reason: 'ignore' });
    return null;
  }
  if (reply.type === 'multi' && Array.isArray(reply.messages)) {
    log('send multi reply', { chatId, count: reply.messages.length });
    let lastMsgId = null;
    for (const message of reply.messages) {
      lastMsgId = await sendReply(chatId, message);
    }
    return lastMsgId;
  }
  let res;
  if (reply.type === 'interactive' && reply.card) {
    log('send interactive reply', { chatId });
    res = await client.im.message.create({
      params: { receive_id_type: 'chat_id' },
      data: {
        receive_id: chatId,
        msg_type: 'interactive',
        content: JSON.stringify(reply.card),
      },
    });
    return res?.data?.message_id || null;
  }
  if (reply.type === 'post') {
    const title = String(reply.title || 'CodePilot 详情');
    log('send post reply', { chatId, title: title.slice(0, 80) });
    res = await client.im.message.create({
      params: { receive_id_type: 'chat_id' },
      data: {
        receive_id: chatId,
        msg_type: 'post',
        content: JSON.stringify(buildPostContent(title, reply.text || '', reply.content || null)),
      },
    });
    return res?.data?.message_id || null;
  }
  const text = String(reply.text || 'CodePilot 已收到，但没有可发送的结果。');
  log('send rich text reply', { chatId, preview: text.slice(0, 80) });
  res = await client.im.message.create({
    params: { receive_id_type: 'chat_id' },
    data: {
      receive_id: chatId,
      msg_type: 'post',
      content: JSON.stringify(buildPostContent('CodePilot 回复', text)),
    },
  });
  return res?.data?.message_id || null;
}

async function updateReply(chatId, messageId, card) {
  if (!messageId || !card) {
    log('skip update reply', { chatId, reason: !messageId ? 'no message_id' : 'no card' });
    return;
  }
  log('update interactive reply', { chatId, messageId });
  await client.im.message.update({
    params: { message_id: messageId },
    data: {
      msg_type: 'interactive',
      content: JSON.stringify(card),
    },
  });
}

const dispatcher = new Lark.EventDispatcher({}).register({
  'im.message.receive_v1': (data) => {
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
    const eventId = data?.header?.event_id || data?.event_id || '';
    log('received text message', { chatId, event_id: eventId, message_id: message.message_id || '', text });
    void processIncomingMessage({
      text,
      chat_id: chatId,
      message_id: message.message_id || '',
      event_id: eventId,
    });
  },
  'card.action.trigger': async (data) => {
    const event = cardActionEvent(data);
    log('received card action', {
      chat_id: cardActionChatId(event),
      message_id: cardActionMessageId(event),
      command: cardActionCommand(event),
    });
    return processCardAction(data);
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
