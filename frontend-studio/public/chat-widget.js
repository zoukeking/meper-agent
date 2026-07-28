/*!
 * AgentFlowChat embed loader —— 把 frontend-client 以 iframe 形态嵌入第三方站点。
 * 纯原生 JS（不进 React 构建、不引框架），放 public/ 由静态服务原样托管。
 *
 * 形态：右下角浮动启动器（AFLogo.png）→ 点击从右侧滑出 drawer（iframe 加载 frontend-client）。
 *
 * 鉴权（运行时注入，client 自身不内置 key）：握手时 client 发 agentflow:request_config，
 * 本脚本回 agentflow:config{apiKey, userToken?} 把以下凭据注入 iframe。
 *   data-api-key      必填，API Key（af_live_xxx）。
 *   data-token-cookie 可选，终端用户 token 的 cookie 名，默认 "mep-access-token"。
 *                     从宿主页 document.cookie 读取（须宿主页域、非 HttpOnly 才可读），
 *                     作为 X-User-Token 注入 client（callback 验证模式 / Key 配了 user_info_url）。
 *   data-user-token   可选，显式 token；优先级高于 cookie，调试或无 cookie 场景用。
 *
 * 用法一（自动初始化，推荐）：
 *   <script src="https://your-client-host/chat-widget.js"
 *           data-chat-url="https://your-client-host"
 *           data-api-key="af_live_xxx"
 *           data-token-cookie="mep-access-token"
 *           data-title="AI 助手"
 *           data-width="680px"></script>
 *
 * 用法二（手动初始化）：
 *   <script src="https://your-client-host/chat-widget.js"></script>
 *   <script>AgentFlowChat.init({ chatUrl:'https://your-client-host', title:'AI 助手', width:'680px' });</script>
 *
 * API：window.AgentFlowChat = { init, open, close, toggle, destroy, isOpen }
 *      事件：window 监听 'agent-flow-chat:open' / 'agent-flow-chat:close'
 */
