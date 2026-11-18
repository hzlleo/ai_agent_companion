"""douyin 通道页面脚本——抖音 web 会话 DOM 采集 / 发送的 JS 表达式。

本模块只产出**字符串**（交给浏览器 sidecar 的 ``evaluate`` 工具在页面上下文
执行），不依赖任何浏览器对象；参数经 JSON 序列化嵌入脚本，因此文本里的引号
/ 换行 / 反斜杠不需要额外转义。

DOM 结构的现实约束（文档化边界）：抖音 web 的会话面板没有公开稳定的 DOM
契约，选择器按「候选列表逐个尝试 + ``data-*`` 属性读取 + 文本哈希兜底」
提取。真实页面的选择器命中情况属**部署机一次性验证面**（见
docs/backend/channels.md「抖音 web 通道」部署要点）；命中失败时脚本返回
空消息列表（采集面空转）或可读错误（发送面），通道循环不因此中断。

脚本契约（与 :mod:`packages.harness.channels.douyin.douyin_models` 对齐）：

    采集 → ``{"conversation_id": str, "messages": [{"message_id", "sender_id",
            "text", "ts_ms", "is_self", "is_system", "is_group", "image_urls"}]}``
    发送 → ``{"ok": bool, "text": str}`` 或 ``{"ok": bool, "error": str}``
"""

from __future__ import annotations

import json
from typing import Final

__all__ = [
    "DOUYIN_COMPOSER_SELECTORS",
    "DOUYIN_MESSAGE_NODE_SELECTORS",
    "DOUYIN_SEND_BUTTON_SELECTORS",
    "build_douyin_collect_script",
    "build_douyin_send_script",
]

#: 会话消息节点的候选选择器（按序尝试，首个命中非空集合即用）。
DOUYIN_MESSAGE_NODE_SELECTORS: Final[tuple[str, ...]] = (
    '[data-e2e="message-item"]',
    '[data-e2e="chat-message"]',
    '[class*="messageItem"]',
    '[class*="message-item"]',
)

#: 输入框候选选择器（可编辑区域优先——抖音 web 输入区是 contenteditable）。
DOUYIN_COMPOSER_SELECTORS: Final[tuple[str, ...]] = (
    '[data-e2e="chat-input"] [contenteditable="true"]',
    '[contenteditable="true"]',
    'textarea',
)

#: 发送按钮候选选择器（空集合时回落到输入框回车事件）。
DOUYIN_SEND_BUTTON_SELECTORS: Final[tuple[str, ...]] = (
    '[data-e2e="send-button"]',
    'button[class*="send"]',
    '[class*="send"] button',
)

_COLLECT_TEMPLATE: str = """(() => {
  const args = __DOUYIN_ARGS__;
  const messageSelectors = __DOUYIN_MESSAGE_SELECTORS__;
  // 兜底标识：DOM 未提供 data-message-id 时用「发送者 + 时间 + 文本」哈希，
  // 同一消息在多次采集间稳定（去重依赖这个稳定性）。
  const hashOf = (value) => {
    let hash = 5381;
    for (let i = 0; i < value.length; i += 1) {
      hash = ((hash << 5) + hash + value.charCodeAt(i)) >>> 0;
    }
    return hash.toString(16);
  };
  const nodes = [];
  for (const selector of messageSelectors) {
    const found = Array.from(document.querySelectorAll(selector));
    if (found.length > 0) {
      nodes.push(...found);
      break;
    }
  }
  const messages = nodes
    .slice(-args.limit)
    .map((node) => {
      const dataset = node.dataset || {};
      const contentNode =
        node.querySelector('[class*="content"], [class*="text"]') || node;
      const text = (contentNode.innerText || node.innerText || '').trim();
      const imageUrls = Array.from(node.querySelectorAll('img'))
        .map((img) => img.currentSrc || img.src || '')
        .filter((src) => src && !src.startsWith('data:'));
      const rawTs = Number(dataset.timestamp || dataset.ts || dataset.createTime || 0);
      const tsMs = rawTs > 0 && rawTs < 1e12 ? rawTs * 1000 : rawTs;
      const senderId = String(dataset.senderId || dataset.uid || '');
      const isSelf =
        node.matches('[data-is-self="true"], [class*="self"], [class*="right"]') ||
        node.closest('[data-is-self="true"], [class*="self"]') !== null;
      const isSystem =
        dataset.isSystem === 'true' || node.matches('[class*="system"]');
      return {
        message_id: String(
          dataset.messageId ||
            dataset.id ||
            'h-' + hashOf(senderId + '|' + tsMs + '|' + text)
        ),
        sender_id: senderId || 'unknown',
        text: text,
        ts_ms: Number.isFinite(tsMs) ? tsMs : 0,
        is_self: isSelf,
        is_system: isSystem,
        is_group: dataset.isGroup === 'true',
        image_urls: imageUrls,
      };
    })
    .filter((item) => item.message_id && (item.text || item.image_urls.length > 0));
  const params = new URLSearchParams(location.search);
  const conversationId = String(
    args.conversation_id ||
      params.get('conversationId') ||
      params.get('cid') ||
      ''
  );
  return {conversation_id: conversationId, messages: messages};
})()"""

