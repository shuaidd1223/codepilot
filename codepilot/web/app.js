/* CodePilot root app bootstrap — render shell and mount wiring. */
/* global Vue, CP */

const { createApp } = Vue;

const RootApp = {
  setup: CP.AppStateBoundary.setup,
  template: `
    <div class="shell">
      <cp-sidebar></cp-sidebar>
      <main class="main">
        <cp-main-header></cp-main-header>
        <div class="main-scroll">
          <cp-toast-stack></cp-toast-stack>
          <div v-if="cp.state.daemonHealth && cp.state.daemonHealth.reason && !cp.state.daemonHealth.alive"
               class="daemon-banner"
               :class="cp.state.daemonHealth.running ? 'warn' : 'err'">
            <span class="dot"></span>
            <div class="body">
              <b>daemon {{ cp.state.daemonHealth.running ? '假死' : '已停止' }}</b>：
              {{ cp.state.daemonHealth.reason }}
              <span v-if="cp.state.daemonHealth.last_heartbeat" class="muted tiny">
                · 最后心跳 {{ cp.state.daemonHealth.last_heartbeat }}
              </span>
            </div>
            <div class="hint tiny">
              启动：<code>codepilot daemon -p &lt;project&gt;</code> 或 <code>codepilot webui start</code>
            </div>
          </div>
          <cp-content-pane></cp-content-pane>
        </div>
      </main>
      <cp-confirm-dialog></cp-confirm-dialog>
    </div>
  `,
};

const app = createApp(RootApp);

/* Surface runtime errors instead of silent blank page */
app.config.errorHandler = (err, _instance, info) => {
  // eslint-disable-next-line no-console
  console.error('[CodePilot]', info, err);
  const root = document.getElementById('app');
  if (root && !root.innerHTML.includes('__cp_crash')) {
    root.innerHTML = '<div id="__cp_crash" style="padding:24px;font-family:ui-monospace,monospace;color:#b42318;background:#fef3f2;border:1px solid #fecdca;border-radius:8px;margin:24px;white-space:pre-wrap"><strong>CodePilot UI 崩溃了（按 F12 查看控制台）</strong>\n\n' +
      String(info) + '\n\n' + (err && err.stack ? err.stack : String(err)) + '</div>';
  }
};

CP.install(app);

/* Register every component under CP.Components with kebab-case name `cp-<name>` */
Object.entries(CP.Components).forEach(([name, comp]) => {
  const kebab = 'cp-' + name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
  app.component(kebab, comp);
});

app.mount('#app');