(function (window, document) {
  'use strict';

  var API_KEY = 'AgentFlowChat';
  var HOST_ID = 'agent-flow-chat-host';
  var currentScript = document.currentScript;

  if (window[API_KEY] && window[API_KEY].__afc) {
    return;
  }

  var state = {
    host: null,
    shadow: null,
    shell: null,
    panel: null,
    launcher: null,
    iframe: null,
    loading: null,
    closeButton: null,
    initialized: false,
    open: false,
    iframeLoaded: false,
    previousFocus: null,
    config: null
  };

  function bool(value, fallback) {
    if (value === undefined || value === null || value === '') return fallback;
    return !/^(false|0|no|off)$/i.test(String(value));
  }

  function cssLength(value, fallback) {
    if (typeof value === 'number' && isFinite(value)) return value + 'px';
    var text = String(value || '').trim();
    return /^\d+(\.\d+)?(px|rem|em|vw|vh|%)$/.test(text) ? text : fallback;
  }

  function intBetween(value, fallback, min, max) {
    var number = parseInt(value, 10);
    return isFinite(number) ? Math.min(max, Math.max(min, number)) : fallback;
  }

  function scriptDataset() {
    return currentScript && currentScript.dataset ? currentScript.dataset : {};
  }

  function resolveOrigin() {
    try {
      if (currentScript && currentScript.src) return new URL(currentScript.src).origin;
    } catch (e) { /* ignore */ }
    return window.location.origin;
  }

  function defaultChatUrl() {
    return resolveOrigin();
  }

  function resolveLogoUrl() {
    try {
      return new URL('/AFLogo.png', resolveOrigin()).href;
    } catch (e) {
      return '/AFLogo.png';
    }
  }

  function normalizeConfig(options) {
    var data = scriptDataset();
    var input = options || {};
    var zIndex = input.zIndex !== undefined ? input.zIndex : data.zIndex;

    return {
      chatUrl: String(input.chatUrl || data.chatUrl || defaultChatUrl()),
      title: String(input.title || data.title || 'AI 助手'),
      width: cssLength(input.width || data.width, '680px'),
      right: cssLength(input.right || data.right, '24px'),
      bottom: cssLength(input.bottom || data.bottom, '24px'),
      zIndex: intBetween(zIndex, 2147483000, 1, 2147483646),
      openOnLoad: bool(input.openOnLoad !== undefined ? input.openOnLoad : data.openOnLoad, false),
      apiKey: String(input.apiKey || data.apiKey || ''),
      userToken: String(input.userToken || data.userToken || ''),
      tokenCookie: String(input.tokenCookie || data.tokenCookie || 'mep-access-token')
    };
  }

  function emit(name) {
    var event;
    try {
      event = new CustomEvent('agent-flow-chat:' + name, { detail: { chatUrl: state.config.chatUrl } });
    } catch (error) {
      event = document.createEvent('CustomEvent');
      event.initCustomEvent('agent-flow-chat:' + name, false, false, { chatUrl: state.config.chatUrl });
    }
    window.dispatchEvent(event);
  }

  function buildHost() {
    var host = document.createElement('div');
    host.id = HOST_ID;
    host.setAttribute('data-agent-flow-chat', '');
    host.style.position = 'fixed';
    host.style.inset = '0';
    host.style.width = '0';
    host.style.height = '0';
    host.style.zIndex = String(state.config.zIndex);
    host.style.pointerEvents = 'none';

    var shadow = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;
    var style = document.createElement('style');
    style.textContent = [
      ':host{all:initial}',
      '*,*::before,*::after{box-sizing:border-box}',
      '.afc-panel{position:fixed;z-index:2;top:0;right:0;width:min(var(--afc-width),100vw);height:100vh;height:100dvh;background:#fff;box-shadow:-18px 0 48px rgba(15,23,42,.18);transform:translate3d(102%,0,0);visibility:hidden;pointer-events:none;transition:transform .34s cubic-bezier(.22,1,.36,1),visibility .34s;overflow:hidden;border-left:1px solid rgba(148,163,184,.22)}',
      '.afc-open .afc-panel{transform:translate3d(0,0,0);visibility:visible;pointer-events:auto}',
      '.afc-close{position:absolute;z-index:5;top:max(15px,env(safe-area-inset-top));right:max(57px,env(safe-area-inset-right));width:30px;height:30px;padding:0;border:1px solid rgba(148,163,184,.18);border-radius:8px;background:rgba(255,255,255,.52);color:rgba(63,73,91,.68);display:grid;place-items:center;cursor:pointer;box-shadow:0 4px 12px rgba(15,23,42,.08);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);transition:background .16s ease,color .16s ease,transform .16s ease}',
      '.afc-close:hover{background:rgba(255,255,255,.82);color:#1f2937;transform:scale(1.06)}',
      '.afc-close:active{transform:scale(.94)}',
      '.afc-close:focus-visible,.afc-launcher:focus-visible{outline:3px solid rgba(94,129,255,.35);outline-offset:3px}',
      '.afc-close svg{width:15px;height:15px;stroke:currentColor}',
      '.afc-body{position:absolute;inset:0;background:#fff}',
      '.afc-frame{display:block;width:100%;height:100%;border:0;background:#fff;opacity:0;transition:opacity .2s ease}',
      '.afc-loaded .afc-frame{opacity:1}',
      '.afc-loading{position:absolute;inset:0;display:grid;place-items:center;background:#fff;color:#667085;font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;transition:opacity .2s ease,visibility .2s ease}',
      '.afc-loaded .afc-loading{opacity:0;visibility:hidden}',
      '.afc-spinner{width:28px;height:28px;border:3px solid #e8ebf2;border-top-color:#7168ff;border-radius:50%;animation:afc-spin .75s linear infinite}',
      '.afc-launcher{position:fixed;z-index:3;right:var(--afc-right);bottom:var(--afc-bottom);width:60px;height:60px;padding:0;border:0;border-radius:20px;background:linear-gradient(145deg,#f7f5ff 5%,#e8f5ff 48%,#fff0fb 100%);box-shadow:0 14px 32px rgba(75,85,150,.22),0 3px 10px rgba(91,106,178,.16),inset 0 0 0 1px rgba(255,255,255,.82);cursor:pointer;display:grid;place-items:center;pointer-events:auto;transition:transform .2s ease,box-shadow .2s ease,opacity .2s ease,visibility .2s ease;animation:afc-float 4.4s ease-in-out infinite}',
      '.afc-launcher::before{content:"";position:absolute;inset:-5px;border-radius:24px;border:1px solid rgba(114,111,255,.18);opacity:.65;animation:afc-pulse 2.8s ease-out infinite}',
      '.afc-launcher:hover{transform:translateY(-3px) scale(1.04);box-shadow:0 18px 38px rgba(75,85,150,.28),0 5px 14px rgba(91,106,178,.2),inset 0 0 0 1px rgba(255,255,255,.9)}',
      '.afc-open .afc-launcher{opacity:0;visibility:hidden;pointer-events:none;transform:translateY(12px) scale(.86)}',
      '.afc-logo{width:42px;height:42px;object-fit:contain;display:block;filter:drop-shadow(0 4px 6px rgba(93,76,220,.22));animation:afc-breathe 3.2s ease-in-out infinite;pointer-events:none;user-select:none;-webkit-user-drag:none}',
      '@keyframes afc-spin{to{transform:rotate(360deg)}}',
      '@keyframes afc-float{0%,100%{margin-bottom:0}50%{margin-bottom:5px}}',
      '@keyframes afc-pulse{0%{transform:scale(.9);opacity:.7}75%,100%{transform:scale(1.18);opacity:0}}',
      '@keyframes afc-breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.08)}}',
      '@media(max-width:640px){.afc-panel{width:100vw;border-left:0}.afc-launcher{right:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom))}}',
      '@media(prefers-reduced-motion:reduce){.afc-panel,.afc-launcher,.afc-loading,.afc-frame{transition:none}.afc-launcher,.afc-launcher::before,.afc-logo,.afc-spinner{animation:none}}'
    ].join('');

    var shell = document.createElement('div');
    shell.className = 'afc-shell';
    shell.style.setProperty('--afc-width', state.config.width);
    shell.style.setProperty('--afc-right', state.config.right);
    shell.style.setProperty('--afc-bottom', state.config.bottom);
    shell.innerHTML = [
      '<aside class="afc-panel" role="dialog" aria-label="对话窗口">',
      '  <button class="afc-close" type="button" aria-label="关闭对话"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.9" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18"/></svg></button>',
      '  <div class="afc-body">',
      '    <iframe class="afc-frame" title="AI 对话" allow="clipboard-read; clipboard-write; microphone" referrerpolicy="strict-origin-when-cross-origin"></iframe>',
      '    <div class="afc-loading" aria-live="polite"><div><div class="afc-spinner"></div><span style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)">正在加载对话</span></div></div>',
      '  </div>',
      '</aside>',
      '<button class="afc-launcher" type="button" aria-label="打开 AI 助手" aria-expanded="false">',
      '  <img class="afc-logo" alt="" />',
      '</button>'
    ].join('');

    shadow.appendChild(style);
    shadow.appendChild(shell);
    (document.body || document.documentElement).appendChild(host);

    state.host = host;
    state.shadow = shadow;
    state.shell = shell;
    state.panel = shell.querySelector('.afc-panel');
    state.launcher = shell.querySelector('.afc-launcher');
    state.iframe = shell.querySelector('.afc-frame');
    state.loading = shell.querySelector('.afc-body');
    state.closeButton = shell.querySelector('.afc-close');

    state.iframe.title = state.config.title;
    shell.querySelector('.afc-logo').src = resolveLogoUrl();

    state.launcher.addEventListener('click', toggle);
    state.closeButton.addEventListener('click', close);
    state.iframe.addEventListener('load', function () {
      state.iframeLoaded = true;
      state.loading.classList.add('afc-loaded');
    });
    document.addEventListener('keydown', onKeyDown, true);
  }

  function loadIframe() {
    if (!state.iframe.getAttribute('src')) {
      state.iframe.setAttribute('src', state.config.chatUrl);
    }
  }

  // client（iframe 内）启动时会发 agentflow:request_config 请求嵌入配置；
  // 这里回 agentflow:config：apiKey 来自 data-api-key，userToken 来自 data-user-token 或 cookie（callback 模式）。
  function onMessage(e) {
    if (!state.iframe || e.source !== state.iframe.contentWindow) return;
    var data = e.data || {};
    if (data.type === 'agentflow:request_config') sendConfig();
  }

  // 从宿主页 document.cookie 读取指定 name（宿主页域、非 HttpOnly 才可读）。
  function readCookie(name) {
    if (!name || !document.cookie) return '';
    var prefix = name + '=';
    var parts = document.cookie.split(';');
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].trim();
      if (part.indexOf(prefix) === 0) {
        var value = part.substring(prefix.length);
        try { value = decodeURIComponent(value); } catch (e) { /* 保留原值 */ }
        return value;
      }
    }
    return '';
  }

  // 终端用户 token（X-User-Token）：显式 data-user-token 优先，否则读 data-token-cookie（默认 mep-access-token）。
  // 每次握手动态读取，保证 cookie 内 token 轮转后取到最新值。
  function resolveUserToken() {
    if (state.config.userToken) return state.config.userToken;
    return readCookie(state.config.tokenCookie);
  }

  function sendConfig() {
    if (!state.iframe || !state.config.apiKey) return;
    var userToken = resolveUserToken();
    var targetOrigin;
    try { targetOrigin = new URL(state.config.chatUrl).origin; } catch (err) { targetOrigin = '*'; }
    state.iframe.contentWindow.postMessage(
      { type: 'agentflow:config', apiKey: state.config.apiKey, userToken: userToken || undefined },
      targetOrigin
    );
  }

  function init(options) {
    if (state.initialized) return api;
    state.config = normalizeConfig(options);
    buildHost();
    state.initialized = true;
    window.addEventListener('message', onMessage);
    if (state.config.openOnLoad) open();
    return api;
  }

  function open() {
    if (!state.initialized) init();
    if (state.open) return api;
    state.previousFocus = document.activeElement;
    state.open = true;
    loadIframe();
    // iframe 已加载时（非首次打开）重推配置，让 cookie 内 token 保持新鲜
    if (state.iframeLoaded) sendConfig();
    state.shell.classList.add('afc-open');
    state.launcher.setAttribute('aria-expanded', 'true');
    window.setTimeout(function () {
      if (state.open && state.closeButton) state.closeButton.focus();
    }, 30);
    emit('open');
    return api;
  }

  function close() {
    if (!state.initialized || !state.open) return api;
    state.open = false;
    state.shell.classList.remove('afc-open');
    state.launcher.setAttribute('aria-expanded', 'false');
    if (state.previousFocus && typeof state.previousFocus.focus === 'function') {
      state.previousFocus.focus();
    }
    emit('close');
    return api;
  }

  function toggle() {
    return state.open ? close() : open();
  }

  function destroy() {
    if (!state.initialized) return;
    document.removeEventListener('keydown', onKeyDown, true);
    window.removeEventListener('message', onMessage);
    if (state.host && state.host.parentNode) state.host.parentNode.removeChild(state.host);
    state.host = null;
    state.shadow = null;
    state.shell = null;
    state.panel = null;
    state.launcher = null;
    state.iframe = null;
    state.initialized = false;
    state.open = false;
    state.iframeLoaded = false;
  }

  function onKeyDown(event) {
    if (state.open && event.key === 'Escape') close();
  }

  var api = {
    __afc: true,
    init: init,
    open: open,
    close: close,
    toggle: toggle,
    destroy: destroy,
    isOpen: function () { return state.open; }
  };

  window[API_KEY] = api;

  function autoInit() {
    var data = scriptDataset();
    if (bool(data.autoInit, true)) init();
  }

  if (document.readyState === 'loading' && !document.body) {
    document.addEventListener('DOMContentLoaded', autoInit, { once: true });
  } else {
    autoInit();
  }
})(window, document);