_SEND_TEMPLATE: str = """(() => {
  const args = __DOUYIN_ARGS__;
  const composerSelectors = __DOUYIN_COMPOSER_SELECTORS__;
  const sendSelectors = __DOUYIN_SEND_BUTTON_SELECTORS__;
  let composer = null;
  for (const selector of composerSelectors) {
    const candidate = document.querySelector(selector);
    if (candidate) {
      composer = candidate;
      break;
    }
  }
  if (!composer) {
    return {ok: false, error: '未找到输入框（会话面板未打开？）'};
  }
  const text = String(args.text || '');
  composer.focus();
  if (composer.tagName === 'TEXTAREA' || composer.tagName === 'INPUT') {
    composer.value = text;
  } else {
    composer.textContent = text;
  }
  // 受控输入框需要 input 事件才会让前端状态同步（React / Vue 的合成事件面）。
  composer.dispatchEvent(
    new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'})
  );
  let sendButton = null;
  for (const selector of sendSelectors) {
    const candidate = document.querySelector(selector);
    if (candidate && !candidate.disabled) {
      sendButton = candidate;
      break;
    }
  }
  if (sendButton) {
    sendButton.click();
  } else {
    composer.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        code: 'Enter',
        keyCode: 13,
        which: 13,
        bubbles: true,
      })
    );
  }
  return {ok: true, text: text};
})()"""


def _js_literal(value: object) -> str:
    """把 Python 值序列化为可安全嵌入 JS 的 JSON 字面量。

    Args:
        value: 待嵌入的值（仅 JSON 可序列化类型）。

    Returns:
        JSON 文本（``ensure_ascii`` 打开——非 ASCII 全部转义，规避
        U+2028 / U+2029 在旧引擎字符串字面量中的兼容性问题）。
    """
    return json.dumps(value, ensure_ascii=True)


def build_douyin_collect_script(*, conversation_id: str, limit: int) -> str:
    """构造会话采集脚本（页面上下文读取会话 DOM）。

    Args:
        conversation_id: 期望的会话标识（空串 = 由页面 URL 参数推断）。
        limit: 采集消息上限（调用方按配置的 ``max_messages_per_poll`` 传入；
            脚本取时间序末尾的该数量）。

    Returns:
        自执行 JS 表达式字符串（``evaluate`` 工具参数 ``script``）。
    """
    args = _js_literal({"conversation_id": conversation_id, "limit": limit})
    script = _COLLECT_TEMPLATE.replace("__DOUYIN_ARGS__", args, 1)
    return script.replace(
        "__DOUYIN_MESSAGE_SELECTORS__", _js_literal(list(DOUYIN_MESSAGE_NODE_SELECTORS)), 1
    )


def build_douyin_send_script(*, text: str) -> str:
    """构造发送脚本（页面上下文写入输入框并触发发送）。

    Args:
        text: 待发送文本（空串合法——通道层出站模型已保证非空，此处只做
            字面量嵌入）。

    Returns:
        自执行 JS 表达式字符串；返回 ``{"ok": ..., "text"/"error": ...}``。
    """
    script = _SEND_TEMPLATE.replace("__DOUYIN_ARGS__", _js_literal({"text": text}), 1)
    script = script.replace(
        "__DOUYIN_COMPOSER_SELECTORS__",
        _js_literal(list(DOUYIN_COMPOSER_SELECTORS)),
        1,
    )
    return script.replace(
        "__DOUYIN_SEND_BUTTON_SELECTORS__",
        _js_literal(list(DOUYIN_SEND_BUTTON_SELECTORS)),
        1,
    )
